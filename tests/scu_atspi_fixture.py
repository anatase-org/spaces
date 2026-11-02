"""A real private AT-SPI registry/application for scu integration tests."""

import json
from functools import lru_cache
import sys
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib
from types import SimpleNamespace

# Stable AT-SPI wire enum values; no libatspi client is used by the server.
Atspi = SimpleNamespace(
    Role=SimpleNamespace(APPLICATION=75, FRAME=23, PUSH_BUTTON=43, ENTRY=79, PASSWORD_TEXT=40,
                         PANEL=39, SLIDER=51, DOCUMENT_WEB=95, TOOL_TIP=64, PARAGRAPH=73, STATIC=116, HEADING=83, LINK=88,
                         SEPARATOR=50, TOOL_BAR=63),
    StateType=SimpleNamespace(DEFUNCT=6, EDITABLE=7, ENABLED=8, FOCUSABLE=11, FOCUSED=12, SENSITIVE=24, SHOWING=25, VISIBLE=30),
)

ROOT = "/org/a11y/atspi/accessible/root"
WINDOW = "/org/a11y/atspi/accessible/window"
WINDOW_TWO = "/org/a11y/atspi/accessible/window2"
BUTTON = "/org/a11y/atspi/accessible/button"
ENTRY = "/org/a11y/atspi/accessible/entry"
PASSWORD = "/org/a11y/atspi/accessible/password"
PANEL = "/org/a11y/atspi/accessible/panel"
SLIDER = "/org/a11y/atspi/accessible/slider"
DOCUMENT = "/org/a11y/atspi/accessible/document"
TOOLTIP = "/org/a11y/atspi/accessible/tooltip"
PROSE = "/org/a11y/atspi/accessible/prose"
CAPTION = "/org/a11y/atspi/accessible/caption"
HEADING = "/org/a11y/atspi/accessible/heading"
LINK = "/org/a11y/atspi/accessible/link"
SHORT = CAPTION + "/short"
TOOLBAR = "/org/a11y/atspi/accessible/toolbar"
SEPARATOR = "/org/a11y/atspi/accessible/separator"
DIVIDER = "/org/a11y/atspi/accessible/divider"
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
<interface name='org.a11y.atspi.Value'>
 <property name='CurrentValue' type='d' access='readwrite'/>
 <property name='MinimumValue' type='d' access='read'/>
 <property name='MaximumValue' type='d' access='read'/>
 <property name='MinimumIncrement' type='d' access='read'/>
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
    settle_after_action = "--settle-after-action" in sys.argv[3:]
    empty = "--empty-window" in sys.argv[3:]
    unsupported = "--unsupported-attributes" in sys.argv[3:]
    keyboard = "--keyboard-entry" in sys.argv[3:]
    refuse_focus = "--refuse-focus" in sys.argv[3:]
    lose_focus = "--lose-focus" in sys.argv[3:]
    readonly = "--readonly-entry" in sys.argv[3:]
    numeric = "--numeric" in sys.argv[3:]
    numeric_keyboard = "--numeric-keyboard" in sys.argv[3:]
    numeric_readonly = "--numeric-readonly" in sys.argv[3:]
    numeric_delay = "--numeric-delay" in sys.argv[3:]
    state.update(value=80.0, value_writes=0)
    hidden = "--hidden-button" in sys.argv[3:]
    hidden_container = "--hidden-container" in sys.argv[3:]
    browser = "--browser" in sys.argv[3:]
    offscreen = "--offscreen" in sys.argv[3:]
    tooltips = "--tooltips" in sys.argv[3:]
    reading_text = "--reading-text" in sys.argv[3:]
    decorations = "--decorations" in sys.argv[3:]
    reading_values = {PROSE: "Read-only paragraph text, with a nested caption.",
                      CAPTION: "The complete caption text is not its accessible name.",
                      HEADING: "Article heading",
                      CAPTION + "/space": " \t\n\u00a0 ",
                      CAPTION + "/separator": " | \u2022 \u00b7 ",
                      CAPTION + "/duplicate": "  by\t ",
                      CAPTION + "/link_label": " Article link ",
                      CAPTION + "/caption_text": "The complete caption text is not its accessible name.",
                      CAPTION + "/punctuation": "C++: x | y."}
    reading_names = {CAPTION + "/space": "", CAPTION + "/separator": "|",
                     CAPTION + "/duplicate": "by", CAPTION + "/link_label": "Article link",
                     CAPTION + "/caption_text": "The complete caption text is not its accessible name.",
                     CAPTION + "/punctuation": "C++: x | y."}
    reading_names.update({CAPTION + "/fragment1": "Consider applying for YC's Winter 2027 batch!",
                          CAPTION + "/fragment2": "Applications",
                          CAPTION + "/fragment3": "are open till November 2.",
                          CAPTION + "/long": "Detailed sentence. " * 20,
                          CAPTION + "/truncated": "Truly long reading text. " * 30,
                          PROSE + "/second": "Paragraph metadata",
                          CAPTION + "/before_code": "Before code",
                          CAPTION + "/code": "#define main oldmain #include <unistd.h>",
                          CAPTION + "/after_code": "After code",
                          CAPTION + "/code/first": "#define main oldmain",
                          CAPTION + "/code/second": "#include <unistd.h>",
                          CAPTION + "/code/second/first": "#include",
                          CAPTION + "/code/second/second": "<unistd.h>",
                          CAPTION + "/punct_code": 'int fd = memfd_create("fuzz space", 0);',
                          CAPTION + "/punct_code/first": "int fd = memfd_create(",
                          CAPTION + "/punct_code/string": '"fuzz space"',
                          CAPTION + "/punct_code/comma": ",",
                          CAPTION + "/punct_code/zero": "0",
                          CAPTION + "/punct_code/end": ");",
                          CAPTION + "/string_spaces": 'print("ab");',
                          CAPTION + "/string_spaces/child": 'print("a b");',
                          PROSE + "/embedded": "Inline paragraph metadata",
                          CAPTION + "/embedded/before": "An owning type like",
                          CAPTION + "/embedded/type": "std::string",
                          CAPTION + "/embedded/between": "and view",
                          CAPTION + "/embedded/view": "std::string_view",
                          CAPTION + "/embedded/end": ".",
                          PROSE + "/fallback": "Text-only metadata",
                          CAPTION + "/fallback/inline": "inline code",
                          PROSE + "/partial": "Long paragraph metadata",
                          CAPTION + "/partial/first": "Complete reading text. " * 20,
                          CAPTION + "/partial/second": "Complete reading text. " * 20,
                          CAPTION + "/echo1": "Echo", CAPTION + "/echo2": "Echo",
                          CAPTION + "/link_first": "Article", CAPTION + "/link_second": "link",
                          SHORT: "四字字字", CAPTION + "/ab": "ab", CAPTION + "/cd": "cd"})
    for path, name in reading_names.items():
        reading_values.setdefault(path, name)
    reading_values[PROSE + "/second"] = "Another paragraph."
    reading_values[PROSE + "/embedded"] = "An owning type like \ufffc and view \ufffc."
    reading_values[PROSE + "/fallback"] = "Only Text exposes this prefix \ufffc and its suffix."
    reading_values[PROSE + "/partial"] = "Complete reading text. " * 40
    reading_children = {
        CAPTION + "/code": [CAPTION + "/code/first", CAPTION + "/code/second"],
        CAPTION + "/code/second": [CAPTION + "/code/second/first", CAPTION + "/code/second/second"],
        CAPTION + "/punct_code": [CAPTION + "/punct_code/" + name for name in ("first", "string", "comma", "zero", "end")],
        CAPTION + "/string_spaces": [CAPTION + "/string_spaces/child"],
        PROSE + "/embedded": [CAPTION + "/embedded/" + name for name in ("before", "type", "between", "view", "end")],
        PROSE + "/fallback": [CAPTION + "/fallback/inline"],
        PROSE + "/partial": [CAPTION + "/partial/first", CAPTION + "/partial/second"],
    }
    reading_nested = {child for paths in reading_children.values() for child in paths}
    state["reading_text_reads"] = 0
    placeholders = next((int(arg.split("=", 1)[1]) for arg in sys.argv[3:] if arg.startswith("--placeholders=")), 0)
    deep = next((int(arg.split("=", 1)[1]) for arg in sys.argv[3:] if arg.startswith("--deep=")), 0)
    wide = next((int(arg.split("=", 1)[1]) for arg in sys.argv[3:] if arg.startswith("--wide=")), 0)
    long_labels = "--long-labels" in sys.argv[3:]
    element_ids = "--element-ids" in sys.argv[3:]
    element_file = Path(state_path).with_suffix(".elements.json")
    element_version = None
    element_settings = {}
    dynamic_paths = set()

    def element_config():
        nonlocal element_version, element_settings
        if not element_ids or not element_file.exists(): return {}
        version = element_file.stat().st_mtime_ns
        if version != element_version:
            element_settings = json.loads(element_file.read_text())
            element_version = version
        return element_settings

    delayed_window = "--delayed-window" in sys.argv[3:]
    title_after_attributes = "--title-after-attributes" in sys.argv[3:]
    geometry_after_attributes = "--geometry-after-attributes" in sys.argv[3:]
    window_exposed = not delayed_window
    window_pending = False
    frame_ready = False
    extra_paths = [f"{PANEL}/{i}" for i in range(deep)] + [f"{BUTTON}/{i}" for i in range(wide)]
    extra_paths += [f"{BUTTON}/empty_{i}" for i in range(placeholders)]
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

    def number_value():
        path = Path(state_path).with_name("kwin-state.json")
        if numeric_keyboard and not numeric_readonly and path.exists():
            return json.loads(path.read_text()).get("input", {}).get("numeric_value", 80.0)
        return state["value"]

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

    def expose_window():
        nonlocal window_exposed
        window_exposed = True
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
        # libatspi asks for individual children. Building the entire sibling
        # list for every index made the wide-tree tests quadratic.
        if element_ids:
            element_config()
        return cached_children(connection, path, element_version, exposed, window_exposed)

    @lru_cache(maxsize=8192)
    def cached_children(connection, path, version, app_exposed, frame_exposed):
        nonlocal window_pending
        if connection == registry:
            return [(app.get_unique_name(), ROOT)]
        if path == ROOT and not window_exposed:
            if not window_pending:
                window_pending = True
                GLib.timeout_add(200, expose_window)
            return []
        if element_ids and path in window_paths:
            settings = element_config()
            names = settings.get("one" if path == WINDOW else "two", ["button", "entry", "password"])
            paths = ["/org/a11y/atspi/accessible/" + name for name in names]
            for child in paths:
                if child.startswith(BUTTON + "/") and child not in dynamic_paths:
                    for info in node.interfaces:
                        if info.name in ("org.a11y.atspi.Accessible", "org.a11y.atspi.Component", "org.a11y.atspi.Action"):
                            app.register_object(child, info, method, property_get, property_set)
                    dynamic_paths.add(child)
            return [(app.get_unique_name(), child) for child in paths]
        if path == WINDOW and (not exposed or empty):
            # Chromium can advertise a child while it still returns null.
            return [("", "/org/a11y/atspi/null")] if not empty else []
        if decorations and path in (WINDOW, TOOLBAR, SEPARATOR):
            paths = {WINDOW: [TOOLBAR, DIVIDER], TOOLBAR: [SEPARATOR], SEPARATOR: [BUTTON, ENTRY, PASSWORD]}[path]
            return [(app.get_unique_name(), p) for p in paths]
        if reading_text and path in (WINDOW, PROSE, LINK, CAPTION, SHORT, *reading_children):
            paths = {WINDOW: [BUTTON, ENTRY, PASSWORD, CAPTION + "/truncated", PROSE, SHORT, CAPTION + "/ab", CAPTION + "/cd"],
                     PROSE: [HEADING, CAPTION, *(p for p in reading_names if
                         not p.endswith(("/link_label", "/link_first", "/link_second", "/caption_text", "/short", "/ab", "/cd", "/truncated")) and
                         p not in reading_nested)],
                     CAPTION: [CAPTION + "/caption_text"], SHORT: [LINK],
                     LINK: [CAPTION + "/link_first", CAPTION + "/link_second"], **reading_children}[path]
            return [(app.get_unique_name(), p) for p in paths]
        if browser and path in (WINDOW, PANEL, DOCUMENT):
            paths = {WINDOW: [PANEL], PANEL: [DOCUMENT], DOCUMENT: [BUTTON, ENTRY, PASSWORD]}[path]
            return [(app.get_unique_name(), p) for p in paths]
        if tooltips and path == WINDOW:
            return [(app.get_unique_name(), p) for p in (TOOLTIP, BUTTON, ENTRY, PASSWORD)]
        if placeholders and path == WINDOW:
            return [(app.get_unique_name(), p) for p in
                    [*(f"{BUTTON}/empty_{i}" for i in range(placeholders)), BUTTON, ENTRY, PASSWORD]]
        if hidden_container and path in (WINDOW, PANEL):
            paths = (PANEL, ENTRY, PASSWORD) if path == WINDOW else (BUTTON,)
            return [(app.get_unique_name(), path) for path in paths]
        if deep:
            if path == WINDOW:
                return [(app.get_unique_name(), f"{PANEL}/0")]
            if path.startswith(PANEL + "/"):
                index = int(path.rsplit("/", 1)[1])
                paths = [f"{PANEL}/{index + 1}"] if index + 1 < deep else [BUTTON, ENTRY, PASSWORD]
                return [(app.get_unique_name(), child) for child in paths]
        if wide and path == WINDOW:
            return [(app.get_unique_name(), f"{BUTTON}/{i}") for i in range(wide)]
        return {ROOT: [(app.get_unique_name(), path) for path in window_paths],
                WINDOW: [(app.get_unique_name(), p) for p in
                         ((BUTTON, ENTRY, PASSWORD, SLIDER) if numeric else (BUTTON, ENTRY, PASSWORD))]}.get(path, [])

    def interfaces(path, connection):
        values = ["org.a11y.atspi.Accessible"]
        if path == ROOT and connection == app:
            values += ["org.a11y.atspi.Application"]
        if path != ROOT:
            values += ["org.a11y.atspi.Component"]
        if (path == BUTTON or path.startswith(BUTTON + "/") or (element_ids and path == ENTRY)
                or (reading_text and path in (*reading_values, LINK))):
            values += ["org.a11y.atspi.Action"]
        if path in (ENTRY, PASSWORD):
            values += ["org.a11y.atspi.Text"]
            if not keyboard or path == PASSWORD:
                values += ["org.a11y.atspi.EditableText"]
        if reading_text and path in reading_values:
            values += ["org.a11y.atspi.Text"]
        if path == SLIDER:
            values += ["org.a11y.atspi.Value"]
        return values

    def method(connection, sender, path, interface, name, parameters, invocation):
        nonlocal selection_queries, activation_pending, focused, frame_ready
        args = parameters.unpack()
        original_path = path
        if path.startswith(PANEL + "/"): path = PANEL
        if path.startswith(BUTTON + "/"): path = BUTTON
        if path.startswith(CAPTION + "/"): path = CAPTION
        if path.startswith(PROSE + "/"): path = PROSE
        if name == "GetExtents":
            # Deliberately wrong SCREEN values expose any accidental raw-coordinate leak.
            rectangle = ((9000,9000,40,30) if args[0] == 0 else
                         {WINDOW:(0,0,40,30),WINDOW_TWO:(0,0,40,30),
                          BUTTON:(5,5,10,5),ENTRY:(5,12,20,5),PASSWORD:(5,20,20,5),SLIDER:(5,25,20,4)}.get(path,(0,0,40,30)))
            if path == WINDOW and geometry_after_attributes and not frame_ready:
                rectangle = (0, 0, 23, 17)
            if args[0] != 0:
                if path == BUTTON and offscreen: rectangle = (5, 300, 10, 5)
                if browser and path in (DOCUMENT, BUTTON, ENTRY, PASSWORD):
                    rectangle = tuple(v * 2 for v in rectangle)
            reply = GLib.Variant("((iiii))", (rectangle,))
        elif name == "GrabFocus":
            focused = not refuse_focus
            state["focus_requests"] += 1
            save()
            reply = GLib.Variant("(b)", (True,))
        elif name == "GetChildren":
            reply = GLib.Variant("(a(so))", (children(connection, original_path),))
        elif name == "GetChildAtIndex":
            entries = children(connection, original_path)
            reply = GLib.Variant("((so))", (entries[args[0]] if 0 <= args[0] < len(entries) else ("", "/org/a11y/atspi/null"),))
        elif name == "GetInterfaces":
            reply = GLib.Variant("(as)", (interfaces(original_path, connection),))
        elif name == "GetRole":
            role = {ROOT: Atspi.Role.APPLICATION, WINDOW: Atspi.Role.FRAME, WINDOW_TWO: Atspi.Role.FRAME,
                    BUTTON: Atspi.Role.PUSH_BUTTON, ENTRY: Atspi.Role.ENTRY,
                    PASSWORD: Atspi.Role.PASSWORD_TEXT, PANEL: Atspi.Role.PANEL, SLIDER: Atspi.Role.SLIDER,
                    DOCUMENT: Atspi.Role.DOCUMENT_WEB, TOOLTIP: Atspi.Role.TOOL_TIP,
                    PROSE: Atspi.Role.PARAGRAPH, CAPTION: Atspi.Role.STATIC, HEADING: Atspi.Role.HEADING, LINK: Atspi.Role.LINK,
                    TOOLBAR: Atspi.Role.TOOL_BAR, SEPARATOR: Atspi.Role.SEPARATOR, DIVIDER: Atspi.Role.SEPARATOR}[path]
            reply = GLib.Variant("(u)", (int(role),))
        elif name in ("GetRoleName", "GetLocalizedRoleName"):
            reply = GLib.Variant("(s)", ({ROOT: "application", WINDOW: "frame", WINDOW_TWO: "frame",
                    BUTTON: "push button", ENTRY: "entry", PASSWORD: "password text", PANEL: "panel", SLIDER: "slider",
                    DOCUMENT: "document web", TOOLTIP: "tool tip", PROSE: "paragraph", CAPTION: "static", HEADING: "heading", LINK: "link",
                    TOOLBAR: "tool bar", SEPARATOR: "separator", DIVIDER: "separator"}[path],))
        elif name == "GetState":
            bits = sum(1 << int(s) for s in (Atspi.StateType.ENABLED, Atspi.StateType.SENSITIVE,
                                             Atspi.StateType.VISIBLE, Atspi.StateType.SHOWING))
            if (path == BUTTON and hidden) or original_path == PANEL:
                bits &= ~((1 << Atspi.StateType.VISIBLE) | (1 << Atspi.StateType.SHOWING))
            if reading_text and path == PROSE:
                bits &= ~((1 << Atspi.StateType.VISIBLE) | (1 << Atspi.StateType.SHOWING))
            if (path == BUTTON and offscreen) or path == TOOLTIP:
                bits &= ~(1 << Atspi.StateType.SHOWING)
            if path == ENTRY:
                bits |= 1 << Atspi.StateType.EDITABLE
            if path == DIVIDER:
                bits |= 1 << Atspi.StateType.FOCUSABLE
            if element_ids and original_path.rsplit("/accessible/", 1)[-1] in element_config().get("defunct", []):
                bits |= 1 << Atspi.StateType.DEFUNCT
            still_focused = focused
            if lose_focus:
                oracle = Path(state_path).with_name("kwin-state.json")
                if oracle.exists() and json.loads(oracle.read_text()).get("input", {}).get("keys", 0):
                    still_focused = False
            if path in (ENTRY, SLIDER) and (selected_fixture or still_focused):
                bits |= 1 << Atspi.StateType.FOCUSED
            reply = GLib.Variant("(au)", ([bits & 0xffffffff, bits >> 32],))
        elif name == "GetApplication":
            reply = GLib.Variant("((so))", ((app.get_unique_name(), ROOT),))
        elif name == "GetAttributes":
            if unsupported:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", name)
                return
            state["attribute_queries"] += 1
            if path == WINDOW: frame_ready = True
            save()
            if path == WINDOW and activation is not None and not activation_pending:
                activation_pending = True
                GLib.timeout_add(activation, activate)
            hold = Path(state_path).with_suffix(".hold-attributes")
            if hold.exists():
                def finish():
                    if hold.exists(): return GLib.SOURCE_CONTINUE
                    invocation.return_value(GLib.Variant("(a{ss})", ({},)))
                    return GLib.SOURCE_REMOVE
                GLib.timeout_add(10, finish)
                return
            reply = GLib.Variant("(a{ss})", ({},))
        elif name == "GetIndexInParent": reply = GLib.Variant("(i)", (0,))
        elif name == "GetLocale": reply = GLib.Variant("(s)", ("en_US.UTF-8",))
        elif name == "GetApplicationBusAddress": reply = GLib.Variant("(s)", ("",))
        elif name == "GetName": reply = GLib.Variant("(s)", (actions()[args[0]],))
        elif name == "GetActions": reply = GLib.Variant("(a(sss))", ([(action, "", "") for action in actions()],))
        elif name == "DoAction":
            state["clicks"] += 1
            state["last_action"] = actions()[args[0]]
            state["last_action_path"] = original_path
            state["action_times"].append(GLib.get_monotonic_time() / 1_000_000)
            if settle_after_action:
                state["text"] = ""
                def settle():
                    state["text"] = "Settled after action"
                    save()
                    return GLib.SOURCE_REMOVE
                GLib.timeout_add(150, settle)
            save()
            reply = GLib.Variant("(b)", (True,))
        elif name == "SetTextContents":
            if not readonly: state["text"] = args[0]
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
            if original_path in (PROSE, CAPTION, HEADING):
                state["reading_text_reads"] += 1
                save()
            content = reading_values[original_path] if original_path in reading_values else text_value()
            reply = GLib.Variant("(s)", (content[start:None if end < 0 else end],))
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
            if path in reading_names: return GLib.Variant("s", reading_names[path])
            if element_ids:
                label = element_config().get("labels", {}).get(path.rsplit("/accessible/", 1)[-1])
                if label is not None: return GLib.Variant("s", label)
            if path == WINDOW or (path == WINDOW_TWO and duplicate_windows):
                if title_after_attributes and not frame_ready:
                    return GLib.Variant("s", "Loading")
                title_file = Path(state_path).with_suffix(".title")
                if title_file.exists(): return GLib.Variant("s", title_file.read_text())
            if path.startswith(PANEL + "/"): return GLib.Variant("s", "")
            if path.startswith(BUTTON + "/empty_"): return GLib.Variant("s", "")
            if path.startswith(BUTTON + "/"):
                # Exercise escaped response bytes before the independent walk
                # deadline, even on slower hosts. Stay below the name limit.
                return GLib.Variant("s", "Repeated button " + path.rsplit("/", 1)[1] + (' "\\' * 70 if long_labels else ""))
            return GLib.Variant("s", {ROOT: "scu fixture", WINDOW: "scu test window", WINDOW_TWO: "scu test window" if duplicate_windows else "scu second window",
                BUTTON: "Fixture button", ENTRY: "Fixture entry", PASSWORD: "Fixture password", PANEL: "Hidden container", SLIDER: "Fixture volume",
                DOCUMENT: "Fixture document", TOOLTIP: "tooltip", PROSE: "Article paragraph", CAPTION: "Caption", HEADING: "Article heading", LINK: "Article link",
                TOOLBAR: "Bookmarks", SEPARATOR: "Separator", DIVIDER: "Focusable divider"}[path])
        if name == "ToolkitName" and browser: return GLib.Variant("s", "Chromium")
        if name in ("Description", "ToolkitName", "Version"): return GLib.Variant("s", "fixture")
        if name == "ChildCount": return GLib.Variant("i", len(children(connection, path)))
        if name == "Parent": return GLib.Variant("(so)", (app.get_unique_name(), ROOT if path in window_paths else WINDOW))
        if name == "NActions": return GLib.Variant("i", len(actions()))
        if name == "CharacterCount": return GLib.Variant("i", len(reading_values[path] if path in reading_values else text_value()))
        if name == "CaretOffset": return GLib.Variant("i", len(text_value()))
        if name == "Id": return GLib.Variant("i", 1)
        if name in ("CurrentValue", "MinimumValue", "MaximumValue"):
            properties = Path(state_path).with_suffix(".value.json")
            overrides = json.loads(properties.read_text()) if properties.exists() else {}
            default = {"CurrentValue": number_value(), "MinimumValue": 0.0, "MaximumValue": 100.0}[name]
            return GLib.Variant("d", float(overrides.get(name, default)))
        if name == "MinimumIncrement": return GLib.Variant("d", 5.0)

    def property_set(connection, sender, path, interface, name, value):
        if path != SLIDER or name != "CurrentValue": return False
        state["value_writes"] += 1
        def apply():
            state["value"] = value.unpack()
            save()
            return GLib.SOURCE_REMOVE
        if not numeric_keyboard:
            if numeric_delay: GLib.timeout_add(150, apply)
            else: apply()
        save()
        return True

    for connection, paths in ((registry, [ROOT]), (app, [ROOT, *window_paths, BUTTON, ENTRY, PASSWORD, PANEL, SLIDER, DOCUMENT, TOOLTIP, PROSE, CAPTION, HEADING, LINK, TOOLBAR, SEPARATOR, DIVIDER, *reading_names, *extra_paths])):
        if connection == registry and "--registry-unavailable" in sys.argv[3:]:
            continue  # Own the name, but expose no working registry object.
        for path in paths:
            for info in node.interfaces:
                connection.register_object(path, info, method, property_get, property_set)
    if "--registry-unavailable" not in sys.argv[3:]:
        registry.register_object("/org/a11y/atspi/registry",
            node.lookup_interface("org.a11y.atspi.Registry"), method, None, None)
    save()
    print("ready", flush=True)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
