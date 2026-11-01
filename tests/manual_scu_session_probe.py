"""Run separate SCU headless/lock tests on win5 and its Fedora Space.

The host fixture's JSON is the input oracle. Headless testing temporarily forces
the panel connector off, then restores detection in finally. Never combines
headless and lockscreen scenarios, and never changes SELinux policy.
"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import select
import shlex
import subprocess
import time
import uuid


class Probe:
    def __init__(self, guest, artifacts, label, *, no_vision=False):
        self.process = subprocess.Popen(["ssh", "-o", "BatchMode=yes", guest, "scu", "mcp", *(["--no-vision"] if no_vision else [])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=(artifacts / f"{label}-stderr.log").open("w"), text=True)
        self.serial, self.artifacts, self.label = 0, artifacts, label
        self.call("initialize", {"protocolVersion": "2025-11-25"})

    def call(self, method, params):
        self.serial += 1
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.serial, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        assert select.select([self.process.stdout], [], [], 65)[0], "SCU timed out"
        line = self.process.stdout.readline()
        assert line, "SCU disconnected"
        reply = json.loads(line)
        assert reply["id"] == self.serial and "result" in reply, reply
        return reply["result"]

    def tool(self, name, **arguments):
        reply = self.call("tools/call", {"name": name, "arguments": arguments})
        if name == "accessibility":
            assert not any(block["type"] == "image" for block in reply.get("content", [])), reply
        prefix = f"{self.label}-{self.serial:02d}-{name}"
        for index, block in enumerate(reply.get("content", [])):
            if block["type"] == "image":
                (self.artifacts / f"{prefix}-{index}.png").write_bytes(base64.b64decode(block["data"]))
        metadata = reply.get("structuredContent", {})
        (self.artifacts / f"{prefix}.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False))
        assert not reply.get("isError"), metadata
        return metadata

    def close(self):
        if not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("headless", "lock"))
    parser.add_argument("--host", default="win5")
    parser.add_argument("--guest", default="win5f")
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = "scu-session-" + uuid.uuid4().hex[:10]
    remote = "/tmp/" + token
    probes = []
    fixture_pid = None
    forced_connectors = []
    original = None
    events = []

    def shell(command, timeout=15):
        return subprocess.check_output(["ssh", "-o", "BatchMode=yes", args.host, command], text=True, timeout=timeout).strip()

    def host(*argv, timeout=15):
        return shell(shlex.join(argv), timeout)

    manager = dict(line.split("=", 1) for line in host("systemctl", "--user", "show-environment").splitlines() if "=" in line)
    session = manager["XDG_SESSION_ID"]
    graphical = ["env", "XDG_RUNTIME_DIR=" + manager["XDG_RUNTIME_DIR"],
                 "DBUS_SESSION_BUS_ADDRESS=" + manager["DBUS_SESSION_BUS_ADDRESS"],
                 "WAYLAND_DISPLAY=" + manager["WAYLAND_DISPLAY"], "QT_QPA_PLATFORM=wayland",
                 "QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1"]

    def kde(*argv):
        return host(*graphical, *argv)

    def outputs():
        return json.loads(kde("kscreen-doctor", "-j"))["outputs"]

    def virtuals():
        return [item for item in outputs() if item["name"].startswith("Virtual-Spaces-SCU-")]

    def locked():
        return host("loginctl", "show-session", session, "-p", "LockedHint", "--value") == "yes"

    def wait(predicate, message, timeout=10):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            try:
                if predicate():
                    return
            except subprocess.CalledProcessError:
                pass
            time.sleep(.1)
        raise AssertionError(message)

    def record(event, **details):
        item = {"event": event, "time": time.time(), **details}
        events.append(item)
        (args.artifacts / "events.json").write_text(json.dumps(events, indent=2))
        print(json.dumps(item), flush=True)

    def restore_arguments():
        # Only enablement is changed by these tests; restore mode, scale and
        # position as well so physical-output return cannot leave a new layout.
        result = []
        for item in original:
            if item["name"].startswith("Virtual-"):
                continue
            name = "output." + item["name"]
            result.append(name + (".enable" if item["enabled"] else ".disable"))
            if item["enabled"]:
                result.extend([name + ".mode." + item["currentModeId"],
                               name + ".scale." + str(item["scale"]),
                               name + f".position.{item['pos']['x']},{item['pos']['y']}"])
        return result

    def restore():
        if original is not None:
            for connector in forced_connectors:
                host("sudo", "-n", "python3", "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('detect')", connector)
                card = "/sys/class/drm/" + Path(connector).parent.name.split("-", 1)[0]
                host("sudo", "-n", "udevadm", "trigger", "--action=change", card)
            if forced_connectors:
                time.sleep(.5)
            kde("kscreen-doctor", *restore_arguments())
            kde("kscreen-doctor", "--dpms", "on")

    def fixture_state():
        return json.loads(host("cat", remote + "/state.json"))

    def host_pid():
        return host("systemctl", "--user", "show", "spaces-scu-host.service", "-p", "MainPID", "--value")

    def new_probe(label):
        probe = Probe(args.guest, args.artifacts, label)
        probes.append(probe)
        return probe

    def fixture_window(probe):
        status = probe.tool("status")
        target = next({"window_id": window["window_id"]}
                      for app in status["apps"] for window in app["windows"]
                      if window["title"] == "Spaces AT-SPI fixture " + token)
        tree = probe.tool("vision", observe={"kind": "window", **target})["observation"]
        rows = {item["label"]: item for item in tree["elements"]}
        return status, rows

    def focus_entry(probe):
        status, rows = fixture_window(probe)
        frame = rows["Spaces fixture entry"]["frame"]
        probe.tool("vision", id=status["canvas"]["id"], observe=False, steps=[
            {"tool": "click", "args": {"x": frame["x"] + frame["width"] / 2,
                                       "y": frame["y"] + frame["height"] / 2}}])
        probe.tool("accessibility", steps=[
            {"tool": "set_value", "args": {"element_token": rows["Spaces fixture entry"]["element_token"], "value": ""}},
        ])
        return rows

    def lock_visible():
        host("loginctl", "lock-session", session)
        wait(locked, "session did not lock")
        kde("kscreen-doctor", "--dpms", "on")
        record("locked-visible", physical_outputs=[item["name"] for item in outputs() if item["enabled"]])
        time.sleep(5)

    try:
        assert host("getenforce") == "Enforcing"
        assert not locked(), "Start this probe with the desktop unlocked"
        wait(lambda: host_pid() == "0", "SCU host is running before any probe client")
        original = outputs()
        (args.artifacts / "outputs-before.json").write_text(json.dumps(original, indent=2))
        assert any(item["name"].startswith("eDP") and item["enabled"] for item in original), "built-in display must begin enabled"
        assert not virtuals(), "an unrelated SCU fallback is already active"
        host("mkdir", "-m", "700", remote)
        source = Path(__file__).with_name("manual_atspi_fixture.py")
        subprocess.run(["scp", str(source), args.host + ":" + remote + "/fixture.py"], check=True, capture_output=True)
        launch = shlex.join([*graphical, "python3", remote + "/fixture.py", "qt", token, remote + "/state.json", "--computer-use"])
        fixture_pid = int(shell("nohup " + launch + " >" + shlex.quote(remote + "/fixture.log") + " 2>&1 </dev/null & echo $!"))
        wait(lambda: host("test", "-f", remote + "/state.json") == "", "fixture did not start")
        first = new_probe("first")
        status, rows = fixture_window(first)
        main_pid = host_pid()
        assert main_pid != "0"
        record("ready", session=session, wayland=manager["WAYLAND_DISPLAY"], host_pid=int(main_pid))

        if args.mode == "headless":
            second = new_probe("second")
            second.tool("status")
            for item in original:
                if not item["enabled"] or item["name"].startswith("Virtual-"):
                    continue
                query = "from pathlib import Path; import sys; print('\\n'.join(str(p) for p in Path('/sys/class/drm').glob('card*-'+sys.argv[1]+'/status')))"
                matches = host("python3", "-c", query, item["name"]).splitlines()
                assert len(matches) == 1, matches
                connector = matches[0]
                forced_connectors.append(connector)
                host("sudo", "-n", "python3", "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('off')", connector)
                # Trigger the card so KWin rereads connector status.
                card = "/sys/class/drm/" + Path(connector).parent.name.split("-", 1)[0]
                host("sudo", "-n", "udevadm", "trigger", "--action=change", card)
            record("outputs-disabled", outputs=outputs())
            try:
                wait(lambda: len(virtuals()) == 1, "shared fallback did not appear")
            except AssertionError:
                record("fallback-diagnostic", outputs=outputs())
                first.tool("status")
                raise
            record("fallback-present", outputs=outputs())
            current = first.tool("status")
            assert (current["canvas"]["width"], current["canvas"]["height"]) == (1280, 720), current["canvas"]
            rows = focus_entry(first)
            expected = "Headless " + token
            first.tool("vision", steps=[{"tool": "type_text", "args": {"text": expected}}], observe=False)
            assert fixture_state()["text"] == expected
            assert not locked()
            helpers = host("pgrep", "-af", "^/usr/bin/krfb-virtualmonitor --name Spaces-SCU-")
            helper_pid = helpers.split()[0]
            assert f"pid={helper_pid}," not in host("ss", "-ltnp"), "virtual monitor opened a VNC listener"
            record("headless-input-verified", canvas=current["canvas"], fixture=fixture_state(), helper_pid=int(helper_pid))
            first.close()
            second.tool("status")
            assert len(virtuals()) == 1
            assert host_pid() == main_pid
            second.close()
            wait(lambda: not virtuals(), "fallback survived the final client")
            wait(lambda: host_pid() == "0", "SCU host survived the final client")
            record("last-client-cleanup")
            third = new_probe("third")
            third.tool("status")
            main_pid = host_pid()
            assert main_pid != "0"
            record("socket-reactivated", host_pid=int(main_pid))
            wait(lambda: len(virtuals()) == 1, "fallback did not return for a new client")
            restore()
            wait(lambda: not virtuals(), "fallback survived physical-output return")
            third.tool("status")
            record("physical-return-cleanup")
        else:
            rows = focus_entry(first)
            clicks = fixture_state()["clicks"]
            lock_visible()
            first.tool("accessibility", steps=[{"tool": "activate", "args": {
                "element_token": rows["Spaces fixture button"]["element_token"]}}])
            assert fixture_state()["clicks"] == clicks + 1
            assert locked(), "accessibility action unlocked the session"
            record("accessibility-stayed-locked", fixture=fixture_state())
            first.tool("status")
            wait(lambda: not locked(), "screenshot did not unlock")
            record("screenshot-unlocked")
            focus_entry(first)
            lock_visible()
            expected = "Unlocked EIS " + token
            first.tool("vision", steps=[{"tool": "type_text", "args": {"text": expected}}], observe=False)
            wait(lambda: not locked(), "EIS did not unlock")
            assert fixture_state()["text"] == expected, fixture_state()
            assert any(item["name"].startswith("eDP") and item["enabled"] for item in outputs())
            assert not virtuals(), "lock test created a fallback despite the enabled physical screen"
            first.tool("status")
            record("eis-unlocked", fixture=fixture_state())
        assert host_pid() == main_pid
        assert host("getenforce") == "Enforcing"
        record("passed", mode=args.mode)
    finally:
        for probe in reversed(probes):
            probe.close()
        try:
            restore()
            if locked():
                host("loginctl", "unlock-session", session)
            (args.artifacts / "outputs-after.json").write_text(json.dumps(outputs(), indent=2))
            if fixture_pid:
                host("kill", "-TERM", str(fixture_pid))
            if fixture_pid:
                (args.artifacts / "fixture-state.json").write_text(host("cat", remote + "/state.json"))
                (args.artifacts / "fixture.log").write_text(host("cat", remote + "/fixture.log"))
                host("rm", "-rf", "--", remote)
            wait(lambda: host_pid() == "0", "SCU host stayed running after probe clients closed")
        finally:
            record("cleanup", fixture_directory=remote)


if __name__ == "__main__":
    main()
