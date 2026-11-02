"""Private KWin/ScreenShot2/Klipper peer; never connects to the real desktop.

The production scu host service loads its fixed script here. The fixture checks
its callback identity, supplies geometry, and sends actual raw pixels and EIS
FDs. State records independently delivered libeis events, not scu submissions.
"""
import argparse
import array
import json
import math
import os
from pathlib import Path
import re
import random
import select
import socket
import subprocess
import threading
import time

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

parser = argparse.ArgumentParser()
parser.add_argument("address")
parser.add_argument("state", type=Path)
parser.add_argument("control", type=Path)
parser.add_argument("backend")
args = parser.parse_args()
flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
bus = Gio.DBusConnection.new_for_address_sync(args.address, flags, None, None)
for name in ("org.kde.KWin", "org.kde.KWin.ScreenShot2", "org.kde.klipper", "org.a11y.Bus"):
    bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                  "RequestName", GLib.Variant("(su)", (name, 0)), None,
                  Gio.DBusCallFlags.NONE, 2000, None)
XML = """<node>
<interface name='org.kde.kwin.Scripting'>
<method name='loadScript'><arg type='s' direction='in'/><arg type='s' direction='in'/><arg type='i' direction='out'/></method>
<method name='unloadScript'><arg type='s' direction='in'/><arg type='b' direction='out'/></method>
</interface>
<interface name='org.kde.kwin.Script'><method name='run'/><method name='stop'/></interface>
<interface name='org.kde.KWin.EIS.RemoteDesktop'>
<method name='connectToEIS'><arg type='i' direction='in'/><arg type='h' direction='out'/><arg type='i' direction='out'/></method>
<method name='disconnect'><arg type='i' direction='in'/></method>
</interface>
<interface name='org.kde.KWin.ScreenShot2'>
<method name='CaptureWindow'><arg type='s' direction='in'/><arg type='a{sv}' direction='in'/><arg type='h' direction='in'/><arg type='a{sv}' direction='out'/></method>
<method name='CaptureArea'><arg type='i' direction='in'/><arg type='i' direction='in'/><arg type='u' direction='in'/><arg type='u' direction='in'/><arg type='a{sv}' direction='in'/><arg type='h' direction='in'/><arg type='a{sv}' direction='out'/></method>
<method name='CaptureWorkspace'><arg type='a{sv}' direction='in'/><arg type='h' direction='in'/><arg type='a{sv}' direction='out'/></method>
</interface>
<interface name='org.kde.klipper.klipper'><method name='setClipboardContents'><arg type='s' direction='in'/></method></interface>
<interface name='org.a11y.Bus'><method name='GetAddress'><arg type='s' direction='out'/></method></interface>
</node>"""
node = Gio.DBusNodeInfo.new_for_xml(XML)
parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
oracle = subprocess.Popen([args.backend, "eis-server", str(child.fileno())], pass_fds=(child.fileno(),))
child.close()
parent.settimeout(3)
state = {"captures": 0, "capture_times": [], "geometry_requests": 0, "unloads": 0, "connections": 0,
         "disconnections": 0, "clipboard": "", "input": {}}
scripts = {}
held = []


def control():
    return json.loads(args.control.read_text())


def save():
    parent.send(b"S")
    state["input"] = json.loads(parent.recv(4096))
    temporary = args.state.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False))
    temporary.replace(args.state)


def geometry():
    config = control()
    outputs = [{"name": "center", "geometry": {"x": 0, "y": 0, "width": 80, "height": 60}, "scale": 2}]
    if config.get("headless"):
        display = args.control.parent / "display-state.json"
        outputs = ([{"name": "Virtual-" + json.loads(display.read_text())["name"],
                    "geometry": {"x": 0, "y": 0, "width": 1280, "height": 720}, "scale": 1}]
                   if display.exists() else [])
    if config.get("triple"):
        outputs = [{"name": name, "geometry": {"x": x, "y": y, "width": 80, "height": 60}, "scale": scale}
                   for name, x, y, scale in (("left", -80, -10, 2), ("center", 0, 0, 1), ("right", 90, 0, 1))]
    if config.get("large_capture"):
        outputs = [{"name": "large", "geometry": {"x": 0, "y": 0, "width": 512, "height": 512}, "scale": 1}]
    if config.get("image_cap"):
        outputs = [{"name": "center", "geometry": {"x": 0, "y": 0, "width": 4096, "height": 64}, "scale": 1}]
    if config.get("resize"):
        outputs[0]["geometry"]["width"] += 10
    if config.get("overlap") and len(outputs) > 1:
        outputs[1]["geometry"]["x"] = -60
    windows = [{"key": "window-1", "pid": config.get("app_pid", 123), "app_name": config.get("app_name", "scu fixture"),
                "title": config.get("window_title", "scu test window"), "frame": {"x": 10, "y": 10, "width": 40, "height": 30},
                "client": {"x": 10, "y": 10, "width": 40, "height": 30}, "shown": True, "active": config.get("active", True)}]
    if state["geometry_requests"] >= config.get("settled_title_after_request", math.inf):
        windows[0]["title"] = config["settled_title"]
    if config.get("image_cap"):
        windows[0]["frame"] = {"x": 0, "y": 0, "width": 4096, "height": 64}
    if "window_x" in config:
        windows[0]["frame"]["x"] = windows[0]["client"]["x"] = config["window_x"]
    if config.get("moving_window"):
        windows[0]["frame"]["x"] = windows[0]["client"]["x"] = 10 + state["geometry_requests"]
    if config.get("two_windows"):
        windows.append({**windows[0], "key": "window-2", "title": config.get("second_window_title", "scu second window"),
                        "frame": {"x": 15, "y": 15, "width": 40, "height": 30},
                        "client": {"x": 15, "y": 15, "width": 40, "height": 30}})
    if config.get("bad_client"):
        windows[0]["client"]["width"] = 27
        windows[0]["frame"]["width"] = 27
    if config.get("buffer_client"):
        windows[0]["buffer"] = windows[0]["client"]
        windows[0]["client"] = {"x": 12, "y": 14, "width": 32, "height": 20}
        windows[0]["frame"] = windows[0]["client"]
    if config.get("duplicate_identity"):
        windows.append({**windows[0], "key": "window-duplicate", "pid": config.get("duplicate_pid", 999999),
                        "app_name": config.get("duplicate_app_name", windows[0]["app_name"])})
    return {"outputs": outputs, "windows": windows}


def bounds(data):
    rects = [item["geometry"] for item in data["outputs"]]
    left, top = min(r["x"] for r in rects), min(r["y"] for r in rects)
    return left, top, max(r["x"] + r["width"] for r in rects) - left, max(r["y"] + r["height"] for r in rects) - top


def write_pixels(fd, pixels):
    try:
        offset = 0
        while offset < len(pixels):
            try:
                offset += os.write(fd, pixels[offset:])
            except BlockingIOError:
                select.select([], [fd], [], 1)
    except (BrokenPipeError, OSError):
        pass
    finally:
        os.close(fd)


def method(connection, sender, path, interface, name, parameters, invocation):
    if name == "GetAddress":
        invocation.return_value(GLib.Variant("(s)", (control()["atspi_address"],)))
        return
    values = parameters.unpack()
    config = control()
    if name == "loadScript":
        script = Path(values[0]).read_text()
        assert "workspace.windowList()" in script and "frameGeometry" in script
        assert "clientGeometry" in script and "callDBus(" in script
        script_id = len(scripts) + 1
        scripts[script_id] = script
        connection.register_object(f"/Scripting/Script{script_id}", node.interfaces[1], method, None, None)
        invocation.return_value(GLib.Variant("(i)", (script_id,)))
    elif name == "run":
        state["geometry_requests"] += 1
        if config.get("hold_geometry"):
            held.append(invocation)
            save()
            return
        script = scripts[int(path.rsplit("Script", 1)[1])]
        target, callback_path, callback_interface, token = re.search(
            r"callDBus\('([^']+)','([^']+)','([^']+)','Publish','([^']+)'", script).groups()
        connection.call(target, callback_path, callback_interface, "Publish",
                        GLib.Variant("(ss)", (token, json.dumps(geometry()))), None,
                        Gio.DBusCallFlags.NONE, 5000, None, None, None)
        invocation.return_value(None)
    elif name == "stop":
        invocation.return_value(None)
    elif name == "unloadScript":
        state["unloads"] += 1
        save()
        invocation.return_value(GLib.Variant("(b)", (True,)))
    elif name == "connectToEIS":
        assert values[0] == 3
        parent.send(b"C")
        payload, ancillary, _flags, _address = parent.recvmsg(1, socket.CMSG_SPACE(4))
        descriptors = array.array("i")
        descriptors.frombytes(ancillary[0][2][:4])
        fd = descriptors[0]
        fds = Gio.UnixFDList.new()
        handle = fds.append(fd)
        os.close(fd)
        state["connections"] += 1
        invocation.return_value_with_unix_fd_list(GLib.Variant("(hi)", (handle, state["connections"])), fds)
        save()
    elif name == "disconnect":
        state["disconnections"] += 1
        # The private oracle has one test client at a time.
        parent.send(b"D")
        save()
        invocation.return_value(None)
    elif name == "setClipboardContents":
        state["clipboard"] = values[0]
        save()
        invocation.return_value(None)
    elif name in ("CaptureWorkspace", "CaptureArea", "CaptureWindow"):
        state["captures"] += 1
        state["capture_times"].append(time.monotonic())
        if config.get("hold_capture") or state["captures"] == config.get("hold_capture_at"):
            held.append(invocation)
            save()
            return
        if config.get("refuse"):
            invocation.return_dbus_error("org.kde.KWin.ScreenShot2.Error.NoAuthorized", "Capture is not authorized")
            save()
            return
        data = geometry()
        options, handle = values[-2:]
        if name == "CaptureWindow":
            assert options["include-decoration"] and not options["include-shadow"]
            window = next(w for w in data["windows"] if w["key"] == values[0])
            x, y, w, h = (window["frame"][key] for key in ("x", "y", "width", "height"))
            state.setdefault("window_captures", []).append(values[0])
            if config.get("change_window_on_capture"):
                config["window_x"] = 20
                args.control.write_text(json.dumps(config))
            if config.get("change_title_on_capture"):
                config["window_title"] = "changed during capture"
                args.control.write_text(json.dumps(config))
        else:
            x, y, w, h = bounds(data) if name == "CaptureWorkspace" else values[:4]
        scale = max(item["scale"] for item in data["outputs"]) if options.get("native-resolution") else 1
        width, height = int(w * scale), int(h * scale)
        if config.get("large_capture"):
            raw = bytearray(random.Random(0).randbytes(width * height * 4))
            raw[3::4] = b"\xff" * (width * height)
        else:
            # Construct scanlines in bulk; per-pixel Python geometry checks
            # dominated the high-resolution screenshot tests.
            raw = bytearray(width * height * 4)
            raw[3::4] = b"\xff" * (width * height)
            red = bytes(int((x + (px + .5) / scale) * 2) % 256 for px in range(width))
            blue = bytes(123 if name == "CaptureWindow" else (px % 2) * 255 for px in range(width))
            for output in data["outputs"]:
                rect = output["geometry"]
                left = max(0, math.ceil((rect["x"] - x) * scale - .5))
                right = min(width, math.ceil((rect["x"] + rect["width"] - x) * scale - .5))
                top = max(0, math.ceil((rect["y"] - y) * scale - .5))
                bottom = min(height, math.ceil((rect["y"] + rect["height"] - y) * scale - .5))
                if right <= left: continue
                for py in range(top, bottom):
                    start, end = (py * width + left) * 4, (py * width + right) * 4
                    raw[start:end:4] = red[left:right]
                    raw[start + 1:end:4] = bytes([int((y + (py + .5) / scale) * 2) % 256]) * (right - left)
                    raw[start + 2:end:4] = blue[left:right]
        # Transfer the received descriptor rather than retaining an extra writer
        # in the invocation. Truncation must produce EOF for the host reader.
        descriptors = invocation.get_message().get_unix_fd_list().steal_fds()
        fd = descriptors[handle]
        for index, descriptor in enumerate(descriptors):
            if index != handle:
                os.close(descriptor)
        stride = width * 4
        fmt = 999 if config.get("bad_format") else 17
        metadata = {"width": GLib.Variant("u", width), "height": GLib.Variant("u", height),
                    "stride": GLib.Variant("u", stride), "format": GLib.Variant("u", fmt), "scale": GLib.Variant("d", scale)}
        invocation.return_value(GLib.Variant("(a{sv})", (metadata,)))
        threading.Thread(target=write_pixels, args=(fd, raw[:-4] if config.get("truncate") else raw), daemon=True).start()
        save()
    else:
        invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", name)


for path, interfaces in (("/Scripting", [0]), ("/org/kde/KWin/EIS/RemoteDesktop", [2]),
                         ("/org/kde/KWin/ScreenShot2", [3]), ("/klipper", [4]), ("/org/a11y/bus", [5])):
    for index in interfaces:
        bus.register_object(path, node.interfaces[index], method, None, None)
save()
print("ready", flush=True)
try:
    GLib.MainLoop().run()
finally:
    parent.send(b"Q")
    oracle.wait(timeout=3)
