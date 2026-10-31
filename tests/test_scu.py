"""Compile and exercise the actual injected executable without host desktop effects."""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("gio-unix-2.0", "atspi-2", "libpipewire-0.3", "libei-1.0", "xkbcommon", "gdk-pixbuf-2.0", "libeis-1.0")


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
        if not shutil.which("cc") or not shutil.which("pkg-config"):
            raise unittest.SkipTest("scu needs a C compiler and pkg-config")
        flags = subprocess.run(["pkg-config", "--cflags", *PACKAGES], capture_output=True, text=True)
        if flags.returncode:
            raise unittest.SkipTest("scu development headers are unavailable")
        cls.temporary = tempfile.TemporaryDirectory(prefix="scu-tests-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        cls.socket_path = cls.directory / "accessibility"
        cls.binary = cls.directory / "scu"
        cls.backend = cls.directory / "backend"
        common = ["cc", "-O1", "-g", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
            "-DYYJSON_READER_DEPTH_LIMIT=64", f'-DSCU_ATSPI_SOCKET="{cls.socket_path}"',
            f'-DSCU_ATSPI_ADDRESS="unix:path={cls.socket_path}"',
            f'-DSCU_SESSION_BUS_SOCKET="{cls.directory / "portal-bus"}"', *shlex.split(flags.stdout)]
        libraries = shlex.split(subprocess.check_output(["pkg-config", "--libs", "gio-unix-2.0"], text=True))
        link = [str(ROOT / "native/vendor/yyjson.c"), "-Wl,--wrap=__libc_start_main",
                "-Wl,--wrap=dlopen", "-Wl,--wrap=dlsym", "-ldl", *libraries]
        for source, binary in ((ROOT / "native/scu.c", cls.binary),
                               (ROOT / "tests/scu_backend_fixture.c", cls.backend)):
            extra = ["-leis", "-lxkbcommon"] if binary == cls.backend else []
            subprocess.run([*common, str(source), *link, *extra, "-o", str(binary)], check=True, capture_output=True)

    def setUp(self):
        self.socket_path.unlink(missing_ok=True)
        self.environment = os.environ.copy()
        for name in ("SPACES_NAME", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR",
                     "DISPLAY", "WAYLAND_DISPLAY"):
            self.environment.pop(name, None)

    def client(self):
        client = Client(self.binary, self.environment)
        self.addCleanup(client.close)
        return client

    def test_discovery_and_permission_error(self):
        client = self.client()
        tools = client.call("tools/list", {})["result"]["tools"]
        self.assertEqual(len(tools), 17)
        self.assertIn("run_steps", {tool["name"] for tool in tools})
        batch_schema = next(tool["inputSchema"] for tool in tools if tool["name"] == "run_steps")
        self.assertIn("anyOf", batch_schema["properties"]["steps"]["items"])
        self.assertIn("anyOf", batch_schema["properties"]["observe"])
        health = client.tool("health_report")
        self.assertFalse(health["isError"])
        self.assertFalse(health["structuredContent"]["ready"])
        result = client.tool("list_windows")
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["code"], "accessibility_permission_required")
        self.assertIn("spaces configure <space>", result["structuredContent"]["hint"])

    def test_bad_options_and_unicode_json(self):
        client = self.client()
        for name, arguments in (("press_key", {"key": "é", "bad": 1}),
                                ("click", {}), ("run_steps", {"steps": []})):
            with self.subTest(name=name):
                result = client.tool(name, **arguments)
                self.assertTrue(result["isError"])
                self.assertEqual(result["structuredContent"]["code"], "invalid_arguments")

    def test_modern_requests_and_metadata(self):
        client = self.client()
        metadata = {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}}
        result = client.call("server/discover", {"_meta": metadata})["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertIn("2026-07-28", result["supportedVersions"])
        self.assertIn("tools", client.call("tools/list", {})["result"])
        self.assertIn("io.modelcontextprotocol/serverInfo", result["_meta"])
        self.assertIn("without replaying", result["instructions"])
        unsupported = client.call("ping", {"_meta": {**metadata, "io.modelcontextprotocol/protocolVersion": "1900-01-01"}})
        self.assertEqual(unsupported["error"]["data"]["requested"], "1900-01-01")
        self.assertIn("tools", client.call("tools/list", {"_meta": metadata})["result"])
        legacy = client.call("initialize", {"protocolVersion": "2025-06-18"})["result"]
        self.assertEqual(legacy["protocolVersion"], "2025-06-18")
        self.assertEqual(legacy["instructions"], result["instructions"])

    def test_malformed_messages_do_not_corrupt_stdio(self):
        client = self.client()
        client.process.stdin.write("not json\n")
        client.process.stdin.flush()
        self.assertTrue(select.select([client.process.stdout], [], [], 5)[0])
        self.assertEqual(json.loads(client.process.stdout.readline())["error"]["code"], -32700)
        self.assertIn("result", client.call("ping", {}))

    def test_backend_invariants(self):
        for case in ("mapping", "frame", "png", "tokens", "input"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as state:
                result = subprocess.run([str(self.backend), case], timeout=20,
                    env={**self.environment, "XDG_STATE_HOME": state}, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode())

    def start_fixture(self):
        try:
            result = subprocess.run(["/usr/bin/python3", "-c",
                'import gi; gi.require_version("Gio","2.0"); from gi.repository import Gio'],
                capture_output=True)
        except FileNotFoundError:
            self.skipTest("AT-SPI fixture needs system Python and PyGObject")
        if result.returncode or not shutil.which("dbus-daemon"):
            self.skipTest("AT-SPI fixture needs PyGObject and dbus-daemon")
        bus = subprocess.Popen(["dbus-daemon", "--session", "--nofork",
            f"--address=unix:path={self.socket_path}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop, bus)
        for _ in range(100):
            if self.socket_path.exists(): break
            time.sleep(.02)
        state = self.directory / "state.json"
        fixture = subprocess.Popen(["/usr/bin/python3", str(ROOT / "tests/scu_atspi_fixture.py"),
            f"unix:path={self.socket_path}", str(state)], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, fixture)
        if not select.select([fixture.stdout], [], [], 5)[0] or fixture.stdout.readline().strip() != "ready":
            self.fail("AT-SPI fixture failed: " + fixture.stderr.read())
        self.atspi_fixture = fixture
        self.atspi_bus = bus
        return state

    @staticmethod
    def stop(process):
        process.terminate()
        try: process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()
        for stream in (process.stdout, process.stderr):
            if stream and not stream.closed: stream.close()

    def test_accessibility_bus_restart_and_fresh_handles(self):
        self.start_fixture()
        client = self.client()
        before = client.tool("list_windows")["structuredContent"]["windows"][0]
        target = {key: before[key] for key in ("pid", "window_id")}
        old = client.tool("get_window_state", **target, tree_format="elements")
        token = old["structuredContent"]["elements"][0]["element_token"]
        self.stop(self.atspi_fixture)
        self.stop(self.atspi_bus)
        self.socket_path.unlink(missing_ok=True)
        self.assertEqual(client.tool("list_windows")["structuredContent"]["code"],
                         "accessibility_permission_required")
        self.start_fixture()
        recovered = client.tool("list_windows")
        self.assertFalse(recovered["isError"], recovered)
        after = recovered["structuredContent"]["windows"][0]
        self.assertNotEqual(after["window_id"], before["window_id"])
        self.assertEqual(client.tool("click", element_token=token)["structuredContent"]["code"],
                         "stale_element")

    def test_missing_optional_libraries_preserves_discovery(self):
        endpoint = socket.socket(socket.AF_UNIX)
        endpoint.bind(str(self.socket_path))
        self.addCleanup(endpoint.close)
        with tempfile.TemporaryDirectory() as directory:
            for name in ("libatspi.so.0", "libei.so.1", "libpipewire-0.3.so.0"):
                (Path(directory) / name).write_bytes(b"invalid ELF")
            self.environment["LD_LIBRARY_PATH"] = directory
            client = self.client()
            self.assertIn("tools", client.call("tools/list", {})["result"])
            health = client.tool("health_report")["structuredContent"]
            self.assertFalse(health["ready"])
            self.assertEqual(health["code"], "missing_dependency")
            self.assertEqual(client.tool("get_desktop_state")["structuredContent"]["code"],
                             "missing_dependency")

    def test_oversized_message_recovers(self):
        client = self.client()
        client.process.stdin.write("x" * (1024 * 1024 + 1) + "\n")
        client.process.stdin.flush()
        self.assertTrue(select.select([client.process.stdout], [], [], 5)[0])
        self.assertEqual(json.loads(client.process.stdout.readline())["error"]["code"], -32700)
        self.assertIn("result", client.call("ping", {}))

    def start_portal(self, mode):
        if not shutil.which("dbus-daemon") or subprocess.run(["/usr/bin/python3", "-c",
                "from gi.repository import Gio"], capture_output=True).returncode:
            self.skipTest("Portal fixture needs dbus-daemon and Python GI/Gio")
        address = "unix:path=" + str(self.directory / "portal-bus")
        bus = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--address=" + address],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop, bus)
        for _ in range(100):
            if (self.directory / "portal-bus").exists(): break
            time.sleep(.01)
        state = self.directory / "portal-state.json"
        fixture = subprocess.Popen(["/usr/bin/python3", str(ROOT / "tests/scu_portal_fixture.py"),
            address, str(state), mode], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, fixture)
        self.addCleanup((self.directory / "portal-bus").unlink, missing_ok=True)
        self.assertTrue(select.select([fixture.stdout], [], [], 5)[0])
        self.assertEqual(fixture.stdout.readline().strip(), "ready")
        return state

    def test_disabled_permission_opens_no_portal(self):
        state = self.start_portal("refuse")
        client = self.client()
        for name, arguments in (("get_desktop_state", {}),
                ("run_steps", {"steps": [{"press_key": {"key": "a"}}]})):
            self.assertEqual(client.tool(name, **arguments)["structuredContent"]["code"],
                             "accessibility_permission_required")
        self.assertEqual(json.loads(state.read_text())["created"], 0)

    def test_portal_consent_refusal(self):
        state = self.start_portal("refuse")
        self.environment["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent-host-bus"
        self.environment["XDG_RUNTIME_DIR"] = "/nonexistent-host-runtime"
        endpoint = socket.socket(socket.AF_UNIX)
        endpoint.bind(str(self.socket_path))
        self.addCleanup(endpoint.close)
        client = self.client()
        result = client.tool("get_desktop_state")
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["code"], "consent_cancelled")
        self.assertEqual(json.loads(state.read_text())["created"], 1)
        refused = client.tool("run_steps", steps=[{"press_key": {"key": "a"}},
                              {"press_key": {"key": "b"}}])
        self.assertTrue(refused["isError"])
        self.assertEqual(refused["structuredContent"]["completed"], 0)
        self.assertEqual(refused["structuredContent"]["skipped"], 1)
        self.assertEqual(refused["structuredContent"]["failed"]["code"], "consent_cancelled")
        self.assertEqual(json.loads(state.read_text())["created"], 2)

    def test_cancellation_and_eof_close_pending_portal(self):
        state = self.start_portal("hold")
        endpoint = socket.socket(socket.AF_UNIX)
        endpoint.bind(str(self.socket_path))
        self.addCleanup(endpoint.close)
        client = self.client()
        client.serial += 1
        identifier = client.serial
        client.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": identifier,
            "method": "tools/call", "params": {"name": "get_desktop_state", "arguments": {}}}) + "\n")
        client.process.stdin.flush()
        deadline = time.monotonic() + 5
        while json.loads(state.read_text())["created"] == 0 and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(json.loads(state.read_text())["created"], 1)
        client.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
            "params": {"requestId": identifier}}) + "\n")
        client.process.stdin.flush()
        self.assertIn("result", client.call("ping", {}))  # No cancelled response precedes this.
        deadline = time.monotonic() + 3
        while json.loads(state.read_text())["closed"] == 0 and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(json.loads(state.read_text())["closed"], 1)
        client.process.stdin.close()
        client.process.wait(timeout=3)

    def test_accessible_actions_batching_and_revocation(self):
        state = self.start_fixture()
        client = self.client()
        discovered = client.tool("list_windows")["structuredContent"]["windows"]
        self.assertEqual(len(discovered), 1)
        window = {key: discovered[0][key] for key in ("pid", "window_id")}
        snapshot = client.tool("get_window_state", **window, tree_format="elements")["structuredContent"]
        elements = {row["label"]: row["element_token"] for row in snapshot["elements"]}
        button, entry = elements["Fixture button"], elements["Fixture entry"]
        bad = client.tool("run_steps", steps=[{"click": {"element_token": button}},
            {"press_key": {"key": "a", "bad": True}}])
        self.assertTrue(bad["isError"])
        self.assertEqual(json.loads(state.read_text())["clicks"], 0)
        unsupported = client.tool("run_steps", steps=[{"click": {"element_token": button}},
            {"press_key": {"key": "a", "delivery_mode": "background"}}])
        self.assertTrue(unsupported["isError"])
        self.assertEqual(json.loads(state.read_text())["clicks"], 0)
        for observation in (True, {"kind": "desktop", "bad": True},
                            {"kind": "window", "include_screenshot": True},
                            {"kind": "unknown"}):
            invalid = client.tool("run_steps", steps=[{"click": {"element_token": button}}],
                                  observe=observation)
            self.assertTrue(invalid["isError"], invalid)
            self.assertEqual(json.loads(state.read_text())["clicks"], 0)
        # A desktop default must not replace semantic token targeting or open a portal.
        semantic = client.tool("run_steps", target={"kind": "desktop", "display_id": "display-1"},
                              steps=[{"click": {"element_token": button}}])
        self.assertFalse(semantic["isError"], semantic)
        self.assertEqual(semantic["structuredContent"]["completed"], 1)
        self.assertNotIn("observation", semantic["structuredContent"])
        self.assertNotIn("steps", semantic["structuredContent"])
        self.assertEqual([item["type"] for item in semantic["content"]], ["text"])
        result = client.tool("run_steps", steps=[{"click": {"element_token": button}},
            {"set_value": {"element_token": entry, "value": "Héllo 世界"}},
            {"type_text": {"element_token": entry, "text": " ✨"}}], observe=window)
        self.assertFalse(result["isError"], result)
        self.assertEqual(json.loads(state.read_text()), {"clicks": 2, "text": "Héllo 世界 ✨", "password_reads": 0})
        self.assertEqual(result["structuredContent"]["completed"], 3)
        stale = client.tool("click", element_token=button)
        self.assertEqual(stale["structuredContent"]["code"], "stale_element")
        failed = client.tool("run_steps", steps=[{"click": {"element_token": "stale"}},
            {"set_value": {"element_token": "stale", "value": "bad"}}])
        self.assertTrue(failed["isError"])
        self.assertEqual(failed["structuredContent"]["completed"], 0)
        self.assertEqual(failed["structuredContent"]["skipped"], 1)
        self.assertEqual(failed["structuredContent"]["failed"]["index"], 0)
        self.assertEqual(failed["structuredContent"]["failed"]["tool"], "click")
        # Preserve completed actions even when explicit final observation fails.
        fresh = client.tool("get_window_state", **window, tree_format="elements")["structuredContent"]
        token = next(row["element_token"] for row in fresh["elements"] if row["label"] == "Fixture button")
        observed = client.tool("run_steps", steps=[{"click": {"element_token": token}}],
                               observe={"kind": "window", "pid": window["pid"], "window_id": -1})
        self.assertTrue(observed["isError"])
        self.assertEqual(observed["structuredContent"]["completed"], 1)
        self.assertEqual(observed["structuredContent"]["skipped"], 0)
        self.assertIn("observation_error", observed["structuredContent"])
        self.assertNotIn("failed", observed["structuredContent"])
        self.assertEqual(json.loads(state.read_text())["clicks"], 3)
        inherited = client.tool("run_steps", steps=[{"click": {
            "element_token": token, "target": {"kind": "window", **window}}}],
            observe={"kind": "window", "tree_format": "elements"})
        self.assertFalse(inherited["isError"], inherited)
        self.assertIn("elements", inherited["structuredContent"]["observation"])
        self.assertEqual(json.loads(state.read_text())["clicks"], 4)
        self.socket_path.unlink()
        self.assertEqual(client.tool("list_windows")["structuredContent"]["code"], "accessibility_permission_required")


if __name__ == "__main__":
    unittest.main()
