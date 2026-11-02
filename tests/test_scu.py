"""Exercise the real MCP executable and host service on private desktop peers."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
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

from tests.native_build import compile_native
from tests.scu_tree import read_result
from tests.test_scu_diff import apply_tree_diff

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("gio-unix-2.0", "atspi-2", "libei-1.0", "xkbcommon", "gdk-pixbuf-2.0", "libeis-1.0")


class Client:
    def __init__(self, binary, environment=None, *, no_vision=False):
        self.process = subprocess.Popen([str(binary), "mcp", *(["--no-vision"] if no_vision else [])], stdin=subprocess.PIPE,
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
        if method == "tools/call" and "result" in result:
            fields = set(result["result"])
            assert {"content", "isError"} <= fields <= {"content", "isError", "_meta", "resultType"}, result
        return result

    def tool(self, name, **arguments):
        result = self.call("tools/call", {"name": name, "arguments": arguments})["result"]
        return result

    def windows(self):
        response = self.tool("status")
        assert not response["isError"], response
        return [{**window, "app_name": app["app_name"]}
                for app in read_result(response)["apps"] for window in app["windows"]]

    def window_state(self, **arguments):
        response = self.tool("vision", observations={"window": arguments.pop("window_id"), "refresh": True}, **arguments)
        assert not response["isError"], response
        return read_result(response)["observations"][0]

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
        common = ["-O0", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
            "-DYYJSON_READER_DEPTH_LIMIT=64"]
        glib = shlex.split(subprocess.check_output(["pkg-config", "--cflags", "--libs", "glib-2.0"], text=True))
        gio = shlex.split(subprocess.check_output(["pkg-config", "--libs", "gio-unix-2.0"], text=True))
        # Compile the large JSON translation unit once rather than once per binary.
        json_object = compile_native("yyjson.o", [ROOT / "native/vendor/yyjson.c"], [*common, "-c"])
        settings = ["-include", str(ROOT / "tests/scu_test_config.h"),
            f'-DSCU_SCREEN_QUERY="{ROOT / "tests/scu_display_fixture.py"}"',
            f'-DSCU_VIRTUAL_MONITOR="{ROOT / "tests/scu_display_fixture.py"}"']
        cls.binary = compile_native("scu", [ROOT / "native/scu.c", json_object],
            [*common, *settings, *glib, "-Wl,--wrap=__libc_start_main"])
        host_flags = [*common, *settings, *shlex.split(flags.stdout), *gio, "-ldl"]
        # The host and oracle include large translation units. Build independent
        # binaries concurrently; compile_native serializes identical cache misses
        # across unittest workers and publishes each binary atomically.
        with ThreadPoolExecutor(max_workers=3) as builds:
            host = builds.submit(compile_native, "scu-host", [ROOT / "native/scu_host.c", json_object], host_flags)
            ring = builds.submit(compile_native, "scu-host-ring", [ROOT / "native/scu_host.c", json_object],
                [*host_flags, "-DELEMENT_CAPACITY=64"])
            backend = builds.submit(compile_native, "backend", [ROOT / "tests/scu_backend_fixture.c", json_object],
                [*common, *shlex.split(flags.stdout), *gio, "-ldl", "-leis", "-lxkbcommon"])
            cls.host_binary, cls.ring_host_binary, cls.backend = host.result(), ring.result(), backend.result()

    def setUp(self):
        self.socket_path.unlink(missing_ok=True)
        self.computer_path.unlink(missing_ok=True)
        self.environment = {**os.environ, "SCU_TEST_COMPUTER_SOCKET": str(self.computer_path)}
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
                            lose_focus=False, readonly=False, hidden_container=False,
                            deep=0, wide=0, delayed_window=False, title_after_attributes=False,
                            geometry_after_attributes=False, numeric=False, numeric_keyboard=False,
                            numeric_readonly=False, numeric_delay=False, browser=False,
                            offscreen=False, tooltips=False, placeholders=0, long_labels=False, element_ids=False, reading_text=False,
                            decorations=False, settle_after_action=False):
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
        if browser:
            command.append("--browser")
        if offscreen:
            command.append("--offscreen")
        if tooltips:
            command.append("--tooltips")
        if reading_text:
            command.append("--reading-text")
        if decorations:
            command.append("--decorations")
        if settle_after_action:
            command.append("--settle-after-action")
        if placeholders:
            command.append(f"--placeholders={placeholders}")
        if deep:
            command.append(f"--deep={deep}")
        if wide:
            command.append(f"--wide={wide}")
        if long_labels:
            command.append("--long-labels")
        if element_ids:
            command.append("--element-ids")
        if delayed_window:
            command.append("--delayed-window")
        if title_after_attributes:
            command.append("--title-after-attributes")
        if geometry_after_attributes:
            command.append("--geometry-after-attributes")
        if lose_focus:
            command.append("--lose-focus")
        if readonly:
            command.append("--readonly-entry")
        for enabled, flag in ((numeric, "--numeric"), (numeric_keyboard, "--numeric-keyboard"),
                              (numeric_readonly, "--numeric-readonly"), (numeric_delay, "--numeric-delay")):
            if enabled: command.append(flag)
        self.atspi_state.with_suffix(".actions.json").unlink(missing_ok=True)
        self.atspi_state.with_suffix(".title").unlink(missing_ok=True)
        self.atspi_state.with_suffix(".elements.json").unlink(missing_ok=True)
        self.atspi_state.with_suffix(".value.json").unlink(missing_ok=True)
        self.atspi_state.with_suffix(".apps.json").unlink(missing_ok=True)
        self.atspi = self.child(command)
        self.ready(self.atspi)
        self.configure(app_pid=self.atspi.pid, two_windows=two_windows, atspi_address=address)

    def add_accessibility_application(self):
        state = self.directory / "other-atspi-state.json"
        state.with_suffix(".title").unlink(missing_ok=True)
        peer = self.child(["/usr/bin/python3", str(ROOT / "tests/scu_atspi_fixture.py"),
                           f"unix:path={self.socket_path}", str(state), "--application-only"])
        self.ready(peer)
        self.atspi_state.with_suffix(".apps.json").write_text(
            json.dumps([json.loads(state.read_text())["bus_name"]]))
        return peer, state

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

    def client(self, *, no_vision=False):
        client = Client(self.binary, self.environment, no_vision=no_vision)
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
        window = first.windows()[0]["window_id"]
        self.snapshot_accessibility(first, window)
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

    def assert_locked_status(self, client):
        result = self.status(client)
        data = read_result(result)
        self.assertEqual(set(data), {"canvas", "apps", "locked"})
        self.assertTrue(data["locked"])
        self.assertEqual(set(data["canvas"]), {"width", "height"})
        self.assertTrue(data["apps"])
        self.assertTrue(any(block.get("type") == "text" and block.get("text", "").startswith(
            "The screen is locked. Vision tools are unavailable.") for block in result["content"]))
        self.assertFalse(any(block["type"] == "image" for block in result["content"]))
        return data

    def test_shared_locked_status_and_native_accessibility_preserve_clients(self):
        self.start_shared()
        self.atspi_state.with_suffix(".actions.json").write_text(json.dumps(["click", "showContextMenu"]))
        clients = [self.client(), self.client(no_vision=True)]
        window = clients[0].windows()[0]["window_id"]
        trees = [self.snapshot_accessibility(client, window) for client in clients]
        self.assertEqual(trees[0]["elements"][0]["element_id"], 0)
        self.login_call("SetLocked", "true")
        for client, tree in zip(clients, trees):
            for _ in range(2):
                self.assert_locked_status(client)
            button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
            entry = next(row for row in tree["elements"] if row["label"] == "Fixture entry")
            result = client.tool("accessibility", steps=[
                {"tool": "activate", "args": {"element_id": button["element_id"]}},
                {"tool": "open_context_menu", "args": {"element_id": button["element_id"]}},
                {"tool": "set_value", "args": {"element_id": entry["element_id"], "value": "locked"}},
                {"tool": "type_text", "args": {"element_id": entry["element_id"], "text": "!"}},
                {"tool": "delay_ms", "args": {"ms": 10}}], observations={"window": window, "refresh": True})
            self.assertFalse(result["isError"], result)
            self.assertEqual([r["element_id"] for r in read_result(result)["observations"][0]["elements"]],
                             [r["element_id"] for r in tree["elements"]])
        registrations = json.loads(self.atspi_state.read_text())["registrations"]
        self.login_call("SetLocked", "false")
        for client, tree in zip(clients, trees):
            self.assertFalse(read_result(self.status(client))["locked"])
            result = client.tool("accessibility", observations={"window": window, "refresh": True})
            self.assertEqual([r["element_id"] for r in read_result(result)["observations"][0]["elements"]],
                             [r["element_id"] for r in tree["elements"]])
        self.assertEqual(json.loads(self.atspi_state.read_text())["registrations"], registrations)
        self.assertEqual(json.loads(self.login_state.read_text())["unlocks"], 0)

    def snapshot_accessibility(self, client, window):
        self.status(client)
        return read_result(client.tool("accessibility", observations={"window": window, "refresh": True}))["observations"][0]

    def test_shared_duplicate_ids_cancel_only_the_originating_connection(self):
        self.start_shared()
        peers = [socket.socket(socket.AF_UNIX) for _ in range(2)]
        for peer in peers:
            self.addCleanup(peer.close)
            peer.settimeout(5)
            peer.connect(str(self.computer_path))
        request = {"jsonrpc": "2.0", "id": 77, "method": "tools/call", "params": {
            "name": "vision", "arguments": {"steps": [{"tool": "delay_ms", "args": {"ms": 600}}], "observations": False}}}
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

    def test_fresh_locked_status_initializes_canvas_in_both_frontends(self):
        self.start_shared(locked=True)
        for no_vision in (False, True):
            client = self.client(no_vision=no_vision)
            data = self.assert_locked_status(client)
            window = data["apps"][0]["windows"][0]["window_id"]
            result = client.tool("accessibility", observations={"window": window, "refresh": True})
            self.assertFalse(result["isError"], result)
            self.assertIn("elements", read_result(result)["observations"][0])
        self.assertEqual(json.loads(self.state.read_text())["captures"], 2)
        self.assertEqual(json.loads(self.login_state.read_text())["unlocks"], 0)

    def test_locked_vision_rejects_every_observation_form_before_effects(self):
        self.start_shared(locked=True)
        client = self.client()
        forms = ({}, {"observations": {"window": 1}},
                 {"observations": {"window": 1, "refresh": True}}, {"observations": False})
        for form in forms:
            for steps in ([], [{"tool": "delay_ms", "args": {"ms": 1}}],
                          [{"tool": "press_keys", "args": {"keys": ["a"]}},
                           {"tool": "type_text", "args": {"text": "unsafe"}}]):
                with self.subTest(form=form, steps=steps):
                    result = client.tool("vision", steps=steps, **form)
                    self.assertTrue(result["isError"], result)
                    self.assertEqual(set(read_result(result)), {"code", "hint"})
                    self.assertEqual(read_result(result)["code"], "desktop_locked")
                    self.assertTrue(read_result(result)["hint"].startswith(
                        "The screen is locked. Vision tools are unavailable."))
                    self.assertFalse(any(b["type"] == "image" for b in result["content"]))
        state = json.loads(self.state.read_text())
        self.assertEqual((state["connections"], state["captures"]), (0, 0))
        self.assertEqual(json.loads(self.login_state.read_text())["unlocks"], 0)
        self.login_call("SetLocked", "false")
        self.assertFalse(client.tool("vision", steps=[{"tool": "press_keys", "args": {"keys": ["a"]}}],
                                     observations=False)["isError"])

    def test_locked_keyboard_fallback_does_not_focus_or_select(self):
        self.start_accessibility(keyboard=True, selection=True)
        self.start_shared()
        client = self.client(no_vision=True)
        tree = self.snapshot_accessibility(client, client.windows()[0]["window_id"])
        entry = next(r for r in tree["elements"] if r["label"] == "Fixture entry")
        before = json.loads(self.atspi_state.read_text())
        self.login_call("SetLocked", "true")
        for name, extra in (("type_text", {"text": "blocked"}), ("set_value", {"value": "blocked"})):
            result = client.tool("accessibility", steps=[{"tool": name, "args": {
                "element_id": entry["element_id"], **extra}}])
            self.assertEqual(read_result(result)["failed"]["code"], "desktop_locked", result)
            self.assertEqual(read_result(result)["failed"]["hint"],
                "This control requires keyboard input, which is unavailable while the screen is locked.")
            self.assertFalse(any(b["type"] == "image" for b in result["content"]))
        after = json.loads(self.atspi_state.read_text())
        self.assertEqual(after["focus_requests"], before["focus_requests"])
        self.assertEqual(after["selection_restores"], before["selection_restores"])
        self.assertEqual(after["text"], before["text"])
        self.assertEqual(json.loads(self.state.read_text())["input"]["text"], "")
        self.login_call("SetLocked", "false")
        result = client.tool("accessibility", steps=[{"tool": "type_text", "args": {
            "element_id": entry["element_id"], "text": "allowed"}}])
        self.assertFalse(result["isError"], result)

    def test_independent_lock_indicators_and_inspection_failure_recover(self):
        self.start_shared()
        client = self.client()
        for method in ("SetScreenLocked", "SetLockedHint"):
            self.login_call(method, "true")
            self.assert_locked_status(client)
            result = client.tool("vision", observations=False)
            self.assertEqual(read_result(result)["code"], "desktop_locked")
            self.login_call(method, "false")
            self.assertFalse(read_result(self.status(client))["locked"])
        self.configure(lock_query_error=True)
        for name in ("status", "vision", "accessibility"):
            result = client.tool(name)
            self.assertEqual(read_result(result)["code"], "desktop_session_unavailable", result)
            self.assertFalse(any(b["type"] == "image" for b in result["content"]))
        self.configure(lock_query_error=False)
        self.assertFalse(read_result(self.status(client))["locked"])
        self.assertEqual(json.loads(self.login_state.read_text())["unlocks"], 0)

    def test_lock_during_vision_delay_preserves_completed_and_stops_batch(self):
        self.start_shared()
        client = self.client()
        self.status(client)
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(client.tool, "vision", steps=[
                {"tool": "press_keys", "args": {"keys": ["a"]}},
                {"tool": "delay_ms", "args": {"ms": 2000}},
                {"tool": "type_text", "args": {"text": "blocked"}}], observations=False)
            time.sleep(.4)
            self.login_call("SetLocked", "true")
            result = pending.result(timeout=5)
        data = read_result(result)
        self.assertEqual((data["completed"], data["failed"]["code"]), (1, "desktop_locked"), result)
        self.assertEqual(json.loads(self.state.read_text())["input"]["text"], "")
        self.assertFalse(any(b["type"] == "image" for b in result["content"]))

    def test_lock_during_window_capture_suppresses_image(self):
        self.start_accessibility(two_windows=True)
        self.start_shared()
        client = self.client()
        windows = [w["window_id"] for w in client.windows()]
        captures = json.loads(self.state.read_text())["captures"]
        self.configure(hold_capture_at=captures + 1)
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(client.tool, "vision", observations={"window": windows[0], "refresh": True})
            self.wait_until(lambda: json.loads(self.state.read_text())["captures"] == captures + 1)
            self.login_call("SetScreenLocked", "true")
            result = pending.result(timeout=5)
        data = read_result(result)
        self.assertEqual((data["status"], data["completed"], data["failed"]["code"]), ("failed", 0, "desktop_locked"), result)
        self.assertEqual(len(data["observations"]), 1)
        self.assertFalse(any(b["type"] == "image" for b in result["content"]))
        self.login_call("SetScreenLocked", "false")
        self.assertFalse(client.tool("vision", observations={"window": windows[0], "refresh": True})["isError"])

    def test_lock_during_tree_read_keeps_helper_and_element_rings(self):
        self.start_shared()
        clients = [self.client(), self.client(no_vision=True)]
        window = clients[0].windows()[0]["window_id"]
        trees = [self.snapshot_accessibility(client, window) for client in clients]
        before = json.loads(self.atspi_state.read_text())
        hold = self.atspi_state.with_suffix(".hold-attributes")
        hold.touch()
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(clients[0].tool, "vision", observations={"window": window, "refresh": True})
            self.wait_until(lambda: json.loads(self.atspi_state.read_text())["attribute_queries"] > before["attribute_queries"])
            self.login_call("SetLocked", "true")
            hold.unlink()
            result = pending.result(timeout=5)
        self.assertTrue(result["isError"], result)
        self.assertFalse(any(b["type"] == "image" for b in result["content"]))
        for client, tree in zip(clients, trees):
            new = read_result(client.tool("accessibility", observations={"window": window, "refresh": True}))["observations"][0]
            self.assertEqual([r["element_id"] for r in new["elements"]], [r["element_id"] for r in tree["elements"]])
            button = next(r for r in tree["elements"] if r["label"] == "Fixture button")
            self.assertFalse(client.tool("accessibility", steps=[{"tool": "activate", "args": {
                "element_id": button["element_id"]}}])["isError"])
        self.assertEqual(json.loads(self.atspi_state.read_text())["registrations"], before["registrations"])

    def test_lock_during_drag_releases_held_input(self):
        self.start_shared(triple=True)
        client = self.client()
        self.status(client)
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(client.tool, "vision", steps=[
                {"tool": "drag", "args": {"from_x": 40, "from_y": 60,
                    "to_x": 400, "to_y": 60, "duration_ms": 2000}},
                {"tool": "type_text", "args": {"text": "blocked"}}])
            time.sleep(.3)
            self.login_call("SetLocked", "true")
            result = pending.result(timeout=5)
        self.assertEqual(read_result(result)["failed"]["code"], "desktop_locked", result)
        self.assertEqual(read_result(result)["completed"], 0)
        self.assert_locked_status(client)  # refresh the independent input oracle
        state = json.loads(self.state.read_text())["input"]
        self.assertEqual((state["buttons_down"], state["keys_down"], state["text"]), (0, 0, ""))

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
            result = json.loads(data)["result"]
            self.assertFalse(result["isError"], result)
            window = read_result(result)["apps"][0]["windows"][0]["window_id"]
            sock.sendall(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "accessibility", "arguments": {
                    "observations": {"window": window, "refresh": True}}}}).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n"):
                part = sock.recv(65536)
                self.assertTrue(part, "host disconnected before tree response")
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
        result = client.tool("accessibility", steps=[{"tool": "activate", "args": {
            "element_id": button["element_id"]}}])
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
        status = read_result(self.status(client))
        app = status["apps"][0]
        window = app["windows"][0]
        return client.window_state(window_id=window["window_id"])

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
        result = client.tool("accessibility", steps=[{"tool": "activate", "args": {
            "element_id": button["element_id"]}}])
        self.assertFalse(result["isError"], result)
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 1)

    def test_clients_share_capture_but_have_private_element_ids_and_one_helper(self):
        self.start_host()
        first, second = self.client(), self.client()
        self.status(first)
        # Pixel input uses the host's latest overview across Spaces/clients.
        result = second.tool("vision",
            steps=[{"tool": "click", "args": {"x": 40, "y": 40}}], observations=False)
        self.assertFalse(result["isError"], result)
        tree = self.snapshot(first)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        result = second.tool("accessibility", steps=[{"tool": "activate", "args": {
            "element_id": button["element_id"]}}])
        self.assertEqual(read_result(result)["failed"]["code"], "stale_element")
        own_tree = self.snapshot(second)
        self.assertEqual([row["element_id"] for row in own_tree["elements"]], list(range(4)))
        result = first.tool("accessibility", steps=[{"tool": "activate", "args": {
            "element_id": button["element_id"]}}])
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
        window = first.windows()[0]["window_id"]
        self.status(second)
        self.assertEqual(state()["registrations"], 0, "status activated accessibility")
        self.snapshot_accessibility(first, window)
        self.snapshot_accessibility(second, window)
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
        self.snapshot_accessibility(third, window)
        self.assertEqual(state()["registrations"], 2)

    def element_tree(self, client, window, **settings):
        path = self.atspi_state.with_suffix(".elements.json")
        temporary = path.with_suffix(".next")
        temporary.write_text(json.dumps(settings))
        temporary.replace(path)
        result = client.tool("accessibility", observations={"window": window, "refresh": True})
        self.assertFalse(result["isError"], result)
        tree = read_result(result)["observations"][0]
        self.assertFalse(tree["truncated"], tree)
        return tree["elements"]

    def test_element_ids_follow_objects_across_windows_reordering_and_property_changes(self):
        self.start_accessibility(two_windows=True, element_ids=True)
        self.start_host()
        first, second = self.client(), self.client()
        one, two = [window["window_id"] for window in first.windows()]
        initial = self.element_tree(first, one, one=["button", "entry"], two=["button/other"])
        self.assertEqual([row["element_id"] for row in initial], [0, 1, 2])
        changed = self.element_tree(first, one, one=["entry", "button"], two=["button/other"],
                                    labels={"button": "Renamed button"})
        self.assertEqual([row["element_id"] for row in changed], [0, 2, 1])
        self.assertEqual([row["element_index"] for row in changed], [0, 1, 2])
        self.assertEqual(changed[-1]["label"], "Renamed button")
        other = read_result(first.tool("accessibility", observations={"window": two, "refresh": True}))["observations"][0]
        self.assertEqual([row["element_id"] for row in other["elements"]], [3, 4])
        own = self.element_tree(second, two, one=["entry"], two=["button/other"])
        self.assertEqual([row["element_id"] for row in own], [0, 1])
        # ID 1 resolves to different objects in the two independent tables,
        # including an element now absent from the first window's tree.
        for client, expected in ((first, "button"), (second, "button/other")):
            result = client.tool("accessibility", steps=[{"tool": "activate", "args": {"element_id": 1}}])
            self.assertFalse(result["isError"], result)
            self.assertTrue(json.loads(self.atspi_state.read_text())["last_action_path"].endswith("/" + expected))
        first.close()
        third = self.client()
        fresh = self.element_tree(third, one, one=["entry"], two=["button/other"])
        self.assertEqual([row["element_id"] for row in fresh], [0, 1])
        self.assertFalse(second.tool("accessibility", steps=[{"tool": "activate", "args": {"element_id": 1}}])["isError"])

    def check_element_ring(self, *, keyboard):
        # Full production capacity is checked locally by the native harness.
        # The smaller ring keeps all end-to-end zero-ID/action/eviction checks.
        self.host_binary = self.ring_host_binary
        self.start_accessibility(element_ids=True, keyboard=keyboard)
        self.start_host()
        client = self.client()
        window = client.windows()[0]["window_id"]
        # Fill this integration build: one window root plus 63 distinct buttons.
        for start in range(0, 63, 16):
            stop = min(start + 16, 63)
            rows = self.element_tree(client, window, one=[f"button/{i}" for i in range(start, stop)])
            self.assertEqual(rows[0]["element_id"], 0)
            self.assertEqual([row["element_id"] for row in rows[1:]], list(range(start + 1, stop + 1)))
            if start == 0:
                # Reads and actions must not turn allocation order into LRU.
                self.assertFalse(client.tool("accessibility", steps=[{"tool": "activate", "args": {"element_id": 1}}])["isError"])
                repeated = self.element_tree(client, window, one=[f"button/{i}" for i in range(start, stop)])
                self.assertEqual([row["element_id"] for row in repeated], [row["element_id"] for row in rows])
        # The next new object replaces the root at zero and supports both
        # activation and editing, exercising zero through every action route.
        rows = self.element_tree(client, window, one=["entry"])
        self.assertEqual(rows[-1]["element_id"], 0)
        result = client.tool("accessibility", steps=[
            {"tool": "activate", "args": {"element_id": 0}},
            {"tool": "set_value", "args": {"element_id": 0, "value": "zero"}},
            {"tool": "type_text", "args": {"element_id": 0, "text": "!"}},
        ])
        self.assertFalse(result["isError"], result)
        state = json.loads(self.atspi_state.read_text())
        self.assertTrue(state["last_action_path"].endswith("/entry"))
        if keyboard:
            self.assertEqual(json.loads(self.state.read_text())["input"]["text"], "zero!")
        else:
            self.assertEqual(state["text"], "zero!")
        # Re-observing the evicted root allocates 1, overwriting button/0.
        # Reintroducing that button must allocate 2, not reuse its evicted ID.
        rows = self.element_tree(client, window, one=["entry", "button/0"])
        self.assertEqual([row["element_id"] for row in rows], [1, 0, 2])
        result = client.tool("accessibility", steps=[{"tool": "activate", "args": {"element_id": 2}}])
        self.assertFalse(result["isError"], result)
        self.assertTrue(json.loads(self.atspi_state.read_text())["last_action_path"].endswith("/button/0"))

    def test_element_ring_wraparound_and_zero_semantic_actions(self):
        self.check_element_ring(keyboard=False)

    def test_element_ring_wraparound_and_zero_keyboard_fallback(self):
        self.check_element_ring(keyboard=True)

    def test_element_ids_reject_legacy_and_malformed_arguments_before_actions(self):
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        for tool, field, value in (("activate", None, None), ("set_value", "value", "x"),
                                   ("type_text", "text", "x")):
            extra = {field: value} if field else {}
            arguments = [{"element_id": value, **extra} for value in
                         (-1, 20000, 1.5, True, None, "1", "s00000001:1", [], {})]
            arguments += [{"element_token": "s00000001:1", **extra},
                          {"element_id": button["element_id"], "element_token": "old", **extra},
                          {"element_id": button["element_id"], "client_id": 1, **extra},
                          {"element_id": button["element_id"], "clients": [1], **extra}]
            for args in arguments:
                with self.subTest(tool=tool, args=args):
                    result = client.tool("accessibility", steps=[
                        {"tool": "activate", "args": {"element_id": button["element_id"]}},
                        {"tool": tool, "args": args}])
                    self.assertEqual(read_result(result)["code"], "invalid_arguments", result)
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 0)
        self.assertEqual(json.loads(self.atspi_state.read_text())["text"], "")
        for element_id in (4, 19999):
            result = client.tool("accessibility", steps=[{"tool": "activate", "args": {"element_id": element_id}}])
            self.assertEqual(read_result(result)["failed"]["code"], "stale_element")

    def test_defunct_element_id_fails_without_retargeting(self):
        self.start_accessibility(element_ids=True)
        self.start_host()
        client = self.client()
        window = client.windows()[0]["window_id"]
        rows = self.element_tree(client, window, one=["button"])
        element_id = rows[-1]["element_id"]
        self.element_tree(client, window, one=["button"], defunct=["button"])
        result = client.tool("accessibility", steps=[{"tool": "activate", "args": {"element_id": element_id}}])
        self.assertEqual(read_result(result)["failed"]["code"], "stale_element")
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 0)

    def test_worker_discards_disconnected_clients_element_tables(self):
        self.start_host()
        worker = self.child([str(self.host_binary), "--atspi-worker", "10"], stdin=subprocess.PIPE,
                            env={**self.environment, "DBUS_SESSION_BUS_ADDRESS": self.address})
        self.addCleanup(worker.stdin.close)

        def request(serial, live, tool, **args):
            worker.stdin.write(json.dumps({"client_id": serial, "clients": live,
                                           "tool": tool, "args": args}) + "\n")
            worker.stdin.flush()
            self.assertTrue(select.select([worker.stdout], [], [], 5)[0])
            return json.loads(worker.stdout.readline())

        listing = request(1, [1, 2], "list_apps")["output"]["apps"][0]
        window = listing["windows"][0]["window_id"]
        args = {"window_id": window, "pid": listing["pid"]}
        tree = request(1, [1, 2], "window_state", **args)["output"]
        self.assertEqual([row["element_id"] for row in tree["elements"]], list(range(4)))
        self.assertIn("output", request(2, [1, 2], "window_state", **args))
        # The next request after connection 1 closes drops only its table.
        self.assertIn("output", request(2, [2], "activate", element_id=1))
        self.assertEqual(request(1, [1, 2], "activate", element_id=1)["error"]["code"], "stale_element")
        self.assertIn("output", request(2, [1, 2], "activate", element_id=1))
        tree = request(1, [1, 2], "window_state", **args)["output"]
        self.assertEqual([row["element_id"] for row in tree["elements"]], list(range(4)))

    def test_element_tables_reset_after_accessibility_worker_restart(self):
        import signal
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        child_file = Path(f"/proc/{self.host.pid}/task/{self.host.pid}/children")
        worker = int(child_file.read_text().strip())
        os.kill(worker, signal.SIGKILL)
        result = client.tool("accessibility", steps=[{"tool": "activate", "args": {"element_id": 1}}])
        self.assertEqual(read_result(result)["failed"]["code"], "accessibility_unavailable", result)
        result = client.tool("accessibility", steps=[{"tool": "activate", "args": {"element_id": 1}}])
        self.assertEqual(read_result(result)["failed"]["code"], "stale_element")
        rows = read_result(client.tool("accessibility", observations={"window": tree["window_id"], "refresh": True}))["observations"][0]["elements"]
        self.assertEqual([row["element_id"] for row in rows], list(range(4)))
        self.assertNotEqual(int(child_file.read_text().strip()), worker)
        self.assertFalse(client.tool("accessibility", steps=[{"tool": "activate", "args": {"element_id": 1}}])["isError"])

    def test_frame_attributes_activate_delayed_null_placeholder_children(self):
        self.start_accessibility(activation=250)
        self.start_host()
        client = self.client()
        started = time.monotonic()
        tree = self.snapshot(client)
        self.assertIn("Fixture button", [row["label"] for row in tree["elements"]])
        self.assertLess(time.monotonic() - started, 3)
        state = json.loads(self.atspi_state.read_text())
        # Refresh once before association and once before the selected snapshot.
        self.assertEqual(state["attribute_queries"], 2)
        self.assertNotIn("snapshot_id", tree)
        self.assertEqual([row["element_id"] for row in self.snapshot(client)["elements"]],
                         [row["element_id"] for row in tree["elements"]])
        self.assertEqual(json.loads(self.atspi_state.read_text())["attribute_queries"], 4)

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
        self.start_accessibility(two_windows=True, activation=1500)
        self.start_host()
        client = self.client()
        windows = client.windows()
        client.serial += 1
        client.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": client.serial,
            "method": "tools/call", "params": {"name": "accessibility", "arguments": {
                "observations": {"window": windows[0]["window_id"]}}}}) + "\n")
        client.process.stdin.flush()
        self.wait_until(lambda: json.loads(self.atspi_state.read_text())["attribute_queries"] > 0)
        client.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
            "params": {"requestId": client.serial}}) + "\n")
        client.process.stdin.flush()
        self.wait_until(lambda: not json.loads(self.atspi_state.read_text())["listeners"])
        self.assertFalse(select.select([client.process.stdout], [], [], .1)[0])
        self.assertEqual(json.loads(self.atspi_state.read_text())["registrations"], 1)
        self.status(client)
        self.assertEqual(json.loads(self.atspi_state.read_text())["registrations"], 1)
        self.snapshot_accessibility(client, windows[0]["window_id"])
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
        self.assertEqual(read_result(client.tool("status"))["code"], "accessibility_permission_required")
        self.assertIn("result", client.call("ping", {}))
        self.computer_path.chmod(0o600)
        self.status(client)

    def test_no_vision_discovery_and_modern_empty_capabilities(self):
        self.start_host()
        metadata = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {}}
        for disabled, expected in ((False, ["status", "vision", "accessibility"]),
                                   (True, ["status", "accessibility"])):
            client = self.client(no_vision=disabled)
            discovered = client.call("tools/list", {"_meta": metadata})["result"]["tools"]
            self.assertEqual([tool["name"] for tool in discovered], expected)
            instructions = client.call("initialize", {"protocolVersion": "2025-11-25"})["result"]["instructions"]
            self.assertEqual("no vision support" in instructions, disabled)
            if disabled:
                self.assertEqual(read_result(client.tool("vision"))["code"], "unknown_tool")
            self.assertEqual(read_result(client.tool("computer"))["code"], "unknown_tool")
        self.assertEqual(json.loads(self.state.read_text())["geometry_requests"], 0)
        self.assertEqual(json.loads(self.atspi_state.read_text())["registrations"], 0)

    def test_readable_status_lists_canvas_and_windows_in_both_modes(self):
        self.start_host()
        for no_vision in (False, True):
            client = self.client(no_vision=no_vision)
            response = client.call("tools/call", {"name": "status", "arguments": {}})["result"]
            data = read_result(response)
            text = response["content"][0]["text"]
            window = data["apps"][0]["windows"][0]
            self.assertIn("Screen: unlocked", text)
            self.assertIn("Canvas: 160 x 120\n", text)
            self.assertNotIn("; id:", text)
            self.assertIn(f'window_id={window["window_id"]} {json.dumps(window["title"])} active bounds=', text)
            self.assertNotIn('"apps":', text)
            self.assertEqual(any(b["type"] == "image" for b in response["content"]), not no_vision)

    def test_readable_frames_round_without_changing_canvas_geometry(self):
        self.start_host()
        client = self.client()
        for x, rounded, button_x in ((10.25, 21, 31), (10.2, 20, 30), (-0.25, -1, 10), (-0.2, 0, 10)):
            with self.subTest(x=x):
                self.configure(window_x=x)
                response = client.call("tools/call", {"name": "status", "arguments": {}})["result"]
                window = read_result(response)["apps"][0]["windows"][0]
                self.assertEqual(window["bounds"]["x"], rounded)
                self.assertIn(f"bounds=[{rounded},20,80,60]", response["content"][0]["text"])
                tree = client.window_state(window_id=window["window_id"])
                button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
                self.assertEqual(button["frame"]["x"], button_x)
                self.assertTrue(all(type(v) is int for row in tree["elements"] if row["frame"]
                                    for v in row["frame"].values()))

    def test_readable_trees_are_the_only_public_representation(self):
        self.start_accessibility(offscreen=True)
        self.start_host()
        for no_vision in (False, True):
            client = self.client(no_vision=no_vision)
            window = client.windows()[0]["window_id"]
            for tool in (("vision", "accessibility") if not no_vision else ("accessibility",)):
                response = client.call("tools/call", {"name": tool, "arguments": {"observations": {"window": window, "refresh": True}}})["result"]
                self.assertFalse(response["isError"], response)
                self.assertEqual(json.dumps(response).count("Fixture button"), 1)
                tree = read_result(response)["observations"][0]
                self.assertNotIn('"elements"', json.dumps(response))
                text = tree["tree_markdown"]
                self.assertTrue(text.startswith('[0] frame "scu test window"'), text)
                self.assertIn('  [1] button "Fixture button"', text)
                self.assertNotIn('actions=', text)
                self.assertNotIn('activate', text)
                self.assertNotIn('open_context_menu', text)
                self.assertIn('type_text set_value', text)
                self.assertIn(text, response["content"][0]["text"])
                if tool == "vision":
                    image = tree["screenshot"]
                    self.assertIn(f"Screenshot: {image['width']} x {image['height']}; bounds=", response["content"][0]["text"])
                self.assertNotIn('"element_index"', json.dumps(response))
                rows = read_result(response)["observations"][0]["elements"]
                button = next(row for row in rows if row["label"] == "Fixture button")
                if tool == "vision":
                    self.assertEqual(button["frame"]["y"], 620)
                else:
                    self.assertNotIn("frame=", text)
                    self.assertIsNone(button["frame"])
                self.assertEqual(any(b["type"] == "image" for b in response["content"]), tool == "vision")
                self.assertFalse(client.tool("accessibility", steps=[{"tool": "activate", "args": {
                    "element_id": button["element_id"]}}])["isError"])
            # Quoted, multiline UTF-8 values must stay on one escaped tree line.
            entry = next(row for row in rows if row["label"] == "Fixture entry")
            expected = 'Héllo "世界"\nsecond line'
            response = client.call("tools/call", {"name": "accessibility", "arguments": {
                "steps": [{"tool": "set_value", "args": {"element_id": entry["element_id"], "value": expected}}],
                "observations": {"window": window, "refresh": True}}})["result"]
            self.assertFalse(response["isError"], response)
            tree = read_result(response)["observations"][0]
            self.assertEqual(len(tree["tree_markdown"].splitlines()), len(rows))
            actual = read_result(response)["observations"][0]["elements"]
            self.assertEqual(next(row["value"] for row in actual if row["label"] == "Fixture entry"), expected)

    def test_decorative_wrappers_preserve_controls_without_allocating_targets(self):
        self.start_accessibility(decorations=True, offscreen=True)
        self.start_host()
        for no_vision in (False, True):
            client = self.client(no_vision=no_vision)
            window = client.windows()[0]["window_id"]
            for tool in (("vision", "accessibility") if not no_vision else ("accessibility",)):
                result = client.tool(tool, observations={"window": window, "refresh": True})
                self.assertFalse(result["isError"], result)
                tree = read_result(result)["observations"][0]
                rows = tree["elements"]
                self.assertEqual([row["element_id"] for row in rows], list(range(5)))
                self.assertFalse(any(row["role"] == "tool bar" or row["label"] == "Separator" for row in rows))
                self.assertEqual([row["label"] for row in rows[1:]],
                                 ["Fixture button", "Fixture entry", "Fixture password", "Focusable divider"])
                self.assertTrue(all(row["depth"] == 1 and row["parent_index"] == 0 for row in rows[1:]))
                if tool == "vision":
                    self.assertNotIn("id", tree)
                    self.assertEqual(rows[1]["frame"]["y"], 620)
                else:
                    self.assertNotIn("id", tree)
                    self.assertNotIn("; id:", result["content"][0]["text"])
                self.assertFalse(client.tool("accessibility", steps=[
                    {"tool": "activate", "args": {"element_id": rows[1]["element_id"]}}])["isError"])

    def test_accessibility_reads_noninteractive_text_without_coordinates(self):
        self.start_accessibility(reading_text=True)
        self.atspi_state.with_suffix(".actions.json").write_text(json.dumps(["clickAncestor", "showContextMenu"]))
        self.start_shared()
        client = self.client()
        window = client.windows()[0]["window_id"]
        before = client.call("tools/call", {"name": "vision", "arguments": {"observations": {"window": window, "refresh": True}}})["result"]
        before_tree = read_result(before)["observations"][0]["tree_markdown"]
        self.assertNotIn("Read-only paragraph text", before_tree)
        self.assertFalse(any(row["role"] in ("static", "paragraph", "heading", "text")
                             for row in read_result(before)["observations"][0]["elements"]))
        self.assertEqual(json.loads(self.atspi_state.read_text())["reading_text_reads"], 0)
        self.login_call("SetLocked", "true")
        for current in (client, self.client(no_vision=True)):
            self.assert_locked_status(current)
            captures = json.loads(self.state.read_text())["captures"]
            response = current.call("tools/call", {"name": "accessibility", "arguments": {"observations": {"window": window, "refresh": True}}})["result"]
            self.assertFalse(response["isError"], response)
            self.assertNotIn("frame=", json.dumps(response))
            self.assertNotIn('"bounds"', json.dumps(response))
            tree = read_result(response)["observations"][0]["tree_markdown"]
            self.assertIn("Read-only paragraph text, with a nested caption.", tree)
            self.assertIn("The complete caption text is not its accessible name.", tree)
            self.assertEqual(tree.count("The complete caption text is not its accessible name."), 1)
            self.assertIn("Article heading", tree)
            self.assertNotIn("Paragraph metadata", tree)
            self.assertNotIn("Article paragraph", tree)
            rows = read_result(response)["observations"][0]["elements"]
            self.assertFalse(any(row["role"] in ("paragraph", "static", "heading") for row in rows))
            text_rows = [row for row in rows if row["role"] == "text"]
            self.assertTrue(text_rows)
            self.assertTrue(all("element_id" not in row and "value" not in row and not row.get("actions")
                                and row["frame"] is None and len(row["label"]) >= 5 for row in text_rows))
            self.assertTrue(any("by C++: x | y." in row["label"] for row in text_rows))
            mixed = next(row for row in text_rows if "Article heading" in row["label"])
            self.assertIn("The complete caption text is not its accessible name.", mixed["label"])
            self.assertIn("Another paragraph.", mixed["label"])
            self.assertIn("Before code #define main oldmain #include <unistd.h> After code", mixed["label"])
            self.assertEqual(tree.count("#define main oldmain"), 1)
            self.assertEqual(tree.count("#include <unistd.h>"), 1)
            self.assertEqual(sum(row["label"].count('int fd = memfd_create("fuzz space", 0);') for row in text_rows), 1)
            self.assertFalse(any('memfd_create( "fuzz space" , 0 );' in row["label"] for row in text_rows))
            self.assertTrue(any('print("ab");' in row["label"] for row in text_rows))
            self.assertTrue(any('print("a b");' in row["label"] for row in text_rows))
            self.assertEqual(tree.count("An owning type like"), 1)
            self.assertTrue(any("An owning type like std::string and view std::string_view" in row["label"] for row in text_rows))
            self.assertIn("Only Text exposes this prefix", tree)
            self.assertIn("and its suffix.", tree)
            self.assertIn("inline code", tree)
            self.assertEqual(tree.count("Complete reading text."), 40)
            self.assertNotIn("text_parts", json.dumps(response))
            self.assertNotIn("text_partial", json.dumps(response))
            self.assertTrue(any("Echo Echo" in row["label"] for row in text_rows))
            self.assertIn("Consider applying for YC's Winter 2027 batch! Applications are open till November 2.", tree)
            self.assertEqual(tree.count("Detailed sentence."), 20)
            for phrase in ("Detailed sentence.", "Complete reading text."):
                recovered = next(row for row in text_rows if phrase in row["label"])
                self.assertNotIn("text_truncated", recovered)
            clipped = next(row for row in text_rows if "Truly long reading text." in row["label"])
            self.assertTrue(clipped["text_truncated"])
            self.assertIn("…", clipped["label"])
            self.assertNotIn("四字字字", tree)
            self.assertTrue(any(row["label"] == "ab cd" for row in text_rows))
            ids = [row["element_id"] for row in rows if "element_id" in row]
            self.assertEqual(sorted(ids), list(range(5)))
            missing = current.tool("accessibility", steps=[{"tool": "activate", "args": {"element_id": 5}}])
            self.assertEqual(read_result(missing)["failed"]["code"], "stale_element")
            link = [row for row in rows if row["label"] == "Article link"]
            self.assertEqual(len(link), 1, link)
            self.assertEqual(link[0]["role"], "link")
            self.assertEqual(tree.count("Article link"), 1)
            self.assertEqual(link[0]["parent_index"], 0)
            self.assertNotIn("actions", link[0])
            for tool in ("activate", "open_context_menu"):
                result = current.tool("accessibility", steps=[{"tool": tool, "args": {"element_id": link[0]["element_id"]}}])
                self.assertFalse(result["isError"], result)
            self.assertFalse(any(block["type"] == "image" for block in response["content"]))
            self.assertEqual(json.loads(self.state.read_text())["captures"], captures)
        state = json.loads(self.atspi_state.read_text())
        self.assertEqual((state["password_reads"], state["focus_requests"]), (0, 0))
        self.assertEqual(json.loads(self.login_state.read_text())["unlocks"], 0)
        self.login_call("SetLocked", "false")
        after = client.call("tools/call", {"name": "vision", "arguments": {"observations": {"window": window, "refresh": True}}})["result"]
        self.assertEqual(read_result(after)["observations"][0]["tree_markdown"], before_tree)
        self.assertTrue(any(block["type"] == "image" for block in after["content"]))

    def test_readable_batch_keeps_window_errors_and_completed_actions(self):
        self.start_host()
        client = self.client(no_vision=True)
        window = client.windows()[0]["window_id"]
        response = client.call("tools/call", {"name": "accessibility", "arguments": {
            "steps": [{"tool": "delay_ms", "args": {"ms": 0}}],
            "observations": {"window": window + 99}}})["result"]
        self.assertTrue(response["isError"], response)
        self.assertEqual(read_result(response)["completed"], 1)
        text = response["content"][0]["text"]
        self.assertIn("Status: failed. completed: 1", text)
        self.assertIn(f"Window {window + 99}", text)
        self.assertIn("Error: window_unavailable", text)
        recovered = client.tool("accessibility", observations={"window": window})
        self.assertFalse(recovered["isError"], recovered)
        self.assertIn('button "Fixture button"', recovered["content"][0]["text"])
        self.assertNotIn('"elements"', json.dumps(response))

    def test_no_vision_status_initializes_canvas_and_is_frontend_local(self):
        self.start_shared()
        hidden, normal = self.client(no_vision=True), self.client()
        response = hidden.tool("status")
        self.assertFalse(response["isError"], response)
        self.assertEqual([block["type"] for block in response["content"]], ["text"])
        data = read_result(response)
        self.assertIn("Screen: unlocked", response["content"][0]["text"])
        self.assertIn("Windows:", response["content"][0]["text"])
        self.assertNotIn("; id:", response["content"][0]["text"])
        self.assertEqual(set(data["canvas"]), {"width", "height"})
        self.assertGreater(json.loads(self.state.read_text())["captures"], 0)
        window = data["apps"][0]["windows"][0]["window_id"]
        tree = hidden.tool("accessibility", observations={"window": window, "refresh": True})
        self.assertFalse(tree["isError"], tree)
        self.assertIn("elements", read_result(tree)["observations"][0])
        self.assertFalse(any(block["type"] == "image" for block in tree["content"]))
        self.assertTrue(any(block["type"] == "image" for block in normal.tool("status")["content"]))
        self.assertFalse(normal.tool("vision")["isError"])
        self.assertEqual(read_result(hidden.tool("vision"))["code"], "unknown_tool")
        hidden.close()
        self.assertFalse(normal.tool("vision")["isError"])

    def test_accessibility_handoff_and_observations_follow_actions(self):
        self.start_host()
        client = self.client()
        initial = client.tool("accessibility", observations={"window": 1, "refresh": True})
        self.assertEqual(read_result(initial)["observations"][0]["error"]["code"], "window_unavailable")
        self.assertEqual(json.loads(self.state.read_text())["captures"], 0)
        tree = self.snapshot(client)  # Vision supplies IDs directly to accessibility.
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        element_id = button["element_id"]
        before = json.loads(self.state.read_text())["captures"]
        for _ in range(2):
            result = client.tool("accessibility", steps=[{"tool": "activate", "args": {
                "element_id": element_id}}])
            self.assertFalse(result["isError"], result)
            self.assertEqual(read_result(result)["observations"], [])
        result = client.tool("accessibility", steps=[{"tool": "activate", "args": {
            "element_id": element_id}}], observations={"window": tree["window_id"], "refresh": True})
        self.assertFalse(result["isError"], result)
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 3)
        self.assertIn("elements", read_result(result)["observations"][0])
        reused = client.tool("accessibility", steps=[{"tool": "activate", "args": {"element_id": element_id}}])
        self.assertFalse(reused["isError"], reused)
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 4)
        self.assertEqual(json.loads(self.state.read_text())["captures"], before)

    def test_accessibility_single_window_errors_and_action_failure_preserve_trees(self):
        self.start_accessibility(two_windows=True)
        self.start_host()
        client = self.client()
        one, two = [w["window_id"] for w in client.windows()]
        for window in (two, one):
            result = client.tool("accessibility", observations={"window": window})
            self.assertFalse(result["isError"], result)
            data = read_result(result)
            self.assertEqual([item["window_id"] for item in data["observations"]], [window])
            self.assertIn("elements", data["observations"][0])
            self.assertFalse(any(b["type"] == "image" for b in result["content"]))
        failed = client.tool("accessibility", observations={"window": max(one, two) + 99999})
        self.assertEqual(read_result(failed)["observations"][0]["error"]["code"], "window_unavailable")
        failed = client.tool("accessibility", steps=[{"tool": "activate", "args": {
            "element_id": 19999}}], observations={"window": one, "refresh": True})
        self.assertTrue(failed["isError"], failed)
        self.assertIn("elements", read_result(failed)["observations"][0])

    def test_accessibility_observation_waits_for_action_rendering(self):
        self.start_accessibility(settle_after_action=True)
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        before = json.loads(self.state.read_text())["captures"]
        for delay in (None, 200):
            with self.subTest(delay=delay):
                steps = [{"tool": "activate", "args": {"element_id": button["element_id"]}}]
                if delay is not None:
                    steps.append({"tool": "delay_ms", "args": {"ms": delay}})
                result = client.tool("accessibility", steps=steps,
                                     observations={"window": tree["window_id"], "refresh": True})
                self.assertFalse(result["isError"], result)
                rows = read_result(result)["observations"][0]["elements"]
                entry = next(row for row in rows if row["label"] == "Fixture entry")
                self.assertEqual(entry["value"], "Settled after action")
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 2)
        self.assertEqual(json.loads(self.state.read_text())["captures"], before)

    def test_split_batch_preflight_rejects_cross_tool_actions_and_observation_options(self):
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        activate = {"tool": "activate", "args": {"element_id": button["element_id"]}}
        click = {"tool": "click", "args": {"x": 60, "y": 50}}
        for name, arguments in (
            ("vision", {"steps": [click, activate]}),
            ("accessibility", {"steps": [activate, click]}),
            ("vision", {"steps": [click, {"tool": "type_text", "args": {"text": "x", "element_id": "unused"}}]}),
            ("accessibility", {"steps": [activate, {"tool": "type_text", "args": {"text": "x"}}]}),
            ("accessibility", {"steps": [activate], "observe": False}),
            ("accessibility", {"steps": [activate], "id": 1}),
            ("accessibility", {"steps": [activate], "screenshot": False}),
            ("vision", {"steps": [click], "observe": False}),
        ):
            with self.subTest(name=name, arguments=arguments):
                result = client.tool(name, **arguments)
                self.assertEqual(read_result(result)["code"], "invalid_arguments", result)
        for observations in ([], [tree["window_id"]], [tree["window_id"]] * 65, [True], [1.5], ["1"], {}, True, [None],
                             {"window": True}, {"window": 1.5}, {"window": "1"}, {"refresh": True},
                             {"window": tree["window_id"], "refresh": 1},
                             {"window": tree["window_id"], "extra": False}, None):
            for name, action in (("accessibility", activate), ("vision", click)):
                result = client.tool(name, steps=[action], observations=observations)
                self.assertEqual(read_result(result)["code"], "invalid_arguments", result)
        for name in ("vision", "accessibility"):
            result = client.tool(name, steps=[{"tool": "delay_ms", "args": {"ms": 0}}] * 65)
            self.assertEqual(read_result(result)["code"], "invalid_arguments", result)
        self.assertEqual(json.loads(self.state.read_text())["input"]["clicks"], 0)
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 0)
        accepted = client.tool("accessibility", steps=[{"tool": "delay_ms", "args": {"ms": 0}}] * 64,
                               observations={"window": tree["window_id"], "refresh": True})
        self.assertFalse(accepted["isError"], accepted)
        self.assertEqual(len(read_result(accepted)["observations"]), 1)

    def test_vision_single_window_observations_preserve_images_and_errors(self):
        self.start_accessibility(two_windows=True)
        self.start_host()
        client = self.client()
        one, two = [window["window_id"] for window in client.windows()]
        for window in (two, one):
            result = client.tool("vision", observations={"window": window})
            self.assertFalse(result["isError"], result)
            observations = read_result(result)["observations"]
            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0]["window_id"], window)
            self.assertIn("elements", observations[0])
            self.assertIn("screenshot", observations[0])
            self.assertEqual(sum(b["type"] == "image" for b in result["content"]), 1)
        failed = client.tool("vision", observations={"window": max(one, two) + 99999})
        self.assertTrue(failed["isError"], failed)
        self.assertEqual(read_result(failed)["observations"][0]["error"]["code"], "window_unavailable")
        self.assertFalse(any(b["type"] == "image" for b in failed["content"]))
        self.assertEqual(json.loads(self.state.read_text())["window_captures"], ["window-2", "window-1"])
        result = client.tool("vision")
        self.assertFalse(result["isError"], result)
        self.assertEqual(self.image(result).size, (160, 120))

    def test_accessibility_keyboard_fallback_never_captures(self):
        self.start_accessibility(keyboard=True)
        self.start_host()
        client = self.client(no_vision=True)
        window = client.windows()[0]["window_id"]
        tree = read_result(client.tool("accessibility", observations={"window": window, "refresh": True}))["observations"][0]
        entry = next(row for row in tree["elements"] if row["label"] == "Fixture entry")
        captures = json.loads(self.state.read_text())["captures"]
        self.configure(hold_capture=True)
        response = client.tool("accessibility", steps=[{"tool": "type_text", "args": {
            "element_id": entry["element_id"], "text": "hello"}}], observations={"window": window, "refresh": True})
        self.assertFalse(response["isError"], response)
        self.assertFalse(any(block["type"] == "image" for block in response["content"]))
        actual = next(row for row in read_result(response)["observations"][0]["elements"]
                      if row["label"] == "Fixture entry")
        self.assertEqual(actual["value"], "hello")
        self.assertEqual(json.loads(self.state.read_text())["captures"], captures)

    def observe_tree(self, client, window, tool="accessibility", **options):
        response = client.tool(tool, observations={"window": window, **options})
        self.assertFalse(response["isError"], response)
        self.assertEqual(sum(b["type"] == "image" for b in response["content"]), tool == "vision")
        observation = read_result(response)["observations"][0]
        marker = "Tree (diff):" if "tree_diff" in observation else "Tree (full):"
        self.assertIn(marker, response["content"][0]["text"])
        return observation

    def test_observation_baselines_are_per_client_tool_and_last_window(self):
        self.start_accessibility(two_windows=True)
        self.start_host()
        first, second = self.client(), self.client(no_vision=True)
        one, two = [w["window_id"] for w in first.windows()]
        self.assertIn("tree_markdown", self.observe_tree(first, one))
        self.assertEqual(self.observe_tree(first, one)["tree_diff"], "No Changes.\n")
        self.assertIn("tree_markdown", self.observe_tree(first, one, "vision"))
        captures = json.loads(self.state.read_text())["captures"]
        self.assertEqual(self.observe_tree(first, one, "vision")["tree_diff"], "No Changes.\n")
        self.assertGreater(json.loads(self.state.read_text())["captures"], captures)
        self.assertEqual(self.observe_tree(first, one)["tree_diff"], "No Changes.\n")
        self.assertIn("tree_markdown", self.observe_tree(second, one))
        self.assertIn("tree_markdown", self.observe_tree(first, two))
        self.assertIn("tree_markdown", self.observe_tree(first, one))
        self.assertEqual(self.observe_tree(first, one)["tree_diff"], "No Changes.\n")
        self.assertIn("tree_markdown", self.observe_tree(first, one, refresh=True))
        self.assertEqual(self.observe_tree(first, one, refresh=False)["tree_diff"], "No Changes.\n")
        # Neither status/desktop capture nor observation-free calls replace a
        # window baseline; each live tool read still uses its own rendering.
        first.tool("status")
        first.tool("vision")
        first.tool("vision", observations=False)
        first.tool("accessibility")
        self.assertEqual(self.observe_tree(first, one, "vision")["tree_diff"], "No Changes.\n")
        self.assertEqual(self.observe_tree(first, one)["tree_diff"], "No Changes.\n")
        first.close()
        self.assertIn("tree_markdown", self.observe_tree(self.client(), one))
        self.assertEqual(self.observe_tree(second, one)["tree_diff"], "No Changes.\n")

    def test_live_window_edits_diff_and_refresh_reconstruct_the_same_tree(self):
        self.start_accessibility(element_ids=True)
        self.start_host()
        client = self.client()
        window = client.windows()[0]["window_id"]
        settings = {"one": ["entry", *(f"button/{i}" for i in range(60))]}
        path = self.atspi_state.with_suffix(".elements.json")
        path.write_text(json.dumps(settings))
        for tool in ("accessibility", "vision"):
            previous = self.observe_tree(client, window, tool, refresh=True)
            entry = next(r for r in previous["elements"] if r["label"] == "Fixture entry")
            expected = "Héllo 世界" if tool == "accessibility" else "next value"
            response = client.tool("accessibility", steps=[{"tool": "set_value", "args": {
                "element_id": entry["element_id"], "value": expected}}])
            self.assertFalse(response["isError"], response)
            changed = self.observe_tree(client, window, tool)
            self.assertIn("@@ -", changed["tree_diff"])
            full = self.observe_tree(client, window, tool, refresh=True)
            self.assertEqual(apply_tree_diff(previous["tree_markdown"], changed["tree_diff"]), full["tree_markdown"])
            self.assertIn(expected, full["tree_markdown"])
            self.assertEqual(self.observe_tree(client, window, tool)["tree_diff"], "No Changes.\n")
        previous = self.observe_tree(client, window, refresh=True)
        # Change child order, insert and remove controls, and rename an object
        # without changing its element ID. The compact tree must reconstruct.
        settings["one"] = ["entry", "button/new", "button/3", "button/1", *settings["one"][5:]]
        settings["labels"] = {"button/1": "Renamed 🌍"}
        path.write_text(json.dumps(settings))
        changed = self.observe_tree(client, window)
        full = self.observe_tree(client, window, refresh=True)
        self.assertEqual(apply_tree_diff(previous["tree_markdown"], changed["tree_diff"]), full["tree_markdown"])
        self.assertIn("Renamed 🌍", full["tree_markdown"])
        settings["labels"] = {name: "Replacement " + name for name in settings["one"]}
        path.write_text(json.dumps(settings))
        rewritten = self.observe_tree(client, window)
        self.assertIn("tree_markdown", rewritten)
        self.assertNotIn("tree_diff", rewritten)
        self.assertEqual(self.observe_tree(client, window)["tree_diff"], "No Changes.\n")

    def test_observation_failures_and_failed_actions_preserve_baseline_rules(self):
        self.start_accessibility(element_ids=True)
        self.start_host()
        client = self.client()
        window = client.windows()[0]["window_id"]
        self.element_tree(client, window, one=[f"button/{i}" for i in range(60)])
        previous = self.observe_tree(client, window, refresh=True)["tree_markdown"]
        missing = client.tool("accessibility", observations={"window": window + 99999})
        self.assertTrue(missing["isError"], missing)
        self.assertEqual(self.observe_tree(client, window)["tree_diff"], "No Changes.\n")
        path = self.atspi_state.with_suffix(".elements.json")
        path.write_text(json.dumps({"one": [f"button/{i}" for i in range(60)], "labels": {"button/0": "Changed"}}))
        failed = client.tool("accessibility", steps=[{"tool": "activate", "args": {
            "element_id": 19999}}], observations={"window": window})
        self.assertTrue(failed["isError"], failed)
        diff = read_result(failed)["observations"][0]["tree_diff"]
        self.assertEqual(self.observe_tree(client, window)["tree_diff"], "No Changes.\n")
        full = self.observe_tree(client, window, refresh=True)["tree_markdown"]
        self.assertEqual(apply_tree_diff(previous, diff), full)
        # A screenshot error still returns a valid tree and advances vision's
        # baseline. Accessibility errors with no tree cannot advance it.
        self.observe_tree(client, window, "vision")
        path.write_text(json.dumps({"one": [f"button/{i}" for i in range(60)], "labels": {"button/0": "Changed again"}}))
        self.configure(refuse=True)
        failed = client.tool("vision", observations={"window": window})
        self.assertTrue(failed["isError"], failed)
        self.assertIn("tree_diff", read_result(failed)["observations"][0])
        self.configure(refuse=False)
        self.assertEqual(self.observe_tree(client, window, "vision")["tree_diff"], "No Changes.\n")

    def test_truncated_observations_always_return_full_trees(self):
        # Hit the depth budget with a small tree. The byte-budget test below
        # separately covers large responses; this test covers baseline handling.
        self.start_accessibility(element_ids=True, deep=100)
        self.start_host()
        client = self.client()
        window = client.windows()[0]["window_id"]
        path = self.atspi_state.with_suffix(".elements.json")
        path.write_text(json.dumps({"one": ["panel/0"]}))
        for _ in range(2):
            response = self.observe_tree(client, window)
            self.assertTrue(response["truncated"])
            self.assertIn("tree_markdown", response)
            self.assertNotIn("tree_diff", response)
        path.write_text(json.dumps({"one": ["entry"]}))
        response = self.observe_tree(client, window)
        self.assertFalse(response["truncated"])
        self.assertIn("tree_markdown", response)
        self.assertEqual(self.observe_tree(client, window)["tree_diff"], "No Changes.\n")

    def test_locked_vision_observation_does_not_advance_its_baseline(self):
        self.start_accessibility(element_ids=True)
        self.start_shared()
        client = self.client()
        window = client.windows()[0]["window_id"]
        settings = {"one": [f"button/{i}" for i in range(30)]}
        path = self.atspi_state.with_suffix(".elements.json")
        path.write_text(json.dumps(settings))
        previous = self.observe_tree(client, window, "vision")["tree_markdown"]
        settings["labels"] = {"button/0": "Changed while capturing"}
        path.write_text(json.dumps(settings))
        captures = json.loads(self.state.read_text())["captures"]
        self.configure(hold_capture_at=captures + 1)
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(client.tool, "vision", observations={"window": window})
            self.wait_until(lambda: json.loads(self.state.read_text())["captures"] == captures + 1)
            self.login_call("SetLocked", "true")
            suppressed = pending.result(timeout=5)
        self.assertTrue(suppressed["isError"], suppressed)
        self.assertFalse(any(b["type"] == "image" for b in suppressed["content"]))
        self.login_call("SetLocked", "false")
        changed = self.observe_tree(client, window, "vision")["tree_diff"]
        current = self.observe_tree(client, window, "vision", refresh=True)["tree_markdown"]
        self.assertNotEqual(changed, "No Changes.\n")
        self.assertEqual(apply_tree_diff(previous, changed), current)

    def test_worker_restart_invalidates_both_tool_baselines_for_all_clients(self):
        import signal
        self.start_host()
        first, second = self.client(), self.client()
        window = first.windows()[0]["window_id"]
        for client in (first, second):
            for tool in ("vision", "accessibility"):
                self.observe_tree(client, window, tool)
        worker = int(Path(f"/proc/{self.host.pid}/task/{self.host.pid}/children").read_text().strip())
        os.kill(worker, signal.SIGKILL)
        failed = first.tool("accessibility", observations={"window": window})
        self.assertTrue(failed["isError"], failed)
        for client in (first, second):
            for tool in ("vision", "accessibility"):
                self.assertIn("tree_markdown", self.observe_tree(client, window, tool))
                self.assertEqual(self.observe_tree(client, window, tool)["tree_diff"], "No Changes.\n")

    def test_catalog_and_permission_errors(self):
        client = self.client()
        tools = client.call("tools/list", {})["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, {"vision", "accessibility", "status"})
        schema = next(tool["inputSchema"] for tool in tools if tool["name"] == "vision")
        self.assertEqual(set(schema["properties"]), {"steps", "observations"})
        variants = schema["properties"]["observations"]["anyOf"]
        self.assertEqual(variants[0]["type"], "object")
        self.assertEqual(variants[0]["required"], ["window"])
        self.assertEqual(variants[0]["properties"]["window"]["type"], "integer")
        self.assertEqual(variants[0]["properties"]["refresh"], {"type": "boolean", "default": False})
        self.assertFalse(variants[0]["additionalProperties"])
        self.assertEqual(variants[1], {"type": "boolean", "enum": [False]})
        self.assertNotIn('"zoom"', json.dumps(schema))
        self.assertNotIn('"screenshot"', json.dumps(schema))
        actions = {step["properties"]["tool"]["enum"][0]: step["properties"]["args"]
                   for step in schema["properties"]["steps"]["items"]["anyOf"]}
        self.assertEqual(list(actions), ["click", "move_cursor", "drag", "scroll", "press_keys",
                                         "type_text", "delay_ms"])
        self.assertEqual(actions["click"]["required"], ["x", "y"])
        self.assertEqual(set(actions["click"]["properties"]), {"x", "y", "button", "count"})
        accessibility = next(tool["inputSchema"] for tool in tools if tool["name"] == "accessibility")
        self.assertEqual(set(accessibility["properties"]), {"steps", "observations"})
        self.assertEqual(accessibility["properties"]["observations"], {**variants[0], "description": accessibility["properties"]["observations"]["description"]})
        semantic = {step["properties"]["tool"]["enum"][0]: step["properties"]["args"]
                    for step in accessibility["properties"]["steps"]["items"]["anyOf"]}
        self.assertEqual(list(semantic), ["activate", "open_context_menu", "type_text", "set_value", "delay_ms"])
        for name in ("activate", "open_context_menu"):
            self.assertEqual(semantic[name]["required"], ["element_id"])
            self.assertEqual(set(semantic[name]["properties"]), {"element_id"})
            prop = semantic[name]["properties"]["element_id"]
            self.assertEqual((prop["type"], prop["minimum"], prop["maximum"]), ("integer", 0, 19999))
        self.assertEqual(actions["type_text"]["required"], ["text"])
        self.assertNotIn("element_id", actions["type_text"]["properties"])
        self.assertEqual(semantic["type_text"]["required"], ["element_id", "text"])
        for field in ('"max_depth"', '"max_elements"', '"tree_format"', '"max_image_dimension"', '"pid"', '"by"'):
            self.assertNotIn(field, json.dumps(schema))
        delay = next(step["properties"]["args"] for step in schema["properties"]["steps"]["items"]["anyOf"]
                     if step["properties"]["tool"]["enum"] == ["delay_ms"])
        self.assertEqual(delay["required"], ["ms"])
        self.assertEqual(set(delay["properties"]), {"ms"})
        for field in ('"target"', '"display_id"', '"delivery_mode"', '"session"', '"capture_id"'):
            self.assertNotIn(field, json.dumps(schema))
        for name, args in (("status", {}), ("vision", {}),
                           ("vision", {"steps": [{"tool": "press_keys", "args": {"keys": ["a"]}}]})):
            result = client.tool(name, **args)
            self.assertTrue(result["isError"], result)
            self.assertEqual(read_result(result)["code"], "accessibility_permission_required")
            self.assertIn("spaces configure <space>", read_result(result)["hint"])

    def test_status_image_and_kwin_window_coordinates(self):
        self.start_host(two_windows=True)
        result = self.status(self.client())
        data = read_result(result)
        self.assertEqual(set(data), {"canvas", "apps", "locked"})
        self.assertFalse(data["locked"])
        self.assertEqual(self.image(result).size, (160, 120))
        canvas = data["canvas"]
        self.assertEqual(set(canvas), {"width", "height"})
        self.assertEqual((canvas["width"], canvas["height"]), (160, 120))
        self.assertEqual(len(data["apps"]), 1)
        windows = data["apps"][0]["windows"]
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0]["bounds"], {"x": 20, "y": 20, "width": 80, "height": 60})
        self.assertEqual(windows[1]["bounds"]["x"], 30)
        self.assertEqual(self.computer_path.stat().st_mode & 0o777, 0o600)

    def test_semantics_use_unified_element_frames_and_ids(self):
        self.start_accessibility()
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        self.assertNotIn("id", tree)
        rows = {row["label"]: row for row in tree["elements"]}
        self.assertNotIn("actions", rows["Fixture button"])
        self.assertEqual(rows["Fixture entry"]["actions"], ["type_text", "set_value"])
        self.assertNotIn("text_input", rows["Fixture entry"])
        self.assertNotIn("actions", rows["Fixture password"])
        self.assertEqual(rows["Fixture button"]["frame"], {"x": 30, "y": 30, "width": 20, "height": 10})
        self.assertNotIn("value", rows["Fixture password"])
        captures = json.loads(self.state.read_text())["captures"]
        result = client.tool("accessibility", steps=[
            {"tool": "activate", "args": {"element_id": rows["Fixture button"]["element_id"]}},
            {"tool": "set_value", "args": {"element_id": rows["Fixture entry"]["element_id"], "value": "Héllo 世界"}},
            {"tool": "type_text", "args": {"element_id": rows["Fixture entry"]["element_id"], "text": "!"}},
            {"tool": "delay_ms", "args": {"ms": 50}},
        ])
        self.assertFalse(result["isError"], result)
        self.assertEqual(set(read_result(result)), {"completed", "status", "observations"})
        self.assertEqual(read_result(result)["status"], "ok")
        self.assertNotIn("observation", read_result(result))
        self.assertEqual(json.loads(self.state.read_text())["captures"], captures)
        state = json.loads(self.atspi_state.read_text())
        self.assertEqual(state["clicks"], 1)
        self.assertEqual(state["text"], "Héllo 世界!")
        self.assertEqual(state["password_reads"], 0)
        self.snapshot(client)
        reused = client.tool("accessibility", steps=[{"tool": "activate", "args": {"element_id": rows["Fixture button"]["element_id"]}}])
        self.assertFalse(reused["isError"], reused)

    def test_numeric_value_and_range_are_observed_and_set_without_keyboard(self):
        self.start_accessibility(numeric=True)
        self.start_host()
        client = self.client()
        row = next(row for row in self.snapshot(client)["elements"] if row["label"] == "Fixture volume")
        self.assertEqual((row["value"], row["minimum"], row["maximum"]), (80, 0, 100))
        result = client.tool("accessibility", steps=[{"tool": "set_value", "args": {
            "element_id": row["element_id"], "value": "50"}}])
        self.assertEqual(read_result(result)["status"], "ok", result)
        self.assertEqual(json.loads(self.atspi_state.read_text())["value"], 50)
        self.assertEqual(json.loads(self.state.read_text())["input"]["keys"], 0)

    def test_invalid_numeric_ranges_are_not_advertised_and_can_recover(self):
        self.start_accessibility(numeric=True)
        self.start_host()
        client = self.client()
        baseline = next(row for row in self.snapshot(client)["elements"] if row["label"] == "Fixture volume")
        overrides = self.atspi_state.with_suffix(".value.json")
        for properties in ({"MinimumValue": 100, "MaximumValue": 0},
                           {"MinimumValue": 0, "MaximumValue": 0, "CurrentValue": 0},
                           {"MinimumValue": float("nan")}, {"MaximumValue": float("inf")},
                           {"CurrentValue": float("nan")}, {"CurrentValue": 101},
                           {"MinimumValue": 4.6e-310, "MaximumValue": 0}):
            with self.subTest(properties=properties):
                overrides.write_text(json.dumps(properties))
                for tool in ("vision", "accessibility"):
                    result = client.tool(tool, observations={"window": client.windows()[0]["window_id"], "refresh": True})
                    self.assertFalse(result["isError"], result)
                    row = next(row for row in read_result(result)["observations"][0]["elements"]
                               if row["label"] == "Fixture volume")
                    self.assertEqual(row["element_id"], baseline["element_id"])
                    self.assertTrue({"value", "minimum", "maximum", "actions"}.isdisjoint(row), row)
        overrides.unlink()
        recovered = next(row for row in self.snapshot(client)["elements"] if row["label"] == "Fixture volume")
        self.assertEqual(recovered["element_id"], baseline["element_id"])
        self.assertEqual((recovered["value"], recovered["minimum"], recovered["maximum"]), (80, 0, 100))
        self.assertEqual(recovered["actions"], ["set_value"])
        self.assertEqual(json.loads(self.atspi_state.read_text())["value_writes"], 0)
        self.assertEqual(json.loads(self.state.read_text())["input"]["keys"], 0)

    def test_delayed_numeric_setter_is_verified_before_keyboard_fallback(self):
        self.start_accessibility(numeric=True, numeric_delay=True)
        self.start_host()
        client = self.client()
        row = next(row for row in self.snapshot(client)["elements"] if row["label"] == "Fixture volume")
        result = client.tool("accessibility", steps=[{"tool": "set_value", "args": {
            "element_id": row["element_id"], "value": "50"}}])
        self.assertEqual(read_result(result)["status"], "ok", result)
        self.assertEqual(json.loads(self.atspi_state.read_text())["value"], 50)
        self.assertEqual(json.loads(self.state.read_text())["input"]["keys"], 0)

    def test_ignored_slider_setter_uses_verified_keyboard_and_readback(self):
        self.start_accessibility(numeric=True, numeric_keyboard=True)
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        row = next(row for row in tree["elements"] if row["label"] == "Fixture volume")
        result = client.tool("accessibility", steps=[{"tool": "set_value", "args": {
            "element_id": row["element_id"], "value": "50"}}],
            observations={"window": tree["window_id"], "refresh": True})
        self.assertEqual(read_result(result)["status"], "ok", result)
        actual = next(row for row in read_result(result)["observations"][0]["elements"]
                      if row["label"] == "Fixture volume")
        self.assertEqual(actual["value"], 50)
        self.assertEqual(json.loads(self.atspi_state.read_text())["value"], 80,
                         "the independent keyboard receiver must supply the changed value")
        self.assertEqual(json.loads(self.state.read_text())["input"]["keys"], 6)

    def test_numeric_keyboard_refusal_stops_the_batch(self):
        self.start_accessibility(numeric=True, numeric_keyboard=True, numeric_readonly=True)
        self.start_host()
        client = self.client()
        row = next(row for row in self.snapshot(client)["elements"] if row["label"] == "Fixture volume")
        result = read_result(client.tool("accessibility", steps=[
            {"tool": "set_value", "args": {"element_id": row["element_id"], "value": "50"}},
            {"tool": "set_value", "args": {"element_id": row["element_id"], "value": "60"}}]))
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["failed"]["code"], "value_readback_failed")
        self.assertEqual(json.loads(self.state.read_text())["input"]["keys"], 1)

    def test_numeric_keyboard_requires_focus(self):
        self.start_accessibility(numeric=True, numeric_keyboard=True, refuse_focus=True)
        self.start_host()
        client = self.client()
        row = next(row for row in self.snapshot(client)["elements"] if row["label"] == "Fixture volume")
        result = read_result(client.tool("accessibility", steps=[{"tool": "set_value", "args": {
            "element_id": row["element_id"], "value": "50"}}]))
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(json.loads(self.state.read_text())["input"]["keys"], 0)

    def test_numeric_keyboard_stops_after_focus_loss(self):
        self.start_accessibility(numeric=True, numeric_keyboard=True, lose_focus=True)
        self.start_host()
        client = self.client()
        row = next(row for row in self.snapshot(client)["elements"] if row["label"] == "Fixture volume")
        result = read_result(client.tool("accessibility", steps=[{"tool": "set_value", "args": {
            "element_id": row["element_id"], "value": "50"}}]))
        self.assertEqual(result["failed"]["code"], "value_focus_changed", result)
        self.assertEqual(json.loads(self.state.read_text())["input"]["keys"], 1)

    def test_numeric_keyboard_requires_the_matched_active_window(self):
        self.start_accessibility(numeric=True, numeric_keyboard=True)
        self.start_host(active=False)
        client = self.client()
        row = next(row for row in self.snapshot(client)["elements"] if row["label"] == "Fixture volume")
        result = read_result(client.tool("accessibility", steps=[{"tool": "set_value", "args": {
            "element_id": row["element_id"], "value": "50"}}]))
        self.assertEqual(result["failed"]["code"], "value_focus_changed", result)
        self.assertEqual(json.loads(self.state.read_text())["input"]["keys"], 0)

    def test_numeric_invalid_values_are_not_submitted(self):
        self.start_accessibility(numeric=True)
        self.start_host()
        client = self.client()
        row = next(row for row in self.snapshot(client)["elements"] if row["label"] == "Fixture volume")
        for value in ("NaN", "infinity", "oops", "101", "-1", ""):
            result = read_result(client.tool("accessibility", steps=[{"tool": "set_value", "args": {
                "element_id": row["element_id"], "value": value}}]))
            self.assertEqual(result["failed"]["code"], "invalid_arguments", result)
        self.assertEqual(json.loads(self.atspi_state.read_text())["value_writes"], 0)
        self.assertEqual(json.loads(self.state.read_text())["input"]["keys"], 0)

    def test_native_text_setter_cannot_report_success_without_readback(self):
        self.start_accessibility(readonly=True)
        self.start_host()
        client = self.client()
        row = next(row for row in self.snapshot(client)["elements"] if row["label"] == "Fixture entry")
        result = read_result(client.tool("accessibility", steps=[{"tool": "set_value", "args": {
            "element_id": row["element_id"], "value": "changed"}}]))
        self.assertEqual(result["failed"]["code"], "text_readback_failed", result)
        self.assertEqual(json.loads(self.atspi_state.read_text())["text"], "")

    def test_toolkit_actions_invoke_without_redundant_tree_flags(self):
        self.start_host()
        client = self.client()
        for name in ("click", "jump", "open", "doDefault", "press", "clickAncestor", "check", "toggle", "select"):
            with self.subTest(name=name):
                self.atspi_state.with_suffix(".actions.json").write_text(json.dumps(["scroll", "showContextMenu", name]))
                row = next(row for row in self.snapshot(client)["elements"] if row["label"] == "Fixture button")
                self.assertNotIn("actions", row)
                self.assertNotIn("click_buttons", row)
                for tool, expected in (("activate", name), ("open_context_menu", "showContextMenu")):
                    result = read_result(client.tool("accessibility", steps=[{"tool": tool, "args": {
                        "element_id": row["element_id"]}}]))
                    self.assertEqual(result["status"], "ok", result)
                    self.assertEqual(json.loads(self.atspi_state.read_text())["last_action"], expected)
        for names, unsupported in ((["scroll", "showContextMenu"], "activate"),
                                   (["jump"], "open_context_menu"),
                                   (["scroll"], "activate")):
            with self.subTest(names=names):
                self.atspi_state.with_suffix(".actions.json").write_text(json.dumps(names))
                row = next(row for row in self.snapshot(client)["elements"] if row["label"] == "Fixture button")
                self.assertNotIn("actions", row)
                self.assertNotIn("click_buttons", row)
                before = json.loads(self.atspi_state.read_text())["clicks"]
                result = read_result(client.tool("accessibility", steps=[{"tool": unsupported, "args": {
                    "element_id": row["element_id"]}}]))
                self.assertEqual(result["failed"]["code"], "accessible_action_failed", result)
                self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], before)
        self.status(client)
        self.assertEqual(json.loads(self.state.read_text())["input"]["clicks"], 0)

    def test_semantic_and_pointer_arguments_fail_before_batch_effects(self):
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        invalid = [("click", {"element_id": "unused"}),
                   ("click", {"x": 60, "y": 50, "element_id": "unused"}),
                   ("click", {"x": 60}), ("click", {"y": 50})]
        for tool in ("activate", "open_context_menu"):
            invalid.append((tool, {}))
            for extra in ({"button": "left"}, {"count": 2}, {"x": 60}, {"y": 50}, {"id": 1}):
                invalid.append((tool, {"element_id": button["element_id"], **extra}))
        for tool, arguments in invalid:
            with self.subTest(tool=tool, arguments=arguments):
                semantic = tool in ("activate", "open_context_menu")
                first = ({"tool": "activate", "args": {"element_id": button["element_id"]}}
                         if semantic else {"tool": "click", "args": {"x": 60, "y": 50}})
                result = client.tool("accessibility" if semantic else "vision",
                    steps=[first, {"tool": tool, "args": arguments}],
                    **({} if semantic else {}))
                self.assertTrue(result["isError"], result)
                self.assertEqual(read_result(result)["code"], "invalid_arguments", result)
        self.status(client)
        self.assertEqual(json.loads(self.state.read_text())["input"]["clicks"], 0)
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 0)

    def test_pixel_clicks_preserve_button_and_count(self):
        self.start_host()
        client = self.client()
        self.status(client)
        clicks = 0
        for button, count, code in (("middle", 1, 274), ("left", 2, 272),
                                    ("right", 3, 273), ("middle", 2, 274)):
            with self.subTest(button=button, count=count):
                result = read_result(client.tool("vision", steps=[{"tool": "click", "args": {
                    "x": 60, "y": 50, "button": button, "count": count}}]))
                self.assertEqual(result["status"], "ok", result)
                clicks += count
                received = json.loads(self.state.read_text())["input"]
                self.assertEqual(received["clicks"], clicks)
                self.assertEqual(received["button"], code)
                self.assertEqual(received["buttons_down"], 0)
                self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 0)

    def test_keyboard_text_target_and_readback(self):
        self.start_accessibility(keyboard=True)
        self.start_host()
        client = self.client()
        row = next(row for row in self.snapshot(client)["elements"] if row["label"] == "Fixture entry")
        self.assertEqual(row["actions"], ["type_text", "set_value"])
        for tool, field, text, expected in (("type_text", "text", "Hello", "Hello"),
                                            ("type_text", "text", " world", "Hello world"),
                                            ("set_value", "value", "Changed", "Changed"),
                                            ("set_value", "value", "", "")):
            with self.subTest(tool=tool, text=text):
                result = read_result(client.tool("accessibility", steps=[{"tool": tool, "args": {
                    "element_id": row["element_id"], field: text}}]))
                self.assertEqual(result["status"], "ok", result)
                self.assertNotIn("warnings", result)
                self.assertEqual(result["observations"], [])
                self.assertEqual(json.loads(self.state.read_text())["input"]["text"], expected)
                self.assertEqual(json.loads(self.atspi_state.read_text())["focus_requests"], 1)

    def test_keyboard_text_requires_focused_target(self):
        self.start_accessibility(keyboard=True, refuse_focus=True)
        self.start_host()
        client = self.client()
        row = next(row for row in self.snapshot(client)["elements"] if row["label"] == "Fixture entry")
        result = read_result(client.tool("accessibility", steps=[{"tool": "type_text", "args": {
            "element_id": row["element_id"], "text": "must not type"}}]))
        self.assertEqual(result["failed"]["code"], "text_focus_changed", result)
        self.assertEqual(json.loads(self.state.read_text())["input"]["keys"], 0)

    def test_controls_remain_actionable_without_visibility_states(self):
        self.start_accessibility(hidden=True)
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        result = client.tool("accessibility", steps=[{"tool": "activate", "args": {
            "element_id": button["element_id"]}}])
        self.assertEqual(read_result(result)["status"], "ok", result)
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 1)

    def test_offscreen_controls_remain_actionable(self):
        self.start_accessibility(offscreen=True)
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        self.assertEqual(button["frame"]["y"], 620)
        result = client.tool("accessibility", steps=[{"tool": "activate", "args": {
            "element_id": button["element_id"]}}])
        self.assertEqual(read_result(result)["status"], "ok", result)
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 1)

    def test_dormant_tooltips_do_not_consume_tree_rows(self):
        self.start_accessibility(tooltips=True)
        self.start_host()
        tree = self.snapshot(self.client())
        self.assertNotIn("tool tip", [row["role"] for row in tree["elements"]])
        self.assertIn("Fixture button", [row["label"] for row in tree["elements"]])

    def test_empty_placeholder_buttons_do_not_hide_named_controls(self):
        self.start_accessibility(placeholders=200)
        self.start_host()
        tree = self.snapshot(self.client())
        self.assertFalse(tree["truncated"], tree)
        self.assertEqual(len(tree["elements"]), 4)
        self.assertIn("Fixture button", [row["label"] for row in tree["elements"]])

    def test_chromium_document_device_coordinates_are_normalized(self):
        self.start_accessibility(browser=True)
        self.start_host()
        tree = self.snapshot(self.client())
        document = next(row for row in tree["elements"] if row["role"] == "document web")
        self.assertEqual(document["frame"], {"x": 20, "y": 20, "width": 80, "height": 60})
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        self.assertEqual(button["frame"], {"x": 30, "y": 30, "width": 20, "height": 10})

    def test_chromium_media_title_preserves_window_association(self):
        self.start_accessibility(browser=True)
        self.atspi_state.with_suffix(".title").write_text("Video - YouTube - Audio playing - Chromium")
        self.start_host(window_title="Video - YouTube - Chromium")
        tree = self.snapshot(self.client())
        self.assertNotIn("accessibility_error", tree)
        self.assertIn("Fixture button", [row["label"] for row in tree["elements"]])

    def test_chromium_media_title_does_not_resolve_ambiguous_windows(self):
        self.start_accessibility(browser=True, duplicates=True)
        self.atspi_state.with_suffix(".title").write_text("Video - YouTube - Audio playing - Chromium")
        self.start_host(window_title="Video - YouTube - Chromium")
        tree = self.snapshot(self.client())
        self.assertEqual(tree["accessibility_error"]["code"], "accessibility_window_ambiguous")

    def test_changed_title_falls_back_to_unique_size_for_other_toolkits(self):
        self.start_accessibility()
        self.atspi_state.with_suffix(".title").write_text("New page title")
        self.start_host(window_title="Old page title")
        tree = self.snapshot(self.client())
        self.assertNotIn("accessibility_error", tree)
        self.assertIn("Fixture button", [row["label"] for row in tree["elements"]])

    def test_changed_title_with_mismatched_pid_falls_back_to_unique_size(self):
        self.start_accessibility(browser=True)
        self.start_host(window_title="New page title", app_pid=self.atspi.pid + 100000)
        tree = self.snapshot(self.client())
        self.assertNotIn("accessibility_error", tree)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        self.assertEqual(button["frame"], {"x": 30, "y": 30, "width": 20, "height": 10})

    def test_size_fallback_rejects_multiple_accessibility_candidates(self):
        self.start_accessibility(duplicates=True)
        self.start_host(window_title="Changed title")
        tree = self.snapshot(self.client())
        self.assertEqual(tree["accessibility_error"]["code"], "accessibility_window_ambiguous")
        self.assertNotIn("elements", tree)

    def test_pid_size_match_ignores_ambiguity_from_other_processes(self):
        self.start_accessibility()
        self.atspi_state.with_suffix(".title").write_text("Loading primary page")
        other, _ = self.add_accessibility_application()
        self.start_host(window_title="New page title", duplicate_identity=True, duplicate_pid=other.pid)
        tree = self.snapshot(self.client())
        self.assertNotIn("accessibility_error", tree)
        self.assertEqual(tree["elements"][0]["label"], "Loading primary page")

    def test_pid_size_match_precedes_title_match_in_other_process(self):
        self.start_accessibility()
        self.atspi_state.with_suffix(".title").write_text("Loading primary page")
        self.add_accessibility_application()
        self.start_host()
        tree = self.snapshot(self.client())
        self.assertNotIn("accessibility_error", tree)
        self.assertEqual(tree["elements"][0]["label"], "Loading primary page")

    def test_pid_ambiguity_is_not_resolved_by_another_process_title(self):
        self.start_accessibility(duplicates=True)
        self.atspi_state.with_suffix(".title").write_text("Loading primary page")
        self.add_accessibility_application()
        self.start_host()
        tree = self.snapshot(self.client())
        self.assertEqual(tree["accessibility_error"]["code"], "accessibility_window_ambiguous")
        self.assertNotIn("elements", tree)

    def test_size_fallback_rejects_multiple_compositor_candidates(self):
        self.start_accessibility()
        self.start_host(window_title="Changed title", duplicate_identity=True, duplicate_pid=self.atspi.pid)
        # Only one accessibility frame fits, but both compositor windows fit
        # it within the same process.
        tree = self.snapshot(self.client())
        self.assertEqual(tree["accessibility_error"]["code"], "accessibility_window_ambiguous")
        self.assertNotIn("elements", tree)

    def test_size_fallback_with_mismatched_pid_rejects_multiple_compositor_candidates(self):
        self.start_accessibility()
        self.start_host(window_title="Changed title", app_pid=self.atspi.pid + 100000, duplicate_identity=True)
        tree = self.snapshot(self.client())
        self.assertEqual(tree["accessibility_error"]["code"], "accessibility_window_ambiguous")
        self.assertNotIn("elements", tree)

    def test_title_match_disambiguates_equal_sizes(self):
        self.start_accessibility(two_windows=True)
        self.start_host()
        tree = self.snapshot(self.client())
        self.assertNotIn("accessibility_error", tree)
        self.assertEqual(tree["elements"][0]["label"], "scu test window")

    def test_transient_window_ambiguity_retries_until_titles_settle(self):
        self.start_accessibility(two_windows=True)
        self.start_host()
        client = self.client()
        window = client.windows()[0]["window_id"]
        for duplicate in (False, True):
            with self.subTest(duplicate_compositor_window=duplicate):
                requests = json.loads(self.state.read_text())["geometry_requests"]
                self.configure(window_title="Loading", duplicate_identity=duplicate,
                               duplicate_pid=self.atspi.pid,
                               settled_title_after_request=requests + 4,
                               settled_title="scu test window")
                result = client.tool("accessibility", observations={"window": window, "refresh": True})
                self.assertFalse(result["isError"], result)
                tree = read_result(result)["observations"][0]
                self.assertEqual(tree["elements"][0]["label"], "scu test window")

    def test_literal_punctuation_keys_and_chords(self):
        self.start_host()
        client = self.client()
        steps = [{"tool": "press_keys", "args": {"keys": keys}} for keys in
                 (["-"], ["+"], ["="], ["A"], ["MINUS"],
                  ["CTRL", "-"], ["CTRL", "+"], ["CTRL", "="])]
        result = client.tool("vision", steps=steps, observations=False)
        self.assertFalse(result["isError"], result)
        self.assertEqual(read_result(result)["completed"], len(steps))
        # The independent EIS receiver decodes the compositor keymap, including
        # implicit Shift for '+', and confirms all chord keys were released.
        received = json.loads(self.state.read_text())["input"]
        self.assertEqual(received["text"], "-+=a-")
        self.assertEqual(received["keys_down"], 0)

    def test_observations_refresh_metadata_without_another_status_call(self):
        self.start_host()
        client = self.client()
        window = client.windows()[0]["window_id"]
        for tool, title in (("vision", "First new title"), ("accessibility", "Second new title")):
            with self.subTest(tool=tool):
                self.atspi_state.with_suffix(".title").write_text(title)
                self.configure(window_title=title, window_x=12)
                before = json.loads(self.atspi_state.read_text())["attribute_queries"]
                result = client.tool(tool, observations={"window": window, "refresh": True})
                self.assertFalse(result["isError"], result)
                tree = read_result(result)["observations"][0]
                self.assertNotIn("accessibility_error", tree)
                self.assertEqual(tree["elements"][0]["label"], title)
                self.assertGreater(json.loads(self.atspi_state.read_text())["attribute_queries"], before)
                if tool == "vision":
                    self.assertEqual(tree["screenshot"]["bounds"]["x"], 24)

    def test_keyboard_text_uses_size_fallback_after_title_changes(self):
        self.start_accessibility(keyboard=True)
        self.start_host(app_pid=self.atspi.pid + 100000)
        client = self.client()
        row = next(row for row in self.snapshot(client)["elements"] if row["label"] == "Fixture entry")
        self.configure(window_title="New page title")
        result = client.tool("accessibility", steps=[{"tool": "set_value", "args": {
            "element_id": row["element_id"], "value": "hello"}}])
        self.assertFalse(result["isError"], result)
        self.assertEqual(json.loads(self.state.read_text())["input"]["text"], "hello")

    def test_keyboard_text_requires_active_window(self):
        self.start_accessibility(keyboard=True)
        self.start_host(active=False)
        client = self.client()
        row = next(row for row in self.snapshot(client)["elements"] if row["label"] == "Fixture entry")
        result = read_result(client.tool("accessibility", steps=[{"tool": "set_value", "args": {
            "element_id": row["element_id"], "value": "must not type"}}]))
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
        result = read_result(client.tool("accessibility", steps=[{"tool": "type_text", "args": {
            "element_id": row["element_id"], "text": "abc"}}]))
        self.assertEqual(result["failed"]["code"], "text_focus_changed", result)
        self.assertEqual(json.loads(self.state.read_text())["input"]["keys"], 1)

    def test_keyboard_text_readback_failure_is_not_retried(self):
        self.start_accessibility(keyboard=True, readonly=True)
        self.start_host()
        client = self.client()
        row = next(row for row in self.snapshot(client)["elements"] if row["label"] == "Fixture entry")
        result = read_result(client.tool("accessibility", steps=[{"tool": "type_text", "args": {
            "element_id": row["element_id"], "text": "abc"}}]))
        self.assertEqual(result["failed"]["code"], "text_readback_failed", result)
        self.assertEqual(json.loads(self.state.read_text())["input"]["text"], "abc")

    def test_click_buttons_counts_and_standalone_meta(self):
        self.start_host()
        client = self.client()
        self.status(client)
        result = read_result(client.tool("vision", steps=[
            {"tool": "click", "args": {"x": 60, "y": 50, "count": 2}},
            {"tool": "click", "args": {"x": 60, "y": 50, "button": "right"}},
            {"tool": "press_keys", "args": {"keys": ["meta"]}},
        ]))
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["completed"], 3)
        self.assertEqual(json.loads(self.state.read_text())["input"]["clicks"], 3)

    def test_deep_toolkit_wrappers_do_not_hide_controls(self):
        self.start_accessibility(deep=48)
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        self.assertFalse(tree["truncated"], tree)
        self.assertEqual(len(tree["elements"]), 4)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        self.assertEqual((button["depth"], button["parent_index"]), (1, 0))
        result = client.tool("accessibility", steps=[{"tool": "activate", "args": {"element_id": button["element_id"]}}])
        self.assertEqual(read_result(result)["status"], "ok", result)

    def test_large_tree_is_bounded_and_reports_complexity(self):
        self.start_accessibility(wide=2000, long_labels=True)
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        self.assertTrue(tree["truncated"])
        self.assertEqual(tree["accessibility_error"]["code"], "accessibility_tree_too_complex")
        size = len(json.dumps({k: v for k, v in tree.items() if k != "elements"}, separators=(",", ":")).encode())
        self.assertGreater(size, 500 * 1024)
        self.assertLessEqual(size, 512 * 1024)
        self.assertIn("tree_markdown", tree)
        button = next(row for row in tree["elements"] if row["role"] == "button")
        result = client.tool("accessibility", steps=[{"tool": "activate", "args": {"element_id": button["element_id"]}}])
        self.assertEqual(read_result(result)["status"], "ok", result)

    def test_large_tree_is_not_cut_off_by_previous_byte_or_row_limits(self):
        self.start_accessibility(wide=1000)
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        self.assertFalse(tree["truncated"], tree)
        self.assertEqual(len(tree["elements"]), 1001)
        self.assertGreater(len(json.dumps(tree, separators=(",", ":")).encode()), 18 * 1024)
        button = tree["elements"][-1]
        self.assertEqual(button["label"], "Repeated button 999")
        result = client.tool("accessibility", steps=[{"tool": "activate", "args": {
            "element_id": button["element_id"]}}])
        self.assertEqual(read_result(result)["status"], "ok", result)

    def test_excessive_depth_is_complex_not_unavailable(self):
        self.start_accessibility(deep=110)
        self.start_host()
        tree = self.snapshot(self.client())
        self.assertEqual(tree["accessibility_error"]["code"], "accessibility_tree_too_complex")
        self.assertTrue(tree["elements"])
        self.assertIn("screenshot", tree)

    def test_frame_attributes_are_refreshed_before_matching(self):
        self.start_accessibility(title_after_attributes=True, geometry_after_attributes=True)
        self.start_host()
        tree = self.snapshot(self.client())
        self.assertNotIn("accessibility_error", tree)
        self.assertIn("Fixture button", [row["label"] for row in tree["elements"]])

    def test_delayed_accessibility_window_is_discovered_without_navigation(self):
        self.start_accessibility(delayed_window=True)
        self.start_host()
        tree = self.snapshot(self.client())
        self.assertNotIn("accessibility_error", tree)
        self.assertIn("Fixture entry", [row["label"] for row in tree["elements"]])

    def test_window_title_is_refreshed_without_a_property_event(self):
        self.start_host()
        client = self.client()
        self.snapshot(client)
        self.atspi_state.with_suffix(".title").write_text("Changed window title")
        self.configure(window_title="Changed window title")
        tree = self.snapshot(client)
        self.assertNotIn("accessibility_error", tree)
        self.assertEqual(tree["elements"][0]["label"], "Changed window title")

    def test_observation_limits_and_formats_are_internal(self):
        self.start_host()
        client = self.client()
        window = client.windows()[0]
        for field, value in (("max_depth", 12), ("max_elements", 120), ("tree_format", "both"), ("max_image_dimension", 100), ("pid", 123)):
            result = client.tool("vision", observations={"window": window["window_id"], "refresh": True}, **{field: value})
            self.assertEqual(read_result(result)["code"], "invalid_arguments")

    def test_desktop_and_window_images_have_a_fixed_cap(self):
        self.start_host(image_cap=True)
        client = self.client()
        status = self.status(client)
        self.assertEqual(self.image(status).size, (2048, 32))
        window = read_result(status)["apps"][0]["windows"][0]
        shot = client.tool("vision", observations={"window": window["window_id"], "refresh": True})
        self.assertEqual(self.image(shot).size, (2048, 32))
        full = client.tool("vision")
        self.assertEqual(self.image(full).size, (2048, 32))

    def test_incompatible_accessibility_geometry_is_not_guessed(self):
        self.start_accessibility()
        self.start_host(bad_client=True)
        client = self.client()
        app = read_result(self.status(client))["apps"][0]
        result = client.tool("vision", observations={"window": app["windows"][0]["window_id"], "refresh": True})
        self.assertFalse(result["isError"], result)
        self.assertEqual(read_result(result)["observations"][0]["accessibility_error"]["code"], "accessibility_geometry_mismatch")
        self.assertEqual(self.image(result).size, (54, 60))

    def test_ambiguous_accessibility_window_is_not_selected_by_title(self):
        self.start_accessibility(duplicates=True)
        self.start_host()
        client = self.client()
        app = read_result(self.status(client))["apps"][0]
        result = client.tool("vision", observations={"window": app["windows"][0]["window_id"], "refresh": True})
        self.assertEqual(read_result(result)["observations"][0]["accessibility_error"]["code"], "accessibility_window_ambiguous")
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 0)

    def test_window_image_is_automatic_and_preserves_overview_coordinates(self):
        self.start_host()
        client = self.client()
        status = read_result(self.status(client))
        app = status["apps"][0]
        target = {"kind": "window", "window_id": app["windows"][0]["window_id"]}
        result = client.tool("vision", observations={"window": target["window_id"], "refresh": True})
        self.assertFalse(result["isError"], result)
        observation = read_result(result)["observations"][0]
        self.assertIn("elements", observation)
        self.assertNotIn("id", observation)
        self.assertEqual(observation["screenshot"], {
            "width": 80, "height": 60,
            "bounds": {"x": 20, "y": 20, "width": 80, "height": 60}})
        image = self.image(result)
        self.assertEqual(image.size, (80, 60))
        self.assertEqual(image.getpixel((0, 0)), (20, 20, 123))
        self.assertEqual(image.getpixel((79, 59)), (99, 79, 123))
        rejected = client.tool("vision", observations={"window": target["window_id"], "refresh": True}, max_image_dimension=20)
        self.assertEqual(read_result(rejected)["code"], "invalid_arguments")
        self.assertEqual(json.loads(self.state.read_text())["window_captures"], ["window-1"])
        rejected = client.tool("vision", steps=[{"tool": "press_keys", "args": {"keys": ["a"]}}],
            observations={"window": target["window_id"], "refresh": True}, include_screenshot=True)
        self.assertEqual(read_result(rejected)["code"], "invalid_arguments")
        self.assertEqual(json.loads(self.state.read_text())["input"]["keys"], 0)

    def test_window_without_accessibility_still_returns_an_image(self):
        self.start_host(window_title="No accessibility tree", bad_client=True)
        client = self.client()
        window = client.windows()[0]
        result = client.tool("vision", observations={"window": window["window_id"], "refresh": True})
        self.assertFalse(result["isError"], result)
        self.assertEqual(self.image(result).size, (54, 60))
        observation = read_result(result)["observations"][0]
        self.assertNotIn("elements", observation)
        self.assertEqual(observation["accessibility_error"]["code"], "accessibility_window_unmatched")

    def test_tree_only_window_omits_coordinates_and_can_act(self):
        self.start_host()
        client = self.client()
        status = read_result(self.status(client))
        target = {"window_id": status["apps"][0]["windows"][0]["window_id"]}
        before = json.loads(self.state.read_text())
        result = client.tool("accessibility", observations={"window": target["window_id"], "refresh": True})
        self.assertEqual(read_result(result)["status"], "ok", result)
        tree = read_result(result)["observations"][0]
        self.assertNotIn("id", tree)
        self.assertNotIn("screenshot", tree)
        self.assertNotIn("screenshot_error", tree)
        self.assertFalse(any(block["type"] == "image" for block in result["content"]))
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        self.assertIsNone(button["frame"])
        self.assertNotIn("frame=", tree["tree_markdown"])
        result = client.tool("accessibility", steps=[{"tool": "activate", "args": {
            "element_id": button["element_id"]}}], observations={"window": target["window_id"], "refresh": True})
        self.assertEqual(read_result(result)["status"], "ok", result)
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 1)
        self.assertEqual(json.loads(self.state.read_text())["captures"], before["captures"])
        self.assertNotIn("window_captures", json.loads(self.state.read_text()))
        # Tree-only reads remain usable after the screenshot canvas goes stale.
        self.configure(resize=True)
        result = client.tool("accessibility", observations={"window": target["window_id"], "refresh": True})
        self.assertFalse(result["isError"], result)
        button = next(row for row in read_result(result)["observations"][0]["elements"]
                      if row["label"] == "Fixture button")
        result = client.tool("accessibility", steps=[{"tool": "activate", "args": {
            "element_id": button["element_id"]}}])
        self.assertFalse(result["isError"], result)
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 2)
        self.assertEqual(json.loads(self.state.read_text())["captures"], before["captures"])

    def test_tree_only_window_can_read_and_act_without_a_current_overview(self):
        self.start_host()
        previous = self.client()
        window = previous.windows()[0]["window_id"]
        previous.close()  # The last client releases the screenshot canvas.
        self.wait_until(lambda: json.loads(self.state.read_text())["disconnections"] == 1)
        before = json.loads(self.state.read_text())
        self.configure(refuse=True)
        client = self.client()
        result = client.tool("accessibility", observations={"window": window, "refresh": True})
        self.assertFalse(result["isError"], result)
        tree = read_result(result)["observations"][0]
        self.assertNotIn("frame=", tree["tree_markdown"])
        self.assertFalse(any(block["type"] == "image" for block in result["content"]))
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        result = client.tool("accessibility", steps=[{"tool": "activate", "args": {
            "element_id": button["element_id"]}}])
        self.assertFalse(result["isError"], result)
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 1)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["captures"], before["captures"])
        self.assertEqual(state["connections"], before["connections"])

    def test_tree_only_window_does_not_fall_back_to_a_screenshot(self):
        self.start_host(window_title="No accessibility tree", bad_client=True)
        client = self.client()
        window = client.windows()[0]
        before = json.loads(self.state.read_text())["captures"]
        result = client.tool("accessibility", observations={"window": window["window_id"], "refresh": True})
        self.assertEqual(read_result(result)["observations"][0]["error"]["code"], "accessibility_window_unmatched")
        self.assertFalse(any(block["type"] == "image" for block in result["content"]))
        self.assertEqual(json.loads(self.state.read_text())["captures"], before)

    def test_vision_observation_opt_out_and_legacy_options_are_rejected(self):
        self.start_host()
        client = self.client()
        window = client.windows()[0]["window_id"]
        result = client.tool("vision", observations={"window": window, "refresh": True})
        self.assertIn("screenshot", read_result(result)["observations"][0])
        self.assertEqual(self.image(result).size, (80, 60))
        captures = json.loads(self.state.read_text())
        skipped = client.tool("vision", observations=False)
        self.assertFalse(skipped["isError"], skipped)
        self.assertEqual(read_result(skipped)["observations"], [])
        self.assertFalse(any(block["type"] == "image" for block in skipped["content"]))
        self.assertEqual(json.loads(self.state.read_text())["captures"], captures["captures"])
        self.assertEqual(json.loads(self.state.read_text())["window_captures"], captures["window_captures"])
        for arguments in ({"screenshot": False}, {"screenshot": True}, {"screenshot": None},
                          {"observe": False}, {"observe": {"kind": "window", "window_id": window}},
                          {"zoom": [0, 0, 20, 20]}):
            result = client.tool("vision", steps=[{"tool": "press_keys", "args": {"keys": ["a"]}}],
                                 observations={"window": window, "refresh": True}, **arguments)
            self.assertEqual(read_result(result)["code"], "invalid_arguments", result)
        self.assertEqual(json.loads(self.state.read_text())["input"]["keys"], 0)

    def test_window_capture_failure_preserves_available_tree(self):
        self.start_host()
        client = self.client()
        app = read_result(self.status(client))["apps"][0]
        self.configure(refuse=True)
        result = client.tool("vision", observations={"window": app["windows"][0]["window_id"], "refresh": True})
        self.assertTrue(result["isError"], result)
        observation = read_result(result)["observations"][0]
        self.assertIn("elements", observation)
        self.assertIn("screenshot_error", observation)
        self.assertFalse(any(block["type"] == "image" for block in result["content"]))

    def test_accessibility_observation_preserves_tree_while_window_moves(self):
        self.start_host(moving_window=True)
        client = self.client()
        window = client.windows()[0]
        result = client.tool("accessibility", observations={"window": window["window_id"], "refresh": True})
        self.assertFalse(result["isError"], result)
        self.assertEqual(read_result(result)["status"], "ok")
        self.assertIn("elements", read_result(result)["observations"][0])
        self.assertFalse(any(block["type"] == "image" for block in result["content"]))

    def test_window_capture_preserves_observation_when_window_moves(self):
        self.start_host(change_window_on_capture=True)
        client = self.client()
        window = client.windows()[0]
        result = client.tool("vision", observations={"window": window["window_id"], "refresh": True})
        self.assertFalse(result["isError"], result)
        self.assertEqual(read_result(result)["status"], "ok")
        self.assertIn("elements", read_result(result)["observations"][0])
        self.assertIn("screenshot", read_result(result)["observations"][0])
        self.assertEqual(self.image(result).size, (80, 60))
        self.assertEqual(json.loads(self.control.read_text())["window_x"], 20)

        button = next(row for row in read_result(result)["observations"][0]["elements"]
                      if row["label"] == "Fixture button")
        result = client.tool("accessibility", steps=[
            {"tool": "activate", "args": {"element_id": button["element_id"]}},
            {"tool": "activate", "args": {"element_id": 19999}},
        ], observations={"window": window["window_id"], "refresh": True})
        self.assertTrue(result["isError"], result)
        data = read_result(result)
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["completed"], 1)
        self.assertEqual(data["failed"]["index"], 1)
        self.assertEqual(data["failed"]["tool"], "activate")
        self.assertEqual(data["failed"]["code"], "stale_element")
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 1)
        self.assertIn("elements", data["observations"][0])

    def test_window_title_change_during_capture_preserves_the_observation(self):
        self.start_host(change_title_on_capture=True)
        client = self.client()
        window = client.windows()[0]
        result = client.tool("vision", observations={"window": window["window_id"], "refresh": True})
        self.assertEqual(read_result(result)["status"], "ok", result)
        self.assertIn("elements", read_result(result)["observations"][0])

    def test_mismatched_pid_matches_only_a_unique_window_and_actions_work(self):
        self.start_accessibility()
        self.start_host(app_pid=self.atspi.pid + 100000, buffer_client=True)
        client = self.client()
        tree = self.snapshot(client)
        self.assertNotIn("pid", tree)
        self.assertNotIn("pid_matched", tree)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        self.assertEqual(button["frame"], {"x": 30, "y": 30, "width": 20, "height": 10})
        result = client.tool("accessibility", steps=[{"tool": "activate", "args": {"element_id": button["element_id"]}}])
        self.assertFalse(result["isError"], result)
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 1)

    def test_mismatched_pid_rejects_ambiguity_in_both_inventories(self):
        self.start_accessibility(duplicates=True)
        self.start_host(app_pid=self.atspi.pid + 100000)
        client = self.client()
        tree = self.snapshot(client)
        self.assertEqual(tree["accessibility_error"]["code"], "accessibility_window_ambiguous")
        self.configure(duplicate_identity=True)
        tree = self.snapshot(client)
        self.assertEqual(tree["accessibility_error"]["code"], "accessibility_window_ambiguous")
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 0)

    def test_mismatched_pid_rejects_multiple_compositor_candidates(self):
        self.start_accessibility()
        self.start_host(app_pid=self.atspi.pid + 100000, duplicate_identity=True)
        tree = self.snapshot(self.client())
        self.assertEqual(tree["accessibility_error"]["code"], "accessibility_window_ambiguous")
        self.assertNotIn("elements", tree)

    def test_window_screenshot_rejects_an_unknown_window_id(self):
        self.start_host()
        client = self.client()
        window = client.windows()[0]
        captures = json.loads(self.state.read_text())["captures"]
        result = client.tool("vision", observations={"window": window["window_id"] + 99999, "refresh": True})
        self.assertEqual(read_result(result)["observations"][0]["error"]["code"], "window_unavailable")
        self.assertEqual(json.loads(self.state.read_text())["captures"], captures)

    def test_prevalidation_and_removed_arguments_send_no_input(self):
        self.start_host()
        client = self.client()
        for name, value in (("target", {}), ("display_id", "primary"), ("pid", 1),
                            ("window_id", 1), ("session", "old"), ("delivery_mode", "foreground"),
                            ("delay_ms", 100), ("capture_id", 1)):
            for args in ({name: value}, {"steps": [{"tool": "press_keys", "args": {"keys": ["a"]}},
                         {"tool": "click", "args": {"x": 1, "y": 1, name: value}}]}):
                result = client.tool("vision", **args)
                self.assertEqual(read_result(result)["code"], "invalid_arguments")
        for invalid_id in (1, 0, -1, 1.5, True, "1", "c1", 9007199254740992):
            for args in ({"id": invalid_id, "steps": [{"tool": "press_keys", "args": {"keys": ["a"]}}]},
                         {"steps": [{"tool": "press_keys", "args": {"keys": ["a"]}},
                                    {"tool": "click", "args": {"x": 1, "y": 1, "id": invalid_id}}]}):
                result = client.tool("vision", **args)
                self.assertEqual(read_result(result)["code"], "invalid_arguments")
        for arguments in ({"keys": []}, {"keys": ["a"] * 6}, {"keys": "a"},
                          {"key": "a"}, {"keys": ["a"], "modifiers": ["ctrl"]}):
            result = client.tool("vision", steps=[
                {"tool": "click", "args": {"x": 1, "y": 1}},
                {"tool": "press_keys", "args": arguments}])
            self.assertEqual(read_result(result)["code"], "invalid_arguments")
        self.assertEqual(json.loads(self.state.read_text())["geometry_requests"], 0)
        for removed in ("computer", "run_steps", "act", "get_desktop_state", "get_window_state", "list_windows", "health_report", "click"):
            self.assertEqual(read_result(client.tool(removed))["code"], "unknown_tool")

    def test_automatic_delays_separate_actions_and_final_screenshot(self):
        self.start_accessibility()
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        click = {"tool": "click", "args": {"x": 60, "y": 50}}
        result = client.tool("vision", steps=[click] * 3)
        self.assertFalse(result["isError"], result)
        self.assertEqual(read_result(result)["completed"], 3)
        times = json.loads(self.state.read_text())["input"]["action_times"]
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
        click = {"tool": "click", "args": {"x": 60, "y": 50}}
        result = client.tool("vision", steps=[click] * 6 + [
            {"tool": "delay_ms", "args": {"ms": 0}}])
        self.assertFalse(result["isError"], result)
        self.assertEqual(read_result(result)["completed"], 7)
        times = json.loads(self.state.read_text())["input"]["action_times"]
        for earlier, later in zip(times, times[1:]):
            self.assertGreaterEqual(later - earlier, .15)
        capture_time = json.loads(self.state.read_text())["capture_times"][-1]
        self.assertLess(capture_time - times[-1], .3)

    def test_intermediate_zero_delay_replaces_only_its_action_gap(self):
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        click = {"tool": "click", "args": {"x": 60, "y": 50}}
        result = client.tool("vision", steps=[click,
            {"tool": "delay_ms", "args": {"ms": 0}}, click, click])
        self.assertFalse(result["isError"], result)
        times = json.loads(self.state.read_text())["input"]["action_times"]
        self.assertEqual(len(times), 3)
        self.assertLess(times[1] - times[0], .15)
        self.assertGreaterEqual(times[2] - times[1], .15)
        self.assertGreaterEqual(json.loads(self.state.read_text())["capture_times"][-1] - times[-1], .3)

    def test_explicit_action_and_final_delays_are_not_padded(self):
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        click = {"tool": "click", "args": {"x": 60, "y": 50}}
        result = client.tool("vision", steps=[click,
            {"tool": "delay_ms", "args": {"ms": 200}}, click,
            {"tool": "delay_ms", "args": {"ms": 50}}])
        self.assertFalse(result["isError"], result)
        times = json.loads(self.state.read_text())["input"]["action_times"]
        self.assertGreaterEqual(times[1] - times[0], .2)
        self.assertLess(times[1] - times[0], .35)
        final_gap = json.loads(self.state.read_text())["capture_times"][-1] - times[-1]
        self.assertGreaterEqual(final_gap, .05)
        self.assertLess(final_gap, .3)

    def test_observation_only_uses_the_default_final_delay(self):
        self.start_host()
        client = self.client()
        start = time.monotonic()
        result = client.tool("vision")
        self.assertFalse(result["isError"], result)
        self.assertGreaterEqual(json.loads(self.state.read_text())["capture_times"][-1] - start, .3)

    def test_unexecuted_trailing_delay_does_not_replace_the_final_pause(self):
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        result = client.tool("vision", steps=[
            {"tool": "click", "args": {"x": 60, "y": 50}},
            {"tool": "delay_ms", "args": {"ms": 0}},
            {"tool": "click", "args": {"x": 99999, "y": 50}},
            {"tool": "delay_ms", "args": {"ms": 0}},
        ])
        self.assertEqual(read_result(result)["completed"], 2)
        self.assertEqual(read_result(result)["status"], "failed")
        last_action = json.loads(self.state.read_text())["input"]["action_times"][-1]
        self.assertGreaterEqual(json.loads(self.state.read_text())["capture_times"][-1] - last_action, .3)

    def test_automatic_delay_stops_on_permission_revocation(self):
        self.start_accessibility()
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        click = {"tool": "activate", "args": {"element_id": button["element_id"]}}
        client.serial += 1
        client.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": client.serial,
            "method": "tools/call", "params": {"name": "accessibility", "arguments": {
                "steps": [click, click]}}}) + "\n")
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
        data = read_result(result)
        self.assertTrue(result["isError"], result)
        self.assertEqual(data["code"], "computer_use_disconnected")
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 1)

    def test_explicit_delays_finish_before_one_final_screenshot(self):
        self.start_host()
        client = self.client()
        self.status(client)
        before = json.loads(self.state.read_text())["captures"]
        start = time.monotonic()
        result = client.tool("vision", steps=[
            {"tool": "press_keys", "args": {"keys": ["a"]}},
            {"tool": "delay_ms", "args": {"ms": 250}},
            {"tool": "press_keys", "args": {"keys": ["b"]}},
            {"tool": "delay_ms", "args": {"ms": 250}},
        ])
        self.assertFalse(result["isError"], result)
        self.assertEqual(read_result(result)["completed"], 4)
        self.assertEqual(sum(block["type"] == "image" for block in result["content"]), 1)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["captures"], before + 1)
        self.assertGreaterEqual(state["capture_times"][-1] - start, .5)
        self.assertEqual(state["input"]["keys"], 2)

    def test_delay_only_vision_observes_after_wait_and_false_skips_capture(self):
        self.start_host()
        client = self.client()
        result = client.tool("vision", steps=[{"tool": "delay_ms", "args": {"ms": 0}}], observations=False)
        self.assertFalse(result["isError"], result)
        self.assertEqual(read_result(result)["observations"], [])
        self.assertFalse(any(block["type"] == "image" for block in result["content"]))
        state = json.loads(self.state.read_text())
        self.assertEqual((state["captures"], state["connections"]), (0, 0))
        start = time.monotonic()
        result = client.tool("vision", steps=[{"tool": "delay_ms", "args": {"ms": 200}}])
        self.assertFalse(result["isError"], result)
        self.assertEqual(self.image(result).size, (160, 120))
        self.assertGreaterEqual(json.loads(self.state.read_text())["capture_times"][-1] - start, .2)

    def test_invalid_delay_is_rejected_before_earlier_steps(self):
        self.start_host()
        client = self.client()
        for args in ({}, {"ms": -1}, {"ms": 30001}, {"ms": .5}, {"ms": "100"},
                     {"ms": True}, {"ms": 1, "element_id": "old"}):
            result = client.tool("vision", steps=[
                {"tool": "press_keys", "args": {"keys": ["a"]}},
                {"tool": "delay_ms", "args": args},
            ])
            self.assertEqual(read_result(result)["code"], "invalid_arguments")
        self.assertEqual(json.loads(self.state.read_text())["connections"], 0)

    def test_delay_cancellation_skips_later_input_and_screenshot(self):
        self.start_host()
        client = self.client()
        client.serial += 1
        request_id = client.serial
        client.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id,
            "method": "tools/call", "params": {"name": "vision", "arguments": {
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
            "method": "tools/call", "params": {"name": "vision", "arguments": {
                "steps": [{"tool": "delay_ms", "args": {"ms": 30000}},
                          {"tool": "press_keys", "args": {"keys": ["a"]}}]}}}) + "\n")
        client.process.stdin.flush()
        time.sleep(.1)
        self.computer_path.unlink()
        self.assertTrue(select.select([client.process.stdout], [], [], 2)[0], "delay did not stop on revocation")
        result = json.loads(client.process.stdout.readline())["result"]
        data = read_result(result)
        self.assertTrue(result["isError"], result)
        self.assertEqual(data["code"], "computer_use_disconnected")
        state = json.loads(self.state.read_text())
        self.assertEqual((state["connections"], state["captures"]), (0, 0))

    def test_real_eis_batch_and_global_coordinates(self):
        self.start_host()
        client = self.client()
        self.status(client)
        result = client.tool("vision", steps=[
            {"tool": "click", "args": {"x": 60, "y": 50}},
            {"tool": "scroll", "args": {"x": 60, "y": 50, "direction": "down", "amount": 2}},
            {"tool": "press_keys", "args": {"keys": ["ctrl", "a"]}},
            {"tool": "type_text", "args": {"text": "世界"}},
        ])
        self.assertFalse(result["isError"], result)
        data = read_result(result)
        self.assertEqual(data["completed"], 4)
        self.assertEqual(set(data), {"completed", "status", "warnings", "observations"})
        self.assertEqual(data["status"], "ok")
        self.assertNotIn("steps", data)
        self.assertEqual(self.image(result).size, (160, 120))
        self.assertNotIn("id", data["observations"][0])
        self.assertNotIn("zoom", data["observations"][0])
        self.assertEqual(set(data["observations"][0]),
                         {"width", "height"})
        self.assertEqual((data["observations"][0]["width"], data["observations"][0]["height"]), (160, 120))
        state = json.loads(self.state.read_text())
        self.assertEqual((state["input"]["x"], state["input"]["y"]), (720, 450))
        self.assertEqual(state["input"]["clicks"], 1)
        self.assertEqual(state["input"]["scrolls"], 1)
        self.assertEqual(state["input"]["buttons_down"], 0)
        self.assertEqual(state["input"]["keys_down"], 0)
        self.assertEqual(state["clipboard"], "世界")
        self.assertEqual(data["warnings"][0]["warning"], "Clipboard and primary selection text replaced.")
        text = result["content"][0]["text"]
        self.assertIn("Warning (step 3, type_text): Clipboard and primary selection text replaced.", text)
        self.assertNotIn('"warning":', text)
        # A later pixel action uses the fresh overview captured by the batch.
        again = client.tool("vision", observations=False, steps=[
            {"tool": "click", "args": {"x": 60, "y": 50}}])
        self.assertFalse(again["isError"], again)
        self.status(client)
        self.assertEqual(json.loads(self.state.read_text())["input"]["clicks"], 2)

    def test_klipper_fallback_restores_a_lost_accessible_selection(self):
        self.start_accessibility(selection=True)
        self.start_host()
        client = self.client()
        self.status(client)
        result = client.tool("vision", steps=[{"tool": "type_text", "args": {"text": "世界"}}])
        self.assertFalse(result["isError"], result)
        self.assertEqual(json.loads(self.atspi_state.read_text())["selection_restores"], 1)
        self.assertEqual(json.loads(self.state.read_text())["clipboard"], "世界")

    def test_unstable_selection_fails_without_sending_paste(self):
        self.start_accessibility(unstable=True)
        self.start_host()
        client = self.client()
        self.status(client)
        result = client.tool("vision", steps=[{"tool": "type_text", "args": {"text": "世界"}}])
        self.assertTrue(result["isError"], result)
        self.assertEqual(read_result(result)["failed"]["code"], "clipboard_selection_changed")
        self.assertEqual(read_result(result)["completed"], 0)
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
        result = client.tool("vision")
        self.assertFalse(result["isError"], result)
        data = read_result(result)["observations"][0]
        self.assertEqual(self.image(result).size, (500, 140))
        # Gap lies at x=320..340 in this overview.
        self.assertEqual(self.image(result).getpixel((330, 75)), (0, 0, 0))
        result = client.tool("vision", steps=[{"tool": "drag", "args": {
            "from_x": 40, "from_y": 60, "to_x": 400, "to_y": 60, "duration_ms": 100}}])
        self.assertFalse(result["isError"], result)
        state = json.loads(self.state.read_text())["input"]
        self.assertEqual((state["x"], state["y"]), (2800, 360))
        self.assertGreater(state["dragged_motions"], 0)
        self.assertEqual(state["clicks"], 1)
        self.assertEqual(state["buttons_down"], 0)

    def test_removed_zoom_is_rejected_before_effects(self):
        self.start_host()
        client = self.client()
        self.status(client)
        for zoom in ([0, 0, 20, 20], [-1, 0, 20, 20], [0, 0, 200, 20]):
            result = client.tool("vision",
                steps=[{"tool": "click", "args": {"x": 40, "y": 40}}], zoom=zoom)
            self.assertEqual(read_result(result)["code"], "invalid_arguments", result)
        self.status(client)
        self.assertEqual(json.loads(self.state.read_text())["input"]["clicks"], 0)

    def test_partial_failure_still_observes_and_reports_capture_errors(self):
        self.start_host()
        client = self.client()
        self.status(client)
        captures = json.loads(self.state.read_text())["captures"]
        result = client.tool("vision", steps=[
            {"tool": "click", "args": {"x": 40, "y": 40}},
            {"tool": "click", "args": {"x": -1, "y": 40}},
            {"tool": "press_keys", "args": {"keys": ["a"]}},
        ])
        data = read_result(result)
        self.assertEqual((data["completed"], data["failed"]["index"]), (1, 1))
        self.assertEqual(set(data), {"completed", "failed", "status", "observations"})
        self.assertEqual(data["status"], "failed")
        self.assertTrue(result["isError"])
        self.assertEqual(len(data["observations"]), 1)
        self.assertEqual(self.image(result).size, (160, 120))
        self.assertEqual(json.loads(self.state.read_text())["captures"], captures + 1)
        self.configure(refuse=True)
        result = client.tool("vision", steps=[{"tool": "click", "args": {"x": 40, "y": 40}}])
        self.assertEqual(read_result(result)["completed"], 1)
        self.assertEqual(read_result(result)["status"], "failed")
        self.assertTrue(result["isError"])
        self.assertIn("error", read_result(result)["observations"][0])
        error = client.tool("status")
        self.assertTrue(error["isError"])
        self.assertIn("code", read_result(error))
        self.assertEqual(set(read_result(error)), {"code", "hint"})

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
        result = client.tool("vision", steps=[{"tool": "press_keys", "args": {"keys": ["a"]}}], observations=False)
        self.assertTrue(result["isError"], result)
        fresh = self.client().tool("status")
        self.assertTrue(fresh["isError"])
        self.computer_path.unlink(missing_ok=True)
        self.start_service()
        self.status(client)

    def test_kwin_replacement_invalidates_existing_handles_then_recovers(self):
        self.start_host()
        client = self.client()
        self.status(client)
        self.stop(self.kwin)
        self.kwin = self.child(["/usr/bin/python3", str(ROOT / "tests/scu_kwin_fixture.py"),
            self.address, str(self.state), str(self.control), str(self.backend)])
        self.ready(self.kwin)
        time.sleep(.1)
        result = client.tool("status")
        self.assertTrue(result["isError"], result)
        self.assertEqual(read_result(result)["code"], "computer_use_disconnected")
        fresh = self.status(client)
        self.assertEqual(read_result(fresh)["canvas"], {"width": 160, "height": 120})
        recovered = client.tool("vision", observations=False, steps=[
            {"tool": "click", "args": {"x": 40, "y": 40}}])
        self.assertFalse(recovered["isError"], recovered)

    def test_removed_zoom_is_rejected_before_keyboard_effects(self):
        self.start_host()
        client = self.client()
        self.status(client)
        self.configure(resize=True)
        result = client.tool("vision", steps=[{"tool": "press_keys", "args": {"keys": ["a"]}}],
            zoom=[0, 0, 20, 20])
        self.assertEqual(read_result(result)["code"], "invalid_arguments")
        self.status(client)
        self.assertEqual(json.loads(self.state.read_text())["input"]["keys"], 0)

    def test_missing_libraries_only_fail_operations_using_that_backend(self):
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
                if library == "libatspi.so.0":
                    window = client.windows()[0]["window_id"]
                    result = client.tool("vision", steps=[
                        {"tool": "press_keys", "args": {"keys": ["a"]}}],
                        observations={"window": window, "refresh": True})
                    self.assertFalse(result["isError"], result)
                    observation = read_result(result)["observations"][0]
                    self.assertEqual(observation["accessibility_error"]["code"], "missing_dependency")
                    self.assertIn("screenshot", observation)
                    self.assertEqual(self.image(result).size, (80, 60))
                    result = client.tool("accessibility", steps=[
                        {"tool": "activate", "args": {"element_id": 0}}])
                    self.assertTrue(result["isError"], result)
                    failure = read_result(result)["failed"]
                    self.assertEqual((failure["code"], failure["index"], failure["tool"]),
                                     ("missing_dependency", 0, "activate"))
                else:
                    before = json.loads(self.state.read_text())
                    registrations = json.loads(self.atspi_state.read_text())["registrations"]
                    result = client.tool("status")
                    self.assertEqual(read_result(result)["code"], "missing_dependency", result)
                    result = client.tool("accessibility", steps=[
                        {"tool": "activate", "args": {"element_id": 0}}])
                    # AT-SPI runs and resolves the ID despite missing visual libraries.
                    failure = read_result(result)["failed"]
                    self.assertEqual((failure["code"], failure["index"], failure["tool"]),
                                     ("stale_element", 0, "activate"))
                    self.assertEqual(json.loads(self.atspi_state.read_text())["registrations"], registrations + 1)
                    after = json.loads(self.state.read_text())
                    self.assertEqual((after["captures"], after["connections"]),
                                     (before["captures"], before["connections"]))
                    result = client.tool("vision", observations=False, steps=[
                        {"tool": "press_keys", "args": {"keys": ["a"]}}])
                    self.assertTrue(result["isError"], result)
                    failure = read_result(result)["failed"]
                    self.assertEqual((failure["code"], failure["index"], failure["tool"]),
                                     ("missing_dependency", 0, "press_keys"))

    def test_missing_native_accessibility_preserves_vision_and_recovers(self):
        self.start_host(accessibility=False)
        client = self.client()
        window = client.windows()[0]["window_id"]
        result = client.tool("vision", steps=[
            {"tool": "press_keys", "args": {"keys": ["a"]}},
            {"tool": "type_text", "args": {"text": "世界"}}],
            observations={"window": window, "refresh": True})
        self.assertFalse(result["isError"], result)
        observation = read_result(result)["observations"][0]
        self.assertEqual(observation["accessibility_error"]["code"], "accessibility_unavailable")
        self.assertIn("screenshot", observation)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["input"]["keys"], 3)
        self.assertEqual(state["clipboard"], "世界")
        result = client.tool("accessibility", observations={"window": window, "refresh": True})
        self.assertEqual(read_result(result)["observations"][0]["error"]["code"], "accessibility_unavailable")
        self.start_accessibility()
        result = client.tool("accessibility", observations={"window": window, "refresh": True})
        self.assertFalse(result["isError"], result)
        self.assertIn("elements", read_result(result)["observations"][0])
        self.assertEqual(json.loads(self.atspi_state.read_text())["registrations"], 1)

    def test_broken_registry_only_blocks_accessibility(self):
        self.start_accessibility(unavailable=True)
        self.start_host()
        client = self.client()
        window = client.windows()[0]["window_id"]
        for name, args in (("status", {}), ("vision", {"steps": [
            {"tool": "press_keys", "args": {"keys": ["a"]}},
            {"tool": "type_text", "args": {"text": "世界"}}]})):
            result = client.tool(name, **args)
            self.assertFalse(result["isError"], result)
        result = client.tool("accessibility", observations={"window": window, "refresh": True})
        self.assertTrue(result["isError"], result)
        self.assertEqual(read_result(result)["observations"][0]["error"]["code"], "accessibility_unavailable")

    def test_backend_invariants(self):
        for case in ("mapping", "images", "elements"):
            with self.subTest(case=case):
                result = subprocess.run([str(self.backend), case], capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_modern_legacy_unicode_and_malformed_stdio(self):
        client = self.client()
        metadata = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {}}
        self.assertIn("supportedVersions", client.call("server/discover", {"_meta": metadata})["result"])
        self.assertEqual(client.call("initialize", {"protocolVersion": "2025-06-18"})["result"]["protocolVersion"], "2025-06-18")
        bad = client.tool("vision", steps=[{"tool": "type_text", "args": {"text": "世界", "bad": True}}])
        self.assertEqual(read_result(bad)["code"], "invalid_arguments")
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

    def test_status_deadline_expires_and_can_recover(self):
        # The native harness checks the production 30-second default. Exercise
        # the real timeout/recovery path here with a shorter private build setting.
        self.environment["SCU_TEST_STATUS_TIMEOUT_MS"] = "250"
        self.start_host(hold_geometry=True)
        client = self.client()
        start = time.monotonic()
        result = client.call("tools/call", {"name": "status", "arguments": {}}, timeout=34)["result"]
        elapsed = time.monotonic() - start
        self.assertTrue(result["isError"], result)
        self.assertGreaterEqual(elapsed, .24)
        self.assertLess(elapsed, 2)
        self.assertEqual(read_result(result)["code"], "timeout")
        self.configure(hold_geometry=False)
        self.status(client)

    def test_cancelled_cross_monitor_drag_releases_button(self):
        self.start_host(triple=True)
        client = self.client()
        self.status(client)  # native overview is 500x140
        client.serial += 1
        request_id = client.serial
        client.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id,
            "method": "tools/call", "params": {"name": "vision", "arguments": {
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
