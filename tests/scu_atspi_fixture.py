"""A real private AT-SPI registry/application for scu integration tests."""

import json
import sys
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib
from types import SimpleNamespace

# Stable AT-SPI wire enum values; no libatspi client is used by the server.
Atspi = SimpleNamespace(
    Role=SimpleNamespace(APPLICATION=75, FRAME=23, PUSH_BUTTON=43, ENTRY=79, PASSWORD_TEXT=40, PANEL=39),
    StateType=SimpleNamespace(EDITABLE=7, ENABLED=8, FOCUSED=12, SENSITIVE=24, SHOWING=25, VISIBLE=30),
)

ROOT = "/org/a11y/atspi/accessible/root"
WINDOW = "/org/a11y/atspi/accessible/window"
WINDOW_TWO = "/org/a11y/atspi/accessible/window2"
BUTTON = "/org/a11y/atspi/accessible/button"
ENTRY = "/org/a11y/atspi/accessible/entry"
PASSWORD = "/org/a11y/atspi/accessible/password"
PANEL = "/org/a11y/atspi/accessible/panel"
XML = """<node>
<interface name='org.a11y.atspi.Accessible'>
 <property name='Name' type='s' access='read'/>
 <property name='Description' type='s' access='read'/>
 <property name='ChildCount' type='i' access='read'/>
 <property name='Parent' type='(so)' access='read'/>
 <method name='GetChildren'><arg type='a(so)' direction='out'/></method>
 <method name='GetChildAtIndex'><arg type='i' direction='in'/><arg type='(so)' direction='out'/></method>
 <method name='GetRole'><arg type='u' direction='out'/></method>
 <method name='GetRoleName'><arg type='s' direction='out'/></method>
 <method name='GetLocalizedRoleName'><arg type='s' direction='out'/></method>
 <method name='GetState'><arg type='au' direction='out'/></method>
 <method name='GetInterfaces'><arg type='as' direction='out'/></method>
 <method name='GetAttributes'><arg type='a{ss}' direction='out'/></method>
 <method name='GetIndexInParent'><arg type='i' direction='out'/></method>
 <method name='GetApplication'><arg type='(so)' direction='out'/></method>
</interface>
<interface name='org.a11y.atspi.Application'>
 <property name='ToolkitName' type='s' access='read'/>
 <property name='Version' type='s' access='read'/>
 <property name='Id' type='i' access='readwrite'/>
 <method name='GetLocale'><arg type='u' direction='in'/><arg type='s' direction='out'/></method>
 <method name='GetApplicationBusAddress'><arg type='s' direction='out'/></method>
</interface>
<interface name='org.a11y.atspi.Component'>
 <method name='GetExtents'><arg type='u' direction='in'/><arg type='(iiii)' direction='out'/></method>
 <method name='GrabFocus'><arg type='b' direction='out'/></method>
</interface>
<interface name='org.a11y.atspi.Action'>
 <property name='NActions' type='i' access='read'/>
 <method name='GetActions'><arg type='a(sss)' direction='out'/></method>
 <method name='GetName'><arg type='i' direction='in'/><arg type='s' direction='out'/></method>
 <method name='DoAction'><arg type='i' direction='in'/><arg type='b' direction='out'/></method>
</interface>
<interface name='org.a11y.atspi.Text'>
 <property name='CharacterCount' type='i' access='read'/>
 <property name='CaretOffset' type='i' access='read'/>
 <method name='GetText'><arg type='i' direction='in'/><arg type='i' direction='in'/><arg type='s' direction='out'/></method>
 <method name='GetNSelections'><arg type='i' direction='out'/></method>
 <method name='GetSelection'><arg type='i' direction='in'/><arg type='i' direction='out'/><arg type='i' direction='out'/></method>
 <method name='AddSelection'><arg type='i' direction='in'/><arg type='i' direction='in'/><arg type='b' direction='out'/></method>
 <method name='SetSelection'><arg type='i' direction='in'/><arg type='i' direction='in'/><arg type='i' direction='in'/><arg type='b' direction='out'/></method>
</interface>
<interface name='org.a11y.atspi.EditableText'>
 <method name='SetTextContents'><arg type='s' direction='in'/><arg type='b' direction='out'/></method>
 <method name='InsertText'><arg type='i' direction='in'/><arg type='s' direction='in'/><arg type='i' direction='in'/><arg type='b' direction='out'/></method>
</interface>
<interface name='org.a11y.atspi.Registry'>
 <method name='RegisterEvent'><arg type='s' direction='in'/><arg type='as' direction='in'/><arg type='s' direction='in'/></method>
 <method name='DeregisterEvent'><arg type='s' direction='in'/><arg type='s' direction='in'/></method>
 <method name='GetRegisteredEvents'><arg type='a(ss)' direction='out'/></method>
</interface>
</node>"""


def main():
    address, state_path = sys.argv[1:3]
    duplicate_windows = "--duplicate-windows" in sys.argv[3:]
    window_paths = [WINDOW, WINDOW_TWO] if duplicate_windows or "--two-windows" in sys.argv[3:] else [WINDOW]
    flags = (Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
             | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION)
    registry = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
    app = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
    registry.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                      "org.freedesktop.DBus", "RequestName",
                      GLib.Variant("(su)", ("org.a11y.atspi.Registry", 0)),
                      None, Gio.DBusCallFlags.NONE, 3000, None)
    node = Gio.DBusNodeInfo.new_for_xml(XML)
    state = {"clicks": 0, "action_times": [], "text": "", "password_reads": 0, "focus_requests": 0,
             "registrations": 0, "deregistrations": 0, "listeners": {},
             "attribute_queries": 0, "idle_pings": 0, "events": 0}
    activation = next((int(arg.split("=", 1)[1]) for arg in sys.argv[3:]
                       if arg.startswith("--activation-delay=")), None)
    exposed = activation is None
    activation_pending = False
    empty = "--empty-window" in sys.argv[3:]
    unsupported = "--unsupported-attributes" in sys.argv[3:]
    keyboard = "--keyboard-entry" in sys.argv[3:]
    refuse_focus = "--refuse-focus" in sys.argv[3:]
    lose_focus = "--lose-focus" in sys.argv[3:]
    readonly = "--readonly-entry" in sys.argv[3:]
    hidden = "--hidden-button" in sys.argv[3:]
    hidden_container = "--hidden-container" in sys.argv[3:]
    focused = False
    selected_fixture = "--selection" in sys.argv[3:] or "--selection-unstable" in sys.argv[3:]
    unstable_selection = "--selection-unstable" in sys.argv[3:]
    selection_queries = 0
    if selected_fixture:
        state.update(text="selected seed", selection_restores=0)

    def save():
        path = Path(state_path)
        temporary = path.with_suffix(".next")
        temporary.write_text(json.dumps(state))
        temporary.replace(path)

    def actions():
        path = Path(state_path).with_suffix(".actions.json")
        return json.loads(path.read_text()) if path.exists() else ["click"]

    def text_value():
        # Read the independent EIS receiver, never the requested edit contents.
        if keyboard and not readonly:
            path = Path(state_path).with_name("kwin-state.json")
            if path.exists():
                return json.loads(path.read_text()).get("input", {}).get("text", "")
        return state["text"]

    def text_selected():
        path = Path(state_path).with_name("kwin-state.json")
        return path.exists() and json.loads(path.read_text()).get("input", {}).get("selected", False)

    def owner_changed(connection, sender, path, interface, signal, parameters, unused):
        name, old, new = parameters.unpack()
        if old and not new and name in state["listeners"]:
            del state["listeners"][name]
            save()

    registry.signal_subscribe("org.freedesktop.DBus", "org.freedesktop.DBus",
                              "NameOwnerChanged", "/org/freedesktop/DBus", None,
                              Gio.DBusSignalFlags.NONE, owner_changed, None)

    def emit_children():
        state["events"] += 1
        app.emit_signal(None, WINDOW, "org.a11y.atspi.Event.Object", "ChildrenChanged",
                        GLib.Variant("(siiv(so))", ("add", 0, 0,
                            GLib.Variant("(so)", (app.get_unique_name(), BUTTON)),
                            (app.get_unique_name(), ROOT))))
        save()

    def activate():
        nonlocal exposed
        exposed = True
        emit_children()
        return GLib.SOURCE_REMOVE

    def idle_probe(sender):
        emit_children()
        def finished(connection, result, unused):
            try:
                connection.call_finish(result)
            except GLib.Error:
                return
            state["idle_pings"] += 1
            save()
        registry.call(sender, "/", "org.freedesktop.DBus.Peer", "Ping", None,
                      None, Gio.DBusCallFlags.NONE, 1500, None, finished, None)
        return GLib.SOURCE_REMOVE

    def children(connection, path):
        if connection == registry:
            return [(app.get_unique_name(), ROOT)]
        if path == WINDOW and (not exposed or empty):
            # Chromium can advertise a child while it still returns null.
            return [("", "/org/a11y/atspi/null")] if not empty else []
        if hidden_container and path in (WINDOW, PANEL):
            paths = (PANEL, ENTRY, PASSWORD) if path == WINDOW else (BUTTON,)
            return [(app.get_unique_name(), path) for path in paths]
        return {ROOT: [(app.get_unique_name(), path) for path in window_paths],
                WINDOW: [(app.get_unique_name(), p) for p in (BUTTON, ENTRY, PASSWORD)]}.get(path, [])

    def interfaces(path, connection):
        values = ["org.a11y.atspi.Accessible"]
        if path == ROOT and connection == app:
            values += ["org.a11y.atspi.Application"]
        if path != ROOT:
            values += ["org.a11y.atspi.Component"]
        if path == BUTTON:
            values += ["org.a11y.atspi.Action"]
        if path in (ENTRY, PASSWORD):
            values += ["org.a11y.atspi.Text"]
            if not keyboard or path == PASSWORD:
                values += ["org.a11y.atspi.EditableText"]
        return values

    def method(connection, sender, path, interface, name, parameters, invocation):
        nonlocal selection_queries, activation_pending, focused
        args = parameters.unpack()
        if name == "GetExtents":
            # Deliberately wrong SCREEN values expose any accidental raw-coordinate leak.
            rectangle = ((9000,9000,40,30) if args[0] == 0 else
                         {WINDOW:(0,0,40,30),WINDOW_TWO:(0,0,40,30),
                          BUTTON:(5,5,10,5),ENTRY:(5,12,20,5),PASSWORD:(5,20,20,5)}.get(path,(0,0,40,30)))
            reply = GLib.Variant("((iiii))", (rectangle,))
        elif name == "GrabFocus":
            focused = not refuse_focus
            state["focus_requests"] += 1
            save()
            reply = GLib.Variant("(b)", (True,))
        elif name == "GetChildren":
            reply = GLib.Variant("(a(so))", (children(connection, path),))
        elif name == "GetChildAtIndex":
            entries = children(connection, path)
            reply = GLib.Variant("((so))", (entries[args[0]] if 0 <= args[0] < len(entries) else ("", "/org/a11y/atspi/null"),))
        elif name == "GetInterfaces":
            reply = GLib.Variant("(as)", (interfaces(path, connection),))
        elif name == "GetRole":
            role = {ROOT: Atspi.Role.APPLICATION, WINDOW: Atspi.Role.FRAME, WINDOW_TWO: Atspi.Role.FRAME,
                    BUTTON: Atspi.Role.PUSH_BUTTON, ENTRY: Atspi.Role.ENTRY,
                    PASSWORD: Atspi.Role.PASSWORD_TEXT, PANEL: Atspi.Role.PANEL}[path]
            reply = GLib.Variant("(u)", (int(role),))
        elif name in ("GetRoleName", "GetLocalizedRoleName"):
            reply = GLib.Variant("(s)", ({ROOT: "application", WINDOW: "frame", WINDOW_TWO: "frame",
                    BUTTON: "push button", ENTRY: "entry", PASSWORD: "password text", PANEL: "panel"}[path],))
        elif name == "GetState":
            bits = sum(1 << int(s) for s in (Atspi.StateType.ENABLED, Atspi.StateType.SENSITIVE,
                                             Atspi.StateType.VISIBLE, Atspi.StateType.SHOWING))
            if (path == BUTTON and hidden) or path == PANEL:
                bits &= ~((1 << Atspi.StateType.VISIBLE) | (1 << Atspi.StateType.SHOWING))
            if path == ENTRY:
                bits |= 1 << Atspi.StateType.EDITABLE
            still_focused = focused
            if lose_focus:
                oracle = Path(state_path).with_name("kwin-state.json")
                if oracle.exists() and json.loads(oracle.read_text()).get("input", {}).get("keys", 0):
                    still_focused = False
            if path == ENTRY and (selected_fixture or still_focused):
                bits |= 1 << Atspi.StateType.FOCUSED
            reply = GLib.Variant("(au)", ([bits & 0xffffffff, bits >> 32],))
        elif name == "GetApplication":
            reply = GLib.Variant("((so))", ((app.get_unique_name(), ROOT),))
        elif name == "GetAttributes":
            if unsupported:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", name)
                return
            state["attribute_queries"] += 1
            save()
            if path == WINDOW and activation is not None and not activation_pending:
                activation_pending = True
                GLib.timeout_add(activation, activate)
            reply = GLib.Variant("(a{ss})", ({},))
        elif name == "GetIndexInParent": reply = GLib.Variant("(i)", (0,))
        elif name == "GetLocale": reply = GLib.Variant("(s)", ("en_US.UTF-8",))
        elif name == "GetApplicationBusAddress": reply = GLib.Variant("(s)", ("",))
        elif name == "GetName": reply = GLib.Variant("(s)", (actions()[args[0]],))
        elif name == "GetActions": reply = GLib.Variant("(a(sss))", ([(action, "", "") for action in actions()],))
        elif name == "DoAction":
            state["clicks"] += 1
            state["last_action"] = actions()[args[0]]
            state["action_times"].append(GLib.get_monotonic_time() / 1_000_000)
            save()
            reply = GLib.Variant("(b)", (True,))
        elif name == "SetTextContents":
            state["text"] = args[0]
            save()
            reply = GLib.Variant("(b)", (True,))
        elif name == "InsertText":
            offset, text, length = args
            state["text"] = state["text"][:offset] + text[:length] + state["text"][offset:]
            save()
            reply = GLib.Variant("(b)", (True,))
        elif name == "GetText":
            if path == PASSWORD:
                state["password_reads"] += 1
                save()
            start, end = args
            reply = GLib.Variant("(s)", (text_value()[start:None if end < 0 else end],))
        elif name == "GetNSelections":
            selection_queries += 1
            if keyboard:
                invocation.return_value(GLib.Variant("(i)", (int(bool(text_value()) and text_selected()),)))
                return
            # Simulate losing PRIMARY between saving and restoring selection.
            reply = GLib.Variant("(i)", (int(selected_fixture and (selection_queries == 1 or
                (state["selection_restores"] > 0 and not unstable_selection))),))
        elif name == "GetSelection":
            reply = GLib.Variant("(ii)", (0, len(text_value())))
        elif name in ("AddSelection", "SetSelection"):
            assert args[-2:] == (0, len(state["text"]))
            state["selection_restores"] += 1
            save()
            reply = GLib.Variant("(b)", (True,))
        elif name == "GetRegisteredEvents":
            reply = GLib.Variant("(a(ss))", ([(owner, event) for owner, events
                in state["listeners"].items() for event in events],))
        elif name == "RegisterEvent":
            state["registrations"] += 1
            state["listeners"].setdefault(sender, []).append(args[0])
            save()
            GLib.timeout_add(400, idle_probe, sender)
            reply = GLib.Variant("()", ())
        elif name == "DeregisterEvent":
            state["deregistrations"] += 1
            events = state["listeners"].get(sender, [])
            if args[0] in events: events.remove(args[0])
            if not events: state["listeners"].pop(sender, None)
            save()
            reply = GLib.Variant("()", ())
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", name)
            return
        invocation.return_value(reply)

    def property_get(connection, sender, path, interface, name):
        if name == "Name":
            return GLib.Variant("s", {ROOT: "scu fixture", WINDOW: "scu test window", WINDOW_TWO: "scu test window" if duplicate_windows else "scu second window",
                BUTTON: "Fixture button", ENTRY: "Fixture entry", PASSWORD: "Fixture password", PANEL: "Hidden container"}[path])
        if name in ("Description", "ToolkitName", "Version"): return GLib.Variant("s", "fixture")
        if name == "ChildCount": return GLib.Variant("i", len(children(connection, path)))
        if name == "Parent": return GLib.Variant("(so)", (app.get_unique_name(), ROOT if path in window_paths else WINDOW))
        if name == "NActions": return GLib.Variant("i", len(actions()))
        if name == "CharacterCount": return GLib.Variant("i", len(text_value()))
        if name == "CaretOffset": return GLib.Variant("i", len(text_value()))
        if name == "Id": return GLib.Variant("i", 1)

    for connection, paths in ((registry, [ROOT]), (app, [ROOT, *window_paths, BUTTON, ENTRY, PASSWORD, PANEL])):
        if connection == registry and "--registry-unavailable" in sys.argv[3:]:
            continue  # Own the name, but expose no working registry object.
        for path in paths:
            for info in node.interfaces:
                connection.register_object(path, info, method, property_get, None)
    if "--registry-unavailable" not in sys.argv[3:]:
        registry.register_object("/org/a11y/atspi/registry",
            node.lookup_interface("org.a11y.atspi.Registry"), method, None, None)
    save()
    print("ready", flush=True)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
