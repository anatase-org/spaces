"""Disposable host-side page oracle and isolated browser desktop entry.

Used by manual_scu_application_probe.py; never alters default applications.
"""
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import urlparse

mode, directory = sys.argv[1:3]
root = Path(directory)
metadata = root / "fixture.json"

if mode == "serve":
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/active":
                trial = json.loads((root / "active.json").read_text())["trial"]
                self.send_response(302)
                self.send_header("Location", "/case/" + trial)
                self.end_headers()
                return
            trial = path.removeprefix("/case/")
            if not path.startswith("/case/"):
                self.send_error(404)
                return
            title = html.escape("SCU " + trial)
            page = f"""<!doctype html><meta charset=utf-8><title>{title}</title>
<h1>{title}</h1><form id=form><label>Message <input id=message name=message></label>
<button>Submit result</button></form><p id=result>Waiting for submission</p>
<script>form.onsubmit=async e=>{{e.preventDefault();
await fetch('/result',{{method:'POST',headers:{{'Content-Type':'application/json'}},
body:JSON.stringify({{trial:{json.dumps(trial)},message:message.value}})}});
result.textContent='Submitted '+message.value;}};</script>"""
            data = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            with (root / "requests.jsonl").open("a") as stream:
                stream.write(json.dumps(dict(time=time.time(), trial=trial)) + "\n")

        def do_POST(self):
            size = min(int(self.headers.get("Content-Length", "0")), 65536)
            value = json.loads(self.rfile.read(size))
            value["time"] = time.time()
            with (root / "results.jsonl").open("a") as stream:
                stream.write(json.dumps(value) + "\n")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    metadata.write_text(json.dumps(dict(port=server.server_port, server_pid=os.getpid())))
    server.serve_forever()

elif mode == "setup":
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / "server.log").open("w") as log:
        subprocess.Popen([sys.executable, __file__, "serve", str(root)], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True)
    for _ in range(100):
        if metadata.exists():
            break
        time.sleep(.05)
    state = json.loads(metadata.read_text())
    token = root.name
    profile = Path.home() / ".var/app/org.anatase.Browser/cache" / token
    profile.mkdir(mode=0o700)
    desktop_id = "org.example." + token + ".desktop"
    desktop = Path.home() / ".local/share/applications" / desktop_id
    desktop.parent.mkdir(parents=True, exist_ok=True)
    command = f'/usr/bin/flatpak run org.anatase.Browser --user-data-dir="{profile}" --no-first-run --no-default-browser-check'
    desktop.write_text(f"[Desktop Entry]\nType=Application\nName=SCU Evaluation Browser\nGenericName=Web Browser\n"
        f"Comment=Isolated browser for SCU application evaluation\nExec={command} %U\n"
        "Terminal=false\nCategories=Network;WebBrowser;\nActions=new-window;\n"
        f"[Desktop Action new-window]\nName=New Window\nExec={command} --new-window http://127.0.0.1:{state['port']}/active\n")
    state.update(profile=str(profile), desktop_id=desktop_id, desktop_path=str(desktop))
    metadata.write_text(json.dumps(state))
    print(json.dumps(state))

elif mode == "cleanup":
    state = json.loads(metadata.read_text())
    # Only signal processes explicitly carrying our unique browser profile.
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            arguments = (entry / "cmdline").read_bytes().split(b"\0")
            profile_arg = ("--user-data-dir=" + state["profile"]).encode()
            # Chromium rewrites argv[0] into a combined process title; bwrap
            # retains a conventional argv. Match our exact profile in either.
            chromium_title = arguments and arguments[0].startswith(b"/app/lib64/chromium-browser/chromium-browser ")
            if profile_arg in arguments or (chromium_title and profile_arg in arguments[0].split()):
                os.kill(int(entry.name), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    Path(state["desktop_path"]).unlink(missing_ok=True)
    try:
        arguments = Path(f"/proc/{state['server_pid']}/cmdline").read_bytes().split(b"\0")
        if b"serve" in arguments and str(root).encode() in arguments:
            os.kill(state["server_pid"], signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    print("Test browser and server stopped; artifacts retained in " + str(root))
