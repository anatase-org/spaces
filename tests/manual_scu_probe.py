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

from scu_tree import read_trees


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("token")
    parser.add_argument("--binary", default="scu")
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--visual-actions", action="store_true")
    parser.add_argument("--entry-point", nargs=2, type=float,
                        help="Entry center in the returned 1280-pixel screenshot; inspect the image first")
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
        return read_trees(metadata)

    try:
        call("initialize", {"protocolVersion": "2025-11-25"})
        status = tool("status")
        assert status["apps"], status
        title = "Spaces AT-SPI fixture " + args.token
        target = next({"window_id": window["window_id"]}
                      for app in status["apps"] for window in app["windows"]
                      if window["title"] == title)
        snapshot = tool("vision", observations=[target["window_id"]])["observations"][0]
        rows = {item["label"]: item for item in snapshot["elements"]}
        expected = "Héllo 世界 " + args.token
        semantic = tool("accessibility", steps=[
            {"tool": "activate", "args": {"element_id": rows["Spaces fixture button"]["element_id"]}},
            {"tool": "set_value", "args": {"element_id": rows["Spaces fixture entry"]["element_id"], "value": expected}},
            {"tool": "activate", "args": {"element_id": rows["Spaces fixture checkbox"]["element_id"]}},
        ], observations=[target["window_id"]])
        rows = {item["label"]: item for item in semantic["observations"][0]["elements"]}
        if args.capture or args.visual_actions:
            first = tool("vision", observations=[])["observations"][0]
            if args.visual_actions:
                if args.entry_point is None:
                    raise AssertionError("Inspect a capture and supply --entry-point X Y for visual tests")
                image = first
                x, y = args.entry_point
                tool("accessibility", steps=[
                    {"tool": "set_value", "args": {"element_id": rows["Spaces fixture entry"]["element_id"], "value": ""}}])
                performed = tool("vision", observations=[], steps=[
                    {"tool": "click", "args": {"id": image["id"], "x": x, "y": y}},
                    {"tool": "type_text", "args": {"text": "replace me"}},
                    {"tool": "press_keys", "args": {"keys": ["ctrl", "a"]}},
                    {"tool": "type_text", "args": {"text": "Visual "}},
                    {"tool": "type_text", "args": {"text": "Unicode 世界 " + args.token}},
                    {"tool": "press_keys", "args": {"keys": ["End"]}},
                    {"tool": "type_text", "args": {"text": "!"}},
                ])
                # Automatic overview replaces the global pixel capture.
                image = performed["observations"][0]
                if args.surface_point is not None:
                    x, y = args.surface_point
                    tool("vision", observations=[], steps=[
                        {"tool": "scroll", "args": {"id": image["id"],
                                    "x": x, "y": y, "direction": "down", "amount": 2}},
                        {"tool": "drag", "args": {"id": image["id"],
                                  "from_x": x - 40, "from_y": y,
                                  "to_x": x + 40, "to_y": y + 20, "duration_ms": 300}},
                    ])
                tool("vision", observations=[])
        print("probe completed; verify the independent host fixture state", flush=True)
    finally:
        process.stdin.close()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill(); process.wait()
        stderr.close()


if __name__ == "__main__":
    main()
