from __future__ import annotations

import configparser
import ctypes
import hashlib
import hmac
import os
import signal
import shlex
import socket
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from spaces import core, session


ROOT = Path(__file__).resolve().parents[1]
HOST_INTERFACES = {
    "Account",
    "Clipboard",
    "Email",
    "GlobalShortcuts",
    "Inhibit",
    "InputCapture",
    "Notification",
    "Print",
    "RemoteDesktop",
    "ScreenCast",
    "Screenshot",
    "Secret",
    "Settings",
    "Wallpaper",
}
SPACE_INTERFACES = {
    "Access",
    "Background",
    "DynamicLauncher",
    "FileChooser",
    "Usb",
}


class PortalConfigurationTests(unittest.TestCase):
    def test_portal_configuration_has_explicit_host_and_space_split(self) -> None:
        path = (
            ROOT
            / "data"
            / "portal"
            / "xdg-desktop-portal"
            / "spaces-portals.conf"
        )
        config = configparser.ConfigParser()
        config.optionxform = str
        config.read(path, encoding="utf-8")
        preferred = config["preferred"]

        for name in HOST_INTERFACES:
            self.assertEqual(
                preferred[f"org.freedesktop.impl.portal.{name}"],
                "spaces",
            )
        self.assertEqual(
            preferred["org.freedesktop.impl.portal.AppChooser"],
            "spaces",
        )
        for name in SPACE_INTERFACES:
            self.assertEqual(
                preferred[f"org.freedesktop.impl.portal.{name}"],
                "kde",
            )
        self.assertEqual(
            preferred["org.freedesktop.impl.portal.Lockdown"], "none"
        )

        descriptor = configparser.ConfigParser()
        descriptor.optionxform = str
        descriptor.read(
            path.parent / "portals" / "spaces.portal",
            encoding="utf-8",
        )
        advertised = {
            value.removeprefix("org.freedesktop.impl.portal.")
            for value in descriptor["portal"]["Interfaces"].split(";")
            if value
        }
        self.assertEqual(advertised, HOST_INTERFACES | {"AppChooser"})
        self.assertEqual(descriptor["portal"]["UseIn"], "Spaces")

    def test_proxy_policy_allows_only_selected_host_portals(self) -> None:
        arguments = session._portal_policy_arguments()
        policy = "\n".join(arguments)
        for name in HOST_INTERFACES:
            self.assertIn(f"org.freedesktop.portal.{name}.*", policy)
        for name in (*SPACE_INTERFACES, "Lockdown"):
            self.assertNotIn(f"org.freedesktop.portal.{name}.*", policy)
        self.assertNotIn("org.freedesktop.portal.*=*", policy)
        self.assertNotIn("--talk=", policy)
        self.assertIn(
            "org.freedesktop.portal.OpenURI.OpenURI", policy
        )
        self.assertIn(
            "org.freedesktop.portal.OpenURI.SchemeSupported", policy
        )
        self.assertNotIn(
            "org.freedesktop.portal.OpenURI.OpenFile", policy
        )
        self.assertNotIn(
            "org.freedesktop.portal.OpenURI.OpenDirectory", policy
        )

        broker_policy = "\n".join(
            session._portal_policy_arguments(
                "org.anatase.Spaces.Open.stest"
            )
        )
        self.assertIn("org.anatase.Spaces.Open1.OpenFile", broker_policy)
        self.assertIn(
            "org.anatase.Spaces.Open1.OpenDirectory", broker_policy
        )

    def test_backend_activation_restarts_after_transient_host_failure(
        self,
    ) -> None:
        activation = configparser.ConfigParser()
        activation.optionxform = str
        activation.read(
            ROOT
            / "data"
            / "portal"
            / "dbus-1"
            / "services"
            / "org.freedesktop.impl.portal.desktop.spaces.service",
            encoding="utf-8",
        )
        self.assertEqual(
            activation["D-BUS Service"]["SystemdService"],
            "spaces-portal.service",
        )

        unit = configparser.ConfigParser()
        unit.optionxform = str
        unit.read(
            ROOT
            / "data"
            / "portal"
            / "systemd"
            / "user"
            / "spaces-portal.service",
            encoding="utf-8",
        )
        self.assertEqual(unit["Service"]["Restart"], "always")
        self.assertEqual(unit["Unit"]["StartLimitIntervalSec"], "0")

    def test_preflight_is_read_only_and_transient(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rootfs = Path(temporary) / "rootfs"
            assets = Path(temporary) / "assets"
            frontend = rootfs / session.GUEST_PORTAL_FRONTENDS[0]
            kde = rootfs / session.GUEST_KDE_PORTAL
            pipewire = rootfs / session.GUEST_PIPEWIRE_CONFIGS[0]
            kwallet = rootfs / session.GUEST_KWALLET_PROVIDERS[0]
            services = assets / "dbus-1" / "services"
            portal_data = assets / "xdg-desktop-portal"
            for path in (
                frontend.parent,
                kde.parent,
                pipewire.parent,
                kwallet.parent,
                services,
                portal_data,
            ):
                path.mkdir(parents=True, exist_ok=True)
            services.joinpath("service").write_text("", encoding="utf-8")
            portal_data.joinpath("config").write_text("", encoding="utf-8")

            binds = (
                (services, "/usr/local/share/dbus-1/services"),
                (
                    portal_data,
                    "/usr/local/share/xdg-desktop-portal",
                ),
            )
            with (
                mock.patch.object(session, "PORTAL_DATA_BINDS", binds),
                self.assertLogs(session.logger, "WARNING"),
            ):
                self.assertEqual(session.portal_bind_arguments(rootfs), ())

            frontend.write_text("", encoding="utf-8")
            kde.write_text("", encoding="utf-8")
            pipewire.write_text("", encoding="utf-8")
            kwallet.write_text("", encoding="utf-8")
            with mock.patch.object(session, "PORTAL_DATA_BINDS", binds):
                self.assertEqual(
                    session.portal_bind_arguments(rootfs),
                    (
                        f"--bind-ro={services}:"
                        "/usr/local/share/dbus-1/services",
                        f"--bind-ro={portal_data}:"
                        "/usr/local/share/xdg-desktop-portal",
                    ),
                )

    def test_secret_activation_and_wallet_configuration_are_managed(self) -> None:
        services = ROOT / "data" / "portal" / "dbus-1" / "services"
        for name in (
            "org.freedesktop.secrets",
            "org.kde.secretservicecompat",
            "org.kde.kwalletd5",
        ):
            service = configparser.ConfigParser()
            service.optionxform = str
            service.read(services / f"{name}.service", encoding="utf-8")
            self.assertEqual(service["D-BUS Service"]["Name"], name)
            self.assertEqual(
                service["D-BUS Service"]["Exec"],
                "/run/spaces-host/bin/spaces-secret-helper "
                f"--activation-name {name}",
            )

        wallet = (
            ROOT / "data" / "portal" / "config" / "kwalletrc"
        ).read_text(encoding="utf-8")
        for entry in (
            "Default Wallet[$i]=spaces-managed-v1",
            "Local Wallet[$i]=spaces-managed-v1",
            "Use One Wallet[$i]=true",
            "Close When Idle[$i]=false",
            "Close on Screensaver[$i]=false",
            "apiEnabled[$i]=true",
        ):
            self.assertIn(entry, wallet)


class PortalProxyTests(unittest.TestCase):
    def test_proxy_uses_app_scope_waits_for_socket_and_restricts_policy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            desktop_user = SimpleNamespace(
                uid=os.getuid(),
                gid=os.getgid(),
                name="desktop",
            )
            listeners: list[socket.socket] = []

            class Process:
                returncode = None

                def poll(self) -> None:
                    return None

                def wait(self, timeout: float | None = None) -> int:
                    del timeout
                    self.returncode = 0
                    return 0

            def popen(
                command: list[str], **arguments: object
            ) -> Process:
                proxy_index = command.index(session.XDG_DBUS_PROXY)
                listener = socket.socket(socket.AF_UNIX)
                listener.bind(command[proxy_index + 2])
                listeners.append(listener)
                readiness_fd = arguments["pass_fds"]
                assert isinstance(readiness_fd, tuple)
                os.write(readiness_fd[0], b"1")
                return Process()

            with (
                mock.patch.object(session, "RUNTIME_ROOT", root / "run"),
                mock.patch.object(
                    session.subprocess, "Popen", side_effect=popen
                ) as spawn,
            ):
                proxy, binding = session.start_portal_proxy(
                    "work", desktop_user, "wayland-session"
                )
                try:
                    command = spawn.call_args.args[0]
                    generation = hashlib.sha256(
                        b"wayland-session"
                    ).hexdigest()[:16]
                    self.assertIn(
                        "--unit=app-spaces-org.anatase.spaces."
                        f"work-{generation}.scope",
                        command,
                    )
                    self.assertEqual(
                        command[command.index(session.XDG_DBUS_PROXY) + 1],
                        f"unix:path=/run/user/{os.getuid()}/bus",
                    )
                    self.assertEqual(
                        binding.destination,
                        f"/run/spaces/desktop/{os.getuid()}/portal/bus",
                    )
                    self.assertTrue(stat_is_socket(binding.source))
                    policy = "\n".join(command)
                    self.assertIn(
                        "org.freedesktop.portal.ScreenCast.*", policy
                    )
                    self.assertNotIn(
                        "org.freedesktop.portal.FileChooser.*", policy
                    )
                finally:
                    proxy.close()
                    for listener in listeners:
                        listener.close()
                    proxy.socket_path.unlink(missing_ok=True)

    def test_proxy_rejects_non_socket_readiness_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            desktop_user = SimpleNamespace(
                uid=os.getuid(),
                gid=os.getgid(),
                name="desktop",
            )

            class Process:
                returncode = None

                def poll(self) -> None:
                    return None

                def wait(self, timeout: float | None = None) -> int:
                    del timeout
                    self.returncode = 0
                    return 0

            def popen(
                command: list[str], **arguments: object
            ) -> Process:
                proxy_index = command.index(session.XDG_DBUS_PROXY)
                Path(command[proxy_index + 2]).write_text(
                    "not a socket", encoding="utf-8"
                )
                readiness_fd = arguments["pass_fds"]
                assert isinstance(readiness_fd, tuple)
                os.write(readiness_fd[0], b"1")
                return Process()

            with (
                mock.patch.object(session, "RUNTIME_ROOT", root / "run"),
                mock.patch.object(
                    session.subprocess, "Popen", side_effect=popen
                ),
                self.assertRaises(core.SpacesError),
            ):
                session.start_portal_proxy(
                    "work", desktop_user, "wayland-session"
                )


def stat_is_socket(path: Path) -> bool:
    return stat.S_ISSOCK(path.lstat().st_mode)


class PortalNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        subprocess.run(
            [
                "make",
                "-C",
                str(ROOT / "native"),
                "spaces-portal",
                "spaces-open-broker",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )

    @staticmethod
    def _bus(path: Path) -> tuple[str, int]:
        address = f"unix:path={path}"
        completed = subprocess.run(
            [
                "dbus-daemon",
                "--session",
                f"--address={address}",
                "--fork",
                "--print-pid",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return address, int(completed.stdout.strip())

    def test_backend_registers_only_host_interfaces_and_rejects_direct_callers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host_address, host_pid = self._bus(root / "host-bus")
            guest_address, guest_pid = self._bus(root / "guest-bus")
            host_portal = subprocess.Popen(
                [
                    "dbus-test-tool",
                    "echo",
                    "--name=org.freedesktop.portal.Desktop",
                ],
                env={
                    **os.environ,
                    "DBUS_SESSION_BUS_ADDRESS": host_address,
                },
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for _attempt in range(100):
                owner = subprocess.run(
                    [
                        "gdbus",
                        "call",
                        "--address",
                        host_address,
                        "--dest",
                        "org.freedesktop.DBus",
                        "--object-path",
                        "/org/freedesktop/DBus",
                        "--method",
                        "org.freedesktop.DBus.NameHasOwner",
                        "org.freedesktop.portal.Desktop",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if owner.returncode == 0 and "true" in owner.stdout:
                    break
                time.sleep(0.01)
            else:
                self.fail("fake host portal did not acquire its bus name")
            process = subprocess.Popen(
                [ROOT / "native" / "spaces-portal"],
                env={
                    **os.environ,
                    "DBUS_SESSION_BUS_ADDRESS": guest_address,
                    "SPACES_NAME": "work",
                    "SPACES_PORTAL_TEST_ADDRESS": host_address,
                },
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                introspection = None
                for _attempt in range(100):
                    completed = subprocess.run(
                        [
                            "gdbus",
                            "introspect",
                            "--address",
                            guest_address,
                            "--dest",
                            "org.freedesktop.impl.portal.desktop.spaces",
                            "--object-path",
                            "/org/freedesktop/portal/desktop",
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    if completed.returncode == 0:
                        introspection = completed.stdout
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(introspection)
                assert introspection is not None
                for name in HOST_INTERFACES:
                    self.assertIn(
                        f"org.freedesktop.impl.portal.{name}",
                        introspection,
                    )
                self.assertIn(
                    "org.freedesktop.impl.portal.AppChooser",
                    introspection,
                )
                self.assertNotIn(
                    "org.freedesktop.impl.portal.FileChooser",
                    introspection,
                )
                file_manager = subprocess.run(
                    [
                        "gdbus",
                        "introspect",
                        "--address",
                        guest_address,
                        "--dest",
                        "org.freedesktop.impl.portal.desktop.spaces",
                        "--object-path",
                        "/org/freedesktop/FileManager1",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
                self.assertIn("ShowItems", file_manager)
                self.assertIn("ShowFolders", file_manager)
                self.assertIn("ShowItemProperties", file_manager)

                denied = subprocess.run(
                    [
                        "gdbus",
                        "call",
                        "--address",
                        guest_address,
                        "--dest",
                        "org.freedesktop.impl.portal.desktop.spaces",
                        "--object-path",
                        "/org/freedesktop/portal/desktop",
                        "--method",
                        "org.freedesktop.impl.portal.ScreenCast.CreateSession",
                        "/org/freedesktop/portal/desktop/request/x/y",
                        "/org/freedesktop/portal/desktop/session/x/y",
                        "spoofed.app",
                        "{}",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(denied.returncode, 0)
                self.assertIn("AccessDenied", denied.stderr)

                host_portal.terminate()
                host_portal.wait(timeout=2)
                self.assertEqual(process.wait(timeout=2), 1)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                if process.stderr is not None:
                    process.stderr.close()
                if host_portal.poll() is None:
                    host_portal.terminate()
                    host_portal.wait(timeout=2)
                for pid in (host_pid, guest_pid):
                    try:
                        os.kill(pid, 15)
                    except ProcessLookupError:
                        pass

    def test_broker_rejects_traversal_and_identity_mismatch(self) -> None:
        try:
            import gi

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib
        except ImportError:
            self.skipTest("PyGObject is unavailable")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            address, bus_pid = self._bus(root / "bus")
            base = root / "base"
            nested = root / "nested"
            (base / "nested").mkdir(parents=True)
            nested.mkdir()
            (base / "a").write_text("base", encoding="utf-8")
            (base / "nested" / "a").write_text(
                "shadowed", encoding="utf-8"
            )
            (nested / "a").write_text("nested", encoding="utf-8")
            base_fd = os.open(base, os.O_PATH)
            nested_fd = os.open(nested, os.O_PATH)
            ready_read, ready_write = os.pipe()
            broker = subprocess.Popen(
                [
                    ROOT / "native" / "spaces-open-broker",
                    "--name",
                    "org.anatase.Spaces.Open.stest",
                    "--ready-fd",
                    str(ready_write),
                    "--map",
                    "/guest",
                    str(base_fd),
                    "--map",
                    "/guest/nested",
                    str(nested_fd),
                ],
                env={
                    **os.environ,
                    "DBUS_SESSION_BUS_ADDRESS": address,
                },
                pass_fds=(base_fd, nested_fd, ready_write),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            os.close(base_fd)
            os.close(nested_fd)
            os.close(ready_write)
            try:
                self.assertEqual(os.read(ready_read, 1), b"1")
                connection = Gio.DBusConnection.new_for_address_sync(
                    address,
                    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                    | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                    None,
                    None,
                )

                def call(path: str, proof: Path) -> str:
                    proof_fd = os.open(proof, os.O_RDONLY)
                    descriptors = Gio.UnixFDList.new()
                    handle = descriptors.append(proof_fd)
                    os.close(proof_fd)
                    try:
                        connection.call_with_unix_fd_list_sync(
                            "org.anatase.Spaces.Open.stest",
                            "/org/anatase/Spaces/Open",
                            "org.anatase.Spaces.Open1",
                            "OpenFile",
                            GLib.Variant(
                                "(shbs)", (path, handle, False, "")
                            ),
                            GLib.VariantType.new("(u)"),
                            Gio.DBusCallFlags.NONE,
                            2000,
                            descriptors,
                            None,
                        )
                    except GLib.Error as error:
                        return str(error)
                    return "success"

                mismatch = call("/guest/a", nested / "a")
                self.assertIn(
                    "proof does not identify the mapped object", mismatch
                )
                traversal = call("/guest/../a", base / "a")
                self.assertIn("no active host mapping", traversal)
                longest = call("/guest/nested/a", nested / "a")
                self.assertNotIn("proof does not identify", longest)
                self.assertNotIn("no active host mapping", longest)
                connection.close_sync(None)
            finally:
                os.close(ready_read)
                if broker.poll() is None:
                    broker.terminate()
                broker.wait(timeout=2)
                try:
                    os.kill(bus_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def test_host_portal_restore_data_round_trip_is_stateless(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            library_path = Path(temporary) / "spaces-portal-test.so"
            pkg_config = subprocess.run(
                ["pkg-config", "--cflags", "--libs", "gio-unix-2.0"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    os.environ.get("CC", "cc"),
                    "-shared",
                    "-fPIC",
                    "-std=gnu11",
                    "-Wno-unused-function",
                    "-o",
                    str(library_path),
                    str(ROOT / "native" / "spaces_portal.c"),
                    *shlex.split(pkg_config.stdout),
                ],
                check=True,
            )
            library = ctypes.CDLL(str(library_path))
            library.g_variant_parse.argtypes = [
                ctypes.c_void_p,
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            library.g_variant_parse.restype = ctypes.c_void_p
            library.g_variant_ref_sink.argtypes = [ctypes.c_void_p]
            library.g_variant_ref_sink.restype = ctypes.c_void_p
            library.g_variant_print.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            library.g_variant_print.restype = ctypes.c_void_p
            library.g_variant_unref.argtypes = [ctypes.c_void_p]
            library.g_free.argtypes = [ctypes.c_void_p]
            library.portal_restore_options_to_host.argtypes = [
                ctypes.c_void_p
            ]
            library.portal_restore_options_to_host.restype = ctypes.c_void_p
            library.portal_restore_results_to_backend.argtypes = [
                ctypes.c_void_p
            ]
            library.portal_restore_results_to_backend.restype = ctypes.c_void_p

            def parse(text: str) -> int:
                error = ctypes.c_void_p()
                variant = library.g_variant_parse(
                    None,
                    text.encode(),
                    None,
                    None,
                    ctypes.byref(error),
                )
                self.assertFalse(error.value)
                self.assertTrue(variant)
                return variant

            def render(variant: int) -> str:
                printed = library.g_variant_print(variant, True)
                self.assertTrue(printed)
                try:
                    return ctypes.string_at(printed).decode()
                finally:
                    library.g_free(printed)

            host_results = parse(
                "{'restore_token': <'host-token'>, "
                "'streams': <@a(ua{sv}) []>}"
            )
            backend_results = library.g_variant_ref_sink(
                library.portal_restore_results_to_backend(host_results)
            )
            backend_text = render(backend_results)
            self.assertNotIn("restore_token", backend_text)
            self.assertIn("restore_data", backend_text)
            self.assertIn(
                "('Spaces', uint32 1, <'host-token'>)",
                backend_text,
            )

            host_options = library.g_variant_ref_sink(
                library.portal_restore_options_to_host(backend_results)
            )
            host_text = render(host_options)
            self.assertIn("'restore_token': <'host-token'>", host_text)
            self.assertNotIn("restore_data", host_text)

            foreign_options = parse(
                "{'restore_data': "
                "<('KDE', uint32 1, <'foreign-token'>)>, "
                "'restore_token': <'injected-token'>}"
            )
            filtered_options = library.g_variant_ref_sink(
                library.portal_restore_options_to_host(foreign_options)
            )
            filtered_text = render(filtered_options)
            self.assertNotIn("restore_data", filtered_text)
            self.assertNotIn("restore_token", filtered_text)

            for variant in (
                filtered_options,
                foreign_options,
                host_options,
                backend_results,
                host_results,
            ):
                library.g_variant_unref(variant)

    def test_secret_derivations_are_domain_separated_and_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            flags = subprocess.run(
                ["pkg-config", "--cflags", "--libs", "gio-unix-2.0"],
                check=True,
                capture_output=True,
                text=True,
            )
            portal_library = root / "portal.so"
            helper_library = root / "helper.so"
            for source, output in (
                ("spaces_portal.c", portal_library),
                ("spaces_secret_helper.c", helper_library),
            ):
                subprocess.run(
                    [
                        os.environ.get("CC", "cc"),
                        "-shared",
                        "-fPIC",
                        "-std=gnu11",
                        "-Wno-unused-function",
                        "-o",
                        str(output),
                        str(ROOT / "native" / source),
                        *shlex.split(flags.stdout),
                    ],
                    check=True,
                )

            portal = ctypes.CDLL(str(portal_library))
            helper = ctypes.CDLL(str(helper_library))
            byte_pointer = ctypes.POINTER(ctypes.c_ubyte)
            portal.portal_derive_secret.argtypes = [
                byte_pointer,
                ctypes.c_size_t,
                ctypes.c_char_p,
                byte_pointer,
            ]
            portal.portal_derive_secret.restype = ctypes.c_int
            helper.spaces_derive_kwallet_key.argtypes = [
                byte_pointer,
                byte_pointer,
            ]
            helper.spaces_derive_kwallet_key.restype = ctypes.c_int
            helper.spaces_prepare_headless_qt.argtypes = []
            helper.spaces_prepare_headless_qt.restype = None

            host = bytes(range(32))
            host_buffer = (ctypes.c_ubyte * len(host)).from_buffer_copy(host)

            def derive(app_id: str) -> bytes:
                output = (ctypes.c_ubyte * 64)()
                self.assertTrue(
                    portal.portal_derive_secret(
                        host_buffer,
                        len(host),
                        app_id.encode(),
                        output,
                    )
                )
                return bytes(output)

            app_id = "org.example.ü"
            expected = hmac.digest(
                host,
                b"org.anatase.spaces.portal-secret.v1\0"
                + len(app_id.encode()).to_bytes(4, "big")
                + app_id.encode(),
                "sha512",
            )
            self.assertEqual(derive(app_id), expected)
            self.assertNotEqual(derive(app_id), derive(""))
            self.assertEqual(derive(""), derive(""))

            portal_buffer = (ctypes.c_ubyte * 64).from_buffer_copy(expected)
            wallet = (ctypes.c_ubyte * 56)()
            self.assertTrue(
                helper.spaces_derive_kwallet_key(portal_buffer, wallet)
            )
            self.assertEqual(
                bytes(wallet),
                hmac.digest(
                    expected,
                    b"org.anatase.spaces.kwallet-unlock.v1\0",
                    "sha512",
                )[:56],
            )

            libc = ctypes.CDLL(None)
            libc.getenv.argtypes = [ctypes.c_char_p]
            libc.getenv.restype = ctypes.c_char_p
            previous_platform = os.environ.get("QT_QPA_PLATFORM")
            try:
                helper.spaces_prepare_headless_qt()
                self.assertEqual(
                    libc.getenv(b"QT_QPA_PLATFORM"), b"offscreen"
                )
            finally:
                if previous_platform is None:
                    os.environ.pop("QT_QPA_PLATFORM", None)
                    libc.unsetenv(b"QT_QPA_PLATFORM")
                else:
                    os.environ["QT_QPA_PLATFORM"] = previous_platform


if __name__ == "__main__":
    unittest.main()
