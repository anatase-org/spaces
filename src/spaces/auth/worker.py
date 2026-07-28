"""Tracked, deadline-bound authentication subprocesses."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading


logger = logging.getLogger(__name__)

WORKER_ENVIRONMENT = {
    "LANG": "C.UTF-8",
    "PATH": "/usr/bin",
}


class WorkerPool:
    def __init__(self) -> None:
        self._processes: set[subprocess.Popen[bytes]] = set()
        self._lock = threading.Lock()

    def run(
        self,
        arguments: list[str],
        *,
        timeout: int,
        pass_fds: tuple[int, ...] = (),
    ) -> int:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=pass_fds,
            start_new_session=True,
            env=WORKER_ENVIRONMENT,
        )
        with self._lock:
            self._processes.add(process)
        try:
            try:
                return process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._kill(process)
                return process.wait()
        finally:
            with self._lock:
                self._processes.discard(process)

    def stop(self, timeout: int) -> None:
        with self._lock:
            processes = tuple(self._processes)
        for process in processes:
            self._kill(process)
        for process in processes:
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                logger.error("Authentication worker could not be reaped.")

    @staticmethod
    def _kill(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
