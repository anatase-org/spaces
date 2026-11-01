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
                + '<div role="group">' * 48 +
                '<button onclick="document.getElementById(\'result\').textContent=\'Button clicked\'" '
                'ondblclick="document.getElementById(\'result\').textContent=\'Button double clicked\'">Probe button</button>'
                '<input aria-label="Probe field">'
                '<a href="about:blank" onclick="event.preventDefault();document.getElementById(\'result\').textContent=\'Link clicked\'">Probe link</a>'
                '<p id="result">Ready</p>'
                '<input id="native" type="range" min="0" max="100" value="80" aria-label="Native volume">'
                '<p id="native-result">Native value 80</p>'
                '<div id="custom" role="slider" tabindex="0" aria-label="ARIA volume" '
                'aria-valuemin="0" aria-valuemax="100" aria-valuenow="80">ARIA volume</div>'
                '<p id="custom-result">ARIA value 80</p>'
                '<script>const n=document.getElementById("native"),c=document.getElementById("custom");'
                'n.oninput=()=>document.getElementById("native-result").textContent="Native value "+n.value;'
                'let actual=80;c.onkeydown=e=>{if(e.key==="ArrowLeft"||e.key==="ArrowDown")actual=Math.max(0,actual-5);'
                'else if(e.key==="ArrowRight"||e.key==="ArrowUp")actual=Math.min(100,actual+5);else return;'
                'c.setAttribute("aria-valuenow",actual);document.getElementById("custom-result").textContent="ARIA value "+actual;'
                'e.preventDefault()};</script>' + '</div>' * 48 +
                '<button style="margin-top:200vh" onclick="document.getElementById(\'result\').textContent=\'Offscreen button clicked\'">Offscreen button</button>')
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
        target = next({"window_id": window["window_id"]}
                      for app in status["apps"] for window in app["windows"] if title in window["title"])
        observation = probe.tool("computer", observe={"kind": "window", **target})["observation"]
        elements = observation.get("elements", [])
        wanted = {("heading", "SCU browser probe"), ("button", "Probe button"), ("entry", "Probe field")}
        found = {(row["role"], row["label"]) for row in elements} & wanted
        record(label, elements=len(elements), controls=sorted(found),
               error=observation.get("accessibility_error"), complete=found == wanted)
        return found == wanted

    def exercise(probe, title):
        status = probe.tool("status")
        target = next({"window_id": window["window_id"]}
                      for app in status["apps"] for window in app["windows"] if title in window["title"])

        def snapshot():
            return probe.tool("computer", observe={"kind": "window", **target})["observation"]

        tree = snapshot()
        button = next(row for row in tree["elements"] if row["label"] == "Probe button")
        result = probe.tool("computer", steps=[{"tool": "click", "args": {
            "element_token": button["element_token"], "count": 2}}], observe={"kind": "window", **target})
        assert result["status"] == "ok", result
        assert any(row["label"] == "Button double clicked" for row in result["observation"]["elements"]), result
        record("double-click", status=result["status"])
        link = next(row for row in result["observation"]["elements"] if row["label"] == "Probe link")
        result = probe.tool("computer", steps=[
            {"tool": "click", "args": {"element_token": link["element_token"], "button": "middle"}},
            {"tool": "press_keys", "args": {"keys": ["CTRL", "Tab"]}},
            {"tool": "delay_ms", "args": {"ms": 500}}], observe={"kind": "window", **target})
        assert result["status"] == "ok", result
        assert any(row["label"] == "Address and search bar" and row.get("value") == "about:blank"
                   for row in result["observation"]["elements"]), result
        record("middle-click", new_tab="about:blank", status=result["status"])
        probe.tool("computer", steps=[{"tool": "press_keys", "args": {"keys": ["CTRL", "w"]}}], observe=False)
        tree = snapshot()
        offscreen = next(row for row in tree["elements"] if row["label"] == "Offscreen button")
        result = probe.tool("computer", steps=[{"tool": "click", "args": {
            "element_token": offscreen["element_token"], "button": "middle"}}], observe=False)
        assert result.get("failed", {}).get("code") == "element_off_screen", result
        record("offscreen-middle-click", error="element_off_screen")

        for label, text in (("Probe button", "Button clicked"), ("Probe link", "Link clicked"),
                            ("Offscreen button", "Offscreen button clicked")):
            tree = snapshot()
            row = next(row for row in tree["elements"] if row["label"] == label)
            if label == "Offscreen button":
                bounds = tree["screenshot"]["bounds"]
                assert row["frame"]["y"] > bounds["y"] + bounds["height"], row
            result = probe.tool("computer", steps=[{"tool": "click", "args": {"element_token": row["element_token"]}}],
                observe={"kind": "window", **target})
            assert result["status"] == "ok", result
            assert any(text in (row.get("label", "") + row.get("value", "")) for row in result["observation"]["elements"]), result
            record("click", target=label, actions=row["actions"], status=result["status"])

        for tool, field, text, expected in (("set_value", "value", "hello", "hello"),
                                             ("set_value", "value", "changed", "changed"),
                                             ("type_text", "text", " world", "changed world"),
                                             ("set_value", "value", "", "")):
            tree = snapshot()
            row = next(row for row in tree["elements"] if row["label"] == "Probe field")
            result = probe.tool("computer", steps=[{"tool": tool, "args": {
                "element_token": row["element_token"], field: text}}],
                observe={"kind": "window", **target})
            assert result["status"] == "ok", result
            actual = next(row for row in result["observation"]["elements"] if row["label"] == "Probe field")
            assert actual.get("value", "") == expected, actual
            record("text", tool=tool, value=actual.get("value"), warnings=result.get("warnings"))

        for label, readback in (("Native volume", "Native value 50"), ("ARIA volume", "ARIA value 50")):
            tree = snapshot()
            row = next(row for row in tree["elements"] if row["label"] == label)
            assert (row["value"], row["minimum"], row["maximum"]) == (80, 0, 100), row
            result = probe.tool("computer", steps=[{"tool": "set_value", "args": {
                "element_token": row["element_token"], "value": "50"}}],
                observe={"kind": "window", **target, "screenshot": False})
            assert result["status"] == "ok", result
            actual = next(row for row in result["observation"]["elements"] if row["label"] == label)
            assert actual["value"] == 50, actual
            # This page text changes only from the application's event handler.
            assert any(row.get("label") == readback for row in result["observation"]["elements"]), result
            record("numeric", target=label, value=actual["value"], application_readback=readback)

        tree = snapshot()
        row = next(row for row in tree["elements"] if row["label"] == "Address and search bar")
        result = probe.tool("computer", steps=[{"tool": "set_value", "args": {
            "element_token": row["element_token"], "value": "about:blank"}}],
            observe={"kind": "window", **target})
        assert result["status"] == "ok", result
        address = next(row for row in result["observation"]["elements"] if row["label"] == "Address and search bar")
        assert address.get("value") == "about:blank", address
        # Discard the address edit in our isolated profile without navigating.
        dismissed = probe.tool("computer", steps=[{"tool": "press_keys", "args": {"keys": ["Escape"]}}])
        assert dismissed["status"] == "ok", dismissed
        record("address-bar", tool="set_value", value=address["value"], dismissed=True)

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
