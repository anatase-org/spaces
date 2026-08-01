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
import threading
import time
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from spaces import core, session


ROOT = Path(__file__).resolve().parents[1]
HOST_INTERFACES = {
    "Account",
    "Access",
    "Background",
    "Camera",
    "Clipboard",
    "DynamicLauncher",
    "Email",
    "GameMode",
    "GlobalShortcuts",
    "Inhibit",
    "InputCapture",
    "Location",
    "NetworkMonitor",
    "Notification",
    "OpenURI",
    "PowerProfileMonitor",
    "Print",
    "ProxyResolver",
    "Realtime",
    "RemoteDesktop",
    "ScreenCast",
    "Screenshot",
    "Secret",
    "Settings",
    "Usb",
    "Wallpaper",
}
TRANSFORMED_INTERFACES = {
    "Background",
    "DynamicLauncher",
    "GameMode",
    "OpenURI",
    "Realtime",
    "Screenshot",
    "Secret",
    "Wallpaper",
}


class PortalConfigurationTests(unittest.TestCase):
    def test_legacy_backend_configuration_is_removed(self) -> None:
        portal_data = ROOT / "data" / "portal" / "xdg-desktop-portal"
        self.assertFalse(portal_data.joinpath("spaces-portals.conf").exists())
        self.assertFalse(portal_data.joinpath("portals", "spaces.portal").exists())
        self.assertFalse(
            ROOT.joinpath(
                "data", "portal", "dbus-1", "services",
                "org.freedesktop.impl.portal.desktop.spaces.service",
            ).exists()
        )

    def test_proxy_policy_allows_only_selected_host_portals(self) -> None:
        arguments = session._portal_policy_arguments()
        policy = "\n".join(arguments)
        for name in HOST_INTERFACES - TRANSFORMED_INTERFACES:
            self.assertIn(f"org.freedesktop.portal.{name}.*", policy)
        for name in TRANSFORMED_INTERFACES:
            self.assertNotIn(f"org.freedesktop.portal.{name}.*", policy)
        self.assertNotIn("org.freedesktop.portal.FileChooser.*", policy)
        self.assertNotIn("org.freedesktop.portal.Lockdown.*", policy)
        self.assertNotIn("org.freedesktop.portal.*=*", policy)
        self.assertNotIn("--talk=", policy)
        for method in (
            "GetCapabilities",
            "Notify",
            "CloseNotification",
            "GetServerInformation",
        ):
            self.assertIn(
                "--call=org.freedesktop.Notifications="
                f"org.freedesktop.Notifications.{method}"
                "@/org/freedesktop/Notifications",
                arguments,
            )
        for signal_name in (
            "NotificationClosed",
            "ActionInvoked",
            "ActivationToken",
        ):
            self.assertIn(
                "--broadcast=org.freedesktop.Notifications="
                f"org.freedesktop.Notifications.{signal_name}"
                "@/org/freedesktop/Notifications",
                arguments,
            )
        self.assertFalse(
            any(
                "org.freedesktop.Notifications.*" in argument
                for argument in arguments
            )
        )
        for path in ("/org/freedesktop/ScreenSaver", "/ScreenSaver"):
            for method in (
                "Lock",
                "SimulateUserActivity",
                "GetActive",
                "GetActiveTime",
                "GetSessionIdleTime",
                "SetActive",
                "Inhibit",
                "UnInhibit",
                "Throttle",
                "UnThrottle",
            ):
                self.assertIn(
                    "--call=org.freedesktop.ScreenSaver="
                    f"org.freedesktop.ScreenSaver.{method}@{path}",
                    arguments,
                )
            self.assertIn(
                "--broadcast=org.freedesktop.ScreenSaver="
                f"org.freedesktop.ScreenSaver.ActiveChanged@{path}",
                arguments,
            )
        self.assertFalse(
            any(
                "org.freedesktop.ScreenSaver.*" in argument
                for argument in arguments
            )
        )
        self.assertIn("org.freedesktop.portal.OpenURI.OpenURI", policy)
        self.assertNotIn("org.freedesktop.portal.OpenURI.OpenFile", policy)
        self.assertNotIn("org.freedesktop.portal.GameMode.QueryStatusByPIDFd", policy)
        self.assertIn("org.freedesktop.portal.Usb.*", policy)
        self.assertIn("--own=org.mpris.MediaPlayer2.spaces.*", arguments)
        self.assertIn("--own=org.kde.StatusNotifierItem.spaces.*", arguments)

        broker_policy = "\n".join(
            session._portal_policy_arguments(
                "org.anatase.Spaces.Integration.stest"
            )
        )
        self.assertIn("org.anatase.Spaces.Integration1.OpenFile", broker_policy)
        self.assertIn(
            "org.anatase.Spaces.Integration1.OpenDirectory", broker_policy
        )
        self.assertIn("org.anatase.Spaces.Integration1.MakeGameMode", broker_policy)
        self.assertIn(
            "org.anatase.Spaces.Integration1.RemoveStagedFile",
            broker_policy,
        )

    def test_public_portal_activation_restarts_after_transient_host_failure(
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
            / "org.freedesktop.portal.Desktop.service",
            encoding="utf-8",
        )
        self.assertEqual(
            activation["D-BUS Service"]["SystemdService"],
            "spaces-portal.service",
        )
        for name in (
            "org.freedesktop.Notifications",
            "org.freedesktop.ScreenSaver",
        ):
            shared = configparser.ConfigParser()
            shared.optionxform = str
            shared.read(
                ROOT
                / "data"
                / "portal"
                / "dbus-1"
                / "services"
                / f"{name}.service",
                encoding="utf-8",
            )
            self.assertEqual(shared["D-BUS Service"]["Name"], name)
            self.assertEqual(
                shared["D-BUS Service"]["SystemdService"],
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
        self.assertEqual(
            unit["Unit"]["Description"],
            "Spaces desktop integration bridge",
        )
        data_files = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["tool"]["setuptools"]["data-files"]
        installed = data_files["share/spaces/portal/dbus-1/services"]
        for name in (
            "org.freedesktop.Notifications",
            "org.freedesktop.ScreenSaver",
        ):
            self.assertIn(
                f"data/portal/dbus-1/services/{name}.service",
                installed,
            )

    def test_graphical_session_target_pulls_in_systemd_session(self) -> None:
        target = configparser.ConfigParser()
        target.optionxform = str
        target.read(
            ROOT
            / "data"
            / "portal"
            / "systemd"
            / "user"
            / "spaces-graphical-session.target",
            encoding="utf-8",
        )

        unit = target["Unit"]
        self.assertEqual(unit["Requires"], "graphical-session.target")
        self.assertEqual(unit["BindsTo"], "graphical-session.target")
        self.assertEqual(unit["Before"], "graphical-session.target")
        self.assertNotIn("RefuseManualStart", unit)
        self.assertNotIn("StopWhenUnneeded", unit)

        data_files = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["tool"]["setuptools"]["data-files"]
        self.assertIn(
            "data/portal/systemd/user/spaces-graphical-session.target",
            data_files["share/spaces/portal/systemd/user"],
        )

    def test_preflight_is_read_only_and_transient(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rootfs = Path(temporary) / "rootfs"
            assets = Path(temporary) / "assets"
            services = assets / "dbus-1" / "services"
            systemd = assets / "systemd" / "user"
            services.mkdir(parents=True)
            services.joinpath("service").write_text("", encoding="utf-8")

            binds = (
                (services, "/usr/local/share/dbus-1/services"),
                (systemd, "/usr/local/share/systemd/user"),
            )
            with (
                mock.patch.object(session, "PORTAL_DATA_BINDS", binds),
                self.assertLogs(session.logger, "WARNING"),
            ):
                self.assertEqual(session.portal_bind_arguments(rootfs), ())

            systemd.mkdir(parents=True)
            with mock.patch.object(session, "PORTAL_DATA_BINDS", binds):
                self.assertEqual(
                    session.portal_bind_arguments(rootfs),
                    (
                        f"--bind-ro={services}:"
                        "/usr/local/share/dbus-1/services",
                        f"--bind-ro={systemd}:"
                        "/usr/local/share/systemd/user",
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
    def test_portal_identity_uses_the_space_name(self) -> None:
        self.assertEqual(
            session._portal_app_id("work"),
            "org.anatase.Spaces.work",
        )

    def test_proxy_uses_app_scope_waits_for_socket_and_restricts_policy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            desktop_user = SimpleNamespace(
                uid=os.getuid(),
                gid=os.getgid(),
                name="desktop",
                host_home=root / "home",
            )
            desktop_user.host_home.mkdir()
            identity = (
                desktop_user.host_home
                / ".local/share/applications"
                / f"{session._portal_app_id('work')}.desktop"
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
                    self.assertIn(
                        "--description=Spaces desktop portal proxy",
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
                    self.assertTrue(identity.is_file())
                finally:
                    proxy.close()
                    self.assertFalse(identity.exists())
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
                host_home=root / "home",
            )
            desktop_user.host_home.mkdir()

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
                "spaces-broker",
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

    @staticmethod
    def _wait_for_bus_name(address: str, name: str) -> bool:
        for _attempt in range(100):
            owner = subprocess.run(
                [
                    "gdbus",
                    "call",
                    "--address",
                    address,
                    "--dest",
                    "org.freedesktop.DBus",
                    "--object-path",
                    "/org/freedesktop/DBus",
                    "--method",
                    "org.freedesktop.DBus.NameHasOwner",
                    name,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if owner.returncode == 0 and "true" in owner.stdout:
                return True
            time.sleep(0.01)
        return False

    def test_router_owns_public_name_without_legacy_backend(
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
            if not self._wait_for_bus_name(
                host_address,
                "org.freedesktop.portal.Desktop",
            ):
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
                            "org.freedesktop.portal.Desktop",
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
                self.assertNotIn("org.freedesktop.impl.portal.", introspection)
                file_manager = subprocess.run(
                    [
                        "gdbus",
                        "introspect",
                        "--address",
                        guest_address,
                        "--dest",
                        "org.freedesktop.FileManager1",
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
                for name in (
                    "org.freedesktop.Notifications",
                    "org.freedesktop.ScreenSaver",
                ):
                    self.assertTrue(
                        self._wait_for_bus_name(guest_address, name)
                    )
                notifications = subprocess.run(
                    [
                        "gdbus",
                        "introspect",
                        "--address",
                        guest_address,
                        "--dest",
                        "org.freedesktop.Notifications",
                        "--object-path",
                        "/org/freedesktop/Notifications",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
                for member in (
                    "Notify",
                    "CloseNotification",
                    "GetCapabilities",
                    "GetServerInformation",
                    "NotificationClosed",
                    "ActionInvoked",
                    "ActivationToken",
                ):
                    self.assertIn(member, notifications)
                self.assertNotIn("NotificationReplied", notifications)
                for path in (
                    "/org/freedesktop/ScreenSaver",
                    "/ScreenSaver",
                ):
                    screen_saver = subprocess.run(
                        [
                            "gdbus",
                            "introspect",
                            "--address",
                            guest_address,
                            "--dest",
                            "org.freedesktop.ScreenSaver",
                            "--object-path",
                            path,
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout
                    for member in (
                        "Lock",
                        "SetActive",
                        "SimulateUserActivity",
                        "GetActive",
                        "GetActiveTime",
                        "GetSessionIdleTime",
                        "Inhibit",
                        "UnInhibit",
                        "Throttle",
                        "UnThrottle",
                        "ActiveChanged",
                    ):
                        self.assertIn(member, screen_saver)

                denied = subprocess.run(
                    [
                        "gdbus",
                        "call",
                        "--address",
                        guest_address,
                        "--dest",
                        "org.freedesktop.portal.Desktop",
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
                self.assertIn("UnknownMethod", denied.stderr)

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

    def test_status_notifier_accepts_a_same_process_bus_connection(
        self,
    ) -> None:
        try:
            import gi

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib
        except ImportError:
            self.skipTest("PyGObject is unavailable")

        item_xml = """
        <node><interface name='org.kde.StatusNotifierItem'>
          <method name='Activate'>
            <arg type='i' direction='in'/><arg type='i' direction='in'/>
          </method>
          <property name='Id' type='s' access='read'/>
          <property name='Menu' type='o' access='read'/>
        </interface></node>
        """
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
                env={**os.environ, "DBUS_SESSION_BUS_ADDRESS": host_address},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            foreign_item = subprocess.Popen(
                [
                    "dbus-test-tool",
                    "echo",
                    "--name=org.example.ForeignStatusItem",
                ],
                env={**os.environ, "DBUS_SESSION_BUS_ADDRESS": guest_address},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            process: subprocess.Popen[str] | None = None
            connections: list[object] = []
            registration = 0
            loop = GLib.MainLoop()
            loop_thread = threading.Thread(target=loop.run, daemon=True)
            try:
                self.assertTrue(
                    self._wait_for_bus_name(
                        host_address, "org.freedesktop.portal.Desktop"
                    )
                )
                self.assertTrue(
                    self._wait_for_bus_name(
                        guest_address, "org.example.ForeignStatusItem"
                    )
                )
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
                self.assertTrue(
                    self._wait_for_bus_name(
                        guest_address, "org.kde.StatusNotifierWatcher"
                    )
                )
                flags = (
                    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                    | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
                )
                caller = Gio.DBusConnection.new_for_address_sync(
                    guest_address, flags, None, None
                )
                item = Gio.DBusConnection.new_for_address_sync(
                    guest_address, flags, None, None
                )
                connections.extend((caller, item))

                def item_call(
                    _connection: object,
                    _sender: str,
                    _path: str,
                    _interface: str,
                    _method: str,
                    _parameters: object,
                    invocation: object,
                ) -> None:
                    invocation.return_value(None)

                def item_property(
                    _connection: object,
                    _sender: str,
                    _path: str,
                    _interface: str,
                    property_name: str,
                ) -> object:
                    if property_name == "Id":
                        return GLib.Variant("s", "Flameshot")
                    if property_name == "Menu":
                        return GLib.Variant("o", "/")
                    raise AssertionError(property_name)

                node = Gio.DBusNodeInfo.new_for_xml(item_xml)
                registration = item.register_object(
                    "/StatusNotifierItem",
                    node.interfaces[0],
                    item_call,
                    item_property,
                    None,
                )
                loop_thread.start()

                watcher_name = "org.kde.StatusNotifierWatcher"
                watcher_path = "/StatusNotifierWatcher"
                with self.assertRaises(GLib.Error) as denied:
                    caller.call_sync(
                        watcher_name,
                        watcher_path,
                        watcher_name,
                        "RegisterStatusNotifierItem",
                        GLib.Variant(
                            "(s)", ("org.example.ForeignStatusItem",)
                        ),
                        GLib.VariantType.new("()"),
                        Gio.DBusCallFlags.NONE,
                        3000,
                        None,
                    )
                self.assertIn("AccessDenied", str(denied.exception))

                reply = caller.call_sync(
                    watcher_name,
                    watcher_path,
                    watcher_name,
                    "RegisterStatusNotifierItem",
                    GLib.Variant("(s)", (item.get_unique_name(),)),
                    GLib.VariantType.new("()"),
                    Gio.DBusCallFlags.NONE,
                    3000,
                    None,
                )
                self.assertEqual(reply.unpack(), ())
                registered = caller.call_sync(
                    watcher_name,
                    watcher_path,
                    "org.freedesktop.DBus.Properties",
                    "Get",
                    GLib.Variant(
                        "(ss)",
                        (watcher_name, "RegisteredStatusNotifierItems"),
                    ),
                    GLib.VariantType.new("(v)"),
                    Gio.DBusCallFlags.NONE,
                    3000,
                    None,
                )
                items = registered.get_child_value(0).get_variant().unpack()
                self.assertIn(item.get_unique_name(), items)
                self.assertTrue(
                    self._wait_for_bus_name(
                        host_address,
                        "org.kde.StatusNotifierItem.spaces.space."
                        "work-flameshot.item.i1",
                    )
                )
            finally:
                if registration and connections:
                    connections[-1].unregister_object(registration)
                for connection in connections:
                    try:
                        connection.close_sync(None)
                    except GLib.Error:
                        pass
                if process is not None and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=2)
                if process is not None and process.stderr is not None:
                    process.stderr.close()
                for child in (foreign_item, host_portal):
                    if child.poll() is None:
                        child.terminate()
                    child.wait(timeout=2)
                loop.quit()
                if loop_thread.is_alive():
                    loop_thread.join(timeout=2)
                for pid in (host_pid, guest_pid):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass

    def test_native_desktop_services_forward_calls_signals_and_lifetimes(
        self,
    ) -> None:
        try:
            import gi

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib
        except ImportError:
            self.skipTest("PyGObject is unavailable")

        notifications_xml = """
        <node><interface name='org.freedesktop.Notifications'>
          <method name='GetCapabilities'><arg type='as' direction='out'/></method>
          <method name='Notify'>
            <arg type='s' direction='in'/><arg type='u' direction='in'/>
            <arg type='s' direction='in'/><arg type='s' direction='in'/>
            <arg type='s' direction='in'/><arg type='as' direction='in'/>
            <arg type='a{sv}' direction='in'/><arg type='i' direction='in'/>
            <arg type='u' direction='out'/>
          </method>
          <method name='CloseNotification'><arg type='u' direction='in'/></method>
          <method name='GetServerInformation'>
            <arg type='s' direction='out'/><arg type='s' direction='out'/>
            <arg type='s' direction='out'/><arg type='s' direction='out'/>
          </method>
          <signal name='NotificationClosed'><arg type='u'/><arg type='u'/></signal>
          <signal name='ActionInvoked'><arg type='u'/><arg type='s'/></signal>
          <signal name='ActivationToken'><arg type='u'/><arg type='s'/></signal>
          <signal name='NotificationReplied'><arg type='u'/><arg type='s'/></signal>
        </interface></node>
        """
        screen_saver_xml = """
        <node><interface name='org.freedesktop.ScreenSaver'>
          <method name='Lock'/><method name='SimulateUserActivity'/>
          <method name='GetActive'><arg type='b' direction='out'/></method>
          <method name='GetActiveTime'><arg type='u' direction='out'/></method>
          <method name='GetSessionIdleTime'><arg type='u' direction='out'/></method>
          <method name='SetActive'><arg type='b' direction='in'/><arg type='b' direction='out'/></method>
          <method name='Inhibit'><arg type='s' direction='in'/><arg type='s' direction='in'/><arg type='u' direction='out'/></method>
          <method name='UnInhibit'><arg type='u' direction='in'/></method>
          <method name='Throttle'><arg type='s' direction='in'/><arg type='s' direction='in'/><arg type='u' direction='out'/></method>
          <method name='UnThrottle'><arg type='u' direction='in'/></method>
          <signal name='ActiveChanged'><arg type='b'/></signal>
        </interface></node>
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host_address, host_pid = self._bus(root / "host-bus")
            guest_address, guest_pid = self._bus(root / "guest-bus")
            flags = (
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
            )
            host = Gio.DBusConnection.new_for_address_sync(
                host_address, flags, None, None
            )
            events: list[tuple[str, tuple[object, ...]]] = []
            next_cookie = {"inhibit": 40, "throttle": 80}

            def request_name(name: str) -> None:
                reply = host.call_sync(
                    "org.freedesktop.DBus",
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus",
                    "RequestName",
                    GLib.Variant("(su)", (name, 0)),
                    GLib.VariantType.new("(u)"),
                    Gio.DBusCallFlags.NONE,
                    2000,
                    None,
                )
                self.assertEqual(reply.unpack(), (1,))

            def release_name(name: str) -> None:
                reply = host.call_sync(
                    "org.freedesktop.DBus",
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus",
                    "ReleaseName",
                    GLib.Variant("(s)", (name,)),
                    GLib.VariantType.new("(u)"),
                    Gio.DBusCallFlags.NONE,
                    2000,
                    None,
                )
                self.assertEqual(reply.unpack(), (1,))

            def notification_call(
                _connection: object,
                _sender: str,
                _path: str,
                _interface: str,
                method: str,
                parameters: object,
                invocation: object,
            ) -> None:
                unpacked = parameters.unpack()
                events.append((method, unpacked))
                if method == "GetCapabilities":
                    invocation.return_value(
                        GLib.Variant("(as)", (["actions", "body"],))
                    )
                elif method == "GetServerInformation":
                    invocation.return_value(
                        GLib.Variant(
                            "(ssss)", ("Host", "Spaces test", "1", "1.3")
                        )
                    )
                elif method == "Notify":
                    notification_id = unpacked[1] or 73
                    invocation.return_value(
                        GLib.Variant("(u)", (notification_id,))
                    )
                else:
                    invocation.return_value(None)

            def screen_saver_call(
                _connection: object,
                _sender: str,
                _path: str,
                _interface: str,
                method: str,
                parameters: object,
                invocation: object,
            ) -> None:
                unpacked = parameters.unpack()
                events.append((method, unpacked))
                if method == "GetActive":
                    invocation.return_value(GLib.Variant("(b)", (False,)))
                elif method == "GetActiveTime":
                    invocation.return_value(GLib.Variant("(u)", (12,)))
                elif method == "GetSessionIdleTime":
                    invocation.return_value(GLib.Variant("(u)", (34,)))
                elif method == "SetActive":
                    invocation.return_value(
                        GLib.Variant("(b)", (unpacked[0],))
                    )
                elif method in ("Inhibit", "Throttle"):
                    key = method.casefold()
                    next_cookie[key] += 1
                    invocation.return_value(
                        GLib.Variant("(u)", (next_cookie[key],))
                    )
                else:
                    invocation.return_value(None)

            notifications_node = Gio.DBusNodeInfo.new_for_xml(
                notifications_xml
            )
            screen_saver_node = Gio.DBusNodeInfo.new_for_xml(
                screen_saver_xml
            )
            registrations = [
                host.register_object(
                    "/org/freedesktop/Notifications",
                    notifications_node.interfaces[0],
                    notification_call,
                    None,
                    None,
                ),
                host.register_object(
                    "/ScreenSaver",
                    screen_saver_node.interfaces[0],
                    screen_saver_call,
                    None,
                    None,
                ),
            ]
            for name in (
                "org.freedesktop.portal.Desktop",
                "org.freedesktop.Notifications",
                "org.freedesktop.ScreenSaver",
            ):
                request_name(name)
            loop = GLib.MainLoop()
            loop_thread = threading.Thread(target=loop.run, daemon=True)
            loop_thread.start()
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
            clients: list[object] = []
            try:
                for name in (
                    "org.freedesktop.Notifications",
                    "org.freedesktop.ScreenSaver",
                ):
                    self.assertTrue(
                        self._wait_for_bus_name(guest_address, name)
                    )

                def client() -> object:
                    connection = Gio.DBusConnection.new_for_address_sync(
                        guest_address, flags, None, None
                    )
                    clients.append(connection)
                    return connection

                def call(
                    connection: object,
                    name: str,
                    path: str,
                    method: str,
                    parameters: object | None,
                    signature: str,
                ) -> tuple[object, ...]:
                    reply = connection.call_sync(
                        name,
                        path,
                        name,
                        method,
                        parameters,
                        GLib.VariantType.new(signature),
                        Gio.DBusCallFlags.NONE,
                        3000,
                        None,
                    )
                    return reply.unpack()

                notification_client = client()
                notification_path = "/org/freedesktop/Notifications"
                self.assertEqual(
                    call(
                        notification_client,
                        "org.freedesktop.Notifications",
                        notification_path,
                        "GetCapabilities",
                        None,
                        "(as)",
                    ),
                    (["actions", "body"],),
                )
                self.assertEqual(
                    call(
                        notification_client,
                        "org.freedesktop.Notifications",
                        notification_path,
                        "GetServerInformation",
                        None,
                        "(ssss)",
                    ),
                    ("Host", "Spaces test", "1", "1.3"),
                )
                self.assertEqual(
                    call(
                        notification_client,
                        "org.freedesktop.Notifications",
                        notification_path,
                        "Notify",
                        GLib.Variant(
                            "(susssasa{sv}i)",
                            (
                                "Guest app",
                                0,
                                "guest-icon",
                                "Summary",
                                "Body",
                                ["default", "Open"],
                                {"urgency": GLib.Variant("y", 1)},
                                -1,
                            ),
                        ),
                        "(u)",
                    ),
                    (73,),
                )
                notification_client.close_sync(None)
                clients.remove(notification_client)
                time.sleep(0.05)
                self.assertFalse(
                    any(event[0] == "CloseNotification" for event in events)
                )

                signal_client = client()
                self.assertEqual(
                    call(
                        signal_client,
                        "org.freedesktop.Notifications",
                        notification_path,
                        "CloseNotification",
                        GLib.Variant("(u)", (73,)),
                        "()",
                    ),
                    (),
                )
                received: list[tuple[str, tuple[object, ...], str]] = []

                def receive(
                    _connection: object,
                    _sender: str,
                    path: str,
                    _interface: str,
                    signal_name: str,
                    parameters: object,
                    _data: object,
                ) -> None:
                    received.append(
                        (signal_name, parameters.unpack(), path)
                    )

                signal_client.signal_subscribe(
                    "org.freedesktop.Notifications",
                    "org.freedesktop.Notifications",
                    None,
                    notification_path,
                    None,
                    Gio.DBusSignalFlags.NONE,
                    receive,
                    None,
                )
                for signal_name, parameters in (
                    ("NotificationClosed", GLib.Variant("(uu)", (73, 2))),
                    ("ActionInvoked", GLib.Variant("(us)", (73, "default"))),
                    ("ActivationToken", GLib.Variant("(us)", (73, "token"))),
                ):
                    host.emit_signal(
                        None,
                        notification_path,
                        "org.freedesktop.Notifications",
                        signal_name,
                        parameters,
                    )
                deadline = time.monotonic() + 2
                context = GLib.MainContext.default()
                while len(received) < 3 and time.monotonic() < deadline:
                    context.iteration(False)
                    time.sleep(0.01)
                self.assertEqual(
                    {item[0] for item in received},
                    {"NotificationClosed", "ActionInvoked", "ActivationToken"},
                )

                screen_client = client()
                screen_name = "org.freedesktop.ScreenSaver"
                standard_path = "/org/freedesktop/ScreenSaver"
                legacy_path = "/ScreenSaver"
                self.assertEqual(
                    call(screen_client, screen_name, standard_path, "GetActive", None, "(b)"),
                    (False,),
                )
                self.assertEqual(
                    call(screen_client, screen_name, legacy_path, "GetActiveTime", None, "(u)"),
                    (12,),
                )
                self.assertEqual(
                    call(screen_client, screen_name, standard_path, "GetSessionIdleTime", None, "(u)"),
                    (34,),
                )
                self.assertEqual(
                    call(
                        screen_client,
                        screen_name,
                        legacy_path,
                        "SetActive",
                        GLib.Variant("(b)", (True,)),
                        "(b)",
                    ),
                    (True,),
                )
                for method in ("Lock", "SimulateUserActivity"):
                    self.assertEqual(
                        call(screen_client, screen_name, standard_path, method, None, "()"),
                        (),
                    )
                inhibit_cookie = call(
                    screen_client,
                    screen_name,
                    standard_path,
                    "Inhibit",
                    GLib.Variant("(ss)", ("app", "reason")),
                    "(u)",
                )[0]
                intruder = client()
                with self.assertRaises(GLib.Error) as denied:
                    call(
                        intruder,
                        screen_name,
                        standard_path,
                        "UnInhibit",
                        GLib.Variant("(u)", (inhibit_cookie,)),
                        "()",
                    )
                self.assertIn("AccessDenied", str(denied.exception))
                throttle_cookie = call(
                    screen_client,
                    screen_name,
                    legacy_path,
                    "Throttle",
                    GLib.Variant("(ss)", ("app", "reason")),
                    "(u)",
                )[0]
                self.assertEqual(
                    call(
                        screen_client,
                        screen_name,
                        standard_path,
                        "UnThrottle",
                        GLib.Variant("(u)", (throttle_cookie,)),
                        "()",
                    ),
                    (),
                )
                screen_client.close_sync(None)
                clients.remove(screen_client)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if any(
                        event == ("UnInhibit", (inhibit_cookie,))
                        for event in events
                    ):
                        break
                    time.sleep(0.01)
                self.assertIn(("UnInhibit", (inhibit_cookie,)), events)

                for path in (standard_path, legacy_path):
                    signal_client.signal_subscribe(
                        screen_name,
                        screen_name,
                        "ActiveChanged",
                        path,
                        None,
                        Gio.DBusSignalFlags.NONE,
                        receive,
                        None,
                    )
                host.emit_signal(
                    None,
                    legacy_path,
                    screen_name,
                    "ActiveChanged",
                    GLib.Variant("(b)", (True,)),
                )
                deadline = time.monotonic() + 2
                while (
                    len([item for item in received if item[0] == "ActiveChanged"]) < 2
                    and time.monotonic() < deadline
                ):
                    context.iteration(False)
                    time.sleep(0.01)
                active_paths = {
                    item[2]
                    for item in received
                    if item[0] == "ActiveChanged"
                }
                self.assertEqual(
                    active_paths, {standard_path, legacy_path}
                )

                release_name(screen_name)
                unavailable = False
                deadline = time.monotonic() + 2
                while not unavailable and time.monotonic() < deadline:
                    try:
                        call(
                            signal_client,
                            screen_name,
                            standard_path,
                            "GetActive",
                            None,
                            "(b)",
                        )
                    except GLib.Error as error:
                        unavailable = "NameHasNoOwner" in str(error)
                    if not unavailable:
                        time.sleep(0.01)
                self.assertTrue(unavailable)
                request_name(screen_name)
                recovered: tuple[object, ...] | None = None
                deadline = time.monotonic() + 2
                while recovered is None and time.monotonic() < deadline:
                    try:
                        recovered = call(
                            signal_client,
                            screen_name,
                            legacy_path,
                            "GetActive",
                            None,
                            "(b)",
                        )
                    except GLib.Error:
                        time.sleep(0.01)
                self.assertEqual(recovered, (False,))
            finally:
                for connection in clients:
                    try:
                        connection.close_sync(None)
                    except GLib.Error:
                        pass
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=2)
                if process.stderr is not None:
                    process.stderr.close()
                loop.quit()
                loop_thread.join(timeout=2)
                for registration in registrations:
                    host.unregister_object(registration)
                host.close_sync(None)
                for pid in (host_pid, guest_pid):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass

    def test_optional_native_name_conflict_does_not_stop_portal(self) -> None:
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
                env={**os.environ, "DBUS_SESSION_BUS_ADDRESS": host_address},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            competitor = subprocess.Popen(
                [
                    "dbus-test-tool",
                    "echo",
                    "--name=org.freedesktop.Notifications",
                ],
                env={**os.environ, "DBUS_SESSION_BUS_ADDRESS": guest_address},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            process: subprocess.Popen[str] | None = None
            try:
                self.assertTrue(
                    self._wait_for_bus_name(
                        host_address, "org.freedesktop.portal.Desktop"
                    )
                )
                self.assertTrue(
                    self._wait_for_bus_name(
                        guest_address, "org.freedesktop.Notifications"
                    )
                )
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
                self.assertTrue(
                    self._wait_for_bus_name(
                        guest_address,
                        "org.freedesktop.portal.Desktop",
                    )
                )
                self.assertTrue(
                    self._wait_for_bus_name(
                        guest_address, "org.freedesktop.ScreenSaver"
                    )
                )
                self.assertIsNone(process.poll())
            finally:
                if process is not None and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=2)
                if process is not None and process.stderr is not None:
                    process.stderr.close()
                for child in (competitor, host_portal):
                    if child.poll() is None:
                        child.terminate()
                    child.wait(timeout=2)
                for pid in (host_pid, guest_pid):
                    try:
                        os.kill(pid, signal.SIGTERM)
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
            # The private daemon still has the host's D-Bus activation paths.
            # Own the portal name so a valid broker call cannot auto-start the
            # real host portal.
            fake_portal = subprocess.Popen(
                [
                    "dbus-test-tool",
                    "echo",
                    "--name=org.freedesktop.portal.Desktop",
                ],
                env={
                    **os.environ,
                    "DBUS_SESSION_BUS_ADDRESS": address,
                },
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if not self._wait_for_bus_name(
                address,
                "org.freedesktop.portal.Desktop",
            ):
                fake_portal.terminate()
                fake_portal.wait(timeout=2)
                try:
                    os.kill(bus_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                self.fail("fake host portal did not acquire its bus name")
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
                    ROOT / "native" / "spaces-broker",
                    "--name",
                    "org.anatase.Spaces.Integration.stest",
                    "--space",
                    "work",
                    "--app-id",
                    session._portal_app_id("work"),
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
                            "org.anatase.Spaces.Integration.stest",
                            "/org/anatase/Spaces/Integration",
                            "org.anatase.Spaces.Integration1",
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
                if fake_portal.poll() is None:
                    fake_portal.terminate()
                fake_portal.wait(timeout=2)
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
            helper.spaces_backup_failed_wallet.argtypes = [
                ctypes.c_char_p,
                ctypes.POINTER(ctypes.c_uint),
            ]
            helper.spaces_backup_failed_wallet.restype = ctypes.c_int

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

            wallet_directory = root / "wallet-data" / "kwalletd"
            wallet_directory.mkdir(parents=True)
            files = {
                "spaces-managed-v1.kwl": b"wallet",
                "spaces-managed-v1.salt": b"salt",
                "spaces-managed-v1_attributes.json": b"attributes",
            }
            for name, contents in files.items():
                (wallet_directory / name).write_bytes(contents)
            (wallet_directory / "spaces-managed-v1.kwl.1").write_bytes(
                b"earlier backup"
            )
            backup_number = ctypes.c_uint()
            self.assertTrue(
                helper.spaces_backup_failed_wallet(
                    os.fsencode(root / "wallet-data"),
                    ctypes.byref(backup_number),
                )
            )
            self.assertEqual(backup_number.value, 2)
            for name, contents in files.items():
                self.assertFalse((wallet_directory / name).exists())
                self.assertEqual(
                    (wallet_directory / f"{name}.2").read_bytes(),
                    contents,
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
