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

    def test_disabled_desktop_closes_before_unmount_and_clears_environment(self):
        self.controller.reconcile_portals()
        order = []
        self.proxy.close.side_effect = lambda: order.append("close")
        self.unmount.side_effect = lambda *_: order.append("unmount")
        self.user.desktop = False
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
        self.user.desktop = False
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

    def test_basic_publishes_accessibility_but_root_does_not(self):
        self.user.accessibility = False
        self.current.plan = replace(self.current.plan, accessibility=False)
        self.controller._repair_accessibility(self.user, self.current)
        self.start.assert_called_once_with(
            "test", self.user, "2", self.source, unrestricted=False
        )
        self.discover.reset_mock()
        self.user.uid = 0
        self.controller._repair_accessibility(self.user, self.current)
        self.discover.assert_not_called()

    def test_forwarding_is_ready_before_guest_graphical_startup(self):
        self.controller.portals_enabled = True
        events = []
        self.start.side_effect = lambda *args, **kwargs: (events.append("accessibility") or (self.proxy, self.binding))
        portal = mock.Mock(socket_path=self.root / "portal")
        with (
            mock.patch.object(self.controller, "_prepare_guest_root"),
            mock.patch.object(self.controller, "_repair_computer"),
            mock.patch.object(session, "start_portal_proxy", return_value=(portal, self.binding)),
            mock.patch.object(self.controller, "_start_graphical_session",
                              side_effect=lambda *args: events.append("graphical")) as graphical,
        ):
            active = self.controller._activate(self.user, self.current.plan)
        self.assertEqual(events, ["accessibility", "graphical"])
        self.assertEqual(graphical.call_args.args[1].environment["AT_SPI_BUS_ADDRESS"],
                         "unix:path=" + self.binding.destination)
        self.assertTrue(active.accessibility_activation)

    def test_activation_environment_imports_accessibility_address_once(self):
        plan = replace(self.current.plan, environment={
            "SPACES_NAME": "test", "AT_SPI_BUS_ADDRESS": "unix:path=" + self.binding.destination
        })
        self.controller._start_graphical_session(self.user, plan)
        command = self.command.call_args_list[0].args[1]
        self.assertNotIn("AT_SPI_BUS_ADDRESS", command)
        self.assertEqual(sum(arg.startswith("AT_SPI_BUS_ADDRESS=") for arg in command), 1)
        self.assertIn("SPACES_NAME", command)

    def test_teardown_removes_activation_before_revoking_forwarding(self):
        self.controller.reconcile_portals()
        self.current.accessibility_activation = True
        events = []
        self.command.side_effect = lambda user, command, **kwargs: events.append(command[-1])
        self.proxy.close.side_effect = lambda: events.append("close-proxy")
        self.controller._deactivate(self.user, self.current)
        self.assertLess(events.index("spaces-portal.service"), events.index("--remove-accessibility"))
        self.assertLess(events.index("--remove-accessibility"), events.index("close-proxy"))

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
        command = next(
            call.args[1] for call in self.command.call_args_list
            if call.args[1][0] == session.DBUS_UPDATE_ACTIVATION_ENVIRONMENT
            and any(arg.startswith("AT_SPI_BUS_ADDRESS=unix:") for arg in call.args[1])
        )
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

    @classmethod
    def setUpClass(cls):
        subprocess.run(["make", "-C", str(Path(__file__).resolve().parents[1] / "native"),
                        "spaces-portal"], check=True, stdout=subprocess.DEVNULL)

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

    def test_basic_provider_registration_callbacks_and_host_isolation(self):
        self.proxy.close()
        self.proxy.socket_path.unlink(missing_ok=True)
        real_popen = subprocess.Popen

        def proxy_popen(command, **kwargs):
            return real_popen(command[command.index(session.XDG_DBUS_PROXY):], **kwargs)

        with mock.patch.object(session, "RUNTIME_ROOT", self.root / "runtime"), mock.patch.object(
            session.subprocess, "Popen", side_effect=proxy_popen
        ):
            self.proxy, self.binding = session.start_accessibility_proxy(
                "test", self.user, "2", self.source, unrestricted=False
            )
        self.addCleanup(self.proxy.close)
        self.client = self.connect("unix:path=" + str(self.proxy.socket_path))
        self.client.set_exit_on_close(False)
        self.addCleanup(lambda: self.client.close_sync(None) if not self.client.is_closed() else None)
        root = "/org/a11y/atspi/accessible/root"
        # A provider can register but cannot enumerate or operate host trees.
        self.call("org.a11y.atspi.Registry", root, "org.a11y.atspi.Socket", "Embed",
                  "((so))", ((self.client.get_unique_name(), "/guest"),))
        for destination, interface, method, signature, args in (
            ("org.a11y.atspi.Registry", "org.a11y.atspi.Accessible", "GetChildren", None, ()),
            (self.application, "org.a11y.atspi.Action", "DoAction", "(i)", (0,)),
            ("org.a11y.atspi.Registry", "org.a11y.atspi.Registry", "RegisterEvent", "(s)", ("object:state-changed",)),
        ):
            with self.subTest(method=method), self.assertRaises(self.GLib.Error):
                self.call(destination, root, interface, method, signature, args)

        # Real host -> proxied guest calls, including a reply and a signal.
        guest_info = self.Gio.DBusNodeInfo.new_for_xml("""<node><interface name='org.a11y.atspi.Action'>
        <method name='DoAction'><arg type='i' direction='in'/><arg type='b' direction='out'/></method>
        </interface></node>""").interfaces[0]
        actions = []

        def act(connection, sender, path, interface, method, parameters, invocation):
            actions.append(parameters.unpack()[0])
            connection.emit_signal(None, path, "org.a11y.atspi.Event.Object", "StateChanged",
                                   self.GLib.Variant("(s)", ("checked",)))
            invocation.return_value(self.GLib.Variant("(b)", (True,)))

        registration = self.client.register_object("/guest", guest_info, act, None, None)
        self.addCleanup(self.client.unregister_object, registration)
        host = self.connect(self.buses[1][0])
        self.addCleanup(host.close_sync, None)
        events = []
        subscription = host.signal_subscribe(None, "org.a11y.atspi.Event.Object", "StateChanged",
                                             "/guest", None, self.Gio.DBusSignalFlags.NONE,
                                             lambda *args: events.append(args[5].unpack()))
        self.addCleanup(host.signal_unsubscribe, subscription)
        result = []

        def finished(connection, reply):
            try:
                result.append(connection.call_finish(reply).unpack())
            except self.GLib.Error as error:
                result.append(error)

        # The registry records the host-side unique name, as the real registry does.
        state = host.call_sync("org.a11y.atspi.Registry", root, "org.spaces.Test", "State",
                               None, None, self.Gio.DBusCallFlags.NONE, 3000, None).unpack()
        provider = json.loads(state[0])["providers"][-1][0]
        host.call(provider, "/guest", "org.a11y.atspi.Action", "DoAction",
                  self.GLib.Variant("(i)", (0,)), None, self.Gio.DBusCallFlags.NONE,
                  3000, None, finished)
        deadline = time.monotonic() + 4
        while (not result or not events) and time.monotonic() < deadline:
            self.GLib.MainContext.default().iteration(False)
            time.sleep(0.005)
        self.assertEqual(result, [(True,)])
        self.assertEqual(actions, [0])
        self.assertEqual(events, [("checked",)])

    def start_relay(self):
        guest_address, guest_bus = self.buses[0]
        # Use a separate private guest session, leaving the fixture on the host session.
        path = self.root / "guest-session"
        process = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--address=unix:path=" + str(path)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(self.stop, process)
        self.wait_path(path, process)
        guest_address = "unix:path=" + str(path)
        native = subprocess.Popen([sys.executable, str(Path(__file__).with_name("atspi_bus_fixture.py")),
                                   guest_address, self.buses[1][0], "--native"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, native)
        self.assertTrue(select.select([native.stdout], [], [], 5)[0])
        self.assertTrue(native.stdout.readline())
        guest = self.connect(guest_address)
        self.addCleanup(guest.close_sync, None)
        self.assertEqual(guest.call_sync("org.a11y.Bus", "/org/a11y/bus", "org.freedesktop.DBus.Properties",
                                        "Get", self.GLib.Variant("(ss)", ("org.a11y.Status", "IsEnabled")),
                                        None, self.Gio.DBusCallFlags.NONE, 3000, None).unpack(), (False,))
        status_socket = self.root / "status-proxy"
        status_proxy = subprocess.Popen([session.XDG_DBUS_PROXY, self.buses[0][0], str(status_socket),
                                         *session._portal_policy_arguments()],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(self.stop, status_proxy)
        self.wait_path(status_socket, status_proxy)
        filtered = self.connect("unix:path=" + str(status_socket))
        self.addCleanup(filtered.close_sync, None)
        with self.assertRaises(self.GLib.Error):
            filtered.call_sync("org.a11y.Bus", "/org/a11y/bus", "org.freedesktop.DBus.Properties", "Set",
                               self.GLib.Variant("(ssv)", ("org.a11y.Status", "IsEnabled", self.GLib.Variant("b", False))),
                               None, self.Gio.DBusCallFlags.NONE, 3000, None)
        binary = Path(__file__).resolve().parents[1] / "native" / "spaces-portal"
        runtime = self.root / "guest-runtime"
        runtime.mkdir()
        environment = {**os.environ, "DBUS_SESSION_BUS_ADDRESS": guest_address,
                       "XDG_RUNTIME_DIR": str(runtime), "SPACES_NAME": "test",
                       "SPACES_PORTAL_TEST_ADDRESS": "unix:path=" + str(status_socket),
                       "AT_SPI_BUS_ADDRESS": "unix:path=" + str(self.proxy.socket_path)}
        router = subprocess.Popen([binary], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(self.stop, router)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                value = guest.call_sync("org.a11y.Bus", "/org/a11y/bus", "org.freedesktop.DBus.Properties", "Get",
                                        self.GLib.Variant("(ss)", ("org.a11y.Status", "IsEnabled")),
                                        None, self.Gio.DBusCallFlags.NONE, 500, None).unpack()
                if value == (True,):
                    return guest, binary, environment, router
            except self.GLib.Error:
                pass
            if router.poll() is not None:
                self.fail(router.stderr.read().decode())
            time.sleep(0.01)
        self.fail("guest accessibility did not relay enabled host status")

    def test_relay_address_status_signals_owner_replacement_and_restart(self):
        guest, binary, environment, router = self.start_relay()

        def call(interface, method, signature=None, args=()):
            return guest.call_sync("org.a11y.Bus", "/org/a11y/bus", interface, method,
                                   self.GLib.Variant(signature, args) if signature else None,
                                   None, self.Gio.DBusCallFlags.NONE, 3000, None).unpack()

        self.assertEqual(call("org.a11y.Bus", "GetAddress"), (environment["AT_SPI_BUS_ADDRESS"],))
        self.assertEqual(call("org.freedesktop.DBus.Properties", "GetAll", "(s)", ("org.a11y.Status",)),
                         ({"IsEnabled": True},))
        with self.assertRaises(self.GLib.Error):
            call("org.freedesktop.DBus.Properties", "Set", "(ssv)",
                 ("org.a11y.Status", "IsEnabled", self.GLib.Variant("b", False)))
        changes = []
        subscription = guest.signal_subscribe("org.a11y.Bus", "org.freedesktop.DBus.Properties", "PropertiesChanged",
                                              "/org/a11y/bus", None, self.Gio.DBusSignalFlags.NONE,
                                              lambda *args: changes.append(args[5].unpack()))
        self.addCleanup(guest.signal_unsubscribe, subscription)
        host = self.connect(self.buses[0][0])
        self.addCleanup(host.close_sync, None)
        host.call_sync("org.a11y.Bus", "/org/a11y/bus", "org.spaces.Test", "SetEnabled",
                       self.GLib.Variant("(b)", (False,)), None, self.Gio.DBusCallFlags.NONE, 3000, None)
        deadline = time.monotonic() + 3
        while not changes and time.monotonic() < deadline:
            self.GLib.MainContext.default().iteration(False)
            time.sleep(0.005)
        self.assertTrue(changes)
        self.assertEqual(changes[-1][1], {"IsEnabled": False})
        self.assertEqual(call("org.freedesktop.DBus.Properties", "Get", "(ss)", ("org.a11y.Status", "IsEnabled")), (False,))
        # GetAddress remains valid even when host accessibility is disabled.
        self.assertEqual(call("org.a11y.Bus", "GetAddress"), (environment["AT_SPI_BUS_ADDRESS"],))
        host_owner = host.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                   "GetNameOwner", self.GLib.Variant("(s)", ("org.a11y.Bus",)),
                                   None, self.Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]
        for method in ("ReleaseStatus", "SetEnabled", "AcquireStatus"):
            host.call_sync(host_owner, "/org/a11y/bus", "org.spaces.Test", method,
                           self.GLib.Variant("(b)", (True,)) if method == "SetEnabled" else None,
                           None, self.Gio.DBusCallFlags.NONE, 3000, None)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                if call("org.freedesktop.DBus.Properties", "Get", "(ss)", ("org.a11y.Status", "IsEnabled")) == (True,):
                    break
            except self.GLib.Error:
                pass
            time.sleep(0.01)
        else:
            self.fail("host status owner replacement was not recovered")
        self.stop(router)
        router = subprocess.Popen([binary], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(self.stop, router)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            checked = subprocess.run([binary, "--check-accessibility"], env=environment, capture_output=True)
            if checked.returncode == 0:
                break
            time.sleep(0.01)
        else:
            self.fail(checked.stderr.decode())
        self.proxy.socket_path.unlink()
        with self.assertRaises(self.GLib.Error):
            call("org.a11y.Bus", "GetAddress")

    def test_relay_optional_screen_reader_property(self):
        self.stop(self.fixture)
        self.fixture = subprocess.Popen([*self.fixture.args, "--screen-reader"],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, self.fixture)
        self.assertTrue(select.select([self.fixture.stdout], [], [], 5)[0])
        self.assertTrue(self.fixture.stdout.readline())
        guest, binary, environment, router = self.start_relay()
        status = guest.call_sync("org.a11y.Bus", "/org/a11y/bus", "org.freedesktop.DBus.Properties", "GetAll",
                                self.GLib.Variant("(s)", ("org.a11y.Status",)), None,
                                self.Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]
        self.assertEqual(status, {"IsEnabled": True, "ScreenReaderEnabled": True})

    def test_runtime_activation_install_cleanup_and_conflicts(self):
        guest, binary, environment, router = self.start_relay()
        for option in ("--install-accessibility", "--install-accessibility"):
            subprocess.run([binary, option], env=environment, check=True, capture_output=True)
        service = Path(environment["XDG_RUNTIME_DIR"]) / "dbus-1/services/org.a11y.Bus.service"
        self.assertIn("SystemdService=spaces-portal.service", service.read_text())
        subprocess.run([binary, "--remove-accessibility"], env=environment, check=True, capture_output=True)
        self.assertFalse(service.exists())
        service.write_text("user-owned service")
        for option in ("--install-accessibility", "--remove-accessibility"):
            result = subprocess.run([binary, option], env=environment, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(service.read_text(), "user-owned service")

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
            {key: state[key] for key in ("clicks", "text", "events")},
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
