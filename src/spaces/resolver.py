"""Bounded, query-only relay for systemd-resolved's public Varlink socket.

Keep the host socket private: even this public endpoint also implements
interfaces such as io.systemd.LogControl which can change host configuration.
DNS configuration continues to use the permission-checked D-Bus bridge.
"""

from __future__ import annotations

import errno
import json
import socket
import socketserver
import threading
from pathlib import Path

HOST_SOCKET = Path("/run/systemd/resolve/io.systemd.Resolve")
SOCKET_NAME = "io.systemd.Resolve"
MAX_MESSAGE = 1024 * 1024
MAX_CLIENTS = 16
TIMEOUT = 30
QUERY_METHODS = frozenset(
    f"io.systemd.Resolve.{name}"
    for name in (
        "DumpDNSConfiguration",
        "ResolveHostname",
        "ResolveAddress",
        "ResolveRecord",
        "ResolveService",
    )
)


def _frames(connection: socket.socket):
    pending = bytearray()
    while chunk := connection.recv(4096):
        pending.extend(chunk)
        while (end := pending.find(0)) >= 0:
            if end > MAX_MESSAGE:
                raise ValueError("Varlink message too large")
            yield bytes(pending[:end])
            del pending[: end + 1]
        if len(pending) > MAX_MESSAGE:
            raise ValueError("Varlink message too large")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate Varlink field")
        result[key] = value
    return result


def _decode(frame: bytes) -> dict:
    value = json.loads(frame, object_pairs_hook=_object)
    if not isinstance(value, dict):
        raise ValueError("Varlink message must be an object")
    return value


def _error(name: str, **parameters) -> bytes:
    return json.dumps({"error": name, "parameters": parameters}).encode() + b"\0"


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Limit workers and close both sides before joining them on shutdown."""

    def __init__(self, address: str, host_socket: Path):
        self.host_socket = host_socket
        self.slots = threading.BoundedSemaphore(MAX_CLIENTS)
        self.lock = threading.Lock()
        self.connections: set[socket.socket] = set()
        self.closing = False
        super().__init__(address, _Handler)

    def track(self, connection: socket.socket) -> None:
        with self.lock:
            if self.closing:
                connection.close()
                raise OSError(errno.ECONNABORTED, "Resolver proxy stopped")
            self.connections.add(connection)

    def untrack(self, connection: socket.socket) -> None:
        with self.lock:
            self.connections.discard(connection)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            self.track(request)
            super().process_request(request, client_address)
        except Exception:
            self.untrack(request)
            self.slots.release()
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.untrack(request)
            self.slots.release()

    def close_connections(self):
        with self.lock:
            self.closing = True
            for connection in self.connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()


class _Handler(socketserver.BaseRequestHandler):
    server: _Server

    def exchange(self, frame: bytes) -> bytes:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
            upstream.settimeout(TIMEOUT)
            self.server.track(upstream)
            try:
                upstream.connect(str(self.server.host_socket))
                upstream.sendall(frame + b"\0")
                try:
                    reply = next(_frames(upstream), None)
                    if reply is None or _decode(reply).get("continues"):
                        raise ValueError("Expected a single Varlink reply")
                except (ValueError, RecursionError) as error:
                    raise OSError(errno.EPROTO, "Invalid resolver reply") from error
                return reply + b"\0"
            finally:
                self.server.untrack(upstream)

    def handle(self):
        self.request.settimeout(TIMEOUT)
        try:
            for frame in _frames(self.request):
                try:
                    message = _decode(frame)
                    method = message.get("method")
                    if not isinstance(method, str):
                        raise ValueError("Missing method")
                    if method not in QUERY_METHODS:
                        self.request.sendall(
                            _error("org.varlink.service.MethodNotFound", method=method)
                        )
                        continue
                    if (
                        message.keys()
                        - {"method", "parameters", "more", "oneway", "upgrade"}
                        or not isinstance(message.get("parameters", {}), dict)
                        or any(
                            message.get(flag, False) is not False
                            for flag in ("more", "oneway", "upgrade")
                        )
                    ):
                        raise ValueError("Unsupported Varlink request")
                    reply = self.exchange(frame)
                except (ValueError, RecursionError):
                    reply = _error(
                        "org.varlink.service.InvalidParameter", parameter="request"
                    )
                except OSError as error:
                    reply = _error("io.systemd.System.Errno", errno=error.errno or errno.EIO)
                self.request.sendall(reply)
        except (OSError, ValueError):
            pass


class ResolverService:
    def __init__(self, directory: Path, host_socket: Path = HOST_SOCKET):
        self.directory = directory
        self.socket = directory / SOCKET_NAME
        self.host_socket = host_socket
        self.server: _Server | None = None
        self.thread: threading.Thread | None = None

    def start(self):
        self.socket.unlink(missing_ok=True)
        self.server = _Server(str(self.socket), self.host_socket)
        self.socket.chmod(0o666)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="spaces-resolver",
        )
        self.thread.start()

    def stop(self):
        if self.server is not None:
            if self.thread is not None and self.thread.is_alive():
                self.server.shutdown()
            self.server.close_connections()
            self.server.server_close()
            self.server = None
            self.socket.unlink(missing_ok=True)
        if self.thread is not None:
            self.thread.join()
            self.thread = None
