#!/usr/bin/env python3

"""Capture the KWin workspace without going through the portal under test."""

import os
import sys

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402


output = os.open(sys.argv[1], os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
try:
    descriptors = Gio.UnixFDList.new()
    descriptors.append(output)
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    bus.call_with_unix_fd_list_sync(
        "org.kde.KWin",
        "/org/kde/KWin/ScreenShot2",
        "org.kde.KWin.ScreenShot2",
        "CaptureWorkspace",
        GLib.Variant("(a{sv}h)", ({}, 0)),
        GLib.VariantType.new("(a{sv})"),
        Gio.DBusCallFlags.NONE,
        10_000,
        descriptors,
        None,
    )
finally:
    os.close(output)
