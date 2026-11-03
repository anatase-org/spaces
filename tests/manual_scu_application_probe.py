"""Sequential real-Codex SCU application trials on win5/win5f.

Run after sync.sh. Uses a disposable host fixture installed by the companion
script; preserves raw evidence and restores the initial screen-lock state.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import select
import shlex
import subprocess
import time

from manual_scu_session_probe import Probe
from scu_tree import read_result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="win5")
    parser.add_argument("--guest", default="win5f")
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--fixture", default="/tmp/scu-app-eval-20261103")
    parser.add_argument("--only", help="Run one trial label, preserving others")
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)

    def remote(device, *command, timeout=30):
        return subprocess.check_output(["ssh", "-o", "BatchMode=yes", device, shlex.join(command)],
                                       text=True, timeout=timeout).strip()

    def host(*command, **kwargs):
        return remote(args.host, *command, **kwargs)

    fixture = json.loads(host("cat", args.fixture + "/fixture.json"))
    session = next(line.split("=", 1)[1] for line in host("systemctl", "--user", "show-environment").splitlines()
                   if line.startswith("XDG_SESSION_ID="))

    def locked():
        return host("loginctl", "show-session", session, "-p", "LockedHint", "--value") == "yes"

    def set_lock(value):
        host("loginctl", "lock-session" if value else "unlock-session", session)
        until = time.monotonic() + 15
        while locked() != value and time.monotonic() < until:
            time.sleep(.1)
        if locked() != value:
            raise RuntimeError("Could not establish requested lock state")
        time.sleep(1)

    initial_lock = locked()
    (artifacts / "initial-lock.json").write_text(json.dumps(dict(locked=initial_lock, session=session)))
    summaries = []
    try:
        for repetition in range(2):
            for no_vision in (False, True):
                for scenario in ("browser", "files", "action"):
                    trial = f"{'locked' if repetition else 'unlocked'}-{'accessibility' if no_vision else 'vision'}-{scenario}"
                    if args.only and trial != args.only:
                        continue
                    directory = artifacts / trial
                    directory.mkdir(exist_ok=True)
                    if (directory / "summary.json").exists() and not args.only:
                        summaries.append(json.loads((directory / "summary.json").read_text()))
                        continue
                    set_lock(bool(repetition))
                    host("python3", "-c", "from pathlib import Path;import sys;Path(sys.argv[1]).write_text(sys.argv[2])",
                         args.fixture + "/active.json", json.dumps(dict(trial=trial)))
                    expected = "verified-" + trial
                    path = args.fixture + "/SCU " + trial + ".txt"
                    if scenario == "files":
                        host("python3", "-c", "from pathlib import Path;import sys;Path(sys.argv[1]).write_text(sys.argv[2])",
                             path, "SCU verification code: " + expected + "\n")
                        task = f"Open the host file {path!r} in the installed text editor and report the verification code shown in the document."
                    elif scenario == "browser":
                        url = f"http://127.0.0.1:{fixture['port']}/case/{trial}"
                        task = f"Find SCU Evaluation Browser, open {url}, enter {expected!r} into the page's Message field, and submit the result."
                    else:
                        task = f"Find SCU Evaluation Browser and invoke its declared new-window launcher action. In the resulting SCU test page, enter {expected!r} into Message and submit the result."
                    prompt = ("Interact with host applications only through the SCU tools. Do not use shell commands, HTTP clients, "
                              "other browser tools, or filesystem reads to perform the task. Preserve unrelated user windows and settings. "
                              "Verify through the application UI. If blocked, report the precise obstacle.\n\n" + task)
                    (directory / "prompt.txt").write_text(prompt)
                    scratch = "/tmp/scu-codex-" + trial
                    remote(args.guest, "mkdir", "-p", scratch)
                    # timeout owns a fresh remote process group so call-limit
                    # termination only affects this trial and its MCP process.
                    remote_pid = scratch + "/timeout.pid"
                    command = ["codex", "exec", "--json", "--skip-git-repo-check", "-C", scratch,
                               "-c", 'mcp_servers.scu.default_tools_approval_mode="approve"',
                               "-c", 'mcp_servers.scu.args=' + json.dumps(["mcp", *(["--no-vision"] if no_vision else [])]),
                               "-o", scratch + "/final.txt", "-"]
                    shell = "echo $$ > " + shlex.quote(remote_pid) + "; exec timeout -k 5 300 " + shlex.join(command)
                    start = time.monotonic()
                    events, started_calls, completed_calls = [], Counter(), Counter()
                    limit = None
                    with (directory / "stderr.log").open("w") as errors, (directory / "events.jsonl").open("w") as stream:
                        process = subprocess.Popen(["ssh", "-o", "BatchMode=yes", args.guest, shell],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True, bufsize=1)
                        process.stdin.write(prompt)
                        process.stdin.close()
                        while True:
                            if not select.select([process.stdout], [], [], 1)[0]:
                                if process.poll() is not None:
                                    break
                                if time.monotonic() - start > 315:
                                    limit = "wall_time"
                                    break
                                continue
                            line = process.stdout.readline()
                            if not line:
                                break
                            stream.write(line); stream.flush()
                            try:
                                event = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            event["observed_elapsed"] = round(time.monotonic() - start, 3)
                            events.append(event)
                            item = event.get("item", {})
                            if item.get("type") == "mcp_tool_call":
                                tool = item.get("tool", "unknown")
                                if event.get("type") == "item.started":
                                    started_calls[tool] += 1
                                if event.get("type") == "item.completed":
                                    completed_calls[tool] += 1
                                if sum(started_calls.values()) >= 50:
                                    limit = "call_count"
                                    break
                        if limit:
                            try:
                                pid = remote(args.guest, "cat", remote_pid)
                                remote(args.guest, "kill", "-TERM", "--", "-" + pid)
                            except subprocess.SubprocessError:
                                pass
                        try:
                            code = process.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            process.terminate()
                            code = process.wait(timeout=5)
                        process.stdout.close()
                    elapsed = round(time.monotonic() - start, 3)
                    (directory / "events-timed.json").write_text(json.dumps(events, indent=2))
                    final = remote(args.guest, "cat", scratch + "/final.txt") if code == 0 else ""
                    (directory / "final.txt").write_text(final)
                    probe = Probe(args.guest, directory, "observer", no_vision=True)
                    try:
                        status = probe.tool("status")
                        windows = [{**w, "application": a["app_name"]} for a in status["apps"] for w in a["windows"]]
                        matching = [w for w in windows if trial in w["title"]]
                        if scenario == "files":
                            evidence = dict(code_reported=expected in final, windows=matching)
                            verified = expected in final and bool(matching)
                        else:
                            results = host("python3", "-c", "from pathlib import Path;import sys;p=Path(sys.argv[1]);print(p.read_text() if p.exists() else '')",
                                           args.fixture + "/results.jsonl")
                            records = [json.loads(line) for line in results.splitlines() if line]
                            matches = [r for r in records if r.get("trial") == trial and r.get("message") == expected]
                            evidence = dict(submissions=matches, windows=matching)
                            verified = bool(matches)
                        (directory / "evidence.json").write_text(json.dumps(evidence, indent=2))
                    finally:
                        probe.close()
                    failures = [e for e in events if e.get("type") in ("error", "turn.failed")]
                    summary = dict(trial=trial, scenario=scenario, locked=bool(repetition), no_vision=no_vision,
                                   exit_code=code, seconds=elapsed, verified=verified,
                                   outcome="completed" if verified else "inconclusive" if limit or code else "failed",
                                   limit=limit, started_calls=dict(started_calls), completed_calls=dict(completed_calls),
                                   errors=failures,
                                   usage=[e.get("usage") for e in events if e.get("type") == "turn.completed"])
                    (directory / "summary.json").write_text(json.dumps(summary, indent=2))
                    summaries.append(summary)
                    (artifacts / "summaries.json").write_text(json.dumps(summaries, indent=2))
                    print(json.dumps(summary), flush=True)
    finally:
        set_lock(initial_lock)


if __name__ == "__main__":
    main()
