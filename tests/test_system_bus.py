"""System bridge policy, runtime mounts, and isolated two-bus integration.

Run integration as root: sudo env PYTHONPATH=src python3 -m unittest discover
-s tests -p test_system_bus.py. The buses and all services are private fixtures;
these tests never call the host NetworkManager or resolver.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from spaces import core, system_bus

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "native/spaces-system-broker"
NM = "org.freedesktop.NetworkManager"
RESOLVE = "org.freedesktop.resolve1"
UPOWER = "org.freedesktop.UPower"

try:
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
except ImportError:
    Gio = GLib = None


class SystemBusRuntimeTests(unittest.TestCase):
    def test_activation_and_daemon_conditions(self):
        bindings = system_bus.guest_bind_arguments(Path("/run/private"))
        self.assertIn("--bind-ro=/run/private:/run/spaces-host/system", bindings)
        for name in (
            "systemd-resolved.service",
            "NetworkManager.service",
            "upower.service",
        ):
            self.assertIn(
                f"--bind-ro={system_bus.DATA_ROOT / 'host-service.conf'}:"
                f"/etc/systemd/system/{name}.d/50-spaces-host-service.conf",
                bindings,
            )
        for name in system_bus.SERVICES:
            self.assertIn(
                f"--bind-ro={system_bus.DATA_ROOT / 'host-service.conf'}:"
                f"/etc/systemd/system/dbus-{name}.service.d/50-spaces-host-service.conf",
                bindings,
            )
            self.assertIn(
                f"--bind-ro={system_bus.DATA_ROOT}/dbus-1/system-services/{name}.service:"
                f"/etc/dbus-1/system-services/{name}.service",
                bindings,
            )
        self.assertIn(
            f"--bind-ro={system_bus.DATA_ROOT / 'multi-user.conf'}:"
            "/etc/systemd/system/multi-user.target.d/50-spaces-system-broker.conf",
            bindings,
        )
        self.assertFalse(any("multi-user.target.wants" in item for item in bindings))
        self.assertFalse(any("login1" in item for item in bindings))

    def test_mounts_leave_package_files_and_unit_aliases_replaceable(self):
        for binding in system_bus.guest_bind_arguments(Path("/run/private")):
            destination = binding.split(":", 1)[1]
            self.assertFalse(destination.startswith(("/usr/", "/lib/")))
            if destination.startswith("/etc/systemd/system/"):
                self.assertTrue(
                    destination.endswith(".conf")
                    or destination.endswith("/spaces-system-broker.service"),
                    destination,
                )

    def test_guest_daemons_are_skipped_only_with_bridge_present(self):
        config = (ROOT / "data/system-bridge/host-service.conf").read_text()
        self.assertIn(f"ConditionPathExists=!{system_bus.GUEST_RUNTIME}\n", config)

    def test_network_permission_is_explicit(self):
        for level in core.NETWORK_LEVELS:
            service = system_bus.SystemBusService("test", level)
            with (
                mock.patch.object(Path, "unlink"),
                mock.patch.object(system_bus.subprocess, "Popen") as popen,
            ):
                service._spawn()
            self.assertEqual("--admin" in popen.call_args.args[0], level == "admin")
        with self.assertRaises(core.SpacesError):
            system_bus.SystemBusService("test", "invented")

    def test_service_files_have_no_desktop_dependency(self):
        unit = (ROOT / "data/system-bridge/spaces-system-broker.service").read_text()
        self.assertIn("After=dbus.service", unit)
        self.assertIn("User=root", unit)
        self.assertNotIn("graphical", unit)
        for service in system_bus.SERVICES:
            entry = (
                ROOT / f"data/system-bridge/dbus-1/system-services/{service}.service"
            ).read_text()
            self.assertIn("SystemdService=spaces-system-broker.service", entry)


@unittest.skipUnless(
    os.geteuid() == 0 and shutil.which("unshare"), "root and unshare required"
)
class SystemBusMountTests(unittest.TestCase):
    def test_package_files_can_be_replaced_with_bridge_mounted(self):
        with tempfile.TemporaryDirectory(prefix="spaces-package-test-") as temporary:
            directory = Path(temporary)
            runtime = directory / "bridge"
            runtime.mkdir()
            rootfs = directory / "rootfs"
            vendor_files = []
            for name, service in zip(system_bus.GUEST_DAEMONS, system_bus.SERVICES):
                unit = rootfs / "usr/lib/systemd/system" / name
                unit.parent.mkdir(parents=True, exist_ok=True)
                unit.write_text("[Service]\nExecStart=/bin/true\n")
                alias = rootfs / "etc/systemd/system" / f"dbus-{service}.service"
                alias.parent.mkdir(parents=True, exist_ok=True)
                alias.symlink_to(f"../../../usr/lib/systemd/system/{name}")
                vendor_files.append(str(unit))
                for prefix in ("usr/share", "usr/local/share"):
                    activation = (
                        rootfs
                        / prefix
                        / "dbus-1/system-services"
                        / f"{service}.service"
                    )
                    activation.parent.mkdir(parents=True, exist_ok=True)
                    activation.write_text("package activation file\n")
                    vendor_files.append(str(activation))
            with mock.patch.object(
                system_bus, "DATA_ROOT", ROOT / "data/system-bridge"
            ):
                bindings = system_bus.guest_bind_arguments(runtime)
            # Keep real mounts confined to a child namespace. Simulate dpkg's
            # hard-link backup followed by replacement of each vendor file.
            script = """import json, os, pathlib, subprocess, sys
rootfs, bindings, vendor_files = json.loads(sys.argv[1])
subprocess.run(['mount', '--make-rprivate', '/'], check=True)
for binding in bindings:
    source, destination = binding.removeprefix('--bind-ro=').split(':', 1)
    target = pathlib.Path(rootfs) / destination.lstrip('/')
    target.parent.mkdir(parents=True, exist_ok=True)
    if pathlib.Path(source).is_dir():
        target.mkdir(exist_ok=True)
    elif not target.exists():
        target.touch()
    subprocess.run(['mount', '--bind', source, str(target)], check=True)
    subprocess.run(['mount', '-o', 'remount,bind,ro', str(target)], check=True)
for filename in vendor_files:
    target = pathlib.Path(filename)
    os.link(target, filename + '.dpkg-tmp')
    replacement = pathlib.Path(filename + '.dpkg-new')
    replacement.write_text('upgraded package file\\n')
    replacement.replace(target)
    assert target.read_text() == 'upgraded package file\\n'
"""
            result = subprocess.run(
                [
                    "unshare",
                    "--mount",
                    sys.executable,
                    "-c",
                    script,
                    json.dumps([str(rootfs), bindings, vendor_files]),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(
    Gio and os.geteuid() == 0 and BINARY.exists(), "root and built bridge required"
)
class SystemBusIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loop = GLib.MainLoop()
        cls.thread = threading.Thread(target=cls.loop.run, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.loop.quit()
        cls.thread.join()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="spaces-system-test-")
        self.directory = Path(self.temporary.name)
        self.directory.chmod(0o755)
        self.processes = []
        self.connections = []
        self.logs = []
        self.calls = []
        self.agent_result = None
        self.host_address = self.start_bus("host")
        self.guest_address = self.start_bus("guest")
        self.host = self.connect(self.host_address)
        self.install_services()
        self.socket = self.directory / "broker.sock"
        self.broker = self.start_bridge("--broker", self.host_address, admin=True)
        self.wait_for(lambda: self.socket.exists())
        self.relay = self.start_bridge("--relay", self.guest_address)
        self.guest = self.connect(self.guest_address)
        self.wait_for(lambda: self.has_owner(self.guest, UPOWER))
        self.wait_for(lambda: len(self.calls) >= 0, timeout=0.1)

    def tearDown(self):
        for connection in self.connections:
            if not connection.is_closed():
                connection.close_sync(None)
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        diagnostics = []
        for log in self.logs:
            log.flush()
            diagnostics.append(Path(log.name).read_text())
            log.close()
        self.temporary.cleanup()
        self.assertNotIn("CRITICAL", "\n".join(diagnostics))

    def start_bus(self, name):
        config = self.directory / f"{name}.conf"
        config.write_text(f"""<busconfig><type>system</type>
<listen>unix:tmpdir={self.directory}</listen><auth>EXTERNAL</auth>
<policy context="default"><allow user="*"/><allow own="*"/>
<allow send_destination="*"/><allow receive_sender="*"/></policy></busconfig>""")
        process = subprocess.Popen(
            ["dbus-daemon", "--nofork", "--print-address=1", f"--config-file={config}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.processes.append(process)
        return process.stdout.readline().strip()

    def start_bridge(self, mode, address, admin=False):
        log = open(self.directory / f"{mode[2:]}-{len(self.logs)}.log", "w+")
        self.logs.append(log)
        command = [str(BINARY), mode, str(self.socket)] + (["--admin"] if admin else [])
        process = subprocess.Popen(
            command,
            env={**os.environ, "DBUS_SYSTEM_BUS_ADDRESS": address},
            stdout=log,
            stderr=log,
        )
        self.processes.append(process)
        return process

    def connect(self, address):
        connection = Gio.DBusConnection.new_for_address_sync(
            address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )
        connection.set_exit_on_close(False)
        self.connections.append(connection)
        return connection

    def wait_for(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        logs = []
        for log in self.logs:
            log.flush()
            logs.append(Path(log.name).read_text())
        self.fail("condition timed out\n" + "\n".join(logs))

    def has_owner(self, connection, name):
        return connection.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", (name,)),
            None,
            Gio.DBusCallFlags.NONE,
            1000,
            None,
        ).unpack()[0]

    def call(self, name, path, interface, method, body=None, connection=None):
        return (connection or self.guest).call_sync(
            name,
            path,
            interface,
            method,
            body,
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )

    def install_services(self):
        self.registrations = []
        definitions = [
            (
                NM,
                "/org/freedesktop",
                "org.freedesktop.DBus.ObjectManager",
                """
<method name="GetManagedObjects"><arg type="a{oa{sa{sv}}}" direction="out"/></method>
<signal name="InterfacesAdded"><arg type="o"/><arg type="a{sa{sv}}"/></signal>""",
            ),
            (
                RESOLVE,
                "/org/freedesktop/resolve1",
                RESOLVE + ".Manager",
                """
<method name="GetLink"><arg type="i" direction="in"/><arg type="o" direction="out"/></method>
<method name="SetLinkDNS"><arg type="i" direction="in"/><arg type="a(iay)" direction="in"/></method>
<method name="ResolveRecord"><arg type="h" direction="out"/></method>
<method name="ResolveService"><arg type="h" direction="in"/><arg type="s" direction="out"/></method>
<method name="MadeUpRead"/><property name="DNS" type="s" access="read"/>
<signal name="Changed"><arg type="s"/></signal>""",
            ),
            (
                NM,
                "/org/freedesktop/NetworkManager",
                NM,
                """
<method name="GetDevices"><arg type="ao" direction="out"/></method>
<method name="Enable"><arg type="b" direction="in"/></method>
<method name="ActivateConnection"><arg type="o" direction="in"/><arg type="o" direction="in"/><arg type="o" direction="in"/><arg type="o" direction="out"/></method>
<method name="GetPermissions"><arg type="a{ss}" direction="out"/></method>
<property name="WirelessEnabled" type="b" access="readwrite"/>
<property name="ConnectivityCheckEnabled" type="b" access="readwrite"/>
<signal name="StateChanged"><arg type="u"/></signal>""",
            ),
            (
                NM,
                "/org/freedesktop/NetworkManager/Settings/1",
                NM + ".Settings.Connection",
                """
<method name="GetSettings"><arg type="a{sa{sv}}" direction="out"/></method>
<method name="GetSecrets"><arg type="s" direction="in"/><arg type="a{sa{sv}}" direction="out"/></method>""",
            ),
            (
                NM,
                "/org/freedesktop/NetworkManager/AgentManager",
                NM + ".AgentManager",
                """
<method name="Register"><arg type="s" direction="in"/></method><method name="Unregister"/>""",
            ),
            (
                UPOWER,
                "/org/freedesktop/UPower",
                UPOWER,
                """
<method name="EnumerateDevices"><arg type="ao" direction="out"/></method>
<property name="OnBattery" type="b" access="read"/>
<signal name="DeviceAdded"><arg type="o"/></signal>""",
            ),
            (
                UPOWER,
                "/org/freedesktop/UPower/devices/DisplayDevice",
                UPOWER + ".Device",
                """
<method name="Refresh"/><method name="EnableChargeThreshold"><arg type="b" direction="in"/></method>
<property name="Percentage" type="d" access="read"/>""",
            ),
        ]
        for name in (RESOLVE, NM, UPOWER):
            self.host.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "RequestName",
                GLib.Variant("(su)", (name, 0)),
                None,
                Gio.DBusCallFlags.NONE,
                1000,
                None,
            )
        for name, path, interface, xml in definitions:
            info = Gio.DBusNodeInfo.new_for_xml(
                f'<node><interface name="{interface}">{xml}</interface></node>'
            )
            registration = self.host.register_object(
                path,
                info.interfaces[0],
                self.method,
                self.get_property,
                self.set_property,
            )
            self.registrations.append(registration)

    def get_property(self, connection, sender, path, interface, name):
        values = {
            "DNS": GLib.Variant("s", "host-dns"),
            "WirelessEnabled": GLib.Variant("b", True),
            "ConnectivityCheckEnabled": GLib.Variant("b", True),
            "OnBattery": GLib.Variant("b", True),
            "Percentage": GLib.Variant("d", 75.0),
        }
        return values[name]

    def set_property(self, connection, sender, path, interface, name, value):
        self.calls.append((sender, name, value.unpack()))
        return True

    def method(self, connection, sender, path, interface, method, body, invocation):
        self.calls.append((sender, method, body.unpack()))
        if method == "GetManagedObjects":
            invocation.return_value(
                GLib.Variant(
                    "(a{oa{sa{sv}}})",
                    (
                        {
                            "/org/freedesktop/NetworkManager": {
                                NM: {"WirelessEnabled": GLib.Variant("b", True)}
                            }
                        },
                    ),
                )
            )
        elif method == "ActivateConnection":
            active_path = body.unpack()[0]
            connection.emit_signal(
                None,
                "/org/freedesktop",
                "org.freedesktop.DBus.ObjectManager",
                "InterfacesAdded",
                GLib.Variant(
                    "(oa{sa{sv}})",
                    (active_path, {NM + ".Connection.Active": {}}),
                ),
            )
            invocation.return_value(GLib.Variant("(o)", (active_path,)))
            connection.emit_signal(
                None, "/org/freedesktop", "org.freedesktop.DBus.ObjectManager",
                "InterfacesAdded",
                GLib.Variant(
                    "(oa{sa{sv}})",
                    (active_path + "/after", {NM + ".Connection.Active": {}}),
                ),
            )
        elif method == "GetLink":
            invocation.return_value(
                GLib.Variant("(o)", ("/org/freedesktop/resolve1/link/_2",))
            )
        elif method == "GetDevices" or method == "EnumerateDevices":
            invocation.return_value(
                GLib.Variant(
                    "(ao)", (["/org/freedesktop/UPower/devices/DisplayDevice"],)
                )
            )
        elif method == "GetPermissions":
            invocation.return_value(
                GLib.Variant(
                    "(a{ss})",
                    ({"org.freedesktop.NetworkManager.network-control": "yes"},),
                )
            )
        elif method in ("GetSettings", "GetSecrets"):
            value = "fixture-secret" if method == "GetSecrets" else "fixture-profile"
            invocation.return_value(
                GLib.Variant(
                    "(a{sa{sv}})", ({"connection": {"id": GLib.Variant("s", value)}},)
                )
            )
        elif method == "ResolveRecord":
            read_fd, write_fd = os.pipe()
            os.write(write_fd, b"descriptor-payload")
            os.close(write_fd)
            fds = Gio.UnixFDList.new()
            index = fds.append(read_fd)
            os.close(read_fd)
            invocation.return_value_with_unix_fd_list(
                GLib.Variant("(h)", (index,)), fds
            )
        elif method == "ResolveService":
            descriptors = invocation.get_message().get_unix_fd_list()
            descriptor = descriptors.get(body.unpack()[0])
            try:
                value = os.read(descriptor, 100).decode()
            finally:
                os.close(descriptor)
            invocation.return_value(GLib.Variant("(s)", (value,)))
        elif method == "MadeUpRead":
            invocation.return_dbus_error("org.example.MockFailure", "fixture failure")
        else:
            invocation.return_value(GLib.Variant("()", ()))

    def test_status_properties_and_unique_destination(self):
        result = self.call(
            RESOLVE,
            "/org/freedesktop/resolve1",
            RESOLVE + ".Manager",
            "GetLink",
            GLib.Variant("(i)", (2,)),
        )
        self.assertEqual(result.unpack()[0], "/org/freedesktop/resolve1/link/_2")
        owner = self.call(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "GetNameOwner",
            GLib.Variant("(s)", (UPOWER,)),
        ).unpack()[0]
        result = self.call(
            owner,
            "/org/freedesktop/UPower",
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", (UPOWER, "OnBattery")),
        )
        self.assertTrue(result.unpack()[0])

    def test_empty_property_interface_is_forwarded(self):
        # sd-bus clients use an empty interface to request all properties.
        before = len(self.calls)
        try:
            self.call(
                RESOLVE,
                "/org/freedesktop/resolve1",
                "org.freedesktop.DBus.Properties",
                "GetAll",
                GLib.Variant("(s)", ("",)),
            )
        except GLib.Error as error:
            # GDBus's built-in mock Properties implementation can reject the
            # empty interface, but the bridge must not reject this read.
            self.assertNotIn("AccessDenied", str(error))
        self.assertEqual(len(self.calls), before)

    def test_networkmanager_object_manager_parent(self):
        result = self.call(
            NM,
            "/org/freedesktop",
            "org.freedesktop.DBus.ObjectManager",
            "GetManagedObjects",
        )
        self.assertIn("/org/freedesktop/NetworkManager", result.unpack()[0])
        owner = self.call(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "GetNameOwner",
            GLib.Variant("(s)", (NM,)),
        ).unpack()[0]
        self.call(
            owner,
            "/org/freedesktop",
            "org.freedesktop.DBus.ObjectManager",
            "GetManagedObjects",
        )

    def test_admin_mutations_and_upower_denial(self):
        self.call(
            RESOLVE,
            "/org/freedesktop/resolve1",
            RESOLVE + ".Manager",
            "SetLinkDNS",
            GLib.Variant("(ia(iay))", (2, [(2, [1, 1, 1, 1])])),
        )
        self.call(
            NM,
            "/org/freedesktop/NetworkManager",
            "org.freedesktop.DBus.Properties",
            "Set",
            GLib.Variant("(ssv)", (NM, "WirelessEnabled", GLib.Variant("b", False))),
        )
        result = self.call(
            NM,
            "/org/freedesktop/NetworkManager/Settings/1",
            NM + ".Settings.Connection",
            "GetSecrets",
            GLib.Variant("(s)", ("connection",)),
        )
        self.assertIn("fixture-secret", str(result))
        with self.assertRaisesRegex(GLib.Error, "AccessDenied"):
            self.call(
                UPOWER,
                "/org/freedesktop/UPower/devices/DisplayDevice",
                UPOWER + ".Device",
                "EnableChargeThreshold",
                GLib.Variant("(b)", (True,)),
            )

    def test_file_descriptors_and_errors(self):
        body, fds = self.guest.call_with_unix_fd_list_sync(
            RESOLVE,
            "/org/freedesktop/resolve1",
            RESOLVE + ".Manager",
            "ResolveRecord",
            None,
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
            None,
        )
        descriptor = fds.get(body.unpack()[0])
        try:
            self.assertEqual(os.read(descriptor, 100), b"descriptor-payload")
        finally:
            os.close(descriptor)
        with self.assertRaisesRegex(GLib.Error, "org.example.MockFailure"):
            self.call(
                RESOLVE, "/org/freedesktop/resolve1", RESOLVE + ".Manager", "MadeUpRead"
            )

    def test_runtime_restart_and_separate_space_permissions(self):
        with (
            mock.patch.object(system_bus, "RUNTIME_ROOT", self.directory / "runtime"),
            mock.patch.object(system_bus, "BROKER", BINARY),
            mock.patch.dict(os.environ, {"DBUS_SYSTEM_BUS_ADDRESS": self.host_address}),
        ):
            service = system_bus.SystemBusService("other", "basic")
            service.start()
            try:
                self.assertEqual(service.directory.stat().st_mode & 0o777, 0o700)
                self.assertEqual(service.socket.stat().st_mode & 0o777, 0o700)
                previous = service._process.pid
                service._process.kill()
                service._process.wait(timeout=5)
                self.wait_for(
                    lambda: service._process.pid != previous and service.socket.exists()
                )
                peer = Gio.DBusConnection.new_for_address_sync(
                    "unix:path=" + str(service.socket),
                    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT,
                    None,
                    None,
                )
                self.connections.append(peer)
                result = peer.call_sync(
                    None,
                    "/org/anatase/Spaces/SystemBridge",
                    "org.anatase.Spaces.SystemBridge1",
                    "Open",
                    GLib.Variant("(ub)", (0, False)),
                    None,
                    Gio.DBusCallFlags.NONE,
                    5000,
                    None,
                )
                self.assertFalse(result.unpack()[0])
                with self.assertRaisesRegex(GLib.Error, "AccessDenied"):
                    self.call(
                        NM,
                        "/org/freedesktop/NetworkManager",
                        NM,
                        "Enable",
                        GLib.Variant("(b)", (True,)),
                        connection=peer,
                    )
                # The original admin space is unaffected by the other broker.
                self.call(
                    NM,
                    "/org/freedesktop/NetworkManager",
                    NM,
                    "Enable",
                    GLib.Variant("(b)", (True,)),
                )
            finally:
                service.stop()
            self.assertFalse(service.socket.exists())

    def test_readonly_broker_policy_matrix(self):
        # Read-only authority comes from the space, not the guest caller's UID.
        self.broker.terminate()
        self.broker.wait(timeout=5)
        self.socket.unlink(missing_ok=True)
        self.broker = self.start_bridge("--broker", self.host_address, admin=False)
        self.wait_for(lambda: self.socket.exists())
        peer = Gio.DBusConnection.new_for_address_sync(
            "unix:path=" + str(self.socket),
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT,
            None,
            None,
        )
        self.connections.append(peer)
        result = peer.call_sync(
            None,
            "/org/anatase/Spaces/SystemBridge",
            "org.anatase.Spaces.SystemBridge1",
            "Open",
            GLib.Variant("(ub)", (1000, False)),
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        self.assertFalse(result.unpack()[0])
        cases = [
            (
                RESOLVE,
                "/org/freedesktop/resolve1",
                RESOLVE + ".Manager",
                "SetLinkDNS",
                GLib.Variant("(ia(iay))", (2, [])),
            ),
            (
                RESOLVE,
                "/org/freedesktop/resolve1",
                RESOLVE + ".Manager",
                "MadeUpRead",
                None,
            ),
            (
                NM,
                "/org/freedesktop/NetworkManager/Settings/1",
                NM + ".Settings.Connection",
                "GetSecrets",
                GLib.Variant("(s)", ("connection",)),
            ),
            (
                NM,
                "/org/freedesktop/NetworkManager/AgentManager",
                NM + ".AgentManager",
                "Register",
                GLib.Variant("(s)", ("org.example.Agent",)),
            ),
            (
                NM,
                "/org/freedesktop/NetworkManager",
                "org.freedesktop.DBus.Properties",
                "Set",
                GLib.Variant("(ssv)", (NM, "WirelessEnabled", GLib.Variant("b", True))),
            ),
            (
                "org.freedesktop.login1",
                "/org/freedesktop/login1",
                "org.freedesktop.login1.Manager",
                "ListSessions",
                None,
            ),
            (UPOWER, "/org/freedesktop/UPowerWrong", UPOWER, "EnumerateDevices", None),
        ]
        before = len(self.calls)
        for name, path, interface, method, body in cases:
            with (
                self.subTest(method=method),
                self.assertRaisesRegex(GLib.Error, "AccessDenied"),
            ):
                self.call(name, path, interface, method, body, connection=peer)
        self.assertEqual(len(self.calls), before)
        self.call(
            NM,
            "/org/freedesktop/NetworkManager/Settings/1",
            NM + ".Settings.Connection",
            "GetSettings",
            connection=peer,
        )
        self.call(
            UPOWER,
            "/org/freedesktop/UPower",
            UPOWER,
            "EnumerateDevices",
            connection=peer,
        )

    def test_incoming_descriptors_and_dynamic_introspection(self):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"from-guest")
        os.close(write_fd)
        descriptors = Gio.UnixFDList.new()
        index = descriptors.append(read_fd)
        os.close(read_fd)
        body, returned_fds = self.guest.call_with_unix_fd_list_sync(
            RESOLVE,
            "/org/freedesktop/resolve1",
            RESOLVE + ".Manager",
            "ResolveService",
            GLib.Variant("(h)", (index,)),
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            descriptors,
            None,
        )
        self.assertEqual(body.unpack()[0], "from-guest")
        xml = self.call(
            UPOWER,
            "/org/freedesktop/UPower/devices/DisplayDevice",
            "org.freedesktop.DBus.Introspectable",
            "Introspect",
        ).unpack()[0]
        self.assertIn("Percentage", xml)
        result = self.call(
            UPOWER,
            "/org/freedesktop/UPower/devices/DisplayDevice",
            "org.freedesktop.DBus.Properties",
            "GetAll",
            GLib.Variant("(s)", (UPOWER + ".Device",)),
        )
        self.assertEqual(result.unpack()[0]["Percentage"], 75.0)

    def test_rejects_oversized_payload_before_host(self):
        before = len(self.calls)
        with self.assertRaisesRegex(GLib.Error, "LimitsExceeded"):
            self.call(
                RESOLVE,
                "/org/freedesktop/resolve1",
                RESOLVE + ".Manager",
                "MadeUpRead",
                GLib.Variant("(s)", ("x" * (8 * 1024 * 1024),)),
            )
        self.assertEqual(len(self.calls), before)

    def test_broker_restart_revokes_admin(self):
        self.call(
            NM,
            "/org/freedesktop/NetworkManager",
            NM,
            "Enable",
            GLib.Variant("(b)", (True,)),
        )
        self.broker.terminate()
        self.broker.wait(timeout=5)
        self.socket.unlink(missing_ok=True)
        self.broker = self.start_bridge("--broker", self.host_address, admin=False)
        self.wait_for(lambda: self.socket.exists())
        time.sleep(0.3)
        with self.assertRaisesRegex(GLib.Error, "AccessDenied"):
            self.call(
                NM,
                "/org/freedesktop/NetworkManager",
                NM,
                "Enable",
                GLib.Variant("(b)", (True,)),
            )
        permissions = self.call(
            NM, "/org/freedesktop/NetworkManager", NM, "GetPermissions"
        ).unpack()[0]
        self.assertEqual(set(permissions.values()), {"no"})
        self.call(NM, "/org/freedesktop/NetworkManager", NM, "GetDevices")

    def test_nonroot_network_admin(self):
        self.check_nonroot_network_permissions(admin=True)

    def test_nonroot_without_network_admin(self):
        self.broker.terminate()
        self.broker.wait(timeout=5)
        self.socket.unlink(missing_ok=True)
        self.broker = self.start_bridge("--broker", self.host_address, admin=False)
        self.wait_for(lambda: self.socket.exists())
        time.sleep(0.3)
        self.check_nonroot_network_permissions(admin=False)

    def check_nonroot_network_permissions(self, admin):
        script = """import gi,sys
from gi.repository import Gio,GLib
bus=Gio.DBusConnection.new_for_address_sync(sys.argv[1], Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT|Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,None,None)
nm="org.freedesktop.NetworkManager"
path="/org/freedesktop/NetworkManager"
calls=[
 ("GetDevices",nm,path,nm,"GetDevices",None),
 ("Enable",nm,path,nm,"Enable",GLib.Variant("(b)",(True,))),
 ("Set",nm,path,"org.freedesktop.DBus.Properties","Set",GLib.Variant("(ssv)",(nm,"ConnectivityCheckEnabled",GLib.Variant("b",False)))),
 ("GetSecrets",nm,path+"/Settings/1",nm+".Settings.Connection","GetSecrets",GLib.Variant("(s)",("vpn",))),
 ("Register",nm,path+"/AgentManager",nm+".AgentManager","Register",GLib.Variant("(s)",("test-agent",))),
 ("SetLinkDNS","org.freedesktop.resolve1","/org/freedesktop/resolve1","org.freedesktop.resolve1.Manager","SetLinkDNS",GLib.Variant("(ia(iay))",(1,[]))),
]
for label,destination,path,interface,method,body in calls:
 try:
  bus.call_sync(destination,path,interface,method,body,None,Gio.DBusCallFlags.NONE,5000,None)
  print(label+":ok")
 except GLib.Error as e: print(label+":"+Gio.DBusError.get_remote_error(e))
permissions=bus.call_sync(nm,"/org/freedesktop/NetworkManager",nm,"GetPermissions",None,None,Gio.DBusCallFlags.NONE,5000,None).unpack()[0]
print("permissions:"+",".join(sorted(set(permissions.values()))))
"""
        result = subprocess.run(
            [
                "runuser",
                "-u",
                "nobody",
                "--",
                "python3",
                "-c",
                script,
                self.guest_address,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("GetDevices:ok", result.stdout)
        expected = "ok" if admin else "org.freedesktop.DBus.Error.AccessDenied"
        for method in ("Enable", "Set", "GetSecrets", "Register", "SetLinkDNS"):
            self.assertIn(f"{method}:{expected}", result.stdout)
        self.assertIn("permissions:" + ("yes" if admin else "no"), result.stdout)

    def test_signals_forward_once_and_reconnect_after_service_restart(self):
        signals = []
        self.guest.signal_subscribe(
            UPOWER,
            UPOWER,
            "DeviceAdded",
            None,
            None,
            Gio.DBusSignalFlags.NONE,
            lambda *args: signals.append(args[5].unpack()),
        )
        # A second caller must not duplicate broadcast signals.
        other = self.connect(self.guest_address)
        self.call(
            UPOWER,
            "/org/freedesktop/UPower",
            UPOWER,
            "EnumerateDevices",
            connection=other,
        )
        time.sleep(1.5)
        self.host.emit_signal(
            None,
            "/org/freedesktop/UPower",
            UPOWER,
            "DeviceAdded",
            GLib.Variant("(o)", ("/org/freedesktop/UPower/devices/DisplayDevice",)),
        )
        self.wait_for(lambda: signals)
        time.sleep(0.1)
        self.assertEqual(len(signals), 1)
        self.host.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "ReleaseName",
            GLib.Variant("(s)", (UPOWER,)),
            None,
            Gio.DBusCallFlags.NONE,
            1000,
            None,
        )
        time.sleep(0.1)
        with self.assertRaises(GLib.Error):
            self.call(UPOWER, "/org/freedesktop/UPower", UPOWER, "EnumerateDevices")
        self.host.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "RequestName",
            GLib.Variant("(su)", (UPOWER, 0)),
            None,
            Gio.DBusCallFlags.NONE,
            1000,
            None,
        )
        self.call(UPOWER, "/org/freedesktop/UPower", UPOWER, "EnumerateDevices")

    def test_activation_signals_preserve_order_on_both_sides_of_reply(self):
        received = []

        def record(connection, message, incoming, data):
            if incoming:
                body = message.get_body()
                if message.get_member() == "InterfacesAdded":
                    received.append(("object", body.unpack()[0]))
                elif (
                    message.get_message_type() == Gio.DBusMessageType.METHOD_RETURN
                    and body is not None
                    and body.get_type_string() == "(o)"
                ):
                    received.append(("reply", body.unpack()[0]))
            return message

        filter_id = self.guest.add_filter(record, None)
        subscription = self.guest.signal_subscribe(
            NM, "org.freedesktop.DBus.ObjectManager", "InterfacesAdded",
            "/org/freedesktop", None, Gio.DBusSignalFlags.NONE, lambda *args: None,
        )
        try:
            for index in range(30):
                path = f"/org/freedesktop/NetworkManager/ActiveConnection/{index}"
                self.call(
                    NM, "/org/freedesktop/NetworkManager", NM,
                    "ActivateConnection", GLib.Variant("(ooo)", (path, "/", "/")),
                )
                self.wait_for(lambda: len(received) >= (index + 1) * 3)
                self.assertEqual(
                    received[-3:],
                    [("object", path), ("reply", path), ("object", path + "/after")],
                )
            time.sleep(0.1)
            self.assertEqual(len(received), 90)
        finally:
            self.guest.signal_unsubscribe(subscription)
            self.guest.remove_filter(filter_id)

    def test_unicast_signal_stays_with_its_guest_caller(self):
        first = self.connect(self.guest_address)
        second = self.connect(self.guest_address)
        received = [[], []]
        for index, connection in enumerate((first, second)):
            connection.signal_subscribe(
                NM,
                NM,
                "StateChanged",
                None,
                None,
                Gio.DBusSignalFlags.NONE,
                lambda *args, target=received[index]: target.append(args[5].unpack()),
            )
        self.call(
            NM, "/org/freedesktop/NetworkManager", NM, "GetDevices", connection=first
        )
        first_host_sender = self.calls[-1][0]
        self.call(
            NM, "/org/freedesktop/NetworkManager", NM, "GetDevices", connection=second
        )
        self.assertNotEqual(first_host_sender, self.calls[-1][0])
        time.sleep(0.1)
        self.host.emit_signal(
            first_host_sender,
            "/org/freedesktop/NetworkManager",
            NM,
            "StateChanged",
            GLib.Variant("(u)", (70,)),
        )
        self.wait_for(lambda: received[0])
        time.sleep(0.1)
        self.assertEqual(received, [[(70,)], []])

    def test_agent_callback_and_caller_disconnect(self):
        agent = self.connect(self.guest_address)
        xml = '<node><interface name="org.freedesktop.NetworkManager.SecretAgent"><method name="GetSecrets"><arg type="s" direction="out"/></method></interface></node>'
        info = Gio.DBusNodeInfo.new_for_xml(xml)
        agent.register_object(
            "/org/freedesktop/NetworkManager/SecretAgent",
            info.interfaces[0],
            lambda bus, sender, path, interface, method, args, invocation: invocation.return_value(
                GLib.Variant("(s)", ("callback-secret",))
            ),
            None,
            None,
        )
        self.call(
            NM,
            "/org/freedesktop/NetworkManager/AgentManager",
            NM + ".AgentManager",
            "Register",
            GLib.Variant("(s)", ("org.example.Agent",)),
            connection=agent,
        )
        sender = next(item[0] for item in reversed(self.calls) if item[1] == "Register")
        reply = self.host.call_sync(
            sender,
            "/org/freedesktop/NetworkManager/SecretAgent",
            NM + ".SecretAgent",
            "GetSecrets",
            None,
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        self.assertEqual(reply.unpack(), ("callback-secret",))
        outsider = self.connect(self.host_address)
        with self.assertRaisesRegex(GLib.Error, "AccessDenied"):
            outsider.call_sync(
                sender,
                "/org/freedesktop/NetworkManager/SecretAgent",
                NM + ".SecretAgent",
                "GetSecrets",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        agent.close_sync(None)
        self.wait_for(lambda: not self.has_owner(self.host, sender))


if __name__ == "__main__":
    unittest.main()
