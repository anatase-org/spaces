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
<interface name='org.a11y.Status'><property name='IsEnabled' type='b' access='readwrite'/></interface>
<interface name='org.a11y.atspi.Socket'>
 <method name='Embed'><arg type='(so)' direction='in'/><arg type='(so)' direction='out'/></method>
 <method name='Unembed'><arg type='(so)' direction='in'/></method>
</interface>
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
 <method name='SetEnabled'><arg type='b' direction='in'/></method>
 <method name='ReleaseStatus'/><method name='AcquireStatus'/>
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
    session_address, a11y_address = sys.argv[1:3]
    native = "--native" in sys.argv[3:]
    host = connect(session_address)
    registry = connect(a11y_address)
    app = connect(a11y_address)
    state = {"clicks": 0, "text": "", "events": [], "enabled": not native, "providers": []}
    xml = XML
    if "--screen-reader" in sys.argv[3:]:
        xml = xml.replace("<property name='IsEnabled' type='b' access='readwrite'/>",
                          "<property name='IsEnabled' type='b' access='readwrite'/>"
                          "<property name='ScreenReaderEnabled' type='b' access='read'/>")
    interfaces = Gio.DBusNodeInfo.new_for_xml(xml).interfaces
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
        elif name == "SetEnabled":
            state["enabled"] = args[0]
            host.emit_signal(None, "/org/a11y/bus", "org.freedesktop.DBus.Properties", "PropertiesChanged",
                             GLib.Variant("(sa{sv}as)", ("org.a11y.Status", {"IsEnabled": GLib.Variant("b", args[0])}, [])))
            reply = GLib.Variant("()", ())
        elif name in ("ReleaseStatus", "AcquireStatus"):
            host.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                           "ReleaseName" if name == "ReleaseStatus" else "RequestName",
                           GLib.Variant("(s)", ("org.a11y.Bus",)) if name == "ReleaseStatus"
                           else GLib.Variant("(su)", ("org.a11y.Bus", 0)),
                           None, Gio.DBusCallFlags.NONE, 3000, None)
            reply = GLib.Variant("()", ())
        elif name == "Embed":
            state["providers"].append(args[0])
            reply = GLib.Variant("((so))", ((registry.get_unique_name(), ROOT),))
        elif name == "Unembed":
            state["providers"].remove(args[0])
            reply = GLib.Variant("()", ())
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

    def property_get(_connection, _sender, _path, interface, _property):
        if interface == "org.a11y.Status":
            return GLib.Variant("b", True if _property == "ScreenReaderEnabled" else state["enabled"])
        return GLib.Variant("s", "Spaces AT-SPI fixture")

    def property_set(*_args):
        state["enabled"] = _args[-1].unpack()
        return True

    for info in interfaces:
        if info.name in ("org.a11y.Bus", "org.a11y.Status", "org.spaces.Test"):
            host.register_object("/org/a11y/bus", info, method, property_get, property_set)
    # A stable portal name lets the real guest router use this private session.
    if not native:
        host.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                       "RequestName", GLib.Variant("(su)", ("org.freedesktop.portal.Desktop", 0)),
                       None, Gio.DBusCallFlags.NONE, 3000, None)
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
            GLib.Variant("(su)", (name, 1)),
            None,
            Gio.DBusCallFlags.NONE,
            3000,
            None,
        )
    print(json.dumps({"application": app.get_unique_name()}), flush=True)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
