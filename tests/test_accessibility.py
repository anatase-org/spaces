from __future__ import annotations

import json
import os
import pwd
import select
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest import mock

from spaces import core, session


def desktop_user(root, uid=None):
    return SimpleNamespace(
        uid=os.getuid() if uid is None else uid,
        gid=os.getgid(),
        name=pwd.getpwuid(os.getuid()).pw_name,
        host_home=root,
        space_home=root / "home",
        guest_home=PurePosixPath("/home/test"),
        desktop=True,
        accessibility=True,
        credential_agents=False,
    )


class AccessibilityPermissionTests(unittest.TestCase):
    def test_canonical_and_legacy_permissions(self):
        identity = core.Identity(1000, 1000, Path("/home/user"))
        for value, expected in (
            (False, "disabled"),
            (True, "basic"),
            *((level, level) for level in core.DESKTOP_LEVELS),
        ):
            with self.subTest(value=value):
                info = core.create_info(
                    "test",
                    {"id": "custom"},
                    identity,
                    "basic",
                    [],
                    desktop=value,
                )
                self.assertEqual(
                    info["permissions"]["users"]["1000"]["permissions"][
                        "desktop"
                    ],
                    expected,
                )
                self.assertEqual(
                    core.defaults_from_info(info, identity)[7], expected
                )
        for value in (0, 1, None, [], {}, "enabled"):
            with (
                self.subTest(invalid=value),
                self.assertRaises(core.SpacesError),
            ):
                core.desktop_level(value)

    def test_live_presets_and_existing_custom_grants(self):
        identity = core.Identity(1000, 1000, Path("/home/user"))
        for preset, expected in (
            ("basic", "basic"),
            ("develop", "accessibility"),
            ("custom", "basic"),
        ):
            info = core.create_info(
                "test", {"id": "custom"}, identity, "basic", [], preset=preset
            )
            info["permissions"]["users"]["1000"]["permissions"][
                "desktop"
            ] = True
            self.assertEqual(
                core.defaults_from_info(info, identity)[7], expected
            )


class AccessibilityDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.user = desktop_user(Path("/tmp"))
        self.path = Path(f"/run/user/{self.user.uid}/at-spi/bus")
        self.metadata = SimpleNamespace(st_dev=1, st_ino=2)

    def discover(self, address):
        reply = subprocess.CompletedProcess(
            [], 0, json.dumps({"type": "s", "data": [address]})
        )
        with mock.patch.object(
            session.subprocess, "run", return_value=reply
        ) as run:
            result = session.discover_accessibility_bus(self.user)
        self.assertEqual(run.call_args.kwargs["user"], self.user.uid)
        self.assertEqual(run.call_args.kwargs["timeout"], 6)
        self.assertNotIn("IsEnabled", run.call_args.args[0])
        return result

    def test_dedicated_socket_and_guid(self):
        with mock.patch.object(
            session,
            "_validated_source",
            return_value=(self.path, self.metadata),
        ):
            binding = self.discover(f"unix:path={self.path},guid={'a' * 32}")
        self.assertEqual(binding.source, self.path)

    def test_rejects_other_transports_and_session_bus(self):
        for address in (
            "tcp:host=localhost,port=1",
            "unix:abstract=a11y",
            f"unix:path=/run/user/{self.user.uid}/bus",
            f"unix:path={self.path};unix:path=/tmp/bus",
            "unix:path=/tmp/%zz",
        ):
            with (
                self.subTest(address=address),
                self.assertRaises(core.SpacesError),
            ):
                self.discover(address)

    def test_rejects_missing_socket_and_escape(self):
        with mock.patch.object(
            session, "_validated_source", return_value=None
        ):
            with self.assertRaises(core.SpacesError):
                self.discover(f"unix:path={self.path}")
        with tempfile.TemporaryDirectory() as directory:
            with socket.socket(socket.AF_UNIX) as listener:
                path = Path(directory) / "bus"
                listener.bind(str(path))
                with self.assertRaises(core.SpacesError):
                    self.discover(f"unix:path={path}")


class AccessibilityLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.user = desktop_user(self.root, uid=1000)
        self.controller = session.DesktopController(
            "test", (self.user,), portals_enabled=False
        )
        self.current = session._ActiveDesktop(
            session.DesktopPlan("2", (), {"DISPLAY": ":0"}, accessibility=True)
        )
        self.controller.active[1000] = self.current
        self.source = session.DesktopBind(
            "/host/a11y", Path("/host/a11y"), 1, 2
        )
        self.listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(self.listener.close)
        self.path = self.root / "proxy"
        self.listener.bind(str(self.path))
        metadata = self.path.stat()
        self.binding = session.DesktopBind(
            "/run/spaces/desktop/1000/a11y/bus",
            self.path,
            metadata.st_dev,
            metadata.st_ino,
        )
        self.proxy = mock.Mock(socket_path=self.path)
        self.proxy.process.poll.return_value = None
        self.patches = [
            mock.patch.object(
                session, "discover_accessibility_bus", return_value=self.source
            ),
            mock.patch.object(
                session,
                "start_accessibility_proxy",
                return_value=(self.proxy, self.binding),
            ),
            mock.patch.object(session, "_write_record"),
            mock.patch.object(self.controller, "_machine_user"),
            mock.patch.object(self.controller, "_mount"),
            mock.patch.object(self.controller, "_unmount"),
        ]
        (
            self.discover,
            self.start,
            self.write,
            self.command,
            self.mount,
            self.unmount,
        ) = [p.start() for p in self.patches]
        for patch in self.patches:
            self.addCleanup(patch.stop)

    def test_forwarding_and_retry_are_independent_of_portals(self):
        self.start.side_effect = [
            RuntimeError("not ready"),
            (self.proxy, self.binding),
        ]
        self.controller.reconcile_portals()
        self.assertIsNone(self.current.accessibility)
        self.assertNotIn("AT_SPI_BUS_ADDRESS", self.current.plan.environment)
        self.controller.reconcile_portals()
        self.assertIs(self.current.accessibility, self.proxy)
        environment = self.write.call_args.args[-1]
        self.assertEqual(environment["DISPLAY"], ":0")
        self.assertEqual(
            environment["AT_SPI_BUS_ADDRESS"],
            "unix:path=" + self.binding.destination,
        )
        self.controller.reconcile_portals()
        self.assertEqual(self.start.call_count, 2)

    def test_downgrade_closes_before_unmount_and_clears_environment(self):
        self.controller.reconcile_portals()
        order = []
        self.proxy.close.side_effect = lambda: order.append("close")
        self.unmount.side_effect = lambda *_: order.append("unmount")
        self.user.accessibility = False
        self.controller.reconcile_portals()
        self.assertEqual(order, ["close", "unmount"])
        self.assertIsNone(self.current.accessibility)
        self.assertNotIn("AT_SPI_BUS_ADDRESS", self.write.call_args.args[-1])
        self.assertIn("AT_SPI_BUS_ADDRESS=", self.command.call_args.args[1])

    def test_discovery_failure_revokes_held_bridge(self):
        self.controller.reconcile_portals()
        self.discover.side_effect = RuntimeError("bus disappeared")
        self.controller.reconcile_portals()
        self.proxy.close.assert_called_once()
        self.assertIsNone(self.current.accessibility)

    def test_failed_environment_clear_is_retried(self):
        self.controller.reconcile_portals()
        self.user.accessibility = False
        self.command.side_effect = [RuntimeError("manager unavailable"), None]
        self.controller.reconcile_portals()
        self.assertTrue(self.current.accessibility_environment_dirty)
        self.controller.reconcile_portals()
        self.assertFalse(self.current.accessibility_environment_dirty)

    def test_socket_replacement_and_proxy_crash_reconnect(self):
        for change in ("source", "process"):
            with self.subTest(change=change):
                self.controller.reconcile_portals()
                if change == "source":
                    self.discover.return_value = replace(self.source, inode=3)
                else:
                    self.proxy.process.poll.return_value = 1
                self.controller.reconcile_portals()
                self.proxy.close.assert_called()
                self.assertIs(self.current.accessibility, self.proxy)
                # Mocked process does not recreate its filesystem socket.
                if not self.path.exists():
                    self.listener.close()
                    self.listener = socket.socket(socket.AF_UNIX)
                    self.addCleanup(self.listener.close)
                    self.listener.bind(str(self.path))

    def test_basic_and_root_do_not_discover_host_bus(self):
        for level, uid in ((False, 1000), (True, 0)):
            self.user.accessibility = level
            self.user.uid = uid
            self.controller._repair_accessibility(self.user, self.current)
        self.discover.assert_not_called()

    def test_downgrade_revokes_even_when_replanning_fails(self):
        self.controller.reconcile_portals()
        self.user.accessibility = False
        login = session.LoginSession("2", True, False, "wayland", "user")
        with (
            mock.patch.object(
                session,
                "host_manager_environment",
                return_value={"XDG_SESSION_ID": "2"},
            ),
            mock.patch.object(
                session,
                "_plan",
                side_effect=RuntimeError("unrelated resource failed"),
            ),
            mock.patch.object(self.controller, "_remove_generated"),
        ):
            with self.assertRaises(session.DesktopSetupError):
                self.controller.reconcile(self.user, (login,))
        self.proxy.close.assert_called_once()
        self.assertIsNone(self.current.accessibility)

    def test_downgrade_revokes_before_reading_host_environment(self):
        self.controller.reconcile_portals()
        self.user.accessibility = False
        with mock.patch.object(
            session,
            "host_manager_environment",
            side_effect=RuntimeError("manager gone"),
        ):
            with self.assertRaises(RuntimeError):
                self.controller.reconcile(self.user, ())
        self.proxy.close.assert_called_once()
        self.assertIsNone(self.current.accessibility)

    def test_failed_update_cannot_restore_revoked_permission(self):
        self.controller.reconcile_portals()
        self.user.accessibility = False
        plan = replace(self.current.plan, accessibility=False)
        login = session.LoginSession("2", True, False, "wayland", "user")
        with (
            mock.patch.object(
                session,
                "host_manager_environment",
                return_value={"XDG_SESSION_ID": "2"},
            ),
            mock.patch.object(session, "_plan", return_value=plan),
            mock.patch.object(self.controller, "_deactivate"),
            mock.patch.object(
                self.controller,
                "_activate",
                side_effect=[
                    RuntimeError("mount failed"),
                    session._ActiveDesktop(plan),
                ],
            ) as activate,
        ):
            with self.assertRaises(session.DesktopSetupError):
                self.controller.reconcile(self.user, (login,))
        self.assertFalse(activate.call_args_list[-1].args[1].accessibility)
        self.assertIsNone(self.current.accessibility)

    def test_logout_closes_the_bridge(self):
        self.controller.reconcile_portals()
        self.controller.reconcile(self.user, (), session_active=False)
        self.proxy.close.assert_called_once()
        self.assertNotIn(1000, self.controller.active)

    def test_portal_recovery_preserves_accessibility_environment(self):
        self.controller.reconcile_portals()
        self.controller.portals_enabled = True
        portal = mock.Mock(
            socket_path=self.root / "portal", broker_process=None
        )
        with mock.patch.object(
            session, "start_portal_proxy", return_value=(portal, self.binding)
        ):
            self.controller._repair_portal(self.user, self.current)
        command = self.command.call_args_list[-2].args[1]
        self.assertIn(
            "AT_SPI_BUS_ADDRESS=unix:path=" + self.binding.destination, command
        )


@unittest.skipUnless(
    shutil.which("dbus-daemon")
    and shutil.which("busctl")
    and shutil.which("xdg-dbus-proxy"),
    "private-bus tests need dbus-daemon, busctl and xdg-dbus-proxy",
)
class AccessibilityProxyIntegrationTests(unittest.TestCase):
    def setUp(self):
        try:
            import gi

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib
        except ImportError:
            self.skipTest("private-bus tests need PyGObject")
        self.Gio, self.GLib = Gio, GLib
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.user = desktop_user(self.root)
        self.buses = []
        for name in ("session", "a11y"):
            path = self.root / name
            process = subprocess.Popen(
                [
                    "dbus-daemon",
                    "--session",
                    "--nofork",
                    f"--address=unix:path={path}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self.addCleanup(self.stop, process)
            self.wait_path(path, process)
            self.buses.append((f"unix:path={path}", process))
        self.fixture = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("atspi_bus_fixture.py")),
                self.buses[0][0],
                self.buses[1][0],
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self.stop, self.fixture)
        self.assertTrue(
            select.select([self.fixture.stdout], [], [], 5)[0],
            "fixture readiness timeout",
        )
        ready = self.fixture.stdout.readline()
        self.assertTrue(
            ready,
            "fixture exited: "
            + (
                self.fixture.stderr.read()
                if self.fixture.poll() is not None
                else ""
            ),
        )
        self.application = json.loads(ready)["application"]
        metadata = (self.root / "a11y").stat()
        self.source = session.DesktopBind(
            str(self.root / "a11y"),
            self.root / "a11y",
            metadata.st_dev,
            metadata.st_ino,
        )
        self.start_proxy()

    def start_proxy(self):
        real_popen = subprocess.Popen

        def proxy_popen(command, **kwargs):
            # Exercise the production proxy startup/readiness/cleanup path;
            # isolate it from the real user's systemd session in this test.
            return real_popen(
                command[command.index(session.XDG_DBUS_PROXY) :], **kwargs
            )

        with (
            mock.patch.object(session, "RUNTIME_ROOT", self.root / "runtime"),
            mock.patch.object(
                session.subprocess, "Popen", side_effect=proxy_popen
            ),
        ):
            self.proxy, self.binding = session.start_accessibility_proxy(
                "test", self.user, "2", self.source
            )
        self.addCleanup(self.proxy.close)
        self.client = self.connect("unix:path=" + str(self.proxy.socket_path))
        self.client.set_exit_on_close(False)
        self.addCleanup(
            lambda: (
                self.client.close_sync(None)
                if not self.client.is_closed()
                else None
            )
        )

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def wait_path(self, path, process):
        deadline = time.monotonic() + 5
        while not path.exists():
            self.assertIsNone(process.poll(), "bus exited before readiness")
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)

    def connect(self, address):
        return self.Gio.DBusConnection.new_for_address_sync(
            address,
            self.Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | self.Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )

    def call(
        self, destination, path, interface, method, signature=None, args=()
    ):
        return self.client.call_sync(
            destination,
            path,
            interface,
            method,
            self.GLib.Variant(signature, args) if signature else None,
            None,
            self.Gio.DBusCallFlags.NONE,
            3000,
            None,
        ).unpack()

    def test_unique_references_actions_text_events_and_reverse_calls(self):
        root = "/org/a11y/atspi/accessible/root"
        registry = "org.a11y.atspi.Registry"
        children = self.call(
            registry, root, "org.a11y.atspi.Accessible", "GetChildren"
        )[0]
        application, path = children[0]
        self.assertEqual(application, self.application)
        name = self.call(
            application,
            path,
            "org.freedesktop.DBus.Properties",
            "Get",
            "(ss)",
            ("org.a11y.atspi.Accessible", "Name"),
        )[0]
        self.assertEqual(name, "Spaces AT-SPI fixture")
        self.call(
            registry,
            root,
            "org.a11y.atspi.Registry",
            "RegisterEvent",
            "(s)",
            ("object:state-changed",),
        )
        events = []
        subscription = self.client.signal_subscribe(
            application,
            "org.a11y.atspi.Event.Object",
            "StateChanged",
            path,
            None,
            self.Gio.DBusSignalFlags.NONE,
            lambda *args: events.append(args[5].unpack()),
        )
        self.addCleanup(self.client.signal_unsubscribe, subscription)
        self.assertEqual(
            self.call(
                application,
                path,
                "org.a11y.atspi.Action",
                "DoAction",
                "(i)",
                (0,),
            ),
            (True,),
        )
        self.assertEqual(
            self.call(
                application,
                path,
                "org.a11y.atspi.EditableText",
                "SetTextContents",
                "(s)",
                ("Héllo 世界",),
            ),
            (True,),
        )
        deadline = time.monotonic() + 3
        context = self.GLib.MainContext.default()
        while not events and time.monotonic() < deadline:
            context.iteration(False)
            time.sleep(0.01)
        self.assertEqual(events[0][0:2], ("checked", 1))
        state = json.loads(
            self.call(registry, root, "org.spaces.Test", "State")[0]
        )
        self.assertEqual(
            state,
            {
                "clicks": 1,
                "text": "Héllo 世界",
                "events": ["object:state-changed"],
            },
        )
        info = self.Gio.DBusNodeInfo.new_for_xml(
            """<node><interface name='org.spaces.Listener'>
            <method name='Ping'><arg type='s' direction='out'/></method></interface></node>"""
        ).interfaces[0]
        registration = self.client.register_object(
            "/listener",
            info,
            lambda *args: args[-1].return_value(
                self.GLib.Variant("(s)", ("pong",))
            ),
            None,
            None,
        )
        self.addCleanup(self.client.unregister_object, registration)
        replies = []

        def finished(connection, result):
            replies.append(connection.call_finish(result).unpack())

        self.client.call(
            registry,
            root,
            "org.spaces.Test",
            "Callback",
            self.GLib.Variant("(o)", ("/listener",)),
            None,
            self.Gio.DBusCallFlags.NONE,
            3000,
            None,
            finished,
        )
        deadline = time.monotonic() + 4
        while not replies and time.monotonic() < deadline:
            context.iteration(False)
            time.sleep(0.01)
        self.assertEqual(replies, [("pong",)])
        new_app = self.call(
            registry, root, "org.spaces.Test", "ReplaceApplication"
        )[0]
        self.assertNotEqual(new_app, application)
        self.assertEqual(
            self.call(
                registry, root, "org.a11y.atspi.Accessible", "GetChildren"
            )[0][0][0],
            new_app,
        )
        with self.assertRaises(self.GLib.Error):
            self.call(
                application, path, "org.a11y.atspi.Accessible", "GetState"
            )
        self.assertEqual(
            self.call(new_app, path, "org.a11y.atspi.Accessible", "GetState"),
            ([1, 0],),
        )

    def test_close_revokes_an_already_open_connection(self):
        self.proxy.close()
        deadline = time.monotonic() + 3
        while not self.client.is_closed() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.client.is_closed())
        with self.assertRaises(self.GLib.Error):
            self.call(
                "org.a11y.atspi.Registry",
                "/org/a11y/atspi/accessible/root",
                "org.a11y.atspi.Accessible",
                "GetChildren",
            )

    def test_private_runtime_and_socket_modes(self):
        self.assertEqual(
            stat.S_IMODE(self.proxy.socket_path.parent.stat().st_mode), 0o700
        )
        self.assertEqual(
            stat.S_IMODE(self.proxy.socket_path.stat().st_mode), 0o600
        )

    def test_bus_restart_allows_a_fresh_proxy_and_application(self):
        old_client = self.client
        self.stop(self.fixture)
        self.stop(self.buses[1][1])
        self.proxy.close()
        deadline = time.monotonic() + 3
        while not old_client.is_closed() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(old_client.is_closed())
        address = self.buses[1][0]
        process = subprocess.Popen(
            ["dbus-daemon", "--session", "--nofork", "--address=" + address],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self.stop, process)
        self.wait_path(self.source.source, process)
        fixture = subprocess.Popen(
            self.fixture.args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self.stop, fixture)
        self.assertTrue(select.select([fixture.stdout], [], [], 5)[0])
        self.assertTrue(fixture.stdout.readline())
        metadata = self.source.source.stat()
        self.source = replace(
            self.source, device=metadata.st_dev, inode=metadata.st_ino
        )
        self.start_proxy()
        application, path = self.call(
            "org.a11y.atspi.Registry",
            "/org/a11y/atspi/accessible/root",
            "org.a11y.atspi.Accessible",
            "GetChildren",
        )[0][0]
        self.assertEqual(
            self.call(
                application,
                path,
                "org.a11y.atspi.Action",
                "DoAction",
                "(i)",
                (0,),
            ),
            (True,),
        )

    def test_discovery_service_uses_real_busctl(self):
        real_run = subprocess.run
        validate = session._validated_source

        def run(command, **kwargs):
            command = list(command)
            command[1] = "--address=" + self.buses[0][0]
            return real_run(command, **kwargs)

        def validate_in_test_runtime(path, **kwargs):
            kwargs["roots"] = (self.root,)
            return validate(path, **kwargs)

        # Substitute only the private session/runtime namespaces; keep real
        # GetAddress traffic, parsing, ownership and socket validation.
        with (
            mock.patch.object(session.subprocess, "run", side_effect=run),
            mock.patch.object(
                session,
                "_validated_source",
                side_effect=validate_in_test_runtime,
            ),
        ):
            discovered = session.discover_accessibility_bus(self.user)
        self.assertEqual(discovered, self.source)


if __name__ == "__main__":
    unittest.main()
