"""Exercise the real MCP executable and host service on private desktop peers."""
from __future__ import annotations

import base64
import array
import io
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("gio-unix-2.0", "atspi-2", "libei-1.0", "xkbcommon", "gdk-pixbuf-2.0", "libeis-1.0")


class Client:
    def __init__(self, binary, environment=None):
        self.process = subprocess.Popen([str(binary), "mcp"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
        self.serial = 0
        self.call("initialize", {"protocolVersion": "2025-11-25"})

    def call(self, method, parameters, timeout=12):
        self.serial += 1
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.serial,
            "method": method, "params": parameters}) + "\n")
        self.process.stdin.flush()
        if not select.select([self.process.stdout], [], [], timeout)[0]:
            raise AssertionError("scu response timed out")
        line = self.process.stdout.readline()
        if not line:
            raise AssertionError(self.process.stderr.read())
        result = json.loads(line)
        assert result["id"] == self.serial, result
        return result

    def tool(self, name, **arguments):
        return self.call("tools/call", {"name": name, "arguments": arguments})["result"]

    def windows(self):
        response = self.tool("status")
        assert not response["isError"], response
        return [{**window, "pid": app["pid"], "app_name": app["app_name"]}
                for app in response["structuredContent"]["apps"] for window in app["windows"]]

    def window_state(self, **arguments):
        response = self.tool("computer", observe={"kind": "window", **arguments})
        assert not response["isError"], response
        return response["structuredContent"]["observation"]

    def close(self):
        if not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()
        self.process.stderr.close()


class ScuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("cc") or not shutil.which("pkg-config") or not shutil.which("dbus-daemon"):
            raise unittest.SkipTest("scu tests need a compiler, development headers and D-Bus")
        flags = subprocess.run(["pkg-config", "--cflags", *PACKAGES], capture_output=True, text=True)
        if flags.returncode:
            raise unittest.SkipTest("scu development headers unavailable")
        (ROOT / ".build").mkdir(exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix="scu-tests-", dir=ROOT / ".build")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        cls.socket_path = cls.directory / "accessibility"
        cls.computer_path = cls.directory / "computer"
        cls.binary = cls.directory / "scu"
        cls.host_binary = cls.directory / "scu-host"
        cls.backend = cls.directory / "backend"
        common = ["cc", "-O1", "-g", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
            "-DYYJSON_READER_DEPTH_LIMIT=64",
            f'-DSCU_SCREEN_QUERY="{ROOT / "tests/scu_display_fixture.py"}"',
            f'-DSCU_VIRTUAL_MONITOR="{ROOT / "tests/scu_display_fixture.py"}"',
            f'-DSCU_COMPUTER_SOCKET="{cls.computer_path}"']
        libraries = shlex.split(subprocess.check_output(["pkg-config", "--libs", "gio-unix-2.0"], text=True))
        for source, binary in ((ROOT / "native/scu.c", cls.binary), (ROOT / "native/scu_host.c", cls.host_binary),
                               (ROOT / "tests/scu_backend_fixture.c", cls.backend)):
            headers = shlex.split(flags.stdout)
            if binary == cls.binary:
                headers = shlex.split(subprocess.check_output(["pkg-config", "--cflags", "glib-2.0"], text=True))
                link = [str(ROOT / "native/vendor/yyjson.c"),
                        *shlex.split(subprocess.check_output(["pkg-config", "--libs", "glib-2.0"], text=True))]
            else:
                link = [str(ROOT / "native/vendor/yyjson.c"), *libraries]
            extra = (["-Wl,--wrap=__libc_start_main"] if binary == cls.binary else ["-ldl"])
            if binary == cls.backend:
                extra += ["-leis", "-lxkbcommon"]
            build = subprocess.run([*common, *headers, str(source), *link, *extra, "-o", str(binary)],
                capture_output=True, text=True)
            if build.returncode:
                raise AssertionError(build.stderr)

    def setUp(self):
        self.socket_path.unlink(missing_ok=True)
        self.computer_path.unlink(missing_ok=True)
        self.environment = os.environ.copy()
        for key in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "DISPLAY", "SPACES_NAME", "AT_SPI_BUS_ADDRESS", "LD_LIBRARY_PATH"):
            self.environment.pop(key, None)
        self.state = self.directory / "kwin-state.json"
        self.control = self.directory / "kwin-control.json"
        self.config = {"atspi_address": f"unix:path={self.socket_path}"}
        self.control.write_text(json.dumps(self.config))
        self.display_state = self.directory / "display-state.json"
        self.display_state.unlink(missing_ok=True)

    def child(self, command, **kwargs):
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs)
        self.addCleanup(self.stop, process)
        return process

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
            if stream and not stream.closed:
                stream.close()

    def ready(self, process):
        self.assertTrue(select.select([process.stdout], [], [], 5)[0], "fixture did not start")
        line = process.stdout.readline()
        self.assertEqual(line.strip(), "ready", process.stderr.read() if process.poll() is not None else line)

    def bus(self, path=None):
        command = ["dbus-daemon", "--session", "--nofork", "--print-address=1"]
        if path:
            command.append(f"--address=unix:path={path}")
        process = self.child(command)
        self.assertTrue(select.select([process.stdout], [], [], 5)[0])
        return process, process.stdout.readline().strip()

    def configure(self, **values):
        self.config.update(values)
        temporary = self.control.with_suffix(".next")
        temporary.write_text(json.dumps(self.config))
        temporary.replace(self.control)

    def start_accessibility(self, two_windows=False, duplicates=False, selection=False, unstable=False,
                            unavailable=False, activation=None, unsupported=False, empty=False,
                            keyboard=False, refuse_focus=False, hidden=False,
                            lose_focus=False, readonly=False, hidden_container=False):
        self.atspi_bus, address = self.bus(self.socket_path)
        self.atspi_state = self.directory / "atspi-state.json"
        command = ["/usr/bin/python3", str(ROOT / "tests/scu_atspi_fixture.py"), address, str(self.atspi_state)]
        if two_windows:
            command.append("--two-windows")
        if duplicates:
            command.append("--duplicate-windows")
        if selection:
            command.append("--selection")
        if unstable:
            command.append("--selection-unstable")
        if unavailable:
            command.append("--registry-unavailable")
        if activation is not None:
            command.append(f"--activation-delay={activation}")
        if unsupported:
            command.append("--unsupported-attributes")
        if empty:
            command.append("--empty-window")
        if keyboard:
            command.append("--keyboard-entry")
        if refuse_focus:
            command.append("--refuse-focus")
        if hidden:
            command.append("--hidden-button")
        if hidden_container:
            command.append("--hidden-container")
        if lose_focus:
            command.append("--lose-focus")
        if readonly:
            command.append("--readonly-entry")
        self.atspi_state.with_suffix(".actions.json").unlink(missing_ok=True)
        self.atspi = self.child(command)
        self.ready(self.atspi)
        self.configure(app_pid=self.atspi.pid, two_windows=two_windows, atspi_address=address)

    def start_host(self, accessibility=True, **config):
        if accessibility and not hasattr(self, "atspi"):
            self.start_accessibility()
        self.configure(**config)
        self.kwin_bus, self.address = self.bus()
        self.kwin = self.child(["/usr/bin/python3", str(ROOT / "tests/scu_kwin_fixture.py"),
            self.address, str(self.state), str(self.control), str(self.backend)])
        self.ready(self.kwin)
        self.start_service()

    def start_service(self):
        ready_read, ready_write = os.pipe()
        self.host = self.child([str(self.host_binary), "--host-service", str(self.computer_path), str(ready_write)],
            pass_fds=(ready_write,), env={**self.environment, "DBUS_SESSION_BUS_ADDRESS": self.address})
        os.close(ready_write)
        try:
            self.assertTrue(select.select([ready_read], [], [], 5)[0], "host service did not start")
            self.assertEqual(os.read(ready_read, 1), b"1",
                self.host.stderr.read() if self.host.poll() is not None else "host ready failure")
        finally:
            os.close(ready_read)

    def client(self):
        client = Client(self.binary, self.environment)
        self.addCleanup(client.close)
        return client

    def start_shared(self, **config):
        self.start_host(**config)
        self.stop(self.host)
        self.login_state = self.directory / "login-state.json"
        self.login = self.child(["/usr/bin/python3", str(ROOT / "tests/scu_login_fixture.py"),
                                 self.address, str(self.control), str(self.login_state)])
        self.ready(self.login)
        path = self.directory / "shared-control"
        path.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        listener.listen(32)
        self.addCleanup(listener.close)
        self.shared_listener = listener
        self.space_listeners = {}
        self.shared_shim = """import os, sys
fd = int(sys.argv[2])
os.dup2(fd, 3, inheritable=True)
if fd != 3: os.close(fd)
os.environ.update(LISTEN_PID=str(os.getpid()), LISTEN_FDS='1')
os.execve(sys.argv[1], [sys.argv[1], '--user-service'], os.environ)
"""
        self.shared_environment = {**self.environment, "DBUS_SESSION_BUS_ADDRESS": self.address,
                       "DBUS_SYSTEM_BUS_ADDRESS": self.address,
                       "SCU_FIXTURE_CONTROL": str(self.control), "SCU_DISPLAY_STATE": str(self.display_state)}
        self.shared_path = path
        self.start_shared_service()
        self.lease = self.register_space(self.computer_path)

    def start_shared_service(self):
        listener = self.shared_listener
        self.host = self.child(["/usr/bin/python3", "-c", self.shared_shim,
                               str(self.host_binary), str(listener.fileno())],
                               pass_fds=(listener.fileno(),), env=self.shared_environment)

    def register_space(self, path):
        listener = self.space_listeners.get(path)
        if listener is None:
            path.unlink(missing_ok=True)
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(path))
            path.chmod(0o600)
            listener.listen(32)
            self.space_listeners[path] = listener
            self.addCleanup(listener.close)
        control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(control.close)
        control.settimeout(5)
        control.connect(str(self.shared_path))
        control.sendmsg([json.dumps({"session": "fixture", "wayland": "fixture-wayland"}).encode() + b"\n"],
                        [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [listener.fileno()]))])
        result = json.loads(control.recv(8192))
        self.assertTrue(result.get("ready"), result)
        self.assertEqual(result["pid"], self.host.pid)
        return control

    def login_call(self, method, *arguments):
        command = ["gdbus", "call", "--address", self.address, "--dest", "org.freedesktop.login1",
                   "--object-path", "/org/freedesktop/login1/session/fixture",
                   "--method", "org.freedesktop.login1.Session." + method, *arguments]
        subprocess.run(command, check=True, capture_output=True, timeout=5)

    def wait_until(self, predicate, timeout=5):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(.02)
        self.fail("shared host state did not converge")

    def test_shared_registration_revokes_only_its_space_and_exits_after_last(self):
        self.start_shared()
        other_path = self.directory / "other-space"
        other = self.register_space(other_path)
        first = self.client()
        self.status(first)
        peer = socket.socket(socket.AF_UNIX)
        self.addCleanup(peer.close)
        peer.settimeout(5)
        peer.connect(str(other_path))
        request = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "status", "arguments": {}}}
        peer.sendall(json.dumps(request).encode() + b"\n")
        response = b""
        while not response.endswith(b"\n"):
            response += peer.recv(65536)
        self.assertFalse(json.loads(response)["result"]["isError"])
        self.lease.sendall(b"R")
        self.assertTrue(json.loads(self.lease.recv(8192))["released"])
        self.assertTrue(first.tool("status")["isError"])
        peer.sendall(json.dumps(request).encode() + b"\n")
        response = b""
        while not response.endswith(b"\n"):
            response += peer.recv(65536)
        self.assertFalse(json.loads(response)["result"]["isError"])
        self.assertEqual(json.loads(self.state.read_text())["connections"], 1)
        atspi = json.loads(self.atspi_state.read_text())
        self.assertEqual(atspi["registrations"], 1)
        self.assertEqual(len(atspi["listeners"]), 1)
        other.sendall(b"R")
        self.assertTrue(json.loads(other.recv(8192))["released"])
        self.host.wait(timeout=5)
        self.wait_until(lambda: not json.loads(self.atspi_state.read_text())["listeners"])

    def test_shared_unlock_is_visual_only_and_accessibility_stays_locked(self):
        self.start_shared()
        client = self.client()
        tree = self.snapshot(client)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        self.login_call("SetLocked", "true")
        result = client.tool("computer", steps=[{"tool": "click", "args": {
            "element_token": button["element_token"]}}], observe=False)
        self.assertFalse(result["isError"], result)
        self.assertTrue(json.loads(self.login_state.read_text())["locked"])
        self.assertEqual(json.loads(self.login_state.read_text())["unlocks"], 0)
        self.status(client)
        self.assertFalse(json.loads(self.login_state.read_text())["locked"])
        self.login_call("SetLocked", "true")
        result = client.tool("computer", steps=[{"tool": "press_keys", "args": {"keys": ["a"]}}], observe=False)
        self.assertFalse(result["isError"], result)
        self.assertEqual(json.loads(self.login_state.read_text())["unlocks"], 2)

    def test_shared_duplicate_ids_cancel_only_the_originating_connection(self):
        self.start_shared()
        peers = [socket.socket(socket.AF_UNIX) for _ in range(2)]
        for peer in peers:
            self.addCleanup(peer.close)
            peer.settimeout(5)
            peer.connect(str(self.computer_path))
        request = {"jsonrpc": "2.0", "id": 77, "method": "tools/call", "params": {
            "name": "computer", "arguments": {"steps": [{"tool": "delay_ms", "args": {"ms": 600}}], "observe": False}}}
        peers[0].sendall(json.dumps(request).encode() + b"\n")
        time.sleep(.05)
        peers[1].sendall(json.dumps(request).encode() + b"\n")
        peers[1].sendall(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                    "params": {"requestId": 77}}).encode() + b"\n")
        response = b""
        while not response.endswith(b"\n"):
            response += peers[0].recv(65536)
        result = json.loads(response)
        self.assertEqual(result["id"], 77)
        self.assertFalse(result["result"]["isError"], result)
        self.assertFalse(select.select([peers[1]], [], [], .3)[0], "another client's ID was cancelled or responded late")

    def test_controller_registers_a_listener_and_closes_only_its_lease(self):
        from types import SimpleNamespace
        from unittest import mock
        from spaces import session
        self.start_shared()
        user = SimpleNamespace(uid=os.getuid(), gid=os.getgid())
        plan = session.DesktopPlan("fixture", (
            session.DesktopBind("/guest/wayland", Path("fixture-wayland"), 1, 2),),
            {"WAYLAND_DISPLAY": "/guest/wayland"})
        connect = socket.socket.connect
        def redirected(sock, address):
            if address == f"/run/user/{user.uid}/spaces-scu/control":
                address = str(self.shared_path)
            return connect(sock, address)
        with mock.patch.object(session, "RUNTIME_ROOT", self.directory / "registration"), \
             mock.patch.object(session, "COMPUTER_SERVICE", str(self.host_binary)), \
             mock.patch.object(session, "_computer_unit") as unit, \
             mock.patch.object(socket.socket, "connect", redirected):
            lease, binding = session.start_computer_service("other", user, plan)
            self.addCleanup(lease.close)
            self.assertTrue(lease.alive())
            self.assertTrue(binding.source.is_socket())
            self.assertEqual(binding.source.stat().st_mode & 0o777, 0o600)
            self.assertIsNone(lease.control)
            unit.assert_not_called()
            lease.close()
            self.assertFalse(lease.alive())
            self.assertIsNone(self.host.poll())
            self.status(self.client())

    def test_shared_unlock_denial_and_timeout_submit_no_input(self):
        self.start_shared(locked=True, deny_unlock=True)
        client = self.client()
        result = client.tool("computer", steps=[{"tool": "press_keys", "args": {"keys": ["a"]}}], observe=False)
        self.assertTrue(result["isError"], result)
        self.assertEqual(result["structuredContent"]["failed"]["code"], "desktop_unlock_failed")
        self.assertEqual(json.loads(self.state.read_text())["connections"], 0)
        self.configure(deny_unlock=False, hold_unlock=True)
        start = time.monotonic()
        result = client.tool("status")
        self.assertTrue(result["isError"], result)
        self.assertLess(time.monotonic() - start, 6)
        self.assertEqual(json.loads(self.state.read_text())["captures"], 0)

    def test_socket_demand_starts_one_host_and_reactivates_without_remounting(self):
        from types import SimpleNamespace
        from unittest import mock
        from spaces import session
        self.start_shared()
        self.lease.sendall(b"R")
        self.assertTrue(json.loads(self.lease.recv(8192))["released"])
        self.host.wait(timeout=5)
        user = SimpleNamespace(uid=os.getuid(), gid=os.getgid())
        plan = session.DesktopPlan("fixture", (
            session.DesktopBind("/guest/wayland", Path("fixture-wayland"), 1, 2),),
            {"WAYLAND_DISPLAY": "/guest/wayland"})
        connect = socket.socket.connect
        def redirected(sock, address):
            if address == f"/run/user/{user.uid}/spaces-scu/control":
                address = str(self.shared_path)
            return connect(sock, address)
        starts = []
        def start_unit(_user, action, unit):
            self.assertEqual((action, unit), ("start", "spaces-scu-host.socket"))
            if self.host.poll() is not None:
                self.start_shared_service()
                starts.append(self.host.pid)
        def peer(path):
            result = socket.socket(socket.AF_UNIX)
            result.settimeout(5)
            self.addCleanup(result.close)
            result.connect(str(path))
            return result
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "status", "arguments": {}}}).encode() + b"\n"
        def receive(sock):
            data = b""
            while not data.endswith(b"\n"):
                part = sock.recv(65536)
                self.assertTrue(part, "host disconnected before responding")
                data += part
            self.assertFalse(json.loads(data)["result"]["isError"])
        with mock.patch.object(session, "RUNTIME_ROOT", self.directory / "demand"), \
             mock.patch.object(session, "COMPUTER_SERVICE", str(self.host_binary)), \
             mock.patch.object(session, "_computer_unit", side_effect=start_unit) as unit, \
             mock.patch.object(socket.socket, "connect", redirected):
            leases = [session.start_computer_service(name, user, plan)[0] for name in ("one", "two")]
            for lease in leases:
                self.addCleanup(lease.close)
                self.assertTrue(lease.alive())
                self.assertIsNone(lease.control)
            unit.assert_not_called()
            identity = leases[0].socket_path.stat().st_ino
            peers = [peer(lease.socket_path) for lease in leases]
            unit.side_effect = RuntimeError("temporary activation failure")
            with self.assertLogs(session.logger, level="WARNING"):
                self.assertTrue(leases[0].alive())
            self.assertIsNone(leases[0].control)
            self.assertEqual(leases[0].socket_path.stat().st_ino, identity)
            unit.side_effect = start_unit
            for connection, lease in zip(peers, leases):
                connection.sendall(request)
                self.assertTrue(lease.alive())
                receive(connection)
            self.assertEqual(len(starts), 1, "each Space started its own host")
            self.assertEqual(json.loads(self.atspi_state.read_text())["registrations"], 1)
            peers[0].close()
            self.wait_until(lambda: bool(select.select([leases[0].control], [], [], 0)[0]))
            self.assertTrue(leases[0].alive())
            self.assertIsNone(leases[0].control)
            self.assertIsNone(self.host.poll(), "an idle Space stopped another Space's client")
            self.assertEqual(len(json.loads(self.atspi_state.read_text())["listeners"]), 1)
            peers[1].sendall(request)
            receive(peers[1])
            peers[1].close()
            self.host.wait(timeout=5)
            self.wait_until(lambda: not json.loads(self.atspi_state.read_text())["listeners"])
            self.assertTrue(leases[1].alive())
            self.assertIsNone(leases[1].control)
            self.assertEqual(leases[0].socket_path.stat().st_ino, identity)
            fresh = peer(leases[0].socket_path)
            fresh.sendall(request)
            self.assertTrue(leases[0].alive())
            receive(fresh)
            self.assertEqual(len(starts), 2)
            self.assertEqual(json.loads(self.atspi_state.read_text())["registrations"], 2)
            self.assertEqual(leases[0].socket_path.stat().st_ino, identity)
            leases[0].close()
            self.assertEqual(fresh.recv(1), b"", "revocation left a live client")
            self.assertFalse(leases[0].alive())

    def test_shared_session_end_retires_all_registrations(self):
        self.start_shared()
        self.status(self.client())
        self.login_call("Deactivate")
        self.host.wait(timeout=5)

    def test_shared_headless_helper_has_client_lifetime_and_returns_to_physical(self):
        self.start_shared(headless=True)
        first, second = self.client(), self.client()
        result = self.status(first)
        self.assertEqual(self.image(result).size, (1280, 720))
        helper = json.loads(self.display_state.read_text())["pid"]
        self.status(second)
        self.assertEqual(json.loads(self.display_state.read_text())["pid"], helper)
        first.close()
        self.assertTrue(self.display_state.exists())
        second.close()
        self.wait_until(lambda: not self.display_state.exists())
        self.host.wait(timeout=5)
        self.start_shared_service()
        self.lease = self.register_space(self.computer_path)
        third = self.client()
        self.status(third)
        self.configure(headless=False)
        self.wait_until(lambda: not self.display_state.exists())
        self.status(third)

    def test_shared_failing_display_helper_does_not_block_accessibility(self):
        self.start_shared()
        client = self.client()
        tree = self.snapshot(client)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        self.configure(headless=True, helper_fail=True)
        result = client.tool("status")
        self.assertTrue(result["isError"], result)
        result = client.tool("computer", steps=[{"tool": "click", "args": {
            "element_token": button["element_token"]}}], observe=False)
        self.assertFalse(result["isError"], result)

    def test_shared_virtual_output_without_enabled_property_is_usable(self):
        self.start_shared(headless=True, omit_enabled=True)
        client = self.client()
        result = self.status(client)
        self.assertEqual(self.image(result).size, (1280, 720))
        self.assertTrue(self.display_state.exists())

    def status(self, client):
        result = client.tool("status")
        self.assertFalse(result["isError"], result)
        return result

    def snapshot(self, client):
        status = self.status(client)["structuredContent"]
        app = status["apps"][0]
        window = app["windows"][0]
        return client.window_state(pid=app["pid"], window_id=window["window_id"], tree_format="elements")

    @staticmethod
    def image(result):
        block = next(block for block in result["content"] if block["type"] == "image")
        return Image.open(io.BytesIO(base64.b64decode(block["data"]))).convert("RGB")

    def test_offline_mcp_recovers_without_reinitializing(self):
        client = self.client()
        self.assertIn("tools", client.call("tools/list", {})["result"])
        self.assertTrue(client.tool("status")["isError"])
        self.assertIn("result", client.call("ping", {}))
        self.start_host()
        metadata = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {}}
        result = client.call("tools/call", {"name": "status", "arguments": {}, "_meta": metadata})["result"]
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["resultType"], "complete")
        self.assertIn("io.modelcontextprotocol/serverInfo", result["_meta"])
        self.status(client)

    def test_guest_has_no_desktop_dependencies_or_accessibility_bus(self):
        self.start_host()
        # These invalid libraries and address affect only the guest frontend.
        library_dir = self.directory / "guest-without-desktop"
        library_dir.mkdir(exist_ok=True)
        for library in ("libatspi.so.0", "libei.so.1", "libxkbcommon.so.0", "libgdk_pixbuf-2.0.so.0"):
            (library_dir / library).write_bytes(b"invalid ELF")
        self.environment["LD_LIBRARY_PATH"] = str(library_dir)
        self.environment["AT_SPI_BUS_ADDRESS"] = "unix:path=/nonexistent/guest-a11y"
        client = self.client()
        tree = self.snapshot(client)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        result = client.tool("computer", steps=[{"tool": "click", "args": {
            "element_token": button["element_token"]}}], observe=False)
        self.assertFalse(result["isError"], result)
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 1)

    def test_clients_share_engine_handles_and_one_accessibility_helper(self):
        self.start_host()
        first, second = self.client(), self.client()
        one = self.status(first)["structuredContent"]
        # The same current capture is valid from another Space/client.
        result = second.tool("computer", id=one["canvas"]["id"],
            steps=[{"tool": "click", "args": {"x": 40, "y": 40}}], observe=False)
        self.assertFalse(result["isError"], result)
        tree = self.snapshot(first)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        result = second.tool("computer", steps=[{"tool": "click", "args": {
            "element_token": button["element_token"]}}], observe=False)
        self.assertFalse(result["isError"], result)
        children = Path(f"/proc/{self.host.pid}/task/{self.host.pid}/children")
        self.assertEqual(len(children.read_text().split()), 1, "only the existing AT-SPI helper should be a child")
        first.close()
        self.status(second)
        self.assertEqual(json.loads(self.state.read_text())["connections"], 1)
        self.assertIsNone(self.host.poll())

    def test_accessibility_consumer_has_shared_client_lifetime_and_dispatches_idle(self):
        self.start_host()
        first, second = self.client(), self.client()
        state = lambda: json.loads(self.atspi_state.read_text())
        self.assertEqual(state()["registrations"], 0, "schema discovery activated accessibility")
        self.status(first)
        self.status(second)
        self.assertEqual(state()["registrations"], 1)
        self.assertEqual(list(state()["listeners"].values()), [["object:children-changed"]])
        # The fixture sends an event and a Peer.Ping while no request is running.
        self.wait_until(lambda: state()["idle_pings"] == 1)
        self.assertEqual(state()["events"], 1)
        first.close()
        self.status(second)
        self.assertEqual(state()["registrations"], 1)
        second.close()
        self.wait_until(lambda: not state()["listeners"])
        self.assertIsNone(self.host.poll(), "the test service should still accept clients")
        third = self.client()
        self.status(third)
        self.assertEqual(state()["registrations"], 2)

    def test_frame_attributes_activate_delayed_null_placeholder_children(self):
        self.start_accessibility(activation=250)
        self.start_host()
        client = self.client()
        started = time.monotonic()
        tree = self.snapshot(client)
        self.assertIn("Fixture button", [row["label"] for row in tree["elements"]])
        self.assertLess(time.monotonic() - started, 3)
        state = json.loads(self.atspi_state.read_text())
        self.assertEqual(state["attribute_queries"], 1)
        previous = tree["snapshot_id"]
        self.assertNotEqual(self.snapshot(client)["snapshot_id"], previous)
        self.assertEqual(json.loads(self.atspi_state.read_text())["attribute_queries"], 2)

    def test_unsupported_frame_attributes_preserve_existing_tree(self):
        self.start_accessibility(unsupported=True)
        self.start_host()
        started = time.monotonic()
        tree = self.snapshot(self.client())
        self.assertIn("Fixture button", [row["label"] for row in tree["elements"]])
        self.assertLess(time.monotonic() - started, 1.5)

    def test_empty_accessible_frame_has_bounded_preparation(self):
        self.start_accessibility(empty=True)
        self.start_host()
        client = self.client()
        self.status(client)
        started = time.monotonic()
        tree = self.snapshot(client)
        self.assertEqual(len(tree["elements"]), 1)
        self.assertGreaterEqual(time.monotonic() - started, 1.9)
        self.assertLess(time.monotonic() - started, 3)

    def test_frame_preparation_cancellation_retires_helper_and_recovers(self):
        self.start_accessibility(activation=1500)
        self.start_host()
        client = self.client()
        window = client.windows()[0]
        client.serial += 1
        client.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": client.serial,
            "method": "tools/call", "params": {"name": "computer", "arguments": {
                "observe": {"kind": "window", "pid": window["pid"],
                            "window_id": window["window_id"], "tree_format": "elements"}}}}) + "\n")
        client.process.stdin.flush()
        self.wait_until(lambda: json.loads(self.atspi_state.read_text())["attribute_queries"] > 0)
        client.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
            "params": {"requestId": client.serial}}) + "\n")
        client.process.stdin.flush()
        self.wait_until(lambda: not json.loads(self.atspi_state.read_text())["listeners"])
        self.assertFalse(select.select([client.process.stdout], [], [], .1)[0])
        self.status(client)
        self.assertEqual(json.loads(self.atspi_state.read_text())["registrations"], 2)

    def test_accessibility_worker_eof_deregisters_consumer(self):
        self.start_host()
        worker = self.child([str(self.host_binary), "--atspi-worker", "1"],
            stdin=subprocess.PIPE, env={**self.environment, "DBUS_SESSION_BUS_ADDRESS": self.address})
        worker.stdin.write(json.dumps({"tool": "check_ready", "args": {}}) + "\n")
        worker.stdin.flush()
        self.assertTrue(select.select([worker.stdout], [], [], 5)[0])
        self.assertIn("output", json.loads(worker.stdout.readline()))
        worker.stdin.close()
        worker.wait(timeout=5)
        self.wait_until(lambda: not json.loads(self.atspi_state.read_text())["listeners"])
        self.assertEqual(json.loads(self.atspi_state.read_text())["deregistrations"], 1)

    def test_host_connection_limit_and_shutdown_close_clients(self):
        self.start_host()
        peers = []
        for _ in range(33):
            peer = socket.socket(socket.AF_UNIX)
            self.addCleanup(peer.close)
            peer.settimeout(3)
            peer.connect(str(self.computer_path))
            peers.append(peer)
        self.assertEqual(peers[-1].recv(1), b"")
        children = Path(f"/proc/{self.host.pid}/task/{self.host.pid}/children")
        self.assertEqual(children.read_text().strip(), "", "clients must not launch host workers")
        self.stop(self.host)
        for peer in peers:
            self.assertEqual(peer.recv(1), b"")
        self.assertFalse(self.computer_path.exists())

    def test_large_image_crosses_relay_buffers(self):
        self.start_host(large_capture=True)
        result = self.status(self.client())
        self.assertEqual(self.image(result).size, (512, 512))
        block = next(item for item in result["content"] if item["type"] == "image")
        self.assertGreater(len(block["data"]), 65536)

    def test_unsafe_socket_permissions_fail_without_ending_mcp(self):
        self.start_host()
        self.computer_path.chmod(0o666)
        client = self.client()
        self.assertEqual(client.tool("status")["structuredContent"]["code"], "accessibility_permission_required")
        self.assertIn("result", client.call("ping", {}))
        self.computer_path.chmod(0o600)
        self.status(client)

    def test_catalog_and_permission_errors(self):
        client = self.client()
        tools = client.call("tools/list", {})["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, {"computer", "status"})
        schema = next(tool["inputSchema"] for tool in tools if tool["name"] == "computer")
        self.assertEqual(set(schema["properties"]), {"steps", "observe", "id"})
        self.assertEqual(schema["properties"]["id"]["type"], "integer")
        self.assertEqual(schema["properties"]["id"]["minimum"], 1)
        advertised = {step["properties"]["tool"]["enum"][0]
                      for step in schema["properties"]["steps"]["items"]["anyOf"]}
        self.assertEqual(advertised, {"click", "move_cursor", "drag", "scroll",
                                     "type_text", "set_value", "press_keys", "delay_ms"})
        delay = next(step["properties"]["args"] for step in schema["properties"]["steps"]["items"]["anyOf"]
                     if step["properties"]["tool"]["enum"] == ["delay_ms"])
        self.assertEqual(delay["required"], ["ms"])
        self.assertEqual(set(delay["properties"]), {"ms"})
        for field in ('"target"', '"display_id"', '"delivery_mode"', '"session"', '"capture_id"'):
            self.assertNotIn(field, json.dumps(schema))
        for name, args in (("status", {}), ("computer", {}),
                           ("computer", {"steps": [{"tool": "press_keys", "args": {"keys": ["a"]}}]})):
            result = client.tool(name, **args)
            self.assertTrue(result["isError"], result)
            self.assertEqual(result["structuredContent"]["code"], "accessibility_permission_required")
            self.assertIn("spaces configure <space>", result["structuredContent"]["hint"])

    def test_status_image_and_kwin_window_coordinates(self):
        self.start_host(two_windows=True)
        result = self.status(self.client())
        data = result["structuredContent"]
        self.assertEqual(set(data), {"canvas", "apps"})
        self.assertEqual(self.image(result).size, (160, 120))
        canvas = data["canvas"]
        self.assertEqual(set(canvas), {"width", "height", "id"})
        self.assertIs(type(canvas["id"]), int)
        self.assertGreater(canvas["id"], 0)
        self.assertEqual((canvas["width"], canvas["height"]), (160, 120))
        self.assertEqual(len(data["apps"]), 1)
        windows = data["apps"][0]["windows"]
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0]["bounds"], {"x": 20, "y": 20, "width": 80, "height": 60})
        self.assertEqual(windows[1]["bounds"]["x"], 30)
        self.assertEqual(self.computer_path.stat().st_mode & 0o777, 0o600)

    def test_semantics_use_unified_element_frames_and_tokens(self):
        self.start_accessibility()
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        self.assertIn("id", tree)
        self.assertIs(type(tree["id"]), int)
        rows = {row["label"]: row for row in tree["elements"]}
        self.assertEqual(rows["Fixture button"]["actions"], ["click"])
        self.assertEqual(rows["Fixture entry"]["actions"], ["type_text", "set_value"])
        self.assertEqual(rows["Fixture entry"]["text_input"], "accessibility")
        self.assertEqual(rows["Fixture password"]["text_input"], "unavailable")
        self.assertEqual(rows["Fixture button"]["frame"], {"x": 30, "y": 30, "width": 20, "height": 10})
        self.assertNotIn("value", rows["Fixture password"])
        captures = json.loads(self.state.read_text())["captures"]
        result = client.tool("computer", steps=[
            {"tool": "click", "args": {"element_token": rows["Fixture button"]["element_token"]}},
            {"tool": "set_value", "args": {"element_token": rows["Fixture entry"]["element_token"], "value": "Héllo 世界"}},
            {"tool": "type_text", "args": {"element_token": rows["Fixture entry"]["element_token"], "text": "!"}},
            {"tool": "delay_ms", "args": {"ms": 50}},
        ])
        self.assertFalse(result["isError"], result)
        self.assertEqual(set(result["structuredContent"]), {"completed", "status"})
        self.assertEqual(result["structuredContent"]["status"], "ok")
        self.assertNotIn("observation", result["structuredContent"])
        self.assertEqual(json.loads(self.state.read_text())["captures"], captures)
        state = json.loads(self.atspi_state.read_text())
        self.assertEqual(state["clicks"], 1)
        self.assertEqual(state["text"], "Héllo 世界!")
        self.assertEqual(state["password_reads"], 0)
        self.snapshot(client)
        stale = client.tool("computer", steps=[{"tool": "click", "args": {"element_token": rows["Fixture button"]["element_token"]}}])
        self.assertEqual(stale["structuredContent"]["failed"]["code"], "stale_element")

    def test_browser_action_names_reuse_click(self):
        self.start_host()
        client = self.client()
        for name in ("jump", "open", "doDefault", "press", "clickAncestor", "check", "toggle"):
            with self.subTest(name=name):
                self.atspi_state.with_suffix(".actions.json").write_text(json.dumps([name, "showContextMenu"]))
                tree = self.snapshot(client)
                row = next(row for row in tree["elements"] if row["label"] == "Fixture button")
                self.assertEqual(row["actions"], ["click"])
                self.assertEqual(row["click_buttons"], ["left", "right"])
                for button, expected in (("left", name), ("right", "showContextMenu")):
                    result = client.tool("computer", steps=[{"tool": "click", "args": {
                        "element_token": row["element_token"], "button": button}}])["structuredContent"]
                    self.assertEqual(result["status"], "ok", result)
                    self.assertEqual(json.loads(self.atspi_state.read_text())["last_action"], expected)
        self.atspi_state.with_suffix(".actions.json").write_text(json.dumps(["scroll", "showContextMenu"]))
        row = next(row for row in self.snapshot(client)["elements"] if row["label"] == "Fixture button")
        self.assertEqual(row["click_buttons"], ["right"])
        result = client.tool("computer", steps=[{"tool": "click", "args": {"element_token": row["element_token"]}}])
        self.assertEqual(result["structuredContent"]["failed"]["code"], "accessible_action_failed")

    def test_keyboard_text_target_and_readback(self):
        self.start_accessibility(keyboard=True)
        self.start_host()
        client = self.client()
        row = next(row for row in self.snapshot(client)["elements"] if row["label"] == "Fixture entry")
        self.assertEqual(row["text_input"], "keyboard")
        for tool, field, text, expected in (("type_text", "text", "Hello", "Hello"),
                                            ("type_text", "text", " world", "Hello world"),
                                            ("set_value", "value", "Changed", "Changed"),
                                            ("set_value", "value", "", "")):
            with self.subTest(tool=tool, text=text):
                result = client.tool("computer", steps=[{"tool": tool, "args": {
                    "element_token": row["element_token"], field: text}}])["structuredContent"]
                self.assertEqual(result["status"], "ok", result)
                self.assertIn("keyboard", result["warnings"][0]["warning"])
                self.assertIn("observation", result)
                self.assertEqual(json.loads(self.state.read_text())["input"]["text"], expected)
                self.assertEqual(json.loads(self.atspi_state.read_text())["focus_requests"], 1)

    def test_keyboard_text_requires_focused_target(self):
        self.start_accessibility(keyboard=True, refuse_focus=True)
        self.start_host()
        client = self.client()
        row = next(row for row in self.snapshot(client)["elements"] if row["label"] == "Fixture entry")
        result = client.tool("computer", steps=[{"tool": "type_text", "args": {
            "element_token": row["element_token"], "text": "must not type"}}])["structuredContent"]
        self.assertEqual(result["failed"]["code"], "text_focus_changed", result)
        self.assertEqual(json.loads(self.state.read_text())["input"]["keys"], 0)

    def test_hidden_nodes_do_not_consume_element_budget(self):
        self.start_accessibility(hidden=True)
        self.start_host()
        tree = self.snapshot(self.client())
        self.assertNotIn("Fixture button", [row["label"] for row in tree["elements"]])
        self.assertIn("Fixture entry", [row["label"] for row in tree["elements"]])

    def test_keyboard_text_requires_active_window(self):
        self.start_accessibility(keyboard=True)
        self.start_host(active=False)
        client = self.client()
        row = next(row for row in self.snapshot(client)["elements"] if row["label"] == "Fixture entry")
        result = client.tool("computer", steps=[{"tool": "set_value", "args": {
            "element_token": row["element_token"], "value": "must not type"}}])["structuredContent"]
        self.assertEqual(result["failed"]["code"], "text_focus_changed", result)
        self.assertEqual(json.loads(self.state.read_text())["input"]["keys"], 0)

    def test_hidden_container_preserves_exposed_descendants(self):
        self.start_accessibility(hidden_container=True)
        self.start_host()
        rows = self.snapshot(self.client())["elements"]
        self.assertNotIn("Hidden container", [row["label"] for row in rows])
        button = next(row for row in rows if row["label"] == "Fixture button")
        self.assertEqual((button["parent_index"], button["depth"]), (0, 1))

    def test_keyboard_text_stops_after_focus_changes(self):
        self.start_accessibility(keyboard=True, lose_focus=True)
        self.start_host()
        client = self.client()
        row = next(row for row in self.snapshot(client)["elements"] if row["label"] == "Fixture entry")
        result = client.tool("computer", steps=[{"tool": "type_text", "args": {
            "element_token": row["element_token"], "text": "abc"}}])["structuredContent"]
        self.assertEqual(result["failed"]["code"], "text_focus_changed", result)
        self.assertEqual(json.loads(self.state.read_text())["input"]["keys"], 1)

    def test_keyboard_text_readback_failure_is_not_retried(self):
        self.start_accessibility(keyboard=True, readonly=True)
        self.start_host()
        client = self.client()
        row = next(row for row in self.snapshot(client)["elements"] if row["label"] == "Fixture entry")
        result = client.tool("computer", steps=[{"tool": "type_text", "args": {
            "element_token": row["element_token"], "text": "abc"}}])["structuredContent"]
        self.assertEqual(result["failed"]["code"], "text_readback_failed", result)
        self.assertEqual(json.loads(self.state.read_text())["input"]["text"], "abc")

    def test_click_buttons_counts_and_standalone_meta(self):
        self.start_host()
        client = self.client()
        self.status(client)
        result = client.tool("computer", steps=[
            {"tool": "click", "args": {"x": 60, "y": 50, "count": 2}},
            {"tool": "click", "args": {"x": 60, "y": 50, "button": "right"}},
            {"tool": "press_keys", "args": {"keys": ["meta"]}},
        ])["structuredContent"]
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["completed"], 3)
        self.assertEqual(json.loads(self.state.read_text())["input"]["clicks"], 3)

    def test_incompatible_accessibility_geometry_is_not_guessed(self):
        self.start_accessibility()
        self.start_host(bad_client=True)
        client = self.client()
        app = self.status(client)["structuredContent"]["apps"][0]
        result = client.tool("computer", observe={"kind": "window", "pid": app["pid"], "window_id": app["windows"][0]["window_id"], "tree_format": "elements"})
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["observation"]["accessibility_error"]["code"], "accessibility_window_unavailable")
        self.assertEqual(self.image(result).size, (54, 60))

    def test_ambiguous_accessibility_window_is_not_selected_by_title(self):
        self.start_accessibility(duplicates=True)
        self.start_host()
        client = self.client()
        app = self.status(client)["structuredContent"]["apps"][0]
        result = client.tool("computer", observe={"kind": "window", "pid": app["pid"],
            "window_id": app["windows"][0]["window_id"], "tree_format": "elements"})
        self.assertEqual(result["structuredContent"]["observation"]["accessibility_error"]["code"], "accessibility_window_unavailable")
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 0)

    def test_window_image_is_automatic_and_preserves_overview_coordinates(self):
        self.start_host()
        client = self.client()
        status = self.status(client)["structuredContent"]
        app = status["apps"][0]
        target = {"kind": "window", "pid": app["pid"], "window_id": app["windows"][0]["window_id"]}
        result = client.tool("computer", observe={**target, "tree_format": "elements"})
        self.assertFalse(result["isError"], result)
        observation = result["structuredContent"]["observation"]
        self.assertIn("elements", observation)
        self.assertEqual(observation["id"], status["canvas"]["id"])
        self.assertEqual(observation["screenshot"], {
            "width": 80, "height": 60,
            "bounds": {"x": 20, "y": 20, "width": 80, "height": 60}})
        image = self.image(result)
        self.assertEqual(image.size, (80, 60))
        self.assertEqual(image.getpixel((0, 0)), (20, 20, 123))
        self.assertEqual(image.getpixel((79, 59)), (99, 79, 123))
        reduced = client.tool("computer", observe={**target, "max_image_dimension": 20})
        self.assertEqual(self.image(reduced).size, (20, 15))
        self.assertEqual(reduced["structuredContent"]["observation"]["id"], observation["id"])
        self.assertEqual(json.loads(self.state.read_text())["window_captures"], ["window-1", "window-1"])
        rejected = client.tool("computer", steps=[{"tool": "press_keys", "args": {"keys": ["a"]}}],
            observe={**target, "include_screenshot": True})
        self.assertEqual(rejected["structuredContent"]["code"], "invalid_arguments")
        self.assertEqual(json.loads(self.state.read_text())["input"]["keys"], 0)

    def test_window_without_accessibility_still_returns_an_image(self):
        self.start_host(window_title="No accessibility tree")
        client = self.client()
        window = client.windows()[0]
        result = client.tool("computer", observe={"kind": "window", "pid": self.atspi.pid,
            "window_id": window["window_id"], "tree_format": "both"})
        self.assertFalse(result["isError"], result)
        self.assertEqual(self.image(result).size, (80, 60))
        observation = result["structuredContent"]["observation"]
        self.assertNotIn("elements", observation)
        self.assertEqual(observation["accessibility_error"]["code"], "accessibility_window_unavailable")

    def test_window_capture_failure_preserves_available_tree(self):
        self.start_host()
        client = self.client()
        app = self.status(client)["structuredContent"]["apps"][0]
        self.configure(refuse=True)
        result = client.tool("computer", observe={"kind": "window", "pid": app["pid"],
            "window_id": app["windows"][0]["window_id"], "tree_format": "elements"})
        self.assertFalse(result["isError"], result)
        observation = result["structuredContent"]["observation"]
        self.assertIn("elements", observation)
        self.assertIn("screenshot_error", observation)
        self.assertFalse(any(block["type"] == "image" for block in result["content"]))

    def test_window_capture_rejects_geometry_races(self):
        self.start_host(change_window_on_capture=True)
        client = self.client()
        window = client.windows()[0]
        result = client.tool("computer", observe={"kind": "window", "pid": self.atspi.pid,
            "window_id": window["window_id"]})
        self.assertEqual(result["structuredContent"]["observation_error"]["code"], "stale_window_geometry")
        self.assertFalse(any(block["type"] == "image" for block in result["content"]))

    def test_mismatched_pid_matches_only_a_unique_window_and_actions_work(self):
        self.start_accessibility()
        self.start_host(app_pid=self.atspi.pid + 100000, buffer_client=True)
        client = self.client()
        tree = self.snapshot(client)
        self.assertFalse(tree["pid_matched"])
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        self.assertEqual(button["frame"], {"x": 30, "y": 30, "width": 20, "height": 10})
        result = client.tool("computer", steps=[{"tool": "click", "args": {"element_token": button["element_token"]}}])
        self.assertFalse(result["isError"], result)
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 1)

    def test_mismatched_pid_rejects_ambiguity_in_both_inventories(self):
        self.start_accessibility(duplicates=True)
        self.start_host(app_pid=self.atspi.pid + 100000)
        client = self.client()
        tree = self.snapshot(client)
        self.assertEqual(tree["accessibility_error"]["code"], "accessibility_window_unavailable")
        self.configure(duplicate_identity=True)
        tree = self.snapshot(client)
        self.assertEqual(tree["accessibility_error"]["code"], "accessibility_window_unavailable")
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 0)

    def test_mismatched_pid_rejects_multiple_compositor_candidates(self):
        self.start_accessibility()
        self.start_host(app_pid=self.atspi.pid + 100000, duplicate_identity=True)
        tree = self.snapshot(self.client())
        self.assertEqual(tree["accessibility_error"]["code"], "accessibility_window_unavailable")
        self.assertNotIn("elements", tree)

    def test_window_screenshot_cannot_retarget_a_stale_pid(self):
        self.start_host()
        client = self.client()
        window = client.windows()[0]
        captures = json.loads(self.state.read_text())["captures"]
        result = client.tool("computer", observe={"kind": "window", "pid": self.atspi.pid + 1,
            "window_id": window["window_id"]})
        self.assertEqual(result["structuredContent"]["observation_error"]["code"], "window_unavailable")
        self.assertEqual(json.loads(self.state.read_text())["captures"], captures)

    def test_prevalidation_and_removed_arguments_send_no_input(self):
        self.start_host()
        client = self.client()
        for name, value in (("target", {}), ("display_id", "primary"), ("pid", 1),
                            ("window_id", 1), ("session", "old"), ("delivery_mode", "foreground"),
                            ("delay_ms", 100), ("capture_id", 1)):
            for args in ({name: value}, {"steps": [{"tool": "press_keys", "args": {"keys": ["a"]}},
                         {"tool": "click", "args": {"x": 1, "y": 1, name: value}}]}):
                result = client.tool("computer", **args)
                self.assertEqual(result["structuredContent"]["code"], "invalid_arguments")
        for invalid_id in (0, -1, 1.5, True, "1", "c1", 9007199254740992):
            for args in ({"id": invalid_id, "steps": [{"tool": "press_keys", "args": {"keys": ["a"]}}]},
                         {"steps": [{"tool": "press_keys", "args": {"keys": ["a"]}},
                                    {"tool": "click", "args": {"x": 1, "y": 1, "id": invalid_id}}]}):
                result = client.tool("computer", **args)
                self.assertEqual(result["structuredContent"]["code"], "invalid_arguments")
        for arguments in ({"keys": []}, {"keys": ["a"] * 6}, {"keys": "a"},
                          {"key": "a"}, {"keys": ["a"], "modifiers": ["ctrl"]}):
            result = client.tool("computer", steps=[
                {"tool": "click", "args": {"x": 1, "y": 1}},
                {"tool": "press_keys", "args": arguments}])
            self.assertEqual(result["structuredContent"]["code"], "invalid_arguments")
        self.assertEqual(json.loads(self.state.read_text())["geometry_requests"], 0)
        for removed in ("run_steps", "act", "get_desktop_state", "get_window_state", "list_windows", "health_report", "click"):
            self.assertEqual(client.tool(removed)["structuredContent"]["code"], "unknown_tool")

    def test_automatic_delays_separate_actions_and_final_screenshot(self):
        self.start_accessibility()
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        click = {"tool": "click", "args": {"element_token": button["element_token"]}}
        result = client.tool("computer", steps=[click] * 3, observe={"kind": "desktop"})
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["completed"], 3)
        times = json.loads(self.atspi_state.read_text())["action_times"]
        for earlier, later in zip(times, times[1:]):
            self.assertGreaterEqual(later - earlier, .15)
        capture_time = json.loads(self.state.read_text())["capture_times"][-1]
        self.assertGreaterEqual(capture_time - times[-1], .3)
        self.assertEqual(sum(block["type"] == "image" for block in result["content"]), 1)

    def test_trailing_zero_delay_replaces_only_the_final_pause(self):
        self.start_accessibility()
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        click = {"tool": "click", "args": {"element_token": button["element_token"]}}
        result = client.tool("computer", steps=[click] * 6 + [
            {"tool": "delay_ms", "args": {"ms": 0}}], observe={"kind": "desktop"})
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["completed"], 7)
        times = json.loads(self.atspi_state.read_text())["action_times"]
        for earlier, later in zip(times, times[1:]):
            self.assertGreaterEqual(later - earlier, .15)
        capture_time = json.loads(self.state.read_text())["capture_times"][-1]
        self.assertLess(capture_time - times[-1], .3)

    def test_intermediate_zero_delay_replaces_only_its_action_gap(self):
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        click = {"tool": "click", "args": {"element_token": button["element_token"]}}
        result = client.tool("computer", steps=[click,
            {"tool": "delay_ms", "args": {"ms": 0}}, click, click], observe={"kind": "desktop"})
        self.assertFalse(result["isError"], result)
        times = json.loads(self.atspi_state.read_text())["action_times"]
        self.assertEqual(len(times), 3)
        self.assertLess(times[1] - times[0], .15)
        self.assertGreaterEqual(times[2] - times[1], .15)
        self.assertGreaterEqual(json.loads(self.state.read_text())["capture_times"][-1] - times[-1], .3)

    def test_explicit_action_and_final_delays_are_not_padded(self):
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        click = {"tool": "click", "args": {"element_token": button["element_token"]}}
        result = client.tool("computer", steps=[click,
            {"tool": "delay_ms", "args": {"ms": 200}}, click,
            {"tool": "delay_ms", "args": {"ms": 50}}], observe={"kind": "desktop"})
        self.assertFalse(result["isError"], result)
        times = json.loads(self.atspi_state.read_text())["action_times"]
        self.assertGreaterEqual(times[1] - times[0], .2)
        self.assertLess(times[1] - times[0], .35)
        final_gap = json.loads(self.state.read_text())["capture_times"][-1] - times[-1]
        self.assertGreaterEqual(final_gap, .05)
        self.assertLess(final_gap, .3)

    def test_observation_only_uses_the_default_final_delay(self):
        self.start_host()
        client = self.client()
        start = time.monotonic()
        result = client.tool("computer", observe={"kind": "desktop"})
        self.assertFalse(result["isError"], result)
        self.assertGreaterEqual(json.loads(self.state.read_text())["capture_times"][-1] - start, .3)

    def test_unexecuted_trailing_delay_does_not_replace_the_final_pause(self):
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        result = client.tool("computer", steps=[
            {"tool": "click", "args": {"element_token": button["element_token"]}},
            {"tool": "delay_ms", "args": {"ms": 0}},
            {"tool": "click", "args": {"element_token": "stale"}},
            {"tool": "delay_ms", "args": {"ms": 0}},
        ], observe={"kind": "desktop"})
        self.assertEqual(result["structuredContent"]["completed"], 2)
        self.assertEqual(result["structuredContent"]["status"], "failed")
        last_action = json.loads(self.atspi_state.read_text())["action_times"][-1]
        self.assertGreaterEqual(json.loads(self.state.read_text())["capture_times"][-1] - last_action, .3)

    def test_automatic_delay_stops_on_permission_revocation(self):
        self.start_accessibility()
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        click = {"tool": "click", "args": {"element_token": button["element_token"]}}
        client.serial += 1
        client.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": client.serial,
            "method": "tools/call", "params": {"name": "computer", "arguments": {
                "steps": [click, click], "observe": False}}}) + "\n")
        client.process.stdin.flush()
        end = time.monotonic() + 2
        while time.monotonic() < end:
            try:
                if json.loads(self.atspi_state.read_text())["clicks"]:
                    break
            except json.JSONDecodeError:
                pass
            time.sleep(.002)
        else:
            self.fail("first action did not arrive")
        # The fixture saves state before replying to the first action. Allow
        # that reply to finish so revocation lands in the 150 ms batch pause.
        time.sleep(.04)
        self.computer_path.unlink()
        self.assertTrue(select.select([client.process.stdout], [], [], 2)[0])
        result = json.loads(client.process.stdout.readline())["result"]
        data = result["structuredContent"]
        self.assertTrue(result["isError"], result)
        self.assertEqual(data["code"], "computer_use_disconnected")
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 1)

    def test_explicit_delays_finish_before_one_final_screenshot(self):
        self.start_host()
        client = self.client()
        self.status(client)
        before = json.loads(self.state.read_text())["captures"]
        start = time.monotonic()
        result = client.tool("computer", steps=[
            {"tool": "press_keys", "args": {"keys": ["a"]}},
            {"tool": "delay_ms", "args": {"ms": 250}},
            {"tool": "press_keys", "args": {"keys": ["b"]}},
            {"tool": "delay_ms", "args": {"ms": 250}},
        ])
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["completed"], 4)
        self.assertEqual(sum(block["type"] == "image" for block in result["content"]), 1)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["captures"], before + 1)
        self.assertGreaterEqual(state["capture_times"][-1] - start, .5)
        self.assertEqual(state["input"]["keys"], 2)

    def test_delay_only_observes_after_wait_and_opt_out_opens_no_input(self):
        self.start_host()
        client = self.client()
        result = client.tool("computer", steps=[{"tool": "delay_ms", "args": {"ms": 0}}], observe=False)
        self.assertFalse(result["isError"], result)
        self.assertNotIn("observation", result["structuredContent"])
        state = json.loads(self.state.read_text())
        self.assertEqual((state["captures"], state["connections"]), (0, 0))
        start = time.monotonic()
        result = client.tool("computer", steps=[{"tool": "delay_ms", "args": {"ms": 200}}])
        self.assertFalse(result["isError"], result)
        self.assertEqual(self.image(result).size, (160, 120))
        self.assertGreaterEqual(json.loads(self.state.read_text())["capture_times"][-1] - start, .2)

    def test_invalid_delay_is_rejected_before_earlier_steps(self):
        self.start_host()
        client = self.client()
        for args in ({}, {"ms": -1}, {"ms": 30001}, {"ms": .5}, {"ms": "100"},
                     {"ms": True}, {"ms": 1, "element_token": "old"}):
            result = client.tool("computer", steps=[
                {"tool": "press_keys", "args": {"keys": ["a"]}},
                {"tool": "delay_ms", "args": args},
            ])
            self.assertEqual(result["structuredContent"]["code"], "invalid_arguments")
        self.assertEqual(json.loads(self.state.read_text())["connections"], 0)

    def test_delay_cancellation_skips_later_input_and_screenshot(self):
        self.start_host()
        client = self.client()
        client.serial += 1
        request_id = client.serial
        client.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id,
            "method": "tools/call", "params": {"name": "computer", "arguments": {
                "steps": [{"tool": "delay_ms", "args": {"ms": 30000}},
                          {"tool": "press_keys", "args": {"keys": ["a"]}}]}}}) + "\n")
        client.process.stdin.flush()
        time.sleep(.1)
        client.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
            "params": {"requestId": request_id}}) + "\n")
        client.process.stdin.flush()
        self.assertIn("result", client.call("ping", {}))
        state = json.loads(self.state.read_text())
        self.assertEqual((state["connections"], state["captures"]), (0, 0))

    def test_delay_stops_on_permission_revocation_before_later_input(self):
        self.start_host()
        client = self.client()
        client.serial += 1
        client.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": client.serial,
            "method": "tools/call", "params": {"name": "computer", "arguments": {
                "steps": [{"tool": "delay_ms", "args": {"ms": 30000}},
                          {"tool": "press_keys", "args": {"keys": ["a"]}}]}}}) + "\n")
        client.process.stdin.flush()
        time.sleep(.1)
        self.computer_path.unlink()
        self.assertTrue(select.select([client.process.stdout], [], [], 2)[0], "delay did not stop on revocation")
        result = json.loads(client.process.stdout.readline())["result"]
        data = result["structuredContent"]
        self.assertTrue(result["isError"], result)
        self.assertEqual(data["code"], "computer_use_disconnected")
        state = json.loads(self.state.read_text())
        self.assertEqual((state["connections"], state["captures"]), (0, 0))

    def test_real_eis_batch_and_global_coordinates_after_zoom(self):
        self.start_host()
        client = self.client()
        capture = self.status(client)["structuredContent"]["canvas"]["id"]
        detail = client.tool("computer", id=capture, observe={"kind": "desktop", "zoom": [20, 20, 100, 80], "max_image_dimension": 0})
        self.assertFalse(detail["isError"], detail)
        self.assertEqual(detail["structuredContent"]["observation"]["id"], capture)
        self.assertEqual(set(detail["structuredContent"]["observation"]),
                         {"id", "width", "height", "zoom"})
        self.assertEqual((detail["structuredContent"]["observation"]["width"],
                          detail["structuredContent"]["observation"]["height"]), (80, 60))
        self.assertEqual(self.image(detail).size, (80, 60))
        result = client.tool("computer", id=capture, steps=[
            {"tool": "click", "args": {"x": 60, "y": 50}},
            {"tool": "scroll", "args": {"x": 60, "y": 50, "direction": "down", "amount": 2}},
            {"tool": "press_keys", "args": {"keys": ["ctrl", "a"]}},
            {"tool": "type_text", "args": {"text": "世界"}},
        ])
        self.assertFalse(result["isError"], result)
        data = result["structuredContent"]
        self.assertEqual(data["completed"], 4)
        self.assertEqual(set(data), {"completed", "status", "warnings", "observation"})
        self.assertEqual(data["status"], "ok")
        self.assertNotIn("steps", data)
        self.assertEqual(self.image(result).size, (160, 120))
        self.assertNotEqual(data["observation"]["id"], capture)
        self.assertIs(type(data["observation"]["id"]), int)
        self.assertGreater(data["observation"]["id"], capture)
        self.assertNotIn("zoom", data["observation"])
        self.assertEqual(set(data["observation"]),
                         {"id", "width", "height"})
        self.assertEqual((data["observation"]["width"], data["observation"]["height"]), (160, 120))
        state = json.loads(self.state.read_text())
        self.assertEqual((state["input"]["x"], state["input"]["y"]), (720, 450))
        self.assertEqual(state["input"]["clicks"], 1)
        self.assertEqual(state["input"]["scrolls"], 1)
        self.assertEqual(state["input"]["buttons_down"], 0)
        self.assertEqual(state["input"]["keys_down"], 0)
        self.assertEqual(state["clipboard"], "世界")
        self.assertEqual(data["warnings"][0]["warning"], "Clipboard and primary selection text replaced.")
        # Explicit numeric step IDs override the batch default in both directions.
        current = data["observation"]["id"]
        override = client.tool("computer", id=capture, observe=False, steps=[
            {"tool": "click", "args": {"x": 60, "y": 50, "id": current}}])
        self.assertFalse(override["isError"], override)
        stale = client.tool("computer", id=current, observe=False, steps=[
            {"tool": "click", "args": {"x": 60, "y": 50, "id": capture}}])
        self.assertTrue(stale["isError"], stale)
        self.assertEqual(stale["structuredContent"]["failed"]["code"], "stale_capture")
        self.assertEqual(stale["structuredContent"]["completed"], 0)
        self.status(client)
        self.assertEqual(json.loads(self.state.read_text())["input"]["clicks"], 2)

    def test_klipper_fallback_restores_a_lost_accessible_selection(self):
        self.start_accessibility(selection=True)
        self.start_host()
        client = self.client()
        self.status(client)
        result = client.tool("computer", steps=[{"tool": "type_text", "args": {"text": "世界"}}])
        self.assertFalse(result["isError"], result)
        self.assertEqual(json.loads(self.atspi_state.read_text())["selection_restores"], 1)
        self.assertEqual(json.loads(self.state.read_text())["clipboard"], "世界")

    def test_unstable_selection_fails_without_sending_paste(self):
        self.start_accessibility(unstable=True)
        self.start_host()
        client = self.client()
        self.status(client)
        result = client.tool("computer", steps=[{"tool": "type_text", "args": {"text": "世界"}}])
        self.assertTrue(result["isError"], result)
        self.assertEqual(result["structuredContent"]["failed"]["code"], "clipboard_selection_changed")
        self.assertEqual(result["structuredContent"]["completed"], 0)
        self.assertEqual(json.loads(self.state.read_text())["input"]["keys"], 0)

    def test_cancellation_interrupts_a_stalled_host(self):
        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        listener.bind(str(self.computer_path))
        self.computer_path.chmod(0o600)
        listener.listen(1)
        client = self.client()
        client.serial += 1
        client.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": client.serial,
            "method": "tools/call", "params": {"name": "status", "arguments": {}}}) + "\n")
        client.process.stdin.flush()
        self.assertTrue(select.select([listener], [], [], 2)[0])
        peer, _ = listener.accept()
        self.addCleanup(peer.close)
        client.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
            "params": {"requestId": client.serial}}) + "\n")
        client.process.stdin.flush()
        time.sleep(.1)
        self.assertIn("result", client.call("ping", {}))

    def test_triple_monitor_gaps_native_zoom_and_drag(self):
        self.start_host(triple=True)
        client = self.client()
        result = client.tool("computer", observe={"kind": "desktop", "max_image_dimension": 100})
        self.assertFalse(result["isError"], result)
        data = result["structuredContent"]["observation"]
        self.assertEqual(self.image(result).size, (100, 28))
        # Gap lies at x=64..68 in this overview.
        self.assertEqual(self.image(result).getpixel((66, 15)), (0, 0, 0))
        detail = client.tool("computer", observe={"kind": "desktop", "zoom": [0, 0, 32, 24], "max_image_dimension": 0})
        self.assertEqual(self.image(detail).size, (160, 120))
        self.assertEqual(detail["structuredContent"]["observation"]["id"], data["id"])
        result = client.tool("computer", steps=[{"tool": "drag", "args": {
            "from_x": 8, "from_y": 12, "to_x": 80, "to_y": 12, "duration_ms": 100}}])
        self.assertFalse(result["isError"], result)
        state = json.loads(self.state.read_text())["input"]
        self.assertEqual((state["x"], state["y"]), (2800, 360))
        self.assertGreater(state["dragged_motions"], 0)
        self.assertEqual(state["clicks"], 1)
        self.assertEqual(state["buttons_down"], 0)

    def test_stale_and_invalid_zoom_fail_before_effects(self):
        self.start_host()
        client = self.client()
        initial = self.status(client)["structuredContent"]["canvas"]["id"]
        self.status(client)
        for observe, capture in (({"kind": "desktop", "zoom": [0, 0, 20, 20]}, initial),
                                 ({"kind": "desktop", "zoom": [-1, 0, 20, 20]}, None),
                                 ({"kind": "desktop", "zoom": [0, 0, 200, 20]}, None)):
            result = client.tool("computer", **({"id": capture} if capture else {}),
                steps=[{"tool": "click", "args": {"x": 40, "y": 40}}], observe=observe)
            self.assertTrue(result["isError"], result)
        self.status(client)
        self.assertEqual(json.loads(self.state.read_text())["input"]["clicks"], 0)

    def test_partial_failure_observation_opt_out_and_capture_error(self):
        self.start_host()
        client = self.client()
        self.status(client)
        captures = json.loads(self.state.read_text())["captures"]
        result = client.tool("computer", observe=False, steps=[
            {"tool": "click", "args": {"x": 40, "y": 40}},
            {"tool": "click", "args": {"x": -1, "y": 40}},
            {"tool": "press_keys", "args": {"keys": ["a"]}},
        ])
        data = result["structuredContent"]
        self.assertEqual((data["completed"], data["failed"]["index"]), (1, 1))
        self.assertEqual(set(data), {"completed", "failed", "status"})
        self.assertEqual(data["status"], "failed")
        self.assertTrue(result["isError"])
        self.assertNotIn("observation", data)
        self.assertEqual(json.loads(self.state.read_text())["captures"], captures)
        self.configure(refuse=True)
        result = client.tool("computer", steps=[{"tool": "click", "args": {"x": 40, "y": 40}}])
        self.assertEqual(result["structuredContent"]["completed"], 1)
        self.assertEqual(result["structuredContent"]["status"], "failed")
        self.assertTrue(result["isError"])
        self.assertIn("observation_error", result["structuredContent"])
        error = client.tool("status")
        self.assertTrue(error["isError"])
        self.assertIn("code", error["structuredContent"])
        self.assertEqual(set(error["structuredContent"]), {"code", "hint"})

    def test_capture_formats_truncation_and_layout_errors(self):
        self.start_host()
        client = self.client()
        for field in ("bad_format", "truncate"):
            self.configure(**{field: True})
            result = client.tool("status")
            self.assertTrue(result["isError"], result)
            self.configure(**{field: False})
        self.configure(triple=True, overlap=True)
        self.assertTrue(client.tool("status")["isError"])

    def test_permission_revocation_disconnects_held_client_and_recovers(self):
        self.start_host()
        client = self.client()
        self.status(client)

        self.stop(self.host)
        result = client.tool("computer", steps=[{"tool": "press_keys", "args": {"keys": ["a"]}}], observe=False)
        self.assertTrue(result["isError"], result)
        fresh = self.client().tool("status")
        self.assertTrue(fresh["isError"])
        self.computer_path.unlink(missing_ok=True)
        self.start_service()
        self.status(client)

    def test_kwin_replacement_invalidates_existing_handles_then_recovers(self):
        self.start_host()
        client = self.client()
        original = self.status(client)["structuredContent"]["canvas"]["id"]
        self.stop(self.kwin)
        self.kwin = self.child(["/usr/bin/python3", str(ROOT / "tests/scu_kwin_fixture.py"),
            self.address, str(self.state), str(self.control), str(self.backend)])
        self.ready(self.kwin)
        time.sleep(.1)
        result = client.tool("status")
        self.assertTrue(result["isError"], result)
        self.assertEqual(result["structuredContent"]["code"], "computer_use_disconnected")
        fresh = self.status(client)
        self.assertNotEqual(fresh["structuredContent"]["canvas"]["id"], original)

    def test_geometry_change_rejects_zoom_batch_before_keyboard_effects(self):
        self.start_host()
        client = self.client()
        self.status(client)
        self.configure(resize=True)
        result = client.tool("computer", steps=[{"tool": "press_keys", "args": {"keys": ["a"]}}],
            observe={"kind": "desktop", "zoom": [0, 0, 20, 20]})
        self.assertEqual(result["structuredContent"]["code"], "stale_capture")
        self.status(client)
        self.assertEqual(json.loads(self.state.read_text())["input"]["keys"], 0)

    def test_missing_libraries_keep_catalog_but_fail_control(self):
        self.start_host()
        for library in ("libatspi.so.0", "libei.so.1", "libxkbcommon.so.0", "libgdk_pixbuf-2.0.so.0"):
            with self.subTest(library=library):
                library_dir = self.directory / ("missing-" + library)
                library_dir.mkdir(exist_ok=True)
                (library_dir / library).write_bytes(b"invalid ELF")
                self.stop(self.host)
                self.environment["LD_LIBRARY_PATH"] = str(library_dir)
                self.start_service()
                client = self.client()
                self.assertIn("tools", client.call("tools/list", {})["result"])
                for name, args in (("status", {}), ("computer", {"steps": [
                    {"tool": "press_keys", "args": {"keys": ["a"]}}]})):
                    result = client.tool(name, **args)
                    self.assertTrue(result["isError"], result)
                    self.assertEqual(result["structuredContent"]["code"], "missing_dependency")
                    self.assertTrue(all(block["type"] == "text" for block in result["content"]))
        state = json.loads(self.state.read_text())
        self.assertEqual((state["captures"], state["connections"]), (0, 0))

    def test_missing_native_accessibility_fails_before_capture_or_input(self):
        self.start_host(accessibility=False)
        client = self.client()
        for name, args in (("status", {}), ("computer", {"steps": [
            {"tool": "press_keys", "args": {"keys": ["a"]}}]})):
            result = client.tool(name, **args)
            self.assertTrue(result["isError"], result)
            self.assertEqual(result["structuredContent"]["code"], "accessibility_unavailable")
            self.assertTrue(all(block["type"] == "text" for block in result["content"]))
            self.assertNotIn("canvas", result["structuredContent"])
            self.assertNotIn("apps", result["structuredContent"])
        state = json.loads(self.state.read_text())
        self.assertEqual((state["captures"], state["connections"]), (0, 0))
        self.start_accessibility()
        self.status(client)  # The same MCP connection recovers once prerequisites work.
        self.assertEqual(json.loads(self.atspi_state.read_text())["registrations"], 1)

    def test_broken_registry_socket_is_not_a_successful_status(self):
        self.start_accessibility(unavailable=True)
        self.start_host()
        client = self.client()
        for name, args in (("status", {}), ("computer", {"steps": [
            {"tool": "press_keys", "args": {"keys": ["a"]}}]})):
            result = client.tool(name, **args)
            self.assertTrue(result["isError"], result)
            self.assertEqual(result["structuredContent"]["code"], "accessibility_unavailable")
            self.assertTrue(all(block["type"] == "text" for block in result["content"]))
        state = json.loads(self.state.read_text())
        self.assertEqual((state["captures"], state["connections"]), (0, 0))

    def test_backend_invariants(self):
        for case in ("mapping", "images"):
            with self.subTest(case=case):
                result = subprocess.run([str(self.backend), case], capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_modern_legacy_unicode_and_malformed_stdio(self):
        client = self.client()
        metadata = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {}}
        self.assertIn("supportedVersions", client.call("server/discover", {"_meta": metadata})["result"])
        self.assertEqual(client.call("initialize", {"protocolVersion": "2025-06-18"})["result"]["protocolVersion"], "2025-06-18")
        bad = client.tool("computer", steps=[{"tool": "type_text", "args": {"text": "世界", "bad": True}}])
        self.assertEqual(bad["structuredContent"]["code"], "invalid_arguments")
        for line in ("not json", "x" * (1024 * 1024 + 1)):
            client.process.stdin.write(line + "\n")
            client.process.stdin.flush()
            self.assertTrue(select.select([client.process.stdout], [], [], 5)[0])
            self.assertEqual(json.loads(client.process.stdout.readline())["error"]["code"], -32700)
            self.assertIn("result", client.call("ping", {}))

    def test_eof_and_cancellation_close_input(self):
        self.start_host()
        client = self.client()
        self.status(client)
        client.close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and json.loads(self.state.read_text())["disconnections"] == 0:
            time.sleep(.02)
        self.assertGreater(json.loads(self.state.read_text())["disconnections"], 0)
        client = self.client()
        self.status(client)
        self.configure(hold_capture=True)
        client.serial += 1
        client.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": client.serial,
            "method": "tools/call", "params": {"name": "status", "arguments": {}}}) + "\n")
        client.process.stdin.flush()
        time.sleep(.2)
        client.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
            "params": {"requestId": client.serial}}) + "\n")
        client.process.stdin.flush()
        self.assertFalse(select.select([client.process.stdout], [], [], .3)[0], "cancelled request emitted a late response")

    def test_status_deadline_is_thirty_seconds_and_can_recover(self):
        self.start_host(hold_geometry=True)
        client = self.client()
        start = time.monotonic()
        result = client.call("tools/call", {"name": "status", "arguments": {}}, timeout=34)["result"]
        elapsed = time.monotonic() - start
        self.assertTrue(result["isError"], result)
        self.assertGreaterEqual(elapsed, 29)
        self.assertLess(elapsed, 32)
        self.assertEqual(result["structuredContent"]["code"], "timeout")
        self.configure(hold_geometry=False)
        time.sleep(.2)
        self.status(client)

    def test_cancelled_cross_monitor_drag_releases_button(self):
        self.start_host(triple=True)
        client = self.client()
        self.status(client)  # native overview is 500x140
        client.serial += 1
        request_id = client.serial
        client.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id,
            "method": "tools/call", "params": {"name": "computer", "arguments": {
                "steps": [{"tool": "drag", "args": {"from_x": 40, "from_y": 60,
                    "to_x": 400, "to_y": 60, "duration_ms": 1000}}]}}}) + "\n")
        client.process.stdin.flush()
        time.sleep(.15)
        client.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
            "params": {"requestId": request_id}}) + "\n")
        client.process.stdin.flush()
        time.sleep(.15)
        self.status(client)
        state = json.loads(self.state.read_text())["input"]
        self.assertEqual(state["buttons_down"], 0)
        self.assertEqual(state["keys_down"], 0)


if __name__ == "__main__":
    unittest.main()
