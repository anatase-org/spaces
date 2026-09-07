"""Per-space host authentication support."""

from __future__ import annotations

import json
import logging
import os
import platform
import pwd
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import _
from . import core


logger = logging.getLogger(__name__)

RUNTIME_ROOT = Path("/run/spaces")
NATIVE_ROOT = Path("/usr/lib/spaces/guest")
PAM_WORKER = Path("/usr/lib/spaces/spaces-pam")
GUEST_RUNTIME = "/run/spaces-host"
GUEST_SOCKET = f"{GUEST_RUNTIME}/auth.sock"
GUEST_NATIVE = f"{GUEST_RUNTIME}/bin"
GUEST_BINARIES = (
    "pam_spaces.so",
    "spaces-portal",
    "spaces-system-broker",
    "spaces-open",
    "spaces-secret-helper",
    "spaces",
)
ELF_MACHINES = {
    "x86_64": 62,
    "aarch64": 183,
}

PROTOCOL_MAGIC = b"SPAU"
PROTOCOL_VERSION = 2
MAX_PAYLOAD = 16 * 1024
FRAME_HEADER = struct.Struct("!4sBBI")
AUTHENTICATE = 2
ERROR = 5

LISTENER_POLL_INTERVAL = 0.5
AUTHENTICATION_TIMEOUT = 120
SHUTDOWN_TIMEOUT = 5
RATE_LIMIT_WINDOW = 30
RATE_LIMIT_ATTEMPTS = 5
MAX_REQUESTS = 64
LISTEN_BACKLOG = 16

WORKER_ENVIRONMENT = {
    "LANG": "C.UTF-8",
    "PATH": "/usr/bin",
}


@dataclass(frozen=True)
class AuthenticationRuntime:
    directory: Path
    socket_path: Path

    @property
    def bind_arguments(self) -> tuple[str, ...]:
        return (f"--bind-ro={self.socket_path}:{GUEST_SOCKET}",)


def native_bind_argument() -> str:
    return f"--bind-ro={NATIVE_ROOT}:{GUEST_NATIVE}"


def _elf_header(path: Path) -> bytes:
    with path.open("rb") as stream:
        return stream.read(20)


def _compatible_elf(header: bytes, expected_machine: int) -> bool:
    return (
        len(header) >= 20
        and header[:6] == b"\x7fELF\x02\x01"
        and int.from_bytes(header[18:20], "little") == expected_machine
    )


def validate_native_bundle(machine: str) -> None:
    expected_machine = ELF_MACHINES[machine]
    for name in GUEST_BINARIES:
        path = NATIVE_ROOT / name
        try:
            header = _elf_header(path)
        except OSError as error:
            raise core.SpacesError(
                _("Guest-native helper is missing: {path}.", path=path)
            ) from error
        if not _compatible_elf(header, expected_machine):
            raise core.SpacesError(
                _(
                    "Guest-native helper has an incompatible "
                    "architecture: {path}.",
                    path=path,
                )
            )


def validate_guest_architecture(rootfs: Path, machine: str) -> None:
    expected_machine = ELF_MACHINES[machine]
    resolved_root = rootfs.resolve(strict=True)
    for relative in ("usr/bin/env", "bin/sh", "usr/bin/sh"):
        candidate = rootfs / relative
        try:
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(resolved_root):
                continue
            header = _elf_header(resolved)
        except OSError:
            continue
        if header[:4] != b"\x7fELF":
            continue
        if not _compatible_elf(header, expected_machine):
            raise core.SpacesError(
                _(
                    "The space architecture is incompatible with the host "
                    "authentication bundle."
                )
            )
        return
    raise core.SpacesError(
        _("Could not determine the space architecture for host authentication.")
    )


def prepare_runtime(
    space_name: str,
    rootfs: Path,
) -> AuthenticationRuntime:
    machine = platform.machine()
    if machine not in ELF_MACHINES:
        raise core.SpacesError(
            _("Host authentication is unsupported on {machine}.", machine=machine)
        )
    if not NATIVE_ROOT.is_dir():
        raise core.SpacesError(
            _("Host authentication bundle is missing at {path}.", path=NATIVE_ROOT)
        )
    validate_native_bundle(machine)
    validate_guest_architecture(rootfs, machine)
    parent = RUNTIME_ROOT / space_name
    directory = parent / "authentication"
    if parent.is_symlink() or directory.is_symlink():
        raise core.SpacesError(_("Unsafe authentication runtime directory."))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in (parent, directory):
        os.chown(path, 0, 0)
        os.chmod(path, 0o700)

    return AuthenticationRuntime(
        directory=directory,
        socket_path=directory / "auth.sock",
    )


def validate_native_runtime(rootfs: Path) -> None:
    """Validate the shared guest-native bundle before nspawn mounts it."""

    machine = platform.machine()
    if machine not in ELF_MACHINES:
        raise core.SpacesError(
            _("Guest-native helpers are unsupported on {machine}.", machine=machine)
        )
    if not NATIVE_ROOT.is_dir():
        raise core.SpacesError(
            _("Guest-native helper bundle is missing at {path}.", path=NATIVE_ROOT)
        )
    validate_native_bundle(machine)
    validate_guest_architecture(rootfs, machine)


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_frame(connection: socket.socket) -> tuple[int, bytes]:
    header = _recv_exact(connection, FRAME_HEADER.size)
    magic, version, message_type, size = FRAME_HEADER.unpack(header)
    if (
        magic != PROTOCOL_MAGIC
        or version != PROTOCOL_VERSION
        or size > MAX_PAYLOAD
    ):
        raise ValueError("invalid authentication protocol frame")
    return message_type, _recv_exact(connection, size)


def send_frame(
    connection: socket.socket,
    message_type: int,
    payload: bytes = b"",
) -> None:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("authentication payload is too large")
    connection.sendall(
        FRAME_HEADER.pack(
            PROTOCOL_MAGIC,
            PROTOCOL_VERSION,
            message_type,
            len(payload),
        )
        + payload
    )


class _WorkerPool:
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


def _cgroup_components(pid: int) -> set[str]:
    value = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    return {
        component
        for line in value.splitlines()
        for component in line.partition(":")[2].split("/")
        if component
    }


class AuthenticationService:
    """Authentication listener owned by one running space."""

    def __init__(
        self,
        space_name: str,
        runtime: AuthenticationRuntime,
        users: dict[int, bool],
    ) -> None:
        self.space_name = space_name
        self.runtime = runtime
        self.users = users
        self._attempts: dict[int, list[float]] = {}
        self._attempts_lock = threading.Lock()
        self._stop = threading.Event()
        self._stop_lock = threading.Lock()
        self._stopped = False
        self._handlers: set[threading.Thread] = set()
        self._handlers_lock = threading.Lock()
        self._request_slots = threading.BoundedSemaphore(MAX_REQUESTS)
        self._connections: set[socket.socket] = set()
        self._workers = _WorkerPool()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        runtime.socket_path.unlink(missing_ok=True)
        self._listener.bind(str(runtime.socket_path))
        runtime.socket_path.chmod(0o666)
        self._listener.listen(LISTEN_BACKLOG)
        self._listener.settimeout(LISTENER_POLL_INTERVAL)
        self._thread = threading.Thread(
            target=self._serve,
            name=f"spaces-{space_name}-authentication",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _address = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                logger.exception("Could not accept an authentication request.")
                continue
            if not self._request_slots.acquire(blocking=False):
                try:
                    send_frame(connection, ERROR)
                except OSError:
                    pass
                finally:
                    connection.close()
                continue
            handler = threading.Thread(
                target=self._handle,
                args=(connection,),
                name=f"spaces-{self.space_name}-auth-request",
                daemon=True,
            )
            with self._handlers_lock:
                self._handlers.add(handler)
                self._connections.add(connection)
            handler.start()

    @staticmethod
    def _peer_pid(connection: socket.socket) -> int:
        value = connection.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,
            struct.calcsize("3i"),
        )
        pid, _uid, _gid = struct.unpack("3i", value)
        return pid

    def _peer_in_space(self, pid: int) -> bool:
        try:
            components = _cgroup_components(pid)
        except OSError:
            return False
        return (
            f"spaces@{self.space_name}.service" in components
            or f"machine-{self.space_name}.scope" in components
        )

    def _consume_attempt(self, uid: int) -> None:
        if uid not in self.users:
            raise PermissionError("user is not configured for this space")
        now = time.monotonic()
        with self._attempts_lock:
            attempts = self._attempts.setdefault(uid, [])
            attempts[:] = [
                attempt
                for attempt in attempts
                if now - attempt < RATE_LIMIT_WINDOW
            ]
            if len(attempts) >= RATE_LIMIT_ATTEMPTS:
                raise PermissionError("authentication rate limit exceeded")
            attempts.append(now)

    def _handle(self, connection: socket.socket) -> None:
        try:
            connection.settimeout(AUTHENTICATION_TIMEOUT)
            message_type, payload = recv_frame(connection)
            if message_type != AUTHENTICATE:
                raise ValueError("unexpected authentication message")
            if not self._peer_in_space(self._peer_pid(connection)):
                raise PermissionError(
                    "request did not originate in this space"
                )
            self._authenticate(connection, payload)
        except (
            EOFError,
            OSError,
            ValueError,
            PermissionError,
            core.SpacesError,
        ) as error:
            logger.debug("Rejected authentication request: %s", error)
            try:
                send_frame(connection, ERROR)
            except OSError:
                pass
        finally:
            connection.close()
            with self._handlers_lock:
                self._handlers.discard(threading.current_thread())
                self._connections.discard(connection)
            self._request_slots.release()

    def _authenticate(
        self,
        connection: socket.socket,
        payload: bytes,
    ) -> None:
        value = json.loads(payload)
        if (
            not isinstance(value, dict)
            or set(value) != {"uid"}
            or not isinstance(value["uid"], int)
            or isinstance(value["uid"], bool)
            or value["uid"] < 0
        ):
            raise ValueError("invalid authentication request")
        uid = value["uid"]
        self._consume_attempt(uid)
        self._run_pam(connection, uid)

    def _run_worker(
        self,
        arguments: list[str],
        *,
        timeout: int = AUTHENTICATION_TIMEOUT,
        pass_fds: tuple[int, ...] = (),
    ) -> int:
        return self._workers.run(
            arguments,
            timeout=timeout,
            pass_fds=pass_fds,
        )

    def _run_pam(
        self,
        connection: socket.socket,
        uid: int,
    ) -> None:
        try:
            user_name = pwd.getpwuid(uid).pw_name
        except KeyError as error:
            raise PermissionError("host user disappeared") from error
        if not PAM_WORKER.is_file():
            raise OSError(f"missing PAM worker: {PAM_WORKER}")
        # Python timeout mode sets O_NONBLOCK on the underlying descriptor.
        # The native PAM conversation uses blocking reads, with its deadline
        # enforced by _run_worker rather than by the socket.
        connection.setblocking(True)
        descriptor = connection.fileno()
        self._run_worker(
            [
                str(PAM_WORKER),
                "--fd",
                str(descriptor),
                "--user",
                user_name,
            ],
            pass_fds=(descriptor,),
        )

    def stop(self) -> None:
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            self._stop.set()
            self._listener.close()
        if self._thread.is_alive():
            self._thread.join(timeout=SHUTDOWN_TIMEOUT)
        with self._handlers_lock:
            handlers = tuple(self._handlers)
            connections = tuple(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        self._workers.stop(SHUTDOWN_TIMEOUT)
        for handler in handlers:
            handler.join(timeout=SHUTDOWN_TIMEOUT)
        self.runtime.socket_path.unlink(missing_ok=True)
        shutil.rmtree(self.runtime.directory, ignore_errors=True)
        try:
            self.runtime.directory.parent.rmdir()
        except OSError:
            pass
