"""Guest-side scu probe, restricted to the named manual AT-SPI fixtures.

Run with the host tests/manual_atspi_fixture.py. The fixture's separate JSON
state file is the action oracle; this probe never trusts tool success alone.
Images and result summaries are written only to the requested artifact directory.
"""

import argparse
import base64
import json
from pathlib import Path
import select
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("token")
    parser.add_argument("--binary", default="scu")
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--visual-actions", action="store_true")
    parser.add_argument("--entry-point", nargs=2, type=float,
                        help="Entry center in the returned 1280-pixel screenshot; inspect the image first")
    parser.add_argument("--display-id", default="primary")
    parser.add_argument("--surface-point", nargs=2, type=float,
                        help="Scroll/drag surface center in the returned screenshot")
    args = parser.parse_args()
    args.artifacts.mkdir(mode=0o700, parents=True, exist_ok=True)
    stderr = (args.artifacts / "stderr.log").open("w")
    process = subprocess.Popen([args.binary, "mcp"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=stderr, text=True)
    serial = 0

    def call(method, parameters):
        nonlocal serial
        serial += 1
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": serial,
            "method": method, "params": parameters}) + "\n")
        process.stdin.flush()
        assert select.select([process.stdout], [], [], 65)[0], "scu response timed out"
        line = process.stdout.readline()
        assert line, "scu exited; inspect stderr.log"
        result = json.loads(line)
        assert "result" in result, result
        return result["result"]

    def tool(name, **arguments):
        response = call("tools/call", {"name": name, "arguments": arguments})
        metadata = response.get("structuredContent", {})
        (args.artifacts / f"{serial:02d}-{name}.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
        for index, block in enumerate(response.get("content", [])):
            if block["type"] == "image":
                (args.artifacts / f"{serial:02d}-{index}.png").write_bytes(base64.b64decode(block["data"]))
        assert not response.get("isError"), metadata
        print(name, json.dumps(metadata, ensure_ascii=False)[:350], flush=True)
        return metadata

    try:
        call("initialize", {"protocolVersion": "2025-11-25"})
        health = tool("health_report")
        assert health["ready"], health
        title = "Spaces AT-SPI fixture " + args.token
        window = next(item for item in tool("list_windows")["windows"] if item["title"] == title)
        target = {key: window[key] for key in ("pid", "window_id")}
        snapshot = tool("get_window_state", **target, tree_format="elements")
        rows = {item["label"]: item for item in snapshot["elements"]}
        expected = "Héllo 世界 " + args.token
        tool("run_steps", steps=[
            {"click": {"element_token": rows["Spaces fixture button"]["element_token"]}},
            {"set_value": {"element_token": rows["Spaces fixture entry"]["element_token"], "value": expected}},
            {"click": {"element_token": rows["Spaces fixture checkbox"]["element_token"]}},
        ], observe={**target, "tree_format": "elements"})
        if args.capture or args.visual_actions:
            first = tool("get_desktop_state", max_image_dimension=1280)
            captures = {first["display"]: first}
            for display in first["displays"]:
                if display["display_id"] not in captures:
                    captures[display["display_id"]] = tool("get_desktop_state",
                        display_id=display["display_id"], max_image_dimension=1280)
            if args.visual_actions:
                if args.entry_point is None:
                    raise AssertionError("Inspect a capture and supply --entry-point X Y for visual tests")
                display_id = first["display"] if args.display_id == "primary" else args.display_id
                image = captures[display_id]
                x, y = args.entry_point
                pixel_target = {"kind": "desktop", "display_id": display_id}
                performed = tool("run_steps", delivery_mode="foreground", steps=[
                    {"click": {"target": pixel_target, "capture_id": image["capture_id"], "x": x, "y": y}},
                    {"hotkey": {"keys": ["ctrl", "a"]}},
                    {"type_text": {"text": "Visual Unicode 世界 " + args.token}},
                    {"press_key": {"key": "End"}},
                    {"type_text": {"text": "!"}},
                ])
                # Automatic final observation supersedes this monitor's capture.
                image = performed["observation"]
                if args.surface_point is not None:
                    x, y = args.surface_point
                    tool("run_steps", delivery_mode="foreground", steps=[
                        {"scroll": {"target": pixel_target, "capture_id": image["capture_id"],
                                    "x": x, "y": y, "direction": "down", "amount": 2}},
                        {"drag": {"target": pixel_target, "capture_id": image["capture_id"],
                                  "from_x": x - 40, "from_y": y,
                                  "to_x": x + 40, "to_y": y + 20, "duration_ms": 300}},
                    ])
                tool("get_desktop_state", display_id=display_id, max_image_dimension=1280)
        print("probe completed; verify the independent host fixture state", flush=True)
    finally:
        process.stdin.close()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill(); process.wait()
        stderr.close()


if __name__ == "__main__":
    main()
