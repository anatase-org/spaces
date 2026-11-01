#!/usr/bin/python3
"""Private replacements for KDE's output-query and virtual-monitor helpers."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time

control = Path(os.environ["SCU_FIXTURE_CONTROL"])
state = Path(os.environ["SCU_DISPLAY_STATE"])
config = json.loads(control.read_text())


def write_state(value):
    temporary = state.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(state)


if "-j" in sys.argv:
    try:
        virtual = json.loads(state.read_text())
    except FileNotFoundError:
        virtual = None
    outputs = [{"name": "eDP-1", "connected": True, "enabled": not config.get("headless", False)}]
    if virtual:
        outputs.append({"name": "Virtual-" + virtual["name"], "connected": True,
                        "enabled": True})
        if config.get("omit_enabled"):
            del outputs[-1]["enabled"]
    print(json.dumps({"outputs": outputs}))
    sys.exit(0)

parser = argparse.ArgumentParser()
parser.add_argument("--name", required=True)
parser.add_argument("--resolution", required=True)
parser.add_argument("--scale", required=True)
args = parser.parse_args()
assert args.resolution == "1280x720" and args.scale == "1"
assert "--password" not in sys.argv
if config.get("helper_fail"):
    sys.exit(1)
write_state({"name": args.name, "pid": os.getpid()})
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
try:
    while True:
        time.sleep(.1)
finally:
    state.unlink(missing_ok=True)
