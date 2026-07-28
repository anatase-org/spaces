from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spaces import core, session


class SessionManifestTests(unittest.TestCase):
    def test_discovery_requires_a_session_endpoint_and_uses_allowlist(
        self,
    ) -> None:
        self.assertIsNone(
            session.discover(
                {
                    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/host/bus",
                    "SSH_AUTH_SOCK": "/run/user/1000/agent",
                    "XDG_CURRENT_DESKTOP": "KDE",
                }
            )
        )

        runtime = f"/run/user/{session.os.getuid()}"
        with mock.patch.object(
            session,
            "_is_socket",
            side_effect=lambda path: path.name == "wayland-0",
        ):
            manifest = session.discover(
                {
                    "XDG_RUNTIME_DIR": runtime,
                    "WAYLAND_DISPLAY": "wayland-0",
                    "XAUTHORITY": "/run/user/1000/xauth",
                    "QT_SCALE_FACTOR": "1.5",
                    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/host/bus",
                    "SSH_AUTH_SOCK": "/run/user/1000/agent",
                }
            )
        self.assertEqual(
            manifest,
            {
                "version": 1,
                "resources": ["appearance", "wayland"],
                "environment": {
                    "QT_SCALE_FACTOR": "1.5",
                    "WAYLAND_DISPLAY": "wayland-0",
                },
            },
        )

    def test_invalid_values_are_omitted_during_discovery(self) -> None:
        self.assertIsNone(
            session.discover(
                {
                    "DISPLAY": "bad\nvalue",
                    "WAYLAND_DISPLAY": "\0",
                }
            )
        )

    def test_discovery_detects_fixed_audio_sockets_without_a_display(
        self,
    ) -> None:
        runtime = f"/run/user/{session.os.getuid()}"

        with mock.patch.object(
            session,
            "_is_socket",
            side_effect=lambda path: path.name in {
                "native",
                "pipewire-0",
            },
        ):
            manifest = session.discover(
                {
                    "XDG_RUNTIME_DIR": runtime,
                    "TERM": "xterm-256color",
                }
            )

        self.assertEqual(
            manifest,
            {
                "version": 1,
                "resources": [
                    "appearance",
                    "pipewire",
                    "pulseaudio",
                ],
                "environment": {"TERM": "xterm-256color"},
            },
        )

    def test_validation_rejects_unknown_keys_and_host_bus(self) -> None:
        invalid = (
            {
                "version": 1,
                "resources": ["appearance", "x11"],
                "environment": {"DISPLAY": ":0"},
                "mounts": ["/etc/shadow"],
            },
            {
                "version": 1,
                "resources": ["appearance", "x11"],
                "environment": {
                    "DISPLAY": ":0",
                    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/host/bus",
                },
            },
            {
                "version": 2,
                "resources": ["appearance", "x11"],
                "environment": {"DISPLAY": ":0"},
            },
            {
                "version": 1,
                "resources": ["appearance", "devices", "x11"],
                "environment": {"DISPLAY": ":0"},
            },
            {
                "version": 1,
                "resources": ["appearance", "x11", "x11"],
                "environment": {"DISPLAY": ":0"},
            },
            {
                "version": 1,
                "resources": ["appearance", "wayland"],
                "environment": {"DISPLAY": ":0"},
            },
        )
        for manifest in invalid:
            with self.subTest(manifest=manifest), self.assertRaises(
                core.SpacesError
            ):
                session.validate_manifest(manifest)

    def test_validation_copies_valid_manifest(self) -> None:
        manifest = {
            "version": 1,
            "resources": ["appearance", "x11"],
            "environment": {
                "DISPLAY": "localhost:10.0",
                "TERM": "xterm-256color",
            },
        }
        self.assertEqual(session.validate_manifest(manifest), manifest)
        self.assertIsNot(
            session.validate_manifest(manifest)["environment"],
            manifest["environment"],
        )


class SessionPrivilegedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state_root = Path(self.temporary.name) / "spaces"
        self.state_patch = mock.patch.object(
            core,
            "STATE_ROOT",
            self.state_root,
        )
        self.state_patch.start()

    def tearDown(self) -> None:
        self.state_patch.stop()
        self.temporary.cleanup()

    def test_session_unit_uses_private_mounts_and_guest_bus(self) -> None:
        binding = session._SessionBind(
            source=Path("/host/wayland-0"),
            staging="/run/spaces-staging/token/wayland",
            destination="/tmp/.spaces-session-token/wayland/wayland-0",
            device=1,
            inode=2,
        )
        plan = session._SessionPlan(
            binds=(binding,),
            environment={
                "WAYLAND_DISPLAY": (
                    "/tmp/.spaces-session-token/wayland/wayland-0"
                ),
                "DBUS_SESSION_BUS_ADDRESS": (
                    "unix:path=/run/user/1000/bus"
                ),
            },
            staging_root="/run/spaces-staging/token",
            destination_root="/tmp/.spaces-session-token",
        )
        account = mock.Mock(pw_name="alice")
        command = session._session_unit_command(
            "work",
            account,
            plan,
            "spaces-enter-token.service",
            ["/usr/bin/kate", "--new"],
        )

        self.assertIn("--property=PrivateMounts=yes", command)
        self.assertNotIn("--property=PAMName=login", command)
        self.assertIn("--property=ExitType=cgroup", command)
        self.assertIn("--property=KillMode=control-group", command)
        self.assertIn("--expand-environment=no", command)
        self.assertIn("--uid=alice", command)
        self.assertIn("--working-directory=/home/alice", command)
        self.assertIn("--pty", command)
        self.assertIn("--pipe", command)
        self.assertIn(
            "--property=BindReadOnlyPaths="
            "/run/spaces-staging/token/wayland:"
            "/tmp/.spaces-session-token/wayland/wayland-0",
            command,
        )
        self.assertIn(
            "--setenv=DBUS_SESSION_BUS_ADDRESS="
            "unix:path=/run/user/1000/bus",
            command,
        )
        self.assertEqual(command[-3], "--")
        self.assertEqual(command[-2:], ["/usr/bin/kate", "--new"])

    def test_session_unit_uses_guest_login_shell_by_default(self) -> None:
        passwd = self.state_root / "work" / "rootfs" / "etc" / "passwd"
        passwd.parent.mkdir(parents=True)
        passwd.write_text(
            "alice:x:1000:1000::/home/alice:/bin/bash\n",
            encoding="utf-8",
        )
        plan = session._SessionPlan(
            binds=(),
            environment={},
            staging_root="/run/spaces-staging/token",
            destination_root="/tmp/.spaces-session-token",
        )
        account = mock.Mock(pw_name="alice")

        command = session._session_unit_command(
            "work",
            account,
            plan,
            "spaces-enter-token.service",
            [],
        )

        self.assertEqual(command[-3:], ["--", "/bin/bash", "-l"])

    def test_session_staging_uses_machinectl_lazy_unmount(self) -> None:
        with mock.patch.object(session, "_machine_root_command") as run:
            session._unmount_session_resource(
                "work",
                "/run/spaces-staging/token/wayland",
            )

        run.assert_called_once_with(
            "work",
            [
                "/usr/bin/umount",
                "--lazy",
                "--",
                "/run/spaces-staging/token/wayland",
            ],
        )

    def test_session_starts_guest_user_manager_for_bus(self) -> None:
        account = mock.Mock(pw_uid=1000)
        with mock.patch.object(session.subprocess, "run") as run:
            session._ensure_guest_user_manager("work", account)

        run.assert_called_once_with(
            [
                "/usr/bin/systemctl",
                "--machine=work",
                "--no-ask-password",
                "start",
                "user@1000.service",
            ],
            check=True,
        )

    def test_validated_source_rejects_escape_type_owner_and_access(
        self,
    ) -> None:
        root = Path(self.temporary.name) / "allowed"
        root.mkdir()
        readable = root / "readable"
        readable.write_text("data", encoding="utf-8")
        outside = Path(self.temporary.name) / "outside"
        outside.write_text("data", encoding="utf-8")
        escaped = root / "escaped"
        escaped.symlink_to(outside)
        account = mock.Mock(
            pw_uid=os.getuid(),
            pw_gid=os.getgid(),
            pw_name="alice",
        )

        with mock.patch.object(
            session.os,
            "getgrouplist",
            return_value=[os.getgid()],
        ):
            checked = session._validated_source(
                readable,
                roots=(root,),
                kinds=(stat.S_IFREG,),
                owner=os.getuid(),
                access_user=account,
            )
            self.assertIsNotNone(checked)

            for path, kinds, owner in (
                (escaped, (stat.S_IFREG,), os.getuid()),
                (Path("/etc/not-a-session-resource"), (stat.S_IFREG,), None),
                (readable, (stat.S_IFSOCK,), os.getuid()),
                (readable, (stat.S_IFREG,), os.getuid() + 1),
            ):
                with self.subTest(path=path), self.assertRaises(
                    core.SpacesError
                ):
                    session._validated_source(
                        path,
                        roots=(root,),
                        kinds=kinds,
                        owner=owner,
                        access_user=account,
                    )

            readable.chmod(0)
            with self.assertRaises(core.SpacesError):
                session._validated_source(
                    readable,
                    roots=(root,),
                    kinds=(stat.S_IFREG,),
                    owner=os.getuid(),
                    access_user=account,
                )

    def test_session_plan_rejects_forged_xdg_directory(self) -> None:
        account = mock.Mock(
            pw_uid=os.getuid(),
            pw_gid=os.getgid(),
            pw_name="alice",
            pw_dir=self.temporary.name,
        )
        manifest = {
            "version": 1,
            "resources": ["appearance", "x11"],
            "environment": {
                "DISPLAY": "localhost:10.0",
                "XDG_CONFIG_HOME": "/etc",
            },
        }

        with self.assertRaises(core.SpacesError):
            session._prepare_session_plan(
                manifest,
                account,
                "token",
                Path(self.temporary.name),
            )

    def test_environment_only_session_still_uses_transient_unit(self) -> None:
        plan = session._SessionPlan(
            binds=(),
            environment={"DISPLAY": "localhost:10.0"},
            staging_root="/run/spaces-staging/token",
            destination_root="/tmp/.spaces-session-token",
        )
        account = mock.Mock(pw_name="alice")
        process = mock.Mock()
        process.poll.return_value = 42
        process.wait.return_value = 42
        temporary_context = mock.MagicMock()
        temporary_context.__enter__.return_value = self.temporary.name
        removed = mock.Mock()
        with (
            mock.patch.object(
                session.tempfile,
                "TemporaryDirectory",
                return_value=temporary_context,
            ),
            mock.patch.object(
                session,
                "_prepare_session_plan",
                return_value=plan,
            ),
            mock.patch.object(session, "_ensure_guest_user_manager"),
            mock.patch.object(
                session,
                "_prepare_guest_session_directories",
            ),
            mock.patch.object(
                session,
                "_session_unit_command",
                return_value=["systemd-run"],
            ),
            mock.patch.object(
                session.subprocess,
                "Popen",
                return_value=process,
            ),
            mock.patch.object(
                session,
                "_wait_for_private_unit",
                return_value=True,
            ),
            mock.patch.object(
                session,
                "_remove_guest_session_paths",
                removed,
            ),
            mock.patch.object(session, "_stop_session_unit") as stop,
            mock.patch.object(
                session.signal,
                "signal",
                return_value=None,
            ),
        ):
            self.assertEqual(
                session.enter(
                    account,
                    "work",
                    ["/usr/bin/kate"],
                    {
                        "version": 1,
                        "resources": ["appearance", "x11"],
                        "environment": {"DISPLAY": "localhost:10.0"},
                    },
                ),
                42,
            )

        stop.assert_called_once()
        self.assertEqual(
            removed.call_args_list,
            [
                mock.call("work", plan.staging_root),
                mock.call(
                    "work",
                    plan.staging_root,
                    plan.destination_root,
                ),
            ],
        )

    def test_session_signals_forward_after_unit_start(self) -> None:
        state = session._SessionSignalState()
        process = mock.Mock()
        process.poll.return_value = None
        state.process = process
        handlers: dict[int, object] = {}

        def set_handler(signum: int, handler: object) -> None:
            handlers[signum] = handler

        with mock.patch.object(
            session.signal,
            "signal",
            side_effect=set_handler,
        ):
            with session._forward_session_signals(state):
                handler = handlers[session.signal.SIGTERM]
                assert callable(handler)
                handler(session.signal.SIGTERM, None)

        self.assertEqual(state.signum, session.signal.SIGTERM)
        process.send_signal.assert_called_once_with(session.signal.SIGTERM)

    def test_session_signal_before_unit_start_interrupts_setup(self) -> None:
        state = session._SessionSignalState()
        handlers: dict[int, object] = {}

        def set_handler(signum: int, handler: object) -> None:
            handlers[signum] = handler

        with (
            mock.patch.object(
                session.signal,
                "signal",
                side_effect=set_handler,
            ),
            self.assertRaises(session._SessionInterrupted),
        ):
            with session._forward_session_signals(state):
                handler = handlers[session.signal.SIGHUP]
                assert callable(handler)
                handler(session.signal.SIGHUP, None)

    def test_session_destination_remains_until_unit_stops(self) -> None:
        plan = session._SessionPlan(
            binds=(),
            environment={"DISPLAY": "localhost:10.0"},
            staging_root="/run/spaces-staging/token",
            destination_root="/tmp/.spaces-session-token",
        )
        account = mock.Mock(pw_name="alice")
        process = mock.Mock()
        process.poll.return_value = 0
        process.wait.return_value = 0
        temporary_context = mock.MagicMock()
        temporary_context.__enter__.return_value = self.temporary.name
        removed = mock.Mock()
        with (
            mock.patch.object(
                session.tempfile,
                "TemporaryDirectory",
                return_value=temporary_context,
            ),
            mock.patch.object(
                session,
                "_prepare_session_plan",
                return_value=plan,
            ),
            mock.patch.object(session, "_ensure_guest_user_manager"),
            mock.patch.object(
                session,
                "_prepare_guest_session_directories",
            ),
            mock.patch.object(
                session,
                "_session_unit_command",
                return_value=["systemd-run"],
            ),
            mock.patch.object(
                session.subprocess,
                "Popen",
                return_value=process,
            ),
            mock.patch.object(
                session,
                "_wait_for_private_unit",
                return_value=False,
            ),
            mock.patch.object(
                session,
                "_remove_guest_session_paths",
                removed,
            ),
            mock.patch.object(session, "_stop_session_unit") as stop,
            mock.patch.object(
                session.signal,
                "signal",
                return_value=None,
            ),
        ):
            self.assertEqual(
                session.enter(
                    account,
                    "work",
                    [],
                    {
                        "version": 1,
                        "resources": ["appearance", "x11"],
                        "environment": {"DISPLAY": "localhost:10.0"},
                    },
                ),
                0,
            )

        stop.assert_called_once()
        removed.assert_called_once_with(
            "work",
            plan.staging_root,
            plan.destination_root,
        )
