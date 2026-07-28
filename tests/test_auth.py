from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spaces import auth, core


class AuthenticationPolicyTests(unittest.TestCase):
    @staticmethod
    def _elf(machine: int = 62) -> bytes:
        value = bytearray(64)
        value[:6] = b"\x7fELF\x02\x01"
        value[18:20] = machine.to_bytes(2, "little")
        return bytes(value)

    def test_runtime_bind_destinations_are_fixed_and_read_only(self) -> None:
        runtime = auth.AuthenticationRuntime(
            directory=Path("/run/spaces/work/authentication"),
            socket_path=Path("/run/spaces/work/authentication/auth.sock"),
        )
        self.assertEqual(
            runtime.bind_arguments,
            (
                "--bind-ro=/run/spaces/work/authentication/auth.sock:"
                "/run/spaces-host/auth.sock",
                "--bind-ro=/usr/lib/spaces/guest:/run/spaces-host/bin",
            ),
        )

    def test_prepare_runtime_creates_only_socket_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rootfs = root / "rootfs"
            guest_bin = rootfs / "usr" / "bin"
            guest_bin.mkdir(parents=True)
            (guest_bin / "env").write_bytes(self._elf())
            native = root / "native"
            native.mkdir()
            for name in auth.GUEST_BINARIES:
                (native / name).write_bytes(self._elf())

            with (
                mock.patch.object(auth, "NATIVE_ROOT", native),
                mock.patch.object(
                    auth, "RUNTIME_ROOT", root / "runtime"
                ),
                mock.patch.object(
                    auth.platform,
                    "machine",
                    return_value="x86_64",
                ),
                mock.patch.object(auth.os, "chown"),
            ):
                runtime = auth.prepare_runtime("work", rootfs)

            self.assertEqual(tuple(runtime.directory.iterdir()), ())

    def test_bundle_rejects_wrong_architecture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            native = Path(temporary)
            for name in auth.GUEST_BINARIES:
                (native / name).write_bytes(self._elf(machine=183))
            with (
                mock.patch.object(auth, "NATIVE_ROOT", native),
                self.assertRaises(core.SpacesError),
            ):
                auth.validate_native_bundle("x86_64")


class AuthenticationProtocolTests(unittest.TestCase):
    def test_protocol_round_trip_and_payload_bound(self) -> None:
        first, second = socket.socketpair()
        self.addCleanup(first.close)
        self.addCleanup(second.close)

        auth.send_frame(first, auth.AUTHENTICATE, b"metadata")
        self.assertEqual(
            auth.recv_frame(second),
            (auth.AUTHENTICATE, b"metadata"),
        )
        with self.assertRaises(ValueError):
            auth.send_frame(first, auth.AUTHENTICATE, b"x" * 20000)


class AuthenticationServiceTests(unittest.TestCase):
    def _service(
        self,
        directory: Path,
        users: dict[int, bool] | None = None,
    ) -> auth.AuthenticationService:
        runtime = auth.AuthenticationRuntime(
            directory=directory,
            socket_path=directory / "auth.sock",
        )
        service = auth.AuthenticationService(
            "work",
            runtime,
            users if users is not None else {1000: True},
        )
        self.addCleanup(service.stop)
        return service

    def test_configured_uid_authenticates_without_session_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self._service(Path(temporary))
            connection, peer = socket.socketpair()
            self.addCleanup(connection.close)
            self.addCleanup(peer.close)
            payload = json.dumps({"uid": 1000}).encode()

            with mock.patch.object(service, "_run_pam") as run:
                service._authenticate(connection, payload)

            run.assert_called_once_with(connection, 1000)

    def test_unconfigured_uid_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self._service(Path(temporary))
            connection, peer = socket.socketpair()
            self.addCleanup(connection.close)
            self.addCleanup(peer.close)
            payload = json.dumps({"uid": 1001}).encode()

            with (
                mock.patch.object(service, "_run_pam") as run,
                self.assertRaises(PermissionError),
            ):
                service._authenticate(connection, payload)

            run.assert_not_called()

    def test_rate_limit_is_scoped_to_the_requested_uid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self._service(
                Path(temporary),
                {1000: True, 1001: True},
            )

            for _attempt in range(auth.RATE_LIMIT_ATTEMPTS):
                service._consume_attempt(1000)
            with self.assertRaises(PermissionError):
                service._consume_attempt(1000)
            service._consume_attempt(1001)

    def test_handler_rejects_process_outside_the_space(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self._service(Path(temporary))
            server, client = socket.socketpair()
            self.addCleanup(client.close)
            auth.send_frame(
                client,
                auth.AUTHENTICATE,
                b'{"uid":1000}',
            )
            self.assertTrue(service._request_slots.acquire(blocking=False))

            with (
                mock.patch.object(
                    service, "_peer_in_space", return_value=False
                ),
                mock.patch.object(service, "_run_pam") as run,
            ):
                service._handle(server)

            message_type, _payload = auth.recv_frame(client)
            self.assertEqual(message_type, auth.ERROR)
            run.assert_not_called()

    def test_pam_worker_receives_a_blocking_conversation_socket(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            service = self._service(directory)
            connection, peer = socket.socketpair()
            self.addCleanup(connection.close)
            self.addCleanup(peer.close)
            connection.settimeout(auth.AUTHENTICATION_TIMEOUT)
            self.assertFalse(os.get_blocking(connection.fileno()))
            worker = directory / "spaces-pam-worker"
            worker.touch()

            def run_worker(
                arguments: list[str],
                **kwargs: object,
            ) -> int:
                self.assertTrue(os.get_blocking(connection.fileno()))
                self.assertIn(str(connection.fileno()), arguments)
                self.assertEqual(
                    kwargs["pass_fds"], (connection.fileno(),)
                )
                return 0

            with (
                mock.patch.object(auth, "PAM_WORKER", worker),
                mock.patch.object(
                    auth.pwd,
                    "getpwuid",
                    return_value=mock.Mock(pw_name="dev"),
                ),
                mock.patch.object(
                    service,
                    "_run_worker",
                    side_effect=run_worker,
                ),
            ):
                service._run_pam(connection, 1000)

            self.assertIsNone(connection.gettimeout())


if __name__ == "__main__":
    unittest.main()
