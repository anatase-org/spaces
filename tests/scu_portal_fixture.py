"""Private portal peer for testing prompt refusal and cancellation, without a GUI."""
import argparse
import json
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

parser = argparse.ArgumentParser()
parser.add_argument("address")
parser.add_argument("state", type=Path)
parser.add_argument("mode", choices=("refuse", "hold"))
args = parser.parse_args()
connection = Gio.DBusConnection.new_for_address_sync(args.address,
    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
    None, None)
connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
    "org.freedesktop.DBus", "RequestName",
    GLib.Variant("(su)", ("org.freedesktop.portal.Desktop", 0)), None,
    Gio.DBusCallFlags.NONE, 3000, None)
node = Gio.DBusNodeInfo.new_for_xml('''<node>
<interface name="org.freedesktop.portal.RemoteDesktop">
<method name="CreateSession"><arg type="a{sv}" direction="in"/><arg type="o" direction="out"/></method>
</interface></node>''')
request = Gio.DBusNodeInfo.new_for_xml('''<node>
<interface name="org.freedesktop.portal.Request"><method name="Close"/></interface>
</node>''')
state = {"created": 0, "closed": 0}


def save():
    temporary = args.state.with_suffix(".tmp")
    temporary.write_text(json.dumps(state))
    temporary.replace(args.state)


def close(_bus, _sender, _path, _interface, _method, _parameters, invocation):
    state["closed"] += 1
    save()
    invocation.return_value(None)


def create(_bus, sender, _path, _interface, _method, parameters, invocation):
    token = parameters.unpack()[0]["handle_token"]
    path = "/org/freedesktop/portal/desktop/request/" + sender[1:].replace(".", "_") + "/" + token
    connection.register_object(path, request.interfaces[0], close, None, None)
    state["created"] += 1
    save()
    invocation.return_value(GLib.Variant("(o)", (path,)))
    if args.mode == "refuse":
        connection.emit_signal(sender, path, "org.freedesktop.portal.Request", "Response",
                               GLib.Variant("(ua{sv})", (1, {})))


connection.register_object("/org/freedesktop/portal/desktop", node.interfaces[0], create, None, None)
save()
print("ready", flush=True)
GLib.MainLoop().run()
