"""Private portal with identity-dependent secrets for broker regression tests."""
import json
import os
from pathlib import Path
import sys

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

NAME = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
XML = """<node>
<interface name='org.freedesktop.host.portal.Registry'>
 <method name='Register'><arg type='s' direction='in'/>
 <arg type='a{sv}' direction='in'/></method></interface>
<interface name='org.freedesktop.portal.Secret'>
 <method name='RetrieveSecret'><arg type='h' direction='in'/>
 <arg type='a{sv}' direction='in'/><arg type='o' direction='out'/>
 </method></interface></node>"""


def main():
    address, events_path, failures, delay = sys.argv[1:]
    failures = int(failures)
    delay = float(delay)
    identities = {}
    bus = Gio.DBusConnection.new_for_address_sync(
        address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None,
    )

    def event(**data):
        with Path(events_path).open("a") as stream:
            stream.write(json.dumps(data) + "\n")

    def method(connection, sender, path, interface, name, parameters, invocation):
        nonlocal failures
        if name == "Register":
            app_id, _ = parameters.unpack()
            event(method="RegisterStarted", app_id=app_id)
            if failures:
                event(method=name, app_id=app_id, success=False)
                failures -= 1
                invocation.return_dbus_error(
                    "org.freedesktop.portal.Error.Failed", "Portal is still starting"
                )
                return
            def finish_registration():
                identities[sender] = app_id
                event(method=name, app_id=app_id, success=True)
                invocation.return_value(GLib.Variant("()", ()))
                return False

            GLib.timeout_add(max(1, int(delay * 1000)), finish_registration)
        else:
            app_id = identities.get(sender, "")
            event(method=name, app_id=app_id)
            handle, options = parameters.unpack()
            descriptor = invocation.get_message().get_unix_fd_list().get(handle)
            # Model a backend that keys its secret by the registered identity.
            os.write(descriptor, b"registered" if app_id else b"unregistered")
            os.close(descriptor)
            request_path = "/org/freedesktop/portal/desktop/request/test/" + options["handle_token"]
            invocation.return_value(GLib.Variant("(o)", (request_path,)))

            def respond():
                connection.emit_signal(sender, request_path,
                    "org.freedesktop.portal.Request", "Response",
                    GLib.Variant("(ua{sv})", (0, {})))
                return False

            GLib.idle_add(respond)

    node = Gio.DBusNodeInfo.new_for_xml(XML)
    for interface in node.interfaces:
        bus.register_object(PATH, interface, method, None, None)
    bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
        "org.freedesktop.DBus", "RequestName", GLib.Variant("(su)", (NAME, 3)),
        None, Gio.DBusCallFlags.NONE, 3000, None)
    event(method="Ready", owner=bus.get_unique_name())
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
