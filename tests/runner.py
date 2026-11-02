"""Run the complete unittest suite in isolated worker processes.

Usage: PYTHONPATH=src python3 -m tests [-j WORKERS] [test.name ...]
The regular `python3 -m unittest` entry point remains available for debugging.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
from pathlib import Path
import queue
import sys
import time
import traceback
import unittest

ROOT = Path(__file__).resolve().parents[1]


def test_ids(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from test_ids(test)
        else:
            yield test.id()


def worker(pending, completed, log_path):
    # Native subprocesses inherit these descriptors as well as Python output.
    with open(log_path, "w", buffering=1) as log:
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
        class Result(unittest.TextTestResult):
            def startTest(self, test):
                self.started = time.perf_counter()
                self.status = "ok"
                super().startTest(test)

            def addFailure(self, test, error):
                self.status = "failure"
                super().addFailure(test, error)

            def addError(self, test, error):
                self.status = "error"
                super().addError(test, error)

            def addSkip(self, test, reason):
                self.status = "skip"
                super().addSkip(test, reason)

            def addSubTest(self, test, subtest, error):
                if error is not None:
                    self.status = "failure"
                super().addSubTest(test, subtest, error)

            def stopTest(self, test):
                completed.put(("test", test.id(), self.status, time.perf_counter() - self.started))
                super().stopTest(test)

        class QueueSuite(unittest.TestSuite):
            _cleanup = False

            def __iter__(self):
                while (name := pending.get()) is not None:
                    yield unittest.defaultTestLoader.loadTestsFromName(name)

        try:
            result = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=Result).run(QueueSuite())
            completed.put(("done", {
                "failures": [(str(test), detail) for test, detail in result.failures],
                "errors": [(str(test), detail) for test, detail in result.errors],
                "skipped": [(str(test), reason) for test, reason in result.skipped],
                "expected_failures": len(result.expectedFailures),
                "unexpected_successes": [str(test) for test in result.unexpectedSuccesses],
            }))
        except BaseException:
            completed.put(("crash", traceback.format_exc()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-j", "--jobs", type=int, default=min(24, len(os.sched_getaffinity(0))))
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("names", nargs="*")
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    started = time.perf_counter()
    sys.path.insert(0, str(ROOT / "src"))
    loader = unittest.defaultTestLoader
    suite = (loader.loadTestsFromNames(args.names) if args.names else
             loader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT)))
    names = list(test_ids(suite))
    # Longest-first scheduling avoids a slow integration test at the tail.
    # Learn timings locally; conservative defaults also work on a fresh checkout.
    timings_path = ROOT / ".build" / "test-runs" / "durations.json"
    try:
        timings = json.loads(timings_path.read_text())
    except (FileNotFoundError, ValueError):
        timings = {}
    def estimated_seconds(name):
        if name in timings: return timings[name]
        if "test_truncated_observations" in name: return 12
        if any(part in name for part in ("test_large_tree", "test_shared_headless",
                "test_toolkit_actions", "test_live_window_edits", "test_invalid_numeric_ranges",
                "test_observation_baselines")): return 6
        if ".test_broker_registration." in name: return 3
        if ".test_tui." in name: return 1
        if ".test_scu." in name: return 1.5
        return .01
    names.sort(key=estimated_seconds, reverse=True)
    if not names:
        print("No tests found")
        return 0
    jobs = min(args.jobs, len(names))
    log_dir = ROOT / ".build" / "test-runs" / str(time.time_ns())
    log_dir.mkdir(parents=True)
    context = multiprocessing.get_context("spawn")
    pending, completed = context.Queue(), context.Queue()
    pending.cancel_join_thread()
    for name in names:
        pending.put(name)
    for _ in range(jobs):
        pending.put(None)
    workers = [context.Process(target=worker, args=(pending, completed, str(log_dir / f"{i}.log")))
               for i in range(jobs)]
    print(f"Running {len(names)} tests with {jobs} workers", flush=True)
    for process in workers:
        process.start()
    records, outcomes, crashes = [], [], []
    try:
        while len(outcomes) + len(crashes) < jobs:
            try:
                message = completed.get(timeout=1)
            except queue.Empty:
                if all(not process.is_alive() for process in workers):
                    raise RuntimeError("workers exited without reporting results")
                continue
            kind, *data = message
            if kind == "test":
                name, status, seconds = data
                records.append({"id": name, "status": status, "seconds": seconds})
                if args.verbose:
                    print(f"{seconds:7.3f}s {status:7} {name}", flush=True)
                else:
                    print({"ok": ".", "skip": "s", "failure": "F", "error": "E"}[status], end="", flush=True)
            elif kind == "done":
                outcomes.append(data[0])
            else:
                crashes.append(data[0])
    finally:
        for process in workers:
            if process.is_alive():
                process.join(timeout=2)
            if process.is_alive():
                process.terminate()
            process.join()
    elapsed = time.perf_counter() - started
    failures = [item for outcome in outcomes for item in outcome["failures"]]
    errors = [item for outcome in outcomes for item in outcome["errors"]]
    skipped = [item for outcome in outcomes for item in outcome["skipped"]]
    unexpected = [item for outcome in outcomes for item in outcome["unexpected_successes"]]
    print(f"\nRan {len(records)} tests in {elapsed:.3f}s ({len(skipped)} skipped)")
    for name, detail in failures + errors:
        print(f"\n{name}\n{detail}")
    for detail in crashes:
        print(detail)
    for name in unexpected:
        print(f"Unexpected success: {name}")
    success = not (failures or errors or crashes or unexpected)
    print("OK" if success else "FAILED")
    print("Slowest tests:")
    for record in sorted(records, key=lambda item: -item["seconds"])[:10]:
        print(f'{record["seconds"]:7.3f}s {record["id"]}')
    report = {"seconds": elapsed, "workers": jobs, "tests": records, "outcomes": outcomes, "crashes": crashes}
    (log_dir / "results.json").write_text(json.dumps(report, indent=2))
    if success:
        timings.update({record["id"]: record["seconds"] for record in records})
        temporary = timings_path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(timings, indent=2))
        temporary.replace(timings_path)
    print(f"Results and worker logs: {log_dir.relative_to(ROOT)}")
    return not success


if __name__ == "__main__":
    sys.exit(main())
