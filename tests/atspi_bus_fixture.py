"""Private-bus AT-SPI fixture; no real desktop or accessibility settings used."""

from __future__ import annotations

import json
import sys

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

ROOT = "/org/a11y/atspi/accessible/root"
APP = "/org/a11y/atspi/accessible/fixture"
XML = """<node>
<interface name='org.a11y.Bus'><method name='GetAddress'>
 <arg type='s' direction='out'/></method></interface>
<interface name='org.a11y.atspi.Accessible'>
 <property name='Name' type='s' access='read'/>
 <method name='GetChildren'><arg type='a(so)' direction='out'/></method>
 <method name='GetState'><arg type='au' direction='out'/></method>
</interface>
<interface name='org.a11y.atspi.Action'><method name='DoAction'>
 <arg type='i' direction='in'/><arg type='b' direction='out'/>
</method></interface>
<interface name='org.a11y.atspi.EditableText'><method name='SetTextContents'>
 <arg type='s' direction='in'/><arg type='b' direction='out'/>
</method></interface>
<interface name='org.a11y.atspi.Registry'><method name='RegisterEvent'>
 <arg type='s' direction='in'/></method></interface>
<interface name='org.spaces.Test'>
 <method name='State'><arg type='s' direction='out'/></method>
 <method name='Callback'><arg type='o' direction='in'/><arg type='s' direction='out'/></method>
 <method name='ReplaceApplication'><arg type='s' direction='out'/></method>
</interface></node>"""


def connect(address):
    return Gio.DBusConnection.new_for_address_sync(
        address,
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None,
        None,
    )


def main():
    session_address, a11y_address = sys.argv[1:]
    host = connect(session_address)
    registry = connect(a11y_address)
    app = connect(a11y_address)
    state = {"clicks": 0, "text": "", "events": []}
    interfaces = Gio.DBusNodeInfo.new_for_xml(XML).interfaces
    registrations = []

    def register_app():
        for info in interfaces:
            registrations.append(
                app.register_object(APP, info, method, property_get, None)
            )

    def method(
        connection, sender, path, interface, name, parameters, invocation
    ):
        nonlocal app
        args = parameters.unpack()
        if name == "GetAddress":
            reply = GLib.Variant("(s)", (a11y_address,))
        elif name == "GetChildren":
            reply = GLib.Variant("(a(so))", ([(app.get_unique_name(), APP)],))
        elif name == "GetState":
            reply = GLib.Variant("(au)", ([state["clicks"], 0],))
        elif name == "DoAction":
            state["clicks"] += 1
            app.emit_signal(
                None,
                APP,
                "org.a11y.atspi.Event.Object",
                "StateChanged",
                GLib.Variant(
                    "(siiva{sv})",
                    (
                        "checked",
                        state["clicks"],
                        0,
                        GLib.Variant("s", "fixture"),
                        {},
                    ),
                ),
            )
            reply = GLib.Variant("(b)", (True,))
        elif name == "SetTextContents":
            state["text"] = args[0]
            reply = GLib.Variant("(b)", (True,))
        elif name == "RegisterEvent":
            state["events"].append(args[0])
            reply = GLib.Variant("()", ())
        elif name == "State":
            reply = GLib.Variant("(s)", (json.dumps(state),))
        elif name == "Callback":
            # Real reverse call to an object exported by the proxied client.
            def finished(conn, result):
                try:
                    invocation.return_value(conn.call_finish(result))
                except GLib.Error as error:
                    invocation.return_dbus_error(
                        "org.spaces.Test.Error", str(error)
                    )

            connection.call(
                sender,
                args[0],
                "org.spaces.Listener",
                "Ping",
                None,
                GLib.VariantType.new("(s)"),
                Gio.DBusCallFlags.NONE,
                3000,
                None,
                finished,
            )
            return
        elif name == "ReplaceApplication":
            app.close_sync(None)
            registrations.clear()
            app = connect(a11y_address)
            register_app()
            reply = GLib.Variant("(s)", (app.get_unique_name(),))
        else:
            invocation.return_dbus_error("org.spaces.Test.Error", name)
            return
        invocation.return_value(reply)

    def property_get(*_args):
        return GLib.Variant("s", "Spaces AT-SPI fixture")

    host.register_object("/org/a11y/bus", interfaces[0], method, None, None)
    for info in interfaces:
        registry.register_object(ROOT, info, method, property_get, None)
    register_app()
    for connection, name in (
        (host, "org.a11y.Bus"),
        (registry, "org.a11y.atspi.Registry"),
    ):
        connection.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "RequestName",
            GLib.Variant("(su)", (name, 0)),
            None,
            Gio.DBusCallFlags.NONE,
            3000,
            None,
        )
    print(json.dumps({"application": app.get_unique_name()}), flush=True)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
