#!/usr/bin/env python3

"""Manual integration probe for a deployed Spaces public-portal router."""

from __future__ import annotations

import os
import hashlib
import sys
import tempfile
import time

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402


DEST = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
REQUEST = "org.freedesktop.portal.Request"
SESSION = "org.freedesktop.portal.Session"
EMPTY = GLib.Variant("a{sv}", {})


class Probe:
    def __init__(self) -> None:
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.counter = 0

    def token(self, prefix: str) -> str:
        self.counter += 1
        return f"spaces_{prefix}_{os.getpid()}_{self.counter}"

    def request_path(self, token: str) -> str:
        sender = self.bus.get_unique_name()[1:].replace(".", "_")
        return f"{PATH}/request/{sender}/{token}"

    def call(
        self,
        interface: str,
        method: str,
        signature: str,
        values: tuple[object, ...],
        *,
        fds: list[int] | None = None,
        timeout_ms: int = 30_000,
    ) -> tuple[GLib.Variant, Gio.UnixFDList | None]:
        parameters = GLib.Variant(signature, values)
        if fds is None:
            reply = self.bus.call_sync(
                DEST,
                PATH,
                interface,
                method,
                parameters,
                None,
                Gio.DBusCallFlags.NONE,
                timeout_ms,
                None,
            )
            return reply, None
        fd_list = Gio.UnixFDList.new()
        for descriptor in fds:
            fd_list.append(descriptor)
        reply, returned_fds = self.bus.call_with_unix_fd_list_sync(
            DEST,
            PATH,
            interface,
            method,
            parameters,
            None,
            Gio.DBusCallFlags.NONE,
            timeout_ms,
            fd_list,
            None,
        )
        return reply, returned_fds

    def close(self, path: str, interface: str = REQUEST) -> str:
        try:
            self.bus.call_sync(
                DEST,
                path,
                interface,
                "Close",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                5_000,
                None,
            )
            return "closed"
        except GLib.Error as error:
            return f"close-error={error.message}"

    def request(
        self,
        interface: str,
        method: str,
        signature: str,
        values: tuple[object, ...],
        token: str,
        *,
        fds: list[int] | None = None,
        wait_seconds: float = 8.0,
    ) -> tuple[str, tuple[object, ...] | None]:
        expected = self.request_path(token)
        response: list[tuple[object, ...]] = []

        def received(
            _connection: Gio.DBusConnection,
            _sender: str,
            _path: str,
            _interface: str,
            _signal: str,
            parameters: GLib.Variant,
            _data: object,
        ) -> None:
            response.append(parameters.unpack())

        subscription = self.bus.signal_subscribe(
            DEST,
            REQUEST,
            "Response",
            expected,
            None,
            Gio.DBusSignalFlags.NONE,
            received,
            None,
        )
        try:
            reply, _ = self.call(
                interface, method, signature, values, fds=fds
            )
            returned = reply.unpack()[0]
            deadline = time.monotonic() + wait_seconds
            context = GLib.MainContext.default()
            while not response and time.monotonic() < deadline:
                context.iteration(False)
                time.sleep(0.01)
            if response:
                return f"handle={returned} expected={expected}", response[0]
            return (
                f"handle={returned} expected={expected} {self.close(expected)}",
                None,
            )
        finally:
            self.bus.signal_unsubscribe(subscription)

    def simple(self, label: str, function: object) -> None:
        try:
            result = function()
            print(f"PASS {label}: {result}", flush=True)
        except GLib.Error as error:
            print(f"FAIL {label}: {error.message}", flush=True)
        except Exception as error:  # manual probe: keep testing after a failure
            print(f"FAIL {label}: {type(error).__name__}: {error}", flush=True)


def options(**values: object) -> dict[str, GLib.Variant]:
    result: dict[str, GLib.Variant] = {}
    for key, value in values.items():
        if isinstance(value, bool):
            result[key] = GLib.Variant("b", value)
        elif isinstance(value, int):
            result[key] = GLib.Variant("u", value)
        elif isinstance(value, str):
            result[key] = GLib.Variant("s", value)
        elif isinstance(value, list) and all(
            isinstance(item, str) for item in value
        ):
            result[key] = GLib.Variant("as", value)
        else:
            raise TypeError(f"unsupported option {key}={value!r}")
    return result


def main() -> int:
    probe = Probe()
    selected = set(sys.argv[1:]) or {"core"}

    if "core" in selected or "gamemode" in selected:
        pid = os.getpid()
        probe.simple(
            "GameMode same-caller QueryStatus",
            lambda: probe.call(
                "org.freedesktop.portal.GameMode", "QueryStatus", "(i)", (pid,)
            )[0].unpack(),
        )
        probe.simple(
            "GameMode same-caller Register/Unregister",
            lambda: (
                probe.call(
                    "org.freedesktop.portal.GameMode", "RegisterGame", "(i)", (pid,)
                )[0].unpack(),
                probe.call(
                    "org.freedesktop.portal.GameMode", "UnregisterGame", "(i)", (pid,)
                )[0].unpack(),
            ),
        )

    if "core" in selected or "realtime" in selected:
        pid = os.getpid()
        probe.simple(
            "Realtime same process/thread pidfds",
            lambda: probe.call(
                "org.freedesktop.portal.Realtime",
                "MakeThreadHighPriorityWithPID",
                "(tti)",
                (pid, pid, -5),
            )[0].unpack(),
        )

    if "core" in selected or "secret" in selected:
        def secret_once() -> tuple[object, ...]:
            descriptor = os.memfd_create("spaces-secret-probe")
            try:
                token = probe.token("secret")
                info, response = probe.request(
                    "org.freedesktop.portal.Secret",
                    "RetrieveSecret",
                    "(ha{sv})",
                    (0, options(handle_token=token)),
                    token,
                    fds=[descriptor],
                    wait_seconds=15,
                )
                os.lseek(descriptor, 0, os.SEEK_SET)
                secret_value = os.read(descriptor, 4096)
                return (
                    info,
                    response,
                    f"secret-bytes={len(secret_value)}",
                    hashlib.sha256(secret_value).hexdigest(),
                )
            finally:
                os.close(descriptor)

        def stable_secret() -> object:
            first = secret_once()
            second = secret_once()
            return first, second, f"stable={first[3] == second[3]}"

        probe.simple("Secret derived FD result", stable_secret)

    if "core" in selected or "openfile" in selected:
        def open_path(method: str, directory: bool = False) -> object:
            path = tempfile.gettempdir() if directory else "/tmp/spaces-open-probe.txt"
            if not directory:
                with open(path, "w", encoding="utf-8") as stream:
                    stream.write("Spaces portal path mapping probe\n")
            flags = os.O_RDONLY | (os.O_DIRECTORY if directory else 0)
            descriptor = os.open(path, flags)
            try:
                token = probe.token(method.lower())
                return probe.request(
                    "org.freedesktop.portal.OpenURI",
                    method,
                    "(sha{sv})",
                    ("", 0, options(handle_token=token)),
                    token,
                    fds=[descriptor],
                    wait_seconds=15,
                )
            finally:
                os.close(descriptor)

        probe.simple("OpenURI OpenFile proof FD", lambda: open_path("OpenFile"))
        probe.simple(
            "OpenURI OpenDirectory proof FD",
            lambda: open_path("OpenDirectory", directory=True),
        )

    if "dialogs" in selected or "filechooser" in selected:
        token = probe.token("chooser")
        probe.simple(
            "FileChooser local backend and cancellation",
            lambda: probe.request(
                "org.freedesktop.portal.FileChooser",
                "OpenFile",
                "(ssa{sv})",
                ("", "Spaces FileChooser probe", options(handle_token=token)),
                token,
                wait_seconds=3,
            ),
        )

    if "dialogs" in selected or "screenshot" in selected:
        token = probe.token("screenshot")
        probe.simple(
            "Screenshot broker staging/cancellation",
            lambda: probe.request(
                "org.freedesktop.portal.Screenshot",
                "Screenshot",
                "(sa{sv})",
                ("", options(handle_token=token, interactive=False)),
                token,
                wait_seconds=15,
            ),
        )

    if "dialogs" in selected or "wallpaper" in selected:
        descriptor = os.open("/usr/share/pixmaps/fedora-logo.png", os.O_RDONLY)
        try:
            token = probe.token("wallpaper")
            wallpaper_options = options(
                handle_token=token, show_preview=True
            )
            wallpaper_options["set-on"] = GLib.Variant("s", "background")
            probe.simple(
                "Wallpaper FD staging/cancellation",
                lambda: probe.request(
                    "org.freedesktop.portal.Wallpaper",
                    "SetWallpaperFile",
                    "(sha{sv})",
                    ("", 0, wallpaper_options),
                    token,
                    fds=[descriptor],
                    wait_seconds=3,
                ),
            )
        finally:
            os.close(descriptor)

    if "dialogs" in selected or "background" in selected:
        token = probe.token("background")
        probe.simple(
            "Background broker request/cancellation",
            lambda: probe.request(
                "org.freedesktop.portal.Background",
                "RequestBackground",
                "(sa{sv})",
                (
                    "",
                    options(
                        handle_token=token,
                        reason="Spaces background integration probe",
                        autostart=True,
                        commandline=["/usr/bin/true"],
                    ),
                ),
                token,
                wait_seconds=8,
            ),
        )

    if "dialogs" in selected or "dynamiclauncher" in selected:
        with open("/usr/share/pixmaps/fedora-logo.png", "rb") as stream:
            icon = Gio.BytesIcon.new(GLib.Bytes.new(stream.read())).serialize()
        token = probe.token("launcher")
        probe.simple(
            "DynamicLauncher PrepareInstall response/cancellation",
            lambda: probe.request(
                "org.freedesktop.portal.DynamicLauncher",
                "PrepareInstall",
                "(ssva{sv})",
                (
                    "",
                    "Spaces portal probe",
                    icon,
                    options(handle_token=token, launcher_type=2),
                ),
                token,
                wait_seconds=3,
            ),
        )

    if "sessions" in selected:
        def request_session(interface: str, method: str = "CreateSession") -> object:
            token = probe.token(interface.lower())
            session_token = probe.token("session")
            info, response = probe.request(
                f"org.freedesktop.portal.{interface}",
                method,
                "(a{sv})",
                (
                    options(
                        handle_token=token,
                        session_handle_token=session_token,
                    ),
                ),
                token,
                wait_seconds=12,
            )
            close_result = None
            followup = None
            if response is not None and response[0] == 0:
                session = response[1].get("session_handle")
                if session:
                    follow_token = probe.token("followup")
                    followups = {
                        "GlobalShortcuts": (
                            "ListShortcuts",
                            "(oa{sv})",
                            (session, options(handle_token=follow_token)),
                        ),
                        "Location": (
                            "Start",
                            "(osa{sv})",
                            (session, "", options(handle_token=follow_token)),
                        ),
                        "RemoteDesktop": (
                            "SelectDevices",
                            "(oa{sv})",
                            (
                                session,
                                options(handle_token=follow_token, types=1),
                            ),
                        ),
                        "ScreenCast": (
                            "SelectSources",
                            "(oa{sv})",
                            (
                                session,
                                options(handle_token=follow_token, types=1),
                            ),
                        ),
                    }
                    follow_method, follow_signature, follow_values = followups[interface]
                    followup = probe.request(
                        f"org.freedesktop.portal.{interface}",
                        follow_method,
                        follow_signature,
                        follow_values,
                        follow_token,
                        wait_seconds=3,
                    )
                    if interface == "RemoteDesktop":
                        clipboard = []
                        for method_name in ("RequestClipboard", "SetSelection"):
                            try:
                                clipboard.append(
                                    probe.call(
                                        "org.freedesktop.portal.Clipboard",
                                        method_name,
                                        "(oa{sv})",
                                        (session, {}),
                                    )[0].unpack()
                                )
                            except GLib.Error as error:
                                clipboard.append(error.message)
                        followup = followup, clipboard
                    close_result = probe.close(session, SESSION)
            return info, response, followup, close_result

        for interface in (
            "GlobalShortcuts",
            "Location",
            "RemoteDesktop",
            "ScreenCast",
        ):
            probe.simple(
                f"{interface} CreateSession translation/cleanup",
                lambda interface=interface: request_session(interface),
            )

        def direct_session(interface: str, signature: str, values: tuple[object, ...]) -> object:
            reply, _ = probe.call(
                f"org.freedesktop.portal.{interface}",
                "CreateSession",
                signature,
                values,
            )
            session = reply.unpack()[0]
            return session, probe.close(session, SESSION)

        usb_token = probe.token("usb_session")
        probe.simple(
            "USB direct session translation/cleanup",
            lambda: direct_session(
                "Usb",
                "(a{sv})",
                (options(session_handle_token=usb_token),),
            ),
        )

        def input_capture() -> object:
            session_token = probe.token("input_session")
            reply, _ = probe.call(
                "org.freedesktop.portal.InputCapture",
                "CreateSession2",
                "(a{sv})",
                (options(session_handle_token=session_token, capabilities=1),),
            )
            results = reply.unpack()[0]
            session = results.get("session_handle")
            zones = None
            if session:
                token = probe.token("zones")
                zones = probe.request(
                    "org.freedesktop.portal.InputCapture",
                    "GetZones",
                    "(oa{sv})",
                    (session, options(handle_token=token)),
                    token,
                    wait_seconds=3,
                )
            return (
                results,
                zones,
                probe.close(session, SESSION) if session else None,
            )

        probe.simple("InputCapture CreateSession2/cleanup", input_capture)

        def inhibit() -> object:
            token = probe.token("inhibit")
            reply, _ = probe.call(
                "org.freedesktop.portal.Inhibit",
                "Inhibit",
                "(sua{sv})",
                ("", 8, options(handle_token=token, reason="Spaces probe")),
            )
            handle = reply.unpack()[0]
            return handle, probe.close(handle, REQUEST)

        probe.simple("Inhibit handle translation/cleanup", inhibit)

    if "requests" in selected:
        request_cases = (
            (
                "Account",
                "GetUserInformation",
                "(sa{sv})",
                lambda token: ("", options(handle_token=token, reason="Spaces probe")),
                3,
            ),
            (
                "Camera",
                "AccessCamera",
                "(a{sv})",
                lambda token: (options(handle_token=token),),
                10,
            ),
            (
                "Email",
                "ComposeEmail",
                "(sa{sv})",
                lambda token: (
                    "",
                    options(handle_token=token, subject="Spaces portal probe"),
                ),
                8,
            ),
            (
                "OpenURI",
                "OpenURI",
                "(ssa{sv})",
                lambda token: (
                    "",
                    "https://example.com/",
                    options(handle_token=token, ask=True),
                ),
                3,
            ),
            (
                "Print",
                "PreparePrint",
                "(ssa{sv}a{sv}a{sv})",
                lambda token: (
                    "",
                    "Spaces portal probe",
                    {},
                    {},
                    options(handle_token=token),
                ),
                3,
            ),
        )
        for interface, method, signature, make_values, wait in request_cases:
            token = probe.token(interface.lower())
            probe.simple(
                f"{interface} {method} response/cancellation",
                lambda interface=interface, method=method, signature=signature,
                make_values=make_values, token=token, wait=wait: probe.request(
                    f"org.freedesktop.portal.{interface}",
                    method,
                    signature,
                    make_values(token),
                    token,
                    wait_seconds=wait,
                ),
            )

        def dynamic_launcher() -> object:
            icon = Gio.ThemedIcon.new("applications-games").serialize()
            try:
                reply, _ = probe.call(
                    "org.freedesktop.portal.DynamicLauncher",
                    "RequestInstallToken",
                    "(sva{sv})",
                    ("Spaces portal probe", icon, {}),
                )
                return reply.unpack()
            except GLib.Error as error:
                if "NotAllowed" not in error.message:
                    raise
                return "expected host app-store allowlist denial"

        probe.simple("DynamicLauncher token through broker", dynamic_launcher)

    if "usb-acquire" in selected:
        def acquire_usb() -> object:
            enumerated, _ = probe.call(
                "org.freedesktop.portal.Usb",
                "EnumerateDevices",
                "(a{sv})",
                ({},),
            )
            devices = enumerated.unpack()[0]
            if not devices:
                return "no host USB devices"
            device_id = devices[0][0]
            token = probe.token("usb_acquire")
            info, response = probe.request(
                "org.freedesktop.portal.Usb",
                "AcquireDevices",
                "(sa(sa{sv})a{sv})",
                ("", [(device_id, {})], options(handle_token=token)),
                token,
                wait_seconds=15,
            )
            if response is None or response[0] != 0:
                return info, response
            handle = probe.request_path(token)
            finished, returned_fds = probe.call(
                "org.freedesktop.portal.Usb",
                "FinishAcquireDevices",
                "(oa{sv})",
                (handle, {}),
                fds=[],
            )
            results, is_finished = finished.unpack()
            fd_count = 0 if returned_fds is None else returned_fds.get_length()
            if returned_fds is not None:
                for index in range(fd_count):
                    os.close(returned_fds.get(index))
            return info, response, results, is_finished, f"fds={fd_count}"

        probe.simple("USB Acquire/Finish and returned FDs", acquire_usb)

    if "camera-fd" in selected:
        def camera_fd() -> object:
            reply, returned_fds = probe.call(
                "org.freedesktop.portal.Camera",
                "OpenPipeWireRemote",
                "(a{sv})",
                ({},),
                fds=[],
            )
            handle = reply.unpack()[0]
            count = 0 if returned_fds is None else returned_fds.get_length()
            metadata = None
            if returned_fds is not None and count:
                descriptor = returned_fds.get(handle)
                metadata = os.fstat(descriptor)
                os.close(descriptor)
            return reply.unpack(), f"fds={count}", metadata

        probe.simple("Camera PipeWire remote FD", camera_fd)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
