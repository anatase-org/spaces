#!/usr/bin/env python3

"""Publish guest tray/MPRIS fixtures for manual host-mirroring tests."""

from __future__ import annotations

import signal

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402


ITEM_NAME = "org.example.SpacesStatusItem"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
PLAYER_NAME = "org.mpris.MediaPlayer2.SpacesProbe"
PLAYER_PATH = "/org/mpris/MediaPlayer2"

ITEM_XML = """
<node><interface name="org.kde.StatusNotifierItem">
 <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
 <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
 <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
 <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
 <property name="Category" type="s" access="read"/><property name="Id" type="s" access="read"/>
 <property name="Title" type="s" access="read"/><property name="Status" type="s" access="read"/>
 <property name="WindowId" type="u" access="read"/><property name="IconName" type="s" access="read"/>
 <property name="IconPixmap" type="a(iiay)" access="read"/><property name="OverlayIconName" type="s" access="read"/>
 <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
 <property name="AttentionIconName" type="s" access="read"/>
 <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
 <property name="AttentionMovieName" type="s" access="read"/>
 <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
 <property name="ItemIsMenu" type="b" access="read"/><property name="Menu" type="o" access="read"/>
 <property name="IconThemePath" type="s" access="read"/>
 <signal name="NewTitle"/><signal name="NewIcon"/><signal name="NewAttentionIcon"/>
 <signal name="NewOverlayIcon"/><signal name="NewToolTip"/><signal name="NewStatus"><arg type="s"/></signal>
</interface></node>
"""

MENU_XML = """
<node><interface name="com.canonical.dbusmenu">
 <method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/>
  <arg type="as" direction="in"/><arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/></method>
 <method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/>
  <arg type="a(ia{sv})" direction="out"/></method>
 <method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
 <method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/><arg type="u" direction="in"/></method>
 <method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
 <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
 <method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/></method>
 <property name="Version" type="u" access="read"/><property name="Status" type="s" access="read"/>
 <property name="TextDirection" type="s" access="read"/><property name="IconThemePath" type="as" access="read"/>
 <signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ias)"/></signal>
 <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
</interface></node>
"""

MPRIS_XML = """
<node>
 <interface name="org.mpris.MediaPlayer2">
  <method name="Raise"/><method name="Quit"/>
  <property name="CanQuit" type="b" access="read"/><property name="CanRaise" type="b" access="read"/>
  <property name="HasTrackList" type="b" access="read"/><property name="Identity" type="s" access="read"/>
  <property name="DesktopEntry" type="s" access="read"/><property name="SupportedUriSchemes" type="as" access="read"/>
  <property name="SupportedMimeTypes" type="as" access="read"/>
 </interface>
 <interface name="org.mpris.MediaPlayer2.Player">
  <method name="Next"/><method name="Previous"/><method name="Pause"/><method name="PlayPause"/>
  <method name="Stop"/><method name="Play"/><method name="Seek"><arg type="x" direction="in"/></method>
  <method name="SetPosition"><arg type="o" direction="in"/><arg type="x" direction="in"/></method>
  <method name="OpenUri"><arg type="s" direction="in"/></method>
  <property name="PlaybackStatus" type="s" access="read"/><property name="LoopStatus" type="s" access="readwrite"/>
  <property name="Rate" type="d" access="readwrite"/><property name="Shuffle" type="b" access="readwrite"/>
  <property name="Metadata" type="a{sv}" access="read"/><property name="Volume" type="d" access="readwrite"/>
  <property name="Position" type="x" access="read"/><property name="MinimumRate" type="d" access="read"/>
  <property name="MaximumRate" type="d" access="read"/><property name="CanGoNext" type="b" access="read"/>
  <property name="CanGoPrevious" type="b" access="read"/><property name="CanPlay" type="b" access="read"/>
  <property name="CanPause" type="b" access="read"/><property name="CanSeek" type="b" access="read"/>
  <property name="CanControl" type="b" access="read"/>
  <signal name="Seeked"><arg type="x"/></signal>
 </interface>
 <interface name="org.mpris.MediaPlayer2.TrackList">
  <method name="GetTracksMetadata"><arg type="ao" direction="in"/><arg type="aa{sv}" direction="out"/></method>
  <method name="AddTrack"><arg type="s" direction="in"/><arg type="o" direction="in"/><arg type="b" direction="in"/></method>
  <method name="RemoveTrack"><arg type="o" direction="in"/></method><method name="GoTo"><arg type="o" direction="in"/></method>
  <property name="Tracks" type="ao" access="read"/><property name="CanEditTracks" type="b" access="read"/>
 </interface>
 <interface name="org.mpris.MediaPlayer2.Playlists">
  <method name="ActivatePlaylist"><arg type="o" direction="in"/></method>
  <method name="GetPlaylists"><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="s" direction="in"/><arg type="b" direction="in"/><arg type="a(oss)" direction="out"/></method>
  <property name="PlaylistCount" type="u" access="read"/><property name="Orderings" type="as" access="read"/>
  <property name="ActivePlaylist" type="(b(oss))" access="read"/>
 </interface>
</node>
"""


def own(bus: Gio.DBusConnection, name: str) -> None:
    reply = bus.call_sync(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "RequestName",
        GLib.Variant("(su)", (name, 0)),
        GLib.VariantType.new("(u)"),
        Gio.DBusCallFlags.NONE,
        5_000,
        None,
    )
    if reply.unpack() != (1,):
        raise RuntimeError(f"could not own {name}: {reply.unpack()}")


def main() -> int:
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    events: list[str] = []
    writable = {"LoopStatus": "None", "Rate": 1.0, "Shuffle": False, "Volume": 0.5}

    def method_call(_bus, _sender, path, interface, method, parameters, invocation):
        event = f"{interface}.{method}{parameters.unpack()}"
        events.append(event)
        print(f"EVENT {event}", flush=True)
        if interface == "com.canonical.dbusmenu" and method == "GetLayout":
            layout = (0, {"label": GLib.Variant("s", "Spaces probe")}, [])
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (1, layout)))
        elif interface == "com.canonical.dbusmenu" and method == "GetGroupProperties":
            invocation.return_value(GLib.Variant("(a(ia{sv}))", ([],)))
        elif interface == "com.canonical.dbusmenu" and method == "GetProperty":
            invocation.return_value(GLib.Variant("(v)", (GLib.Variant("s", "Spaces probe"),)))
        elif interface == "com.canonical.dbusmenu" and method == "EventGroup":
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif interface == "com.canonical.dbusmenu" and method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif interface == "com.canonical.dbusmenu" and method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        elif interface == "org.mpris.MediaPlayer2.TrackList" and method == "GetTracksMetadata":
            invocation.return_value(GLib.Variant("(aa{sv})", ([],)))
        elif interface == "org.mpris.MediaPlayer2.Playlists" and method == "GetPlaylists":
            invocation.return_value(GLib.Variant("(a(oss))", ([],)))
        else:
            invocation.return_value(None)

    def get_property(_bus, _sender, path, interface, name):
        if interface == "org.kde.StatusNotifierItem":
            values = {
                "Category": GLib.Variant("s", "ApplicationStatus"),
                "Id": GLib.Variant("s", "spaces-probe"),
                "Title": GLib.Variant("s", "Spaces tray probe"),
                "Status": GLib.Variant("s", "Active"),
                "WindowId": GLib.Variant("u", 0),
                "IconName": GLib.Variant("s", "applications-games"),
                "IconPixmap": GLib.Variant("a(iiay)", []),
                "OverlayIconName": GLib.Variant("s", ""),
                "OverlayIconPixmap": GLib.Variant("a(iiay)", []),
                "AttentionIconName": GLib.Variant("s", ""),
                "AttentionIconPixmap": GLib.Variant("a(iiay)", []),
                "AttentionMovieName": GLib.Variant("s", ""),
                "ToolTip": GLib.Variant("(sa(iiay)ss)", ("", [], "Spaces probe", "Mirrored from Fedora")),
                "ItemIsMenu": GLib.Variant("b", False),
                "Menu": GLib.Variant("o", MENU_PATH),
                "IconThemePath": GLib.Variant("s", ""),
            }
            return values[name]
        if interface == "com.canonical.dbusmenu":
            return {
                "Version": GLib.Variant("u", 3),
                "Status": GLib.Variant("s", "normal"),
                "TextDirection": GLib.Variant("s", "ltr"),
                "IconThemePath": GLib.Variant("as", []),
            }[name]
        if interface == "org.mpris.MediaPlayer2":
            return {
                "CanQuit": GLib.Variant("b", True), "CanRaise": GLib.Variant("b", True),
                "HasTrackList": GLib.Variant("b", True), "Identity": GLib.Variant("s", "Spaces MPRIS probe"),
                "DesktopEntry": GLib.Variant("s", "spaces-probe"),
                "SupportedUriSchemes": GLib.Variant("as", ["file"]),
                "SupportedMimeTypes": GLib.Variant("as", ["audio/ogg"]),
            }[name]
        if interface == "org.mpris.MediaPlayer2.Player":
            if name in writable:
                signature = {"LoopStatus": "s", "Rate": "d", "Shuffle": "b", "Volume": "d"}[name]
                return GLib.Variant(signature, writable[name])
            values = {
                "PlaybackStatus": GLib.Variant("s", "Playing"),
                "Metadata": GLib.Variant("a{sv}", {
                    "mpris:trackid": GLib.Variant("o", "/org/mpris/MediaPlayer2/track/1"),
                    "xesam:title": GLib.Variant("s", "Spaces probe track"),
                    "mpris:artUrl": GLib.Variant("s", "file:///usr/share/pixmaps/fedora-logo.png"),
                }),
                "Position": GLib.Variant("x", 123456), "MinimumRate": GLib.Variant("d", 1.0),
                "MaximumRate": GLib.Variant("d", 1.0), "CanGoNext": GLib.Variant("b", True),
                "CanGoPrevious": GLib.Variant("b", True), "CanPlay": GLib.Variant("b", True),
                "CanPause": GLib.Variant("b", True), "CanSeek": GLib.Variant("b", True),
                "CanControl": GLib.Variant("b", True),
            }
            return values[name]
        if interface == "org.mpris.MediaPlayer2.TrackList":
            return {"Tracks": GLib.Variant("ao", ["/org/mpris/MediaPlayer2/track/1"]),
                    "CanEditTracks": GLib.Variant("b", False)}[name]
        if interface == "org.mpris.MediaPlayer2.Playlists":
            return {"PlaylistCount": GLib.Variant("u", 0), "Orderings": GLib.Variant("as", ["Alphabetical"]),
                    "ActivePlaylist": GLib.Variant("(b(oss))", (False, ("/", "", "")))}[name]
        raise KeyError((path, interface, name))

    def set_property(_bus, _sender, _path, _interface, name, value):
        writable[name] = value.unpack()
        print(f"SET {name}={writable[name]!r}", flush=True)
        return True

    own(bus, ITEM_NAME)
    own(bus, PLAYER_NAME)
    for xml, path in ((ITEM_XML, ITEM_PATH), (MENU_XML, MENU_PATH)):
        node = Gio.DBusNodeInfo.new_for_xml(xml)
        bus.register_object(path, node.interfaces[0], method_call, get_property, None)
    player_node = Gio.DBusNodeInfo.new_for_xml(MPRIS_XML)
    for interface in player_node.interfaces:
        bus.register_object(PLAYER_PATH, interface, method_call, get_property, set_property)

    def registered(connection, result, _data):
        try:
            connection.call_finish(result)
            print("STATUS-REGISTERED", flush=True)
        except GLib.Error as error:
            print(f"STATUS-REGISTER-ERROR {error.message}", flush=True)

    bus.call(
        "org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
        "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
        GLib.Variant("(s)", (ITEM_NAME,)), None,
        Gio.DBusCallFlags.NONE, 5_000, None, registered, None,
    )
    loop = GLib.MainLoop()
    def emit_probe_signals() -> bool:
        bus.emit_signal(
            None, ITEM_PATH, "org.kde.StatusNotifierItem", "NewTitle", None
        )
        bus.emit_signal(
            None,
            PLAYER_PATH,
            "org.freedesktop.DBus.Properties",
            "PropertiesChanged",
            GLib.Variant(
                "(sa{sv}as)",
                (
                    "org.mpris.MediaPlayer2.Player",
                    {"PlaybackStatus": GLib.Variant("s", "Paused")},
                    [],
                ),
            ),
        )
        print("SIGNALS-EMITTED", flush=True)
        return GLib.SOURCE_CONTINUE

    GLib.unix_signal_add(
        GLib.PRIORITY_DEFAULT, signal.SIGUSR1, emit_probe_signals
    )
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, loop.quit)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, loop.quit)
    print("READY", flush=True)
    loop.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
