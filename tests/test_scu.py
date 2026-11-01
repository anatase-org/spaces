"""Exercise the real MCP executable and host service on private desktop peers."""
from __future__ import annotations

import base64
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
        cls.temporary = tempfile.TemporaryDirectory(prefix="scu-tests-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        cls.socket_path = cls.directory / "accessibility"
        cls.computer_path = cls.directory / "computer"
        cls.binary = cls.directory / "scu"
        cls.backend = cls.directory / "backend"
        common = ["cc", "-O1", "-g", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
            "-DYYJSON_READER_DEPTH_LIMIT=64", f'-DSCU_ATSPI_SOCKET="{cls.socket_path}"',
            f'-DSCU_ATSPI_ADDRESS="unix:path={cls.socket_path}"',
            f'-DSCU_COMPUTER_SOCKET="{cls.computer_path}"', *shlex.split(flags.stdout)]
        libraries = shlex.split(subprocess.check_output(["pkg-config", "--libs", "gio-unix-2.0"], text=True))
        link = [str(ROOT / "native/vendor/yyjson.c"), "-Wl,--wrap=__libc_start_main",
                "-Wl,--wrap=dlopen", "-Wl,--wrap=dlsym", "-ldl", *libraries]
        for source, binary in ((ROOT / "native/scu.c", cls.binary), (ROOT / "tests/scu_backend_fixture.c", cls.backend)):
            extra = ["-leis", "-lxkbcommon"] if binary == cls.backend else []
            build = subprocess.run([*common, str(source), *link, *extra, "-o", str(binary)], capture_output=True, text=True)
            if build.returncode:
                raise AssertionError(build.stderr)

    def setUp(self):
        self.socket_path.unlink(missing_ok=True)
        self.computer_path.unlink(missing_ok=True)
        self.environment = os.environ.copy()
        for key in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "DISPLAY", "SPACES_NAME"):
            self.environment.pop(key, None)
        self.state = self.directory / "kwin-state.json"
        self.control = self.directory / "kwin-control.json"
        self.config = {}
        self.control.write_text("{}")

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
        self.control.write_text(json.dumps(self.config))

    def start_accessibility(self, two_windows=False, duplicates=False, selection=False, unstable=False, unavailable=False):
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
        self.atspi = self.child(command)
        self.ready(self.atspi)
        self.configure(app_pid=self.atspi.pid, two_windows=two_windows)

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
        self.host = self.child([str(self.binary), "--host-service", str(self.computer_path), str(ready_write)],
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

    def test_catalog_and_permission_errors(self):
        client = self.client()
        tools = client.call("tools/list", {})["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, {"computer", "status"})
        schema = next(tool["inputSchema"] for tool in tools if tool["name"] == "computer")
        self.assertEqual(set(schema["properties"]), {"steps", "observe", "id"})
        self.assertEqual(schema["properties"]["id"]["type"], "integer")
        self.assertEqual(schema["properties"]["id"]["minimum"], 1)
        delay = next(step["properties"]["args"] for step in schema["properties"]["steps"]["items"]["anyOf"]
                     if step["properties"]["tool"]["enum"] == ["delay_ms"])
        self.assertEqual(delay["required"], ["ms"])
        self.assertEqual(set(delay["properties"]), {"ms"})
        for field in ('"target"', '"display_id"', '"delivery_mode"', '"session"', '"capture_id"'):
            self.assertNotIn(field, json.dumps(schema))
        for name, args in (("status", {}), ("computer", {}),
                           ("computer", {"steps": [{"tool": "press_key", "args": {"key": "a"}}]})):
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

    def test_incompatible_accessibility_geometry_is_not_guessed(self):
        self.start_accessibility()
        self.start_host(bad_client=True)
        client = self.client()
        app = self.status(client)["structuredContent"]["apps"][0]
        result = client.tool("computer", observe={"kind": "window", "pid": app["pid"], "window_id": app["windows"][0]["window_id"], "tree_format": "elements"})
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["observation_error"]["code"], "accessibility_window_unavailable")

    def test_ambiguous_accessibility_window_is_not_selected_by_title(self):
        self.start_accessibility(duplicates=True)
        self.start_host()
        client = self.client()
        app = self.status(client)["structuredContent"]["apps"][0]
        result = client.tool("computer", observe={"kind": "window", "pid": app["pid"],
            "window_id": app["windows"][0]["window_id"], "tree_format": "elements"})
        self.assertEqual(result["structuredContent"]["observation_error"]["code"], "accessibility_window_unavailable")
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 0)

    def test_prevalidation_and_removed_arguments_send_no_input(self):
        self.start_host()
        client = self.client()
        for name, value in (("target", {}), ("display_id", "primary"), ("pid", 1),
                            ("window_id", 1), ("session", "old"), ("delivery_mode", "foreground"),
                            ("delay_ms", 100), ("capture_id", 1)):
            for args in ({name: value}, {"steps": [{"tool": "press_key", "args": {"key": "a"}},
                         {"tool": "click", "args": {"x": 1, "y": 1, name: value}}]}):
                result = client.tool("computer", **args)
                self.assertEqual(result["structuredContent"]["code"], "invalid_arguments")
        for invalid_id in (0, -1, 1.5, True, "1", "c1", 9007199254740992):
            for args in ({"id": invalid_id, "steps": [{"tool": "press_key", "args": {"key": "a"}}]},
                         {"steps": [{"tool": "press_key", "args": {"key": "a"}},
                                    {"tool": "click", "args": {"x": 1, "y": 1, "id": invalid_id}}]}):
                result = client.tool("computer", **args)
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
        self.assertGreaterEqual(capture_time - times[-1], .15)
        self.assertEqual(sum(block["type"] == "image" for block in result["content"]), 1)

    def test_explicit_zero_delay_disables_all_automatic_pauses(self):
        self.start_accessibility()
        self.start_host()
        client = self.client()
        tree = self.snapshot(client)
        button = next(row for row in tree["elements"] if row["label"] == "Fixture button")
        click = {"tool": "click", "args": {"element_token": button["element_token"]}}
        # Six actions would otherwise introduce at least 750 ms of pauses.
        result = client.tool("computer", steps=[click] * 6 + [
            {"tool": "delay_ms", "args": {"ms": 0}}], observe={"kind": "desktop"})
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["completed"], 7)
        times = json.loads(self.atspi_state.read_text())["action_times"]
        self.assertLess(times[-1] - times[0], .6)
        capture_time = json.loads(self.state.read_text())["capture_times"][-1]
        self.assertLess(capture_time - times[-1], .15)

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
        self.assertEqual(data["completed"], 1)
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["failed"]["index"], 1)
        self.assertEqual(data["failed"]["code"], "accessibility_permission_required")
        self.assertEqual(json.loads(self.atspi_state.read_text())["clicks"], 1)

    def test_explicit_delays_finish_before_one_final_screenshot(self):
        self.start_host()
        client = self.client()
        self.status(client)
        before = json.loads(self.state.read_text())["captures"]
        start = time.monotonic()
        result = client.tool("computer", steps=[
            {"tool": "press_key", "args": {"key": "a"}},
            {"tool": "delay_ms", "args": {"ms": 250}},
            {"tool": "press_key", "args": {"key": "b"}},
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
                {"tool": "press_key", "args": {"key": "a"}},
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
                          {"tool": "press_key", "args": {"key": "a"}}]}}}) + "\n")
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
                          {"tool": "press_key", "args": {"key": "a"}}]}}}) + "\n")
        client.process.stdin.flush()
        time.sleep(.1)
        self.computer_path.unlink()
        self.assertTrue(select.select([client.process.stdout], [], [], 2)[0], "delay did not stop on revocation")
        result = json.loads(client.process.stdout.readline())["result"]
        data = result["structuredContent"]
        self.assertTrue(result["isError"], result)
        self.assertEqual(data["completed"], 0)
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["failed"]["index"], 0)
        self.assertEqual(data["failed"]["tool"], "delay_ms")
        self.assertEqual(data["failed"]["code"], "accessibility_permission_required")
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
            {"tool": "hotkey", "args": {"keys": ["ctrl", "a"]}},
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

    def test_cancellation_interrupts_a_stalled_peer_authentication(self):
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
            {"tool": "press_key", "args": {"key": "a"}},
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
        result = client.tool("computer", steps=[{"tool": "press_key", "args": {"key": "a"}}], observe=False)
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
        result = client.tool("computer", steps=[{"tool": "press_key", "args": {"key": "a"}}],
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
                self.environment["LD_LIBRARY_PATH"] = str(library_dir)
                client = self.client()
                self.assertIn("tools", client.call("tools/list", {})["result"])
                for name, args in (("status", {}), ("computer", {"steps": [
                    {"tool": "press_key", "args": {"key": "a"}}]})):
                    result = client.tool(name, **args)
                    self.assertTrue(result["isError"], result)
                    self.assertEqual(result["structuredContent"]["code"], "missing_dependency")
                    self.assertTrue(all(block["type"] == "text" for block in result["content"]))
        state = json.loads(self.state.read_text())
        self.assertEqual((state["captures"], state["connections"]), (0, 0))

    def test_missing_accessibility_fails_before_capture_or_input(self):
        self.start_host(accessibility=False)
        client = self.client()
        for name, args in (("status", {}), ("computer", {"steps": [
            {"tool": "press_key", "args": {"key": "a"}}]})):
            result = client.tool(name, **args)
            self.assertTrue(result["isError"], result)
            self.assertEqual(result["structuredContent"]["code"], "accessibility_permission_required")
            self.assertTrue(all(block["type"] == "text" for block in result["content"]))
            self.assertNotIn("canvas", result["structuredContent"])
            self.assertNotIn("apps", result["structuredContent"])
        state = json.loads(self.state.read_text())
        self.assertEqual((state["captures"], state["connections"]), (0, 0))
        self.start_accessibility()
        self.status(client)  # The same MCP connection recovers once prerequisites work.

    def test_broken_registry_socket_is_not_a_successful_status(self):
        self.start_accessibility(unavailable=True)
        self.start_host()
        client = self.client()
        for name, args in (("status", {}), ("computer", {"steps": [
            {"tool": "press_key", "args": {"key": "a"}}]})):
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
