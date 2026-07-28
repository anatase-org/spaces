from __future__ import annotations

import array
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from spaces import auth, core
from spaces.auth import protocol as auth_protocol
from spaces.auth import runtime as auth_runtime
from spaces.auth import service as auth_service
from spaces.auth import session as auth_session


class AuthenticationPolicyTests(unittest.TestCase):
    @staticmethod
    def _elf(machine: int = 62) -> bytes:
        value = bytearray(64)
        value[:6] = b"\x7fELF\x02\x01"
        value[18:20] = machine.to_bytes(2, "little")
        return bytes(value)

    def test_generated_policy_replaces_only_auth_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "system-auth"
            destination = root / "generated"
            source.write_text(
                "# distro policy\n"
                "auth required pam_unix.so\n"
                "-auth optional pam_faillock.so\n"
                "account required pam_unix.so\n"
                "password required pam_unix.so\n"
                "session required pam_limits.so\n",
                encoding="utf-8",
            )

            auth.generate_pam_policy(source, destination)

            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                "# distro policy\n"
                "auth required /run/spaces-host/bin/pam_spaces.so\n"
                "account required pam_unix.so\n"
                "password required pam_unix.so\n"
                "session required pam_limits.so\n",
            )
            self.assertEqual(destination.stat().st_mode & 0o777, 0o400)

    def test_generated_session_policy_appends_hook_after_distro_modules(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "common-session"
            destination = root / "generated"
            source.write_text(
                "session required pam_unix.so\n"
                "session optional pam_systemd.so\n",
                encoding="utf-8",
            )

            auth.generate_pam_session_policy(source, destination)

            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                "session required pam_unix.so\n"
                "session optional pam_systemd.so\n"
                "session optional /run/spaces-host/bin/pam_spaces.so\n",
            )

    def test_combined_policy_places_session_hook_after_preserved_records(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "system-auth"
            destination = root / "generated"
            source.write_text(
                "auth required pam_unix.so\n"
                "account required pam_unix.so\n"
                "session optional pam_systemd.so\n",
                encoding="utf-8",
            )

            auth.generate_pam_policy(
                source,
                destination,
                session_hook=True,
            )

            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                "auth required /run/spaces-host/bin/pam_spaces.so\n"
                "account required pam_unix.so\n"
                "session optional pam_systemd.so\n"
                "session optional /run/spaces-host/bin/pam_spaces.so\n",
            )

    def test_safe_policy_rejects_symlink_and_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rootfs = Path(temporary) / "rootfs"
            rootfs.mkdir()
            outside = Path(temporary) / "outside"
            outside.write_text("auth required pam_unix.so\n", encoding="utf-8")
            (rootfs / "policy").symlink_to(outside)

            with self.assertRaises(core.SpacesError):
                auth_runtime._safe_policy(rootfs, "/policy")
            with self.assertRaises(core.SpacesError):
                auth_runtime._safe_policy(rootfs, "/../outside")

    def test_runtime_bind_destinations_are_fixed_and_read_only(self) -> None:
        runtime = auth.AuthenticationRuntime(
            directory=Path("/run/spaces/work/authentication"),
            socket_path=Path("/run/spaces/work/authentication/auth.sock"),
            policy_binds=(
                (
                    Path(
                        "/run/spaces/work/authentication/"
                        "authentication-common-auth"
                    ),
                    "/etc/pam.d/common-auth",
                ),
                (
                    Path(
                        "/run/spaces/work/authentication/"
                        "session-common-session"
                    ),
                    "/etc/pam.d/common-session",
                ),
            ),
        )
        self.assertEqual(
            runtime.bind_arguments,
            (
                "--bind-ro=/run/spaces/work/authentication/auth.sock:"
                "/run/spaces-host/auth.sock",
                "--bind-ro=/usr/lib/spaces/guest:/run/spaces-host/bin",
                "--bind-ro=/run/spaces/work/authentication/"
                "authentication-common-auth:"
                "/etc/pam.d/common-auth",
                "--bind-ro=/run/spaces/work/authentication/"
                "session-common-session:/etc/pam.d/common-session",
            ),
        )

    def test_prepare_runtime_does_not_modify_the_rootfs_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rootfs = root / "rootfs"
            pam_directory = rootfs / "etc" / "pam.d"
            pam_directory.mkdir(parents=True)
            original = "auth required pam_unix.so\naccount required pam_unix.so\n"
            policy = pam_directory / "common-auth"
            policy.write_text(original, encoding="utf-8")
            session_original = (
                "session required pam_unix.so\n"
                "session optional pam_systemd.so\n"
            )
            session_policy = pam_directory / "common-session"
            session_policy.write_text(session_original, encoding="utf-8")
            guest_bin = rootfs / "usr" / "bin"
            guest_bin.mkdir(parents=True)
            (guest_bin / "env").write_bytes(self._elf())
            native = root / "native"
            native.mkdir()
            for name in auth.GUEST_BINARIES:
                (native / name).write_bytes(self._elf())

            with (
                mock.patch.object(auth_runtime, "NATIVE_ROOT", native),
                mock.patch.object(
                    auth_runtime, "RUNTIME_ROOT", root / "runtime"
                ),
                mock.patch.object(
                    auth_runtime.platform,
                    "machine",
                    return_value="x86_64",
                ),
                mock.patch.object(auth_runtime.os, "chown"),
            ):
                runtime = auth.prepare_runtime(
                    "work",
                    rootfs,
                    "/etc/pam.d/common-auth",
                    "/etc/pam.d/common-session",
                )

            self.assertEqual(policy.read_text(encoding="utf-8"), original)
            self.assertEqual(
                session_policy.read_text(encoding="utf-8"),
                session_original,
            )
            authentication_policy = runtime.policy_binds[0][0]
            generated_session_policy = runtime.policy_binds[1][0]
            self.assertEqual(
                authentication_policy.read_text(encoding="utf-8"),
                "auth required /run/spaces-host/bin/pam_spaces.so\n"
                "account required pam_unix.so\n",
            )
            self.assertEqual(
                generated_session_policy.read_text(encoding="utf-8"),
                session_original
                + "session optional "
                "/run/spaces-host/bin/pam_spaces.so\n",
            )

    def test_bundle_rejects_wrong_architecture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            native = Path(temporary)
            for name in auth.GUEST_BINARIES:
                (native / name).write_bytes(
                    self._elf(machine=183)
                )
            with (
                mock.patch.object(auth_runtime, "NATIVE_ROOT", native),
                self.assertRaises(core.SpacesError),
            ):
                auth.validate_native_bundle("x86_64")


class AuthenticationProtocolTests(unittest.TestCase):
    def test_user_manager_process_uses_verified_session_hint(self) -> None:
        with (
            mock.patch.object(
                auth_session,
                "session_for_pid",
                side_effect=core.SpacesError("not in a session scope"),
            ),
            mock.patch.dict(
                os.environ, {"XDG_SESSION_ID": "2"}, clear=True
            ),
        ):
            self.assertEqual(auth.client_session(12), "2")

        with (
            mock.patch.object(auth_session, "session_uid", return_value=1000),
            mock.patch.object(
                auth_session,
                "session_for_pid",
                side_effect=core.SpacesError("not in a session scope"),
            ),
            mock.patch.object(
                auth_session.Path,
                "read_bytes",
                return_value=b"PATH=/usr/bin\0XDG_SESSION_ID=2\0",
            ),
            mock.patch.object(
                auth_session.Path,
                "read_text",
                return_value=(
                    "0::/user.slice/user-1000.slice/"
                    "user@1000.service/app.slice/terminal.scope\n"
                ),
            ),
        ):
            self.assertTrue(auth.process_session_matches(12, "2", 1000))

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

    def test_protocol_transfers_one_cloexec_descriptor(self) -> None:
        first, second = socket.socketpair()
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        descriptor = os.pidfd_open(os.getpid())
        self.addCleanup(os.close, descriptor)
        payload = b'{"uid":1000,"service":"polkit-1"}'
        frame = auth_protocol.HEADER.pack(
            auth_protocol.MAGIC,
            auth_protocol.VERSION,
            auth.AUTHENTICATE,
            len(payload),
        ) + payload
        rights = array.array("i", [descriptor])

        first.sendmsg(
            [frame],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)],
        )
        message_type, received, descriptors = (
            auth_protocol.recv_frame_with_descriptors(second)
        )
        self.addCleanup(os.close, descriptors[0])

        self.assertEqual(message_type, auth.AUTHENTICATE)
        self.assertEqual(received, payload)
        self.assertEqual(len(descriptors), 1)
        self.assertFalse(os.get_inheritable(descriptors[0]))

    def test_lease_exists_only_while_registration_connection_is_live(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            runtime = auth.AuthenticationRuntime(
                directory=directory,
                socket_path=directory / "auth.sock",
                policy_binds=(
                    (directory / "common-auth", "/etc/pam.d/common-auth"),
                ),
            )
            uid = os.getuid()
            subject = auth.LeaseSubject(
                pid=os.getpid(),
                start_time=123,
                uid=uid,
                gid=os.getgid(),
                session_id="session-1",
            )
            service = auth.AuthenticationService(
                "work", runtime, {uid: True}
            )
            self.addCleanup(service.stop)
            server, client = socket.socketpair()
            self.addCleanup(client.close)
            thread = threading.Thread(
                target=service._register,
                args=(server, subject.payload(), 0),
            )
            with (
                mock.patch.object(
                    auth_service, "process_start_time", return_value=123
                ),
                mock.patch.object(
                    auth_service,
                    "process_session_matches",
                    return_value=True,
                ),
            ):
                thread.start()
                message_type, token = auth.recv_frame(client)
                self.assertEqual(message_type, auth.TOKEN)
                token_text = token.decode()
                self.assertIn(token_text, service._leases)
                service._leases[token_text].guest_sessions.add("c1")
                client.close()
                thread.join(timeout=5)
                server.close()

            self.assertFalse(thread.is_alive())
            self.assertEqual(service._leases, {})

    def test_polkit_worker_receives_only_fixed_subject_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            runtime = auth.AuthenticationRuntime(
                directory=directory,
                socket_path=directory / "auth.sock",
                policy_binds=(
                    (directory / "common-auth", "/etc/pam.d/common-auth"),
                ),
            )
            service = auth.AuthenticationService(
                "work", runtime, {1000: True}
            )
            self.addCleanup(service.stop)
            connection, peer = socket.socketpair()
            self.addCleanup(connection.close)
            self.addCleanup(peer.close)
            subject = auth.LeaseSubject(12, 34, 1000, 1000, "c1")
            worker = directory / "spaces-polkit-worker"
            worker.touch()

            with (
                mock.patch.object(auth_service, "POLKIT_WORKER", worker),
                mock.patch.object(
                    auth_service,
                    "session_process",
                    return_value=(12, 34),
                ),
                mock.patch.object(
                    service, "_run_worker", return_value=0
                ) as run,
            ):
                service._run_polkit(connection, subject)

            arguments = run.call_args.args[0]
            self.assertEqual(
                arguments[1:],
                [
                    "--uid",
                    "1000",
                    "--gid",
                    "1000",
                    "--pid",
                    "12",
                    "--start",
                    "34",
                ],
            )
            self.assertNotIn("guest-action", json.dumps(arguments))
            self.assertEqual(auth.recv_frame(peer)[0], auth.RESULT)

    def test_polkit_helper_uses_lease_for_exact_guest_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            runtime = auth.AuthenticationRuntime(
                directory=directory,
                socket_path=directory / "auth.sock",
                policy_binds=(
                    (directory / "common-auth", "/etc/pam.d/common-auth"),
                ),
            )
            uid = os.getuid()
            subject = auth.LeaseSubject(
                os.getpid(),
                auth.process_start_time(os.getpid()),
                uid,
                os.getgid(),
                "host-session",
            )
            lease = auth.LiveLease(
                subject=subject,
                pidfd=os.pidfd_open(os.getpid()),
                created_at=auth_service.time.monotonic(),
                guest_sessions={"c7"},
            )
            service = auth.AuthenticationService(
                "work", runtime, {uid: True}
            )
            self.addCleanup(service.stop)
            service._leases["a" * 64] = lease
            connection, peer = socket.socketpair()
            self.addCleanup(connection.close)
            self.addCleanup(peer.close)
            payload = json.dumps(
                {"uid": uid, "service": "polkit-1"}
            ).encode()

            with (
                mock.patch.object(
                    service,
                    "_validate_direct_polkit_helper",
                    return_value=42,
                ),
                mock.patch.object(
                    auth_service,
                    "cgroup_components",
                    return_value={
                        "machine-work.scope",
                        "session-c7.scope",
                    },
                ),
                mock.patch.object(service, "_run_polkit") as run,
            ):
                service._authenticate(connection, payload, 42)

            run.assert_called_once_with(connection, subject)
            self.assertEqual(len(lease.attempts), 1)

    def test_only_polkit_may_authenticate_without_a_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            runtime = auth.AuthenticationRuntime(
                directory=directory,
                socket_path=directory / "auth.sock",
                policy_binds=(),
            )
            service = auth.AuthenticationService(
                "work", runtime, {os.getuid(): True}
            )
            self.addCleanup(service.stop)
            connection, peer = socket.socketpair()
            self.addCleanup(connection.close)
            self.addCleanup(peer.close)
            payload = json.dumps(
                {"uid": os.getuid(), "service": "sudo"}
            ).encode()

            with self.assertRaises(PermissionError):
                service._authenticate(connection, payload, 42)

    def test_socket_activated_polkit_helper_routes_with_agent_pidfd(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            runtime = auth.AuthenticationRuntime(
                directory=directory,
                socket_path=directory / "auth.sock",
                policy_binds=(),
            )
            uid = os.getuid()
            subject = auth.LeaseSubject(
                os.getpid(),
                auth.process_start_time(os.getpid()),
                uid,
                os.getgid(),
                "host-session",
            )
            lease = auth.LiveLease(
                subject=subject,
                pidfd=os.pidfd_open(os.getpid()),
                created_at=auth_service.time.monotonic(),
                guest_sessions={"c7"},
            )
            service = auth.AuthenticationService(
                "work", runtime, {uid: True}
            )
            self.addCleanup(service.stop)
            service._leases["a" * 64] = lease
            connection, peer = socket.socketpair()
            self.addCleanup(connection.close)
            self.addCleanup(peer.close)
            payload = json.dumps(
                {"uid": uid, "service": "polkit-1"}
            ).encode()

            with (
                mock.patch.object(
                    service,
                    "_process_from_agent_pidfd",
                    return_value=42,
                ) as process_from_pidfd,
                mock.patch.object(
                    auth_service,
                    "cgroup_components",
                    return_value={"session-c7.scope"},
                ),
                mock.patch.object(service, "_run_polkit") as run,
            ):
                service._authenticate(
                    connection,
                    payload,
                    99,
                    (123,),
                )

            process_from_pidfd.assert_called_once_with(123, uid)
            run.assert_called_once_with(connection, subject)

    def test_agent_pidfd_is_live_and_identifies_expected_executable(
        self,
    ) -> None:
        descriptor = os.pidfd_open(os.getpid())
        self.addCleanup(os.close, descriptor)

        with mock.patch.object(
            auth_service,
            "GUEST_POLKIT_AGENT",
            Path("/proc/self/exe"),
        ):
            self.assertEqual(
                auth_service.AuthenticationService._process_from_agent_pidfd(
                    descriptor,
                    os.getuid(),
                ),
                os.getpid(),
            )

    def test_direct_polkit_helper_requires_expected_binary_and_uids(
        self,
    ) -> None:
        uid = os.getuid()
        status = f"Name:\thelper\nUid:\t{uid}\t0\t0\t0\n"

        with (
            mock.patch.object(
                auth_service.Path,
                "readlink",
                return_value=Path(
                    "/usr/lib/polkit-1/polkit-agent-helper-1"
                ),
            ),
            mock.patch.object(
                auth_service.Path,
                "read_text",
                return_value=status,
            ),
        ):
            validate = (
                auth_service.AuthenticationService._validate_direct_polkit_helper
            )
            self.assertEqual(
                validate(42, uid),
                42,
            )

    def test_polkit_helper_without_a_registered_session_is_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            runtime = auth.AuthenticationRuntime(
                directory=directory,
                socket_path=directory / "auth.sock",
                policy_binds=(),
            )
            service = auth.AuthenticationService(
                "work", runtime, {os.getuid(): True}
            )
            self.addCleanup(service.stop)
            connection, peer = socket.socketpair()
            self.addCleanup(connection.close)
            self.addCleanup(peer.close)
            payload = json.dumps(
                {"uid": os.getuid(), "service": "polkit-1"}
            ).encode()

            with (
                mock.patch.object(
                    service,
                    "_validate_direct_polkit_helper",
                    return_value=42,
                ),
                mock.patch.object(
                    auth_service,
                    "cgroup_components",
                    return_value={"session-c9.scope"},
                ),
                self.assertRaises(PermissionError),
            ):
                service._authenticate(connection, payload, 42)

    def test_pam_worker_receives_a_blocking_conversation_socket(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            runtime = auth.AuthenticationRuntime(
                directory=directory,
                socket_path=directory / "auth.sock",
                policy_binds=(
                    (directory / "common-auth", "/etc/pam.d/common-auth"),
                ),
            )
            service = auth.AuthenticationService(
                "work", runtime, {1000: True}
            )
            self.addCleanup(service.stop)
            connection, peer = socket.socketpair()
            self.addCleanup(connection.close)
            self.addCleanup(peer.close)
            connection.settimeout(auth.AUTHENTICATION_TIMEOUT)
            self.assertFalse(os.get_blocking(connection.fileno()))
            worker = directory / "spaces-pam-worker"
            worker.touch()
            subject = auth.LeaseSubject(12, 34, 1000, 1000, "c1")

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
                mock.patch.object(auth_service, "PAM_WORKER", worker),
                mock.patch.object(
                    auth_service.pwd,
                    "getpwuid",
                    return_value=mock.Mock(pw_name="dev"),
                ),
                mock.patch.object(
                    service,
                    "_run_worker",
                    side_effect=run_worker,
                ),
            ):
                service._run_pam(connection, subject)

            self.assertIsNone(connection.gettimeout())

    def test_guest_session_event_records_and_removes_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            runtime = auth.AuthenticationRuntime(
                directory=directory,
                socket_path=directory / "auth.sock",
                policy_binds=(
                    (directory / "common-auth", "/etc/pam.d/common-auth"),
                ),
            )
            uid = os.getuid()
            subject = auth.LeaseSubject(
                os.getpid(),
                auth.process_start_time(os.getpid()),
                uid,
                os.getgid(),
                "host-session",
            )
            lease = auth.LiveLease(
                subject=subject,
                pidfd=os.pidfd_open(os.getpid()),
                attempts=[],
                created_at=auth_service.time.monotonic(),
                guest_sessions=set(),
            )
            service = auth.AuthenticationService(
                "work", runtime, {uid: True}
            )
            self.addCleanup(service.stop)
            token = "a" * 64
            service._leases[token] = lease
            payload = json.dumps(
                {"token": token, "uid": uid, "session_id": "c7"}
            ).encode()

            with mock.patch.object(
                service,
                "_peer_in_guest_session",
                return_value=True,
            ):
                service._session_event(payload, 12, opening=True)
                self.assertEqual(lease.guest_sessions, {"c7"})

                service._session_event(payload, 12, opening=False)
                self.assertEqual(lease.guest_sessions, set())


if __name__ == "__main__":
    unittest.main()
