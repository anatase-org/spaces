"""Per-space authentication broker, leases, and isolated workers."""

from __future__ import annotations

import json
import logging
import os
import pwd
import secrets
import select
import shutil
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import _
from .. import core
from .protocol import (
    AUTHENTICATE,
    ERROR,
    REGISTER,
    RESULT,
    SESSION_CLOSE,
    SESSION_OPEN,
    TOKEN,
    LeaseSubject,
    decode_object,
    recv_frame,
    recv_frame_with_descriptors,
    send_frame,
    valid_guest_session_id,
    valid_integer,
    valid_token,
)
from .runtime import AuthenticationRuntime, NATIVE_ROOT
from .session import (
    cgroup_components,
    process_session_matches,
    process_start_time,
    session_process,
)
from .worker import WorkerPool


logger = logging.getLogger(__name__)

PAM_WORKER = Path("/usr/lib/spaces/spaces-pam-worker")
POLKIT_WORKER = Path("/usr/lib/spaces/spaces-polkit-worker")
GUEST_POLKIT_AGENT = NATIVE_ROOT / "spaces-polkit-agent"
SESSION_ENV = "SPACES_AUTH_SESSION"
POLKIT_SERVICE = "polkit-1"
POLKIT_HELPER_NAME = "polkit-agent-helper-1"

LEASE_LIFETIME = 12 * 60 * 60
LISTENER_POLL_INTERVAL = 0.5
AUTHENTICATION_TIMEOUT = 120
SHUTDOWN_TIMEOUT = 5
RATE_LIMIT_WINDOW = 30
RATE_LIMIT_ATTEMPTS = 5
MAX_REQUESTS = 64
LISTEN_BACKLOG = 16
TOKEN_BYTES = 32
PAM_SUCCESS = 0
PAM_AUTH_ERR = 7


@dataclass
class LiveLease:
    subject: LeaseSubject
    pidfd: int
    created_at: float
    attempts: list[float] = field(default_factory=list)
    guest_sessions: set[str] = field(default_factory=set)

    def close(self) -> None:
        try:
            os.close(self.pidfd)
        except OSError:
            pass


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
        self._leases: dict[str, LiveLease] = {}
        self._leases_lock = threading.Lock()
        self._stop = threading.Event()
        self._stop_lock = threading.Lock()
        self._stopped = False
        self._handlers: set[threading.Thread] = set()
        self._handlers_lock = threading.Lock()
        self._request_slots = threading.BoundedSemaphore(MAX_REQUESTS)
        self._connections: set[socket.socket] = set()
        self._workers = WorkerPool()
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
    def _peer_credentials(connection: socket.socket) -> tuple[int, int, int]:
        value = connection.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,
            struct.calcsize("3i"),
        )
        return struct.unpack("3i", value)

    def _peer_in_space(self, pid: int) -> bool:
        try:
            components = cgroup_components(pid)
        except OSError:
            return False
        return (
            f"spaces@{self.space_name}.service" in components
            or f"machine-{self.space_name}.scope" in components
        )

    @staticmethod
    def _peer_in_guest_session(pid: int, session_id: str) -> bool:
        try:
            return f"session-{session_id}.scope" in cgroup_components(pid)
        except OSError:
            return False

    @staticmethod
    def _subject_alive(lease: LiveLease) -> bool:
        readable, _writable, _exceptional = select.select(
            [lease.pidfd], [], [], 0
        )
        if readable:
            return False
        try:
            return (
                process_start_time(lease.subject.pid)
                == lease.subject.start_time
            )
        except core.SpacesError:
            return False

    def _live_lease_locked(
        self,
        token: str,
        uid: int,
        *,
        consume_attempt: bool = False,
    ) -> LiveLease:
        lease = self._leases.get(token)
        if lease is None:
            raise PermissionError("invalid or expired authentication lease")
        return self._validate_lease_locked(
            lease,
            uid,
            consume_attempt=consume_attempt,
        )

    def _validate_lease_locked(
        self,
        lease: LiveLease,
        uid: int,
        *,
        consume_attempt: bool = False,
    ) -> LiveLease:
        if (
            lease.subject.uid != uid
            or uid not in self.users
            or time.monotonic() - lease.created_at > LEASE_LIFETIME
            or not self._subject_alive(lease)
        ):
            raise PermissionError("invalid or expired authentication lease")
        if consume_attempt:
            now = time.monotonic()
            lease.attempts[:] = [
                attempt
                for attempt in lease.attempts
                if now - attempt < RATE_LIMIT_WINDOW
            ]
            if len(lease.attempts) >= RATE_LIMIT_ATTEMPTS:
                raise PermissionError("authentication rate limit exceeded")
            lease.attempts.append(now)
        return lease

    def _live_lease(
        self,
        token: str,
        uid: int,
        *,
        consume_attempt: bool = False,
    ) -> LiveLease:
        with self._leases_lock:
            return self._live_lease_locked(
                token,
                uid,
                consume_attempt=consume_attempt,
            )

    def _retire_lease(self, lease: LiveLease) -> None:
        lease.guest_sessions.clear()
        lease.close()

    def _handle(self, connection: socket.socket) -> None:
        descriptors: tuple[int, ...] = ()
        try:
            connection.settimeout(AUTHENTICATION_TIMEOUT)
            message_type, payload, descriptors = recv_frame_with_descriptors(
                connection
            )
            peer_pid, peer_uid, _peer_gid = self._peer_credentials(connection)
            if message_type == REGISTER:
                if descriptors:
                    raise ValueError(
                        "registration included an unexpected descriptor"
                    )
                self._register(connection, payload, peer_uid)
            elif message_type in {SESSION_OPEN, SESSION_CLOSE}:
                if descriptors:
                    raise ValueError(
                        "session event included an unexpected descriptor"
                    )
                if not self._peer_in_space(peer_pid):
                    raise PermissionError(
                        "session event did not originate in this space"
                    )
                self._session_event(
                    payload,
                    peer_pid,
                    opening=message_type == SESSION_OPEN,
                )
            elif message_type == AUTHENTICATE:
                if not self._peer_in_space(peer_pid):
                    raise PermissionError(
                        "request did not originate in this space"
                    )
                self._authenticate(
                    connection,
                    payload,
                    peer_pid,
                    descriptors,
                )
            else:
                raise ValueError("unexpected authentication message")
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
            for descriptor in descriptors:
                os.close(descriptor)
            connection.close()
            with self._handlers_lock:
                self._handlers.discard(threading.current_thread())
                self._connections.discard(connection)
            self._request_slots.release()

    def _authenticate(
        self,
        connection: socket.socket,
        payload: bytes,
        peer_pid: int,
        descriptors: tuple[int, ...] = (),
    ) -> None:
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError("invalid authentication request")
        fields = set(value)
        if fields not in (
            {"token", "uid", "service"},
            {"uid", "service"},
        ):
            raise ValueError("invalid authentication request")
        token = value.get("token")
        uid = value["uid"]
        service = value["service"]
        if (
            not valid_integer(uid)
            or not isinstance(service, str)
            or not service
            or len(service) > 128
        ):
            raise ValueError("invalid authentication request")
        if token is None:
            if service != POLKIT_SERVICE:
                raise PermissionError(
                    "only the Polkit helper may omit a lease token"
                )
            if len(descriptors) > 1:
                raise ValueError("invalid number of Polkit agent pidfds")
            routing_pid = (
                self._process_from_agent_pidfd(descriptors[0], uid)
                if descriptors
                else self._validate_direct_polkit_helper(peer_pid, uid)
            )
            lease = self._lease_for_guest_session(routing_pid, uid)
        else:
            if descriptors:
                raise ValueError(
                    "token authentication included an unexpected descriptor"
                )
            if not valid_token(token):
                raise ValueError("invalid authentication request")
            lease = self._live_lease(token, uid, consume_attempt=True)
        if service == POLKIT_SERVICE:
            self._run_polkit(connection, lease.subject)
        else:
            self._run_pam(connection, lease.subject)

    @staticmethod
    def _process_from_agent_pidfd(descriptor: int, uid: int) -> int:
        readable, _writable, _exceptional = select.select(
            [descriptor], [], [], 0
        )
        if readable:
            raise PermissionError("Polkit agent pidfd is no longer live")
        fields = {}
        for line in Path(f"/proc/self/fdinfo/{descriptor}").read_text(
            encoding="utf-8"
        ).splitlines():
            key, separator, value = line.partition(":")
            if separator:
                fields[key] = value.strip()
        try:
            pid = int(fields["Pid"])
        except (KeyError, ValueError) as error:
            raise PermissionError("invalid Polkit agent pidfd") from error
        process = Path(f"/proc/{pid}")
        if process.stat().st_uid != uid:
            raise PermissionError("Polkit agent UID does not match the lease")
        try:
            if not (process / "exe").samefile(GUEST_POLKIT_AGENT):
                raise PermissionError(
                    "Polkit pidfd does not identify the Spaces agent"
                )
        except OSError as error:
            raise PermissionError(
                "could not verify the Spaces Polkit agent"
            ) from error
        readable, _writable, _exceptional = select.select(
            [descriptor], [], [], 0
        )
        if readable:
            raise PermissionError("Polkit agent pidfd is no longer live")
        return pid

    @staticmethod
    def _validate_direct_polkit_helper(peer_pid: int, uid: int) -> int:
        try:
            executable = Path(f"/proc/{peer_pid}/exe").readlink()
            status = Path(f"/proc/{peer_pid}/status").read_text(
                encoding="utf-8"
            )
        except OSError as error:
            raise PermissionError(
                "could not verify the Polkit helper"
            ) from error
        if executable.name != POLKIT_HELPER_NAME:
            raise PermissionError(
                "tokenless request did not originate from the Polkit helper"
            )
        uid_line = next(
            (line for line in status.splitlines() if line.startswith("Uid:")),
            "",
        )
        try:
            real_uid, effective_uid, _saved_uid, _filesystem_uid = (
                int(value) for value in uid_line.removeprefix("Uid:").split()
            )
        except ValueError as error:
            raise PermissionError(
                "could not verify the Polkit helper credentials"
            ) from error
        if real_uid != uid or effective_uid != 0:
            raise PermissionError(
                "Polkit helper credentials do not match the lease"
            )
        return peer_pid

    def _lease_for_guest_session(
        self,
        peer_pid: int,
        uid: int,
    ) -> LiveLease:
        components = cgroup_components(peer_pid)
        session_ids = {
            component.removeprefix("session-").removesuffix(".scope")
            for component in components
            if component.startswith("session-")
            and component.endswith(".scope")
            and valid_guest_session_id(
                component.removeprefix("session-").removesuffix(".scope")
            )
        }
        if len(session_ids) != 1:
            raise PermissionError(
                "Polkit helper does not belong to one guest login session"
            )
        session_id = session_ids.pop()
        with self._leases_lock:
            matches: list[LiveLease] = []
            for lease in self._leases.values():
                if session_id not in lease.guest_sessions:
                    continue
                try:
                    matches.append(
                        self._validate_lease_locked(
                            lease,
                            uid,
                            consume_attempt=False,
                        )
                    )
                except PermissionError:
                    continue
            if len(matches) != 1:
                raise PermissionError(
                    "guest login session has no unique live lease"
                )
            lease = matches[0]
            return self._validate_lease_locked(
                lease,
                uid,
                consume_attempt=True,
            )

    def _register(
        self,
        connection: socket.socket,
        payload: bytes,
        peer_uid: int,
    ) -> None:
        if peer_uid != 0:
            raise PermissionError("only the privileged enter helper may register")
        subject = LeaseSubject.from_payload(payload)
        if subject.uid not in self.users:
            raise PermissionError("user is not configured for this space")
        try:
            account = pwd.getpwuid(subject.uid)
        except KeyError as error:
            raise PermissionError("host user disappeared") from error
        if (
            account.pw_gid != subject.gid
            or Path(f"/proc/{subject.pid}").stat().st_uid != subject.uid
            or process_start_time(subject.pid) != subject.start_time
            or not process_session_matches(
                subject.pid,
                subject.session_id,
                subject.uid,
            )
        ):
            raise PermissionError("authentication subject metadata changed")
        lease = LiveLease(
            subject=subject,
            pidfd=os.pidfd_open(subject.pid),
            created_at=time.monotonic(),
        )
        token = secrets.token_hex(TOKEN_BYTES)
        with self._leases_lock:
            self._leases[token] = lease
        try:
            send_frame(connection, TOKEN, token.encode())
            connection.settimeout(LEASE_LIFETIME)
            while connection.recv(1):
                pass
        finally:
            with self._leases_lock:
                if self._leases.get(token) is lease:
                    self._leases.pop(token)
                lease.guest_sessions.clear()
            lease.close()

    def _session_event(
        self,
        payload: bytes,
        peer_pid: int,
        *,
        opening: bool,
    ) -> None:
        value = decode_object(payload, {"token", "uid", "session_id"})
        token = value["token"]
        uid = value["uid"]
        session_id = value["session_id"]
        if (
            not valid_token(token)
            or not valid_integer(uid)
            or not valid_guest_session_id(session_id)
        ):
            raise ValueError("invalid guest session event")
        if opening and not self._peer_in_guest_session(peer_pid, session_id):
            raise PermissionError("guest login session does not match peer")

        with self._leases_lock:
            lease = self._live_lease_locked(token, uid)
            if opening:
                lease.guest_sessions.add(session_id)
            else:
                lease.guest_sessions.discard(session_id)

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
        subject: LeaseSubject,
    ) -> None:
        try:
            user_name = pwd.getpwuid(subject.uid).pw_name
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

    def _run_polkit(
        self,
        connection: socket.socket,
        subject: LeaseSubject,
    ) -> None:
        if not self.users.get(subject.uid, False):
            send_frame(connection, RESULT, struct.pack("!i", PAM_AUTH_ERR))
            return
        if not POLKIT_WORKER.is_file():
            raise OSError(f"missing Polkit worker: {POLKIT_WORKER}")
        polkit_pid, polkit_start_time = session_process(
            subject.session_id,
            subject.uid,
        )
        return_code = self._run_worker(
            [
                str(POLKIT_WORKER),
                "--uid",
                str(subject.uid),
                "--gid",
                str(subject.gid),
                "--pid",
                str(polkit_pid),
                "--start",
                str(polkit_start_time),
            ]
        )
        result = PAM_SUCCESS if return_code == 0 else PAM_AUTH_ERR
        send_frame(connection, RESULT, struct.pack("!i", result))

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
        with self._leases_lock:
            leases = tuple(self._leases.values())
            self._leases.clear()
        for lease in leases:
            self._retire_lease(lease)
        self.runtime.socket_path.unlink(missing_ok=True)
        shutil.rmtree(self.runtime.directory, ignore_errors=True)
        try:
            self.runtime.directory.parent.rmdir()
        except OSError:
            pass


class LeaseConnection:
    """Live connection which owns an opaque entry-session lease."""

    def __init__(self, socket_path: Path, subject: LeaseSubject) -> None:
        self.connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.connection.connect(str(socket_path))
        send_frame(self.connection, REGISTER, subject.payload())
        message_type, payload = recv_frame(self.connection)
        if message_type != TOKEN:
            self.connection.close()
            raise core.SpacesError(
                _("Could not register host authentication session.")
            )
        try:
            token = payload.decode("ascii")
        except UnicodeDecodeError as error:
            self.connection.close()
            raise core.SpacesError(
                _("Authentication service returned an invalid token.")
            ) from error
        if not valid_token(token):
            self.connection.close()
            raise core.SpacesError(
                _("Authentication service returned an invalid token.")
            )
        self.token = token

    def close(self) -> None:
        self.connection.close()
