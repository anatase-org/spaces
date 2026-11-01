"""Check browser activation through the real SCU service on win5/win5f.

Uses isolated browser profiles and no accessibility environment overrides.
--talk-a11y tests the proposed Flatpak permission without persisting an override.
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import shlex
import subprocess
import time
import uuid

from manual_scu_session_probe import Probe


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="win5")
    parser.add_argument("--guest", default="win5f")
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--talk-a11y", action="store_true")
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
    ssh = ["ssh", "-o", "BatchMode=yes", args.host]
    probes, browsers = [], []
    events = []

    def host(*argv):
        return subprocess.check_output([*ssh, shlex.join(argv)], text=True, timeout=20).strip()

    manager = dict(line.split("=", 1) for line in host("systemctl", "--user", "show-environment").splitlines()
                   if "=" in line)
    graphical = ["env", *(key + "=" + manager[key] for key in
                         ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "WAYLAND_DISPLAY"))]
    permission = ["--talk-name=org.a11y.Bus"] if args.talk_a11y else []

    def record(event, **values):
        events.append({"event": event, "time": time.time(), **values})
        (args.artifacts / "events.json").write_text(json.dumps(events, indent=2, ensure_ascii=False))
        print(json.dumps(events[-1], ensure_ascii=False), flush=True)

    def state(event):
        address = ast.literal_eval(host(*graphical, "gdbus", "call", "--session", "--dest", "org.a11y.Bus",
            "--object-path", "/org/a11y/bus", "--method", "org.a11y.Bus.GetAddress"))[0]
        record(event,
            flag=host(*graphical, "gdbus", "call", "--session", "--dest", "org.a11y.Bus", "--object-path",
                      "/org/a11y/bus", "--method", "org.freedesktop.DBus.Properties.Get", "org.a11y.Status", "IsEnabled"),
            registry=host(*graphical, "gdbus", "call", "--address", address, "--dest", "org.a11y.atspi.Registry",
                          "--object-path", "/org/a11y/atspi/registry", "--method", "org.a11y.atspi.Registry.GetRegisteredEvents"),
            service=host("systemctl", "--user", "show", "spaces-scu-host.service", "-p", "MainPID"))

    def connect(label):
        probe = Probe(args.guest, args.artifacts, label)
        probes.append(probe)
        probe.tool("status")
        return probe

    def launch(label):
        profile = host("python3", "-c", "import os,tempfile;print(tempfile.mkdtemp("
            "prefix='scu-browser-probe-',dir=os.path.expanduser('~/.var/app/org.anatase.Browser/cache')))")
        title = label + "-" + uuid.uuid4().hex[:8]
        page = (f"data:text/html,<title>{title}</title><h1>SCU browser probe</h1>"
                '<button onclick="document.getElementById(\'result\').textContent=\'Button clicked\'">Probe button</button>'
                '<input aria-label="Probe field">'
                '<a href="about:blank" onclick="event.preventDefault();document.getElementById(\'result\').textContent=\'Link clicked\'">Probe link</a>'
                '<p id="result">Ready</p>')
        log = (args.artifacts / f"{label}-browser.log").open("w")
        command = [*graphical, "flatpak", "run", *permission, "org.anatase.Browser",
                   "--user-data-dir=" + profile, "--ozone-platform=wayland", "--no-first-run",
                   "--no-default-browser-check", "--disable-background-networking", "--disable-extensions", page]
        process = subprocess.Popen([*ssh, shlex.join(command)], stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        browsers.append((profile, process, log))
        time.sleep(2)
        return title

    def observe(probe, title, label):
        status = probe.tool("status")
        target = next({"pid": app["pid"], "window_id": window["window_id"]}
                      for app in status["apps"] for window in app["windows"] if title in window["title"])
        observation = probe.tool("computer", observe={"kind": "window", **target,
            "tree_format": "elements", "max_elements": 1000, "max_depth": 24})["observation"]
        elements = observation.get("elements", [])
        wanted = {("heading", "SCU browser probe"), ("button", "Probe button"), ("entry", "Probe field")}
        found = {(row["role"], row["label"]) for row in elements} & wanted
        record(label, elements=len(elements), controls=sorted(found),
               error=observation.get("accessibility_error"), complete=found == wanted)
        return found == wanted

    def exercise(probe, title):
        status = probe.tool("status")
        target = next({"pid": app["pid"], "window_id": window["window_id"]}
                      for app in status["apps"] for window in app["windows"] if title in window["title"])

        def snapshot():
            return probe.tool("computer", observe={"kind": "window", **target,
                "tree_format": "both", "max_elements": 1000, "max_depth": 24})["observation"]

        for label, text in (("Probe button", "Button clicked"), ("Probe link", "Link clicked")):
            tree = snapshot()
            row = next(row for row in tree["elements"] if row["label"] == label)
            result = probe.tool("computer", steps=[{"tool": "click", "args": {"element_token": row["element_token"]}}],
                observe={"kind": "window", **target, "tree_format": "both", "max_elements": 1000, "max_depth": 24})
            assert result["status"] == "ok", result
            assert text in result["observation"]["tree_markdown"], result
            record("click", target=label, actions=row["actions"], status=result["status"])

        for tool, field, text, expected in (("set_value", "value", "hello", "hello"),
                                             ("set_value", "value", "changed", "changed"),
                                             ("type_text", "text", " world", "changed world"),
                                             ("set_value", "value", "", "")):
            tree = snapshot()
            row = next(row for row in tree["elements"] if row["label"] == "Probe field")
            result = probe.tool("computer", steps=[{"tool": tool, "args": {
                "element_token": row["element_token"], field: text}}],
                observe={"kind": "window", **target, "tree_format": "both", "max_elements": 1000, "max_depth": 24})
            assert result["status"] == "ok", result
            actual = next(row for row in result["observation"]["elements"] if row["label"] == "Probe field")
            assert actual.get("value", "") == expected, actual
            record("text", tool=tool, route=row["text_input"], value=actual.get("value"), warnings=result.get("warnings"))

        tree = snapshot()
        row = next(row for row in tree["elements"] if row["label"] == "Address and search bar")
        result = probe.tool("computer", steps=[{"tool": "set_value", "args": {
            "element_token": row["element_token"], "value": "about:blank"}}],
            observe={"kind": "window", **target, "tree_format": "both", "max_elements": 1000, "max_depth": 24})
        assert result["status"] == "ok", result
        address = next(row for row in result["observation"]["elements"] if row["label"] == "Address and search bar")
        assert address.get("value") == "about:blank", address
        # Discard the address edit in our isolated profile without navigating.
        dismissed = probe.tool("computer", steps=[{"tool": "press_keys", "args": {"keys": ["Escape"]}}])
        assert dismissed["status"] == "ok", dismissed
        record("address-bar", tool="set_value", route=row["text_input"], value=address["value"], dismissed=True)

    # Kill only instances using our unique profile, including Flatpak sub-sandboxes.
    cleanup = """import subprocess,sys,shutil
profile=sys.argv[1]
for line in subprocess.check_output(['flatpak','ps','--columns=instance,application,pid'],text=True).splitlines():
    fields=line.split()
    try:
        command=open('/proc/'+fields[2]+'/cmdline','rb').read()
    except (FileNotFoundError,IndexError):
        continue
    if profile.encode() in command:
        subprocess.run(['flatpak','kill',fields[0]],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
shutil.rmtree(profile,ignore_errors=True)
"""
    try:
        state("initial")
        before = launch("before")
        first = connect("first")
        state("active")
        observe(first, before, "browser-started-before-activation")
        after = launch("after")
        complete = observe(first, after, "browser-started-after-activation")
        if complete:
            exercise(first, after)
        diagnostics = ('gsettings get org.gnome.desktop.interface toolkit-accessibility; '
            'gdbus call --address "$AT_SPI_BUS_ADDRESS" --dest org.a11y.atspi.Registry '
            '--object-path /org/a11y/atspi/registry --method org.a11y.atspi.Registry.GetRegisteredEvents; '
            'gdbus call --session --dest org.a11y.Bus --object-path /org/a11y/bus '
            '--method org.freedesktop.DBus.Properties.Get org.a11y.Status IsEnabled')
        result = subprocess.run([*ssh, shlex.join([*graphical, "flatpak", "run", *permission,
            "--command=sh", "org.anatase.Browser", "-c", diagnostics])], text=True, capture_output=True, timeout=20)
        record("sandbox", stdout=result.stdout, stderr=result.stderr, returncode=result.returncode)
        second = connect("second")
        first.close()
        state("one-client")
        complete = observe(second, after, "shared-client") and complete
        second.close()
        time.sleep(3)
        state("idle")
        third = connect("third")
        complete = observe(third, after, "reactivated-browser") and complete
        state("reactivated")
        if not complete:
            raise SystemExit("Browser controls unavailable; inspect events.json and compare --talk-a11y.")
    finally:
        for probe in probes:
            if probe.process.poll() is None:
                probe.close()
        for profile, process, log in browsers:
            host("python3", "-c", cleanup, profile)
            process.wait(timeout=5)
            log.close()
        time.sleep(3)
        state("final")
        record("selinux", mode=host("getenforce"))


if __name__ == "__main__":
    main()
