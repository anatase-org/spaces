"""Private logind and screen-locker peer on a test bus; no real desktop calls."""
import json
import os
from pathlib import Path
import sys

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

address, control_path, state_path = sys.argv[1:]
control, state_file = Path(control_path), Path(state_path)
flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
bus = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
for name in ("org.freedesktop.login1", "org.kde.screensaver"):
    bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                  "RequestName", GLib.Variant("(su)", (name, 0)), None, Gio.DBusCallFlags.NONE, 2000, None)

XML = """<node>
<interface name='org.freedesktop.login1.Manager'>
 <method name='GetSession'><arg type='s' direction='in'/><arg type='o' direction='out'/></method>
 <signal name='SessionRemoved'><arg type='s'/><arg type='o'/></signal>
</interface>
<interface name='org.freedesktop.login1.Session'>
 <method name='Unlock'/><method name='SetLocked'><arg type='b' direction='in'/></method>
 <method name='Deactivate'/>
 <property name='User' type='(uo)' access='read'/><property name='Active' type='b' access='read'/>
 <property name='Remote' type='b' access='read'/><property name='Type' type='s' access='read'/>
 <property name='Class' type='s' access='read'/><property name='LockedHint' type='b' access='read'/>
</interface>
<interface name='org.freedesktop.ScreenSaver'>
 <method name='GetActive'><arg type='b' direction='out'/></method>
</interface></node>"""
node = Gio.DBusNodeInfo.new_for_xml(XML)
path = "/org/freedesktop/login1/session/fixture"
state = {"locked": json.loads(control.read_text()).get("locked", False), "unlocks": 0, "active": True}


def save():
    temporary = state_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(state))
    temporary.replace(state_file)


def locked(value):
    state["locked"] = value
    bus.emit_signal(None, path, "org.freedesktop.DBus.Properties", "PropertiesChanged",
        GLib.Variant("(sa{sv}as)", ("org.freedesktop.login1.Session", {"LockedHint": GLib.Variant("b", value)}, [])))
    save()


def method(connection, sender, object_path, interface, name, parameters, invocation):
    config = json.loads(control.read_text())
    if name == "GetSession":
        invocation.return_value(GLib.Variant("(o)", (path,)))
    elif name == "GetActive":
        invocation.return_value(GLib.Variant("(b)", (state["locked"],)))
    elif name == "SetLocked":
        locked(parameters.unpack()[0])
        invocation.return_value(None)
    elif name == "Unlock":
        state["unlocks"] += 1
        save()
        if config.get("deny_unlock"):
            invocation.return_dbus_error("org.freedesktop.DBus.Error.AccessDenied", "fixture denies unlock")
            return
        if not config.get("hold_unlock"):
            locked(False)
        invocation.return_value(None)
    elif name == "Deactivate":
        state["active"] = False
        save()
        bus.emit_signal(None, path, "org.freedesktop.DBus.Properties", "PropertiesChanged",
            GLib.Variant("(sa{sv}as)", ("org.freedesktop.login1.Session", {"Active": GLib.Variant("b", False)}, [])))
        invocation.return_value(None)


def property_value(connection, sender, object_path, interface, name):
    config = json.loads(control.read_text())
    values = {"User": GLib.Variant("(uo)", (config.get("login_uid", os.getuid()), "/org/freedesktop/login1/user/fixture")),
              "Active": GLib.Variant("b", state["active"]), "Remote": GLib.Variant("b", False),
              "Type": GLib.Variant("s", "wayland"), "Class": GLib.Variant("s", "user"),
              "LockedHint": GLib.Variant("b", state["locked"])}
    return values[name]


bus.register_object("/org/freedesktop/login1", node.interfaces[0], method, None, None)
bus.register_object(path, node.interfaces[1], method, property_value, None)
bus.register_object("/ScreenSaver", node.interfaces[2], method, None, None)
save()
print("ready", flush=True)
GLib.MainLoop().run()
