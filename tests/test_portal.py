from __future__ import annotations

import configparser
import hashlib
import os
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
    "Settings",
    "Wallpaper",
}
SPACE_INTERFACES = {
    "Access",
    "AppChooser",
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
        for name in SPACE_INTERFACES:
            self.assertEqual(
                preferred[f"org.freedesktop.impl.portal.{name}"],
                "kde",
            )
        self.assertEqual(
            preferred["org.freedesktop.impl.portal.Secret"], "none"
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
        self.assertEqual(advertised, HOST_INTERFACES)
        self.assertEqual(descriptor["portal"]["UseIn"], "Spaces")

    def test_proxy_policy_allows_only_selected_host_portals(self) -> None:
        arguments = session._portal_policy_arguments()
        policy = "\n".join(arguments)
        for name in HOST_INTERFACES:
            self.assertIn(f"org.freedesktop.portal.{name}.*", policy)
        for name in (*SPACE_INTERFACES, "Secret", "Lockdown"):
            self.assertNotIn(f"org.freedesktop.portal.{name}.*", policy)
        self.assertNotIn("org.freedesktop.portal.*=*", policy)
        self.assertNotIn("--talk=", policy)

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
            services = assets / "dbus-1" / "services"
            portal_data = assets / "xdg-desktop-portal"
            for path in (
                frontend.parent,
                kde.parent,
                pipewire.parent,
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
            ["make", "-C", str(ROOT / "native"), "spaces-portal"],
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
                self.assertNotIn(
                    "org.freedesktop.impl.portal.FileChooser",
                    introspection,
                )

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


if __name__ == "__main__":
    unittest.main()
