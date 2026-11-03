"""Private systemd/application bus, or a disposable launched process recorder."""
import json
import os
from pathlib import Path
import sys
import time

if sys.argv[1] == "--record":
    state = dict(pid=os.getpid(), args=sys.argv[3:],
                 wayland=os.environ.get("WAYLAND_DISPLAY"),
                 desktop=os.environ.get("GIO_LAUNCHED_DESKTOP_FILE"))
    with open(sys.argv[2], "a") as stream:
        stream.write(json.dumps(state) + "\n")
    print("Application output must not enter SCU's worker protocol", flush=True)
    if "--hold" in sys.argv[3:]:
        time.sleep(120)
    sys.exit(0)

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

address, state_path, control_path = sys.argv[1:]
state_file, control_file = Path(state_path), Path(control_path)
state = dict(scopes=[], calls=[])
flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
bus = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
for name in ("org.freedesktop.systemd1", "org.example.ScuApp"):
    bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                  "RequestName", GLib.Variant("(su)", (name, 0)), None, Gio.DBusCallFlags.NONE, 2000, None)
node = Gio.DBusNodeInfo.new_for_xml("""<node>
<interface name='org.freedesktop.systemd1.Manager'>
 <method name='StartTransientUnit'><arg type='s' direction='in'/><arg type='s' direction='in'/>
 <arg type='a(sv)' direction='in'/><arg type='a(sa(sv))' direction='in'/><arg type='o' direction='out'/></method>
 <method name='GetUnit'><arg type='s' direction='in'/><arg type='o' direction='out'/></method>
</interface>
<interface name='org.freedesktop.systemd1.Unit'><property name='ActiveState' type='s' access='read'/></interface>
<interface name='org.freedesktop.Application'>
 <method name='Activate'><arg type='a{sv}' direction='in'/></method>
 <method name='Open'><arg type='as' direction='in'/><arg type='a{sv}' direction='in'/></method>
 <method name='ActivateAction'><arg type='s' direction='in'/><arg type='av' direction='in'/><arg type='a{sv}' direction='in'/></method>
</interface></node>""")


def save():
    temporary = state_file.with_suffix(".next")
    temporary.write_text(json.dumps(state))
    temporary.replace(state_file)


def method(connection, sender, path, interface, name, parameters, invocation):
    config = json.loads(control_file.read_text())
    values = parameters.unpack()
    if name == "StartTransientUnit":
        if config.get("scope_error"):
            invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", "Scope creation rejected")
            return
        state["scopes"].append(dict(name=values[0], mode=values[1], properties=dict(values[2])))
        save()
        invocation.return_value(GLib.Variant("(o)", ("/org/freedesktop/systemd1/job/1",)))
    elif name == "GetUnit":
        invocation.return_value(GLib.Variant("(o)", ("/org/freedesktop/systemd1/unit/fixture",)))
    else:
        state["calls"].append(dict(method=name, args=values))
        save()
        invocation.return_value(None)


bus.register_object("/org/freedesktop/systemd1", node.interfaces[0], method, None, None)
bus.register_object("/org/freedesktop/systemd1/unit/fixture", node.interfaces[1], None,
                    lambda *args: GLib.Variant("s", "active"), None)
bus.register_object("/org/example/ScuApp", node.interfaces[2], method, None, None)
save()
print("ready", flush=True)
GLib.MainLoop().run()
