from __future__ import annotations

import json
import os
import pwd
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from spaces import core
from spaces import launch as launch_module


class LaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state_root = Path(self.temporary.name) / "spaces"
        self.space = self.state_root / "work"
        self.rootfs = self.space / "rootfs"
        self.rootfs.mkdir(parents=True)
        self.home = self.space / "home"
        self.home.mkdir()
        self.root_home = self.home / "root"
        self.host_home = Path(self.temporary.name) / "host-home"
        self.host_home.mkdir()
        etc = self.rootfs / "etc"
        etc.mkdir()
        (etc / "passwd").write_text(
            "root:x:0:0:root:/root:/bin/sh\n",
            encoding="utf-8",
        )
        (etc / "group").write_text("root:x:0:\n", encoding="utf-8")
        (etc / "shadow").write_text(
            "root:!:20000:0:99999:7:::\n",
            encoding="utf-8",
        )
        (etc / "gshadow").write_text("root:!::\n", encoding="utf-8")
        (etc / "skel").mkdir()
        self.identity = core.Identity(1000, 1000, Path("/home/user"))
        self.state_root_patch = mock.patch.object(
            core, "STATE_ROOT", self.state_root
        )
        self.state_root_patch.start()
        host_user = pwd.struct_passwd(
            (
                "user",
                "x",
                1000,
                1000,
                "Test User",
                str(self.host_home),
                "/bin/sh",
            )
        )
        self.passwd_patch = mock.patch.object(
            launch_module.pwd,
            "getpwuid",
            return_value=host_user,
        )
        self.passwd_patch.start()
        self.monitor = mock.Mock()
        self.monitor.state.return_value = "offline"
        self.monitor_patch = mock.patch.object(
            launch_module,
            "_LoginMonitor",
            return_value=self.monitor,
        )
        self.monitor_patch.start()
        self.worker = mock.Mock()
        self.worker_patch = mock.patch.object(
            launch_module,
            "_MountWorker",
            return_value=self.worker,
        )
        self.worker_class = self.worker_patch.start()

    def tearDown(self) -> None:
        self.worker_patch.stop()
        self.monitor_patch.stop()
        self.passwd_patch.stop()
        self.state_root_patch.stop()
        self.temporary.cleanup()

    def _write_info(
        self,
        network: str = "basic",
        name: str = "work",
        home: list[str] | None = None,
        host_authentication: bool = False,
    ) -> None:
        info = core.create_info(
            name,
            {"id": "custom"},
            self.identity,
            network,
            home or [],
            host_authentication=host_authentication,
        )
        (self.space / "info.json").write_text(
            json.dumps(info),
            encoding="utf-8",
        )

    def test_network_permissions_build_expected_launch(self) -> None:
        kept_caps = [
            "CAP_CHOWN",
            "CAP_DAC_OVERRIDE",
            "CAP_DAC_READ_SEARCH",
            "CAP_FOWNER",
            "CAP_FSETID",
            "CAP_IPC_OWNER",
            "CAP_KILL",
            "CAP_LEASE",
            "CAP_LINUX_IMMUTABLE",
            "CAP_MKNOD",
            "CAP_SETFCAP",
            "CAP_SETGID",
            "CAP_SETPCAP",
            "CAP_SETUID",
            "CAP_SYS_ADMIN",
            "CAP_SYS_BOOT",
            "CAP_SYS_CHROOT",
            "CAP_SYS_NICE",
            "CAP_SYS_RESOURCE",
        ]
        dropped_caps = [
            "CAP_AUDIT_CONTROL",
            "CAP_AUDIT_WRITE",
            "CAP_NET_BIND_SERVICE",
            "CAP_NET_BROADCAST",
            "CAP_NET_RAW",
            "CAP_SYS_PTRACE",
            "CAP_SYS_TTY_CONFIG",
        ]
        network_caps = {
            "basic": [],
            "advanced": ["CAP_NET_BIND_SERVICE"],
            "admin": [
                "CAP_NET_BIND_SERVICE",
                "CAP_NET_RAW",
                "CAP_NET_ADMIN",
            ],
        }
        common = [
            "/usr/bin/systemd-nspawn",
            "--quiet",
            f"--directory={self.rootfs}",
            "--machine=work",
            f"--bind={self.home}:/home",
            f"--bind={self.root_home}:/root",
            "--boot",
            "--setenv=SYSTEMD_GETTY_AUTO=no",
            "--console=read-only",
            "--private-users=no",
            "--keep-unit",
            "--settings=no",
            "--notify-ready=yes",
            "--resolv-conf=bind-host",
        ]

        for network, added_caps in network_caps.items():
            with self.subTest(network=network):
                self._write_info(network)
                caller_thread = threading.current_thread()
                called_thread: threading.Thread | None = None

                process = mock.Mock()
                process.wait.return_value = 42

                def run(*args: object, **kwargs: object) -> mock.Mock:
                    nonlocal called_thread
                    called_thread = threading.current_thread()
                    var_home = self.rootfs / "var" / "home"
                    self.assertTrue(var_home.is_symlink())
                    self.assertEqual(os.readlink(var_home), "/home")
                    return process

                with (
                    mock.patch.object(
                        launch_module,
                        "configure_logging",
                    ) as configure_logging,
                    mock.patch.dict(
                        os.environ,
                        {
                            "PRESERVED": "yes",
                            launch_module.API_VFS_WRITABLE: "yes",
                        },
                        clear=True,
                    ),
                    mock.patch.object(
                        launch_module.subprocess,
                        "Popen",
                        side_effect=run,
                    ) as run_mock,
                    mock.patch.object(launch_module.signal, "signal"),
                ):
                    self.assertEqual(launch_module.launch("work"), 42)

                self.assertIs(called_thread, caller_thread)
                configure_logging.assert_called_once_with(rich=False)
                self.assertTrue(self.root_home.is_dir())
                self.assertEqual(self.root_home.stat().st_mode & 0o777, 0o700)
                run_mock.assert_called_once()
                arguments, = run_mock.call_args.args
                environment = run_mock.call_args.kwargs["env"]
                expected_kept = [*kept_caps, *added_caps]
                expected_dropped = [
                    capability
                    for capability in dropped_caps
                    if capability not in expected_kept
                ]
                self.assertEqual(
                    arguments,
                    [
                        *common,
                        f"--drop-capability={','.join(expected_dropped)}",
                        f"--capability={','.join(expected_kept)}",
                    ],
                )
                self.assertEqual(environment["PRESERVED"], "yes")
                if network == "admin":
                    self.assertEqual(
                        environment[launch_module.API_VFS_WRITABLE],
                        "network",
                    )
                else:
                    self.assertNotIn(
                        launch_module.API_VFS_WRITABLE,
                        environment,
                    )

    def test_enabled_authentication_starts_service_and_adds_exact_binds(
        self,
    ) -> None:
        info = core.create_info(
            "work",
            {"id": "ubuntu", "version": "resolute"},
            self.identity,
            "basic",
            [],
            host_authentication=True,
        )
        del info["permissions"]["system"]["host_authentication"]
        (self.space / "info.json").write_text(
            json.dumps(info), encoding="utf-8"
        )
        pam_directory = self.rootfs / "etc" / "pam.d"
        pam_directory.mkdir()
        (pam_directory / "common-auth").write_text(
            "auth required pam_unix.so\n", encoding="utf-8"
        )
        (pam_directory / "common-session").write_text(
            "session optional pam_systemd.so\n", encoding="utf-8"
        )
        runtime = mock.Mock()
        runtime.bind_arguments = (
            "--bind-ro=/run/spaces/work/authentication/auth.sock:"
            "/run/spaces-host/auth.sock",
            "--bind-ro=/usr/lib/spaces/guest:/run/spaces-host/bin",
            "--bind-ro=/run/spaces/work/authentication/common-auth:"
            "/etc/pam.d/common-auth",
            "--bind-ro=/run/spaces/work/authentication/common-session:"
            "/etc/pam.d/common-session",
        )
        authentication = mock.Mock()
        process = mock.Mock()
        process.wait.return_value = 0

        with (
            mock.patch.object(
                launch_module.auth,
                "prepare_runtime",
                return_value=runtime,
            ) as prepare,
            mock.patch.object(
                launch_module.auth,
                "AuthenticationService",
                return_value=authentication,
            ) as service,
            mock.patch.object(
                launch_module.subprocess, "Popen", return_value=process
            ) as popen,
            mock.patch.object(launch_module.signal, "signal"),
        ):
            self.assertEqual(launch_module.launch("work"), 0)

        prepare.assert_called_once_with(
            "work",
            self.rootfs,
            "/etc/pam.d/common-auth",
            "/etc/pam.d/common-session",
        )
        service.assert_called_once_with("work", runtime, {1000: True})
        authentication.start.assert_called_once_with()
        authentication.stop.assert_called_once_with()
        command = popen.call_args.args[0]
        for bind in runtime.bind_arguments:
            self.assertIn(bind, command)

    def test_disabled_authentication_has_no_runtime_binds(self) -> None:
        self._write_info(host_authentication=False)
        process = mock.Mock()
        process.wait.return_value = 0
        with (
            mock.patch.object(
                launch_module.subprocess, "Popen", return_value=process
            ) as popen,
            mock.patch.object(
                launch_module.auth, "prepare_runtime"
            ) as prepare,
            mock.patch.object(launch_module.signal, "signal"),
        ):
            self.assertEqual(launch_module.launch("work"), 0)

        prepare.assert_not_called()
        self.assertFalse(
            any(
                "spaces-host" in argument
                for argument in popen.call_args.args[0]
            )
        )

    def test_enabled_unknown_policy_is_rejected(self) -> None:
        self._write_info(host_authentication=True)

        with self.assertRaises(core.SpacesError):
            launch_module.launch("work")

    def test_launch_forwards_sigterm_and_waits(self) -> None:
        self._write_info()
        process = mock.Mock()
        process.wait.return_value = 42

        with (
            mock.patch.object(
                launch_module.subprocess,
                "Popen",
                return_value=process,
            ),
            mock.patch.object(launch_module.signal, "signal") as set_handler,
        ):
            self.assertEqual(launch_module.launch("work"), 42)

        set_handler.assert_called_once()
        signum, handler = set_handler.call_args.args
        self.assertEqual(signum, launch_module.signal.SIGTERM)
        self.assertTrue(callable(handler))
        handler(launch_module.signal.SIGTERM, None)
        handler(launch_module.signal.SIGTERM, None)
        self.assertEqual(
            process.send_signal.call_args_list,
            [
                mock.call(launch_module.signal.SIGTERM),
                mock.call(launch_module.signal.SIGTERM),
            ],
        )
        process.wait.assert_called_once_with()

    def test_initial_user_mounts_are_given_to_nspawn_and_worker(self) -> None:
        (self.host_home / "Projects").mkdir()
        self._write_info(home=["Projects"])
        self.monitor.state.return_value = "lingering"
        process = mock.Mock()
        process.wait.return_value = 0

        with (
            mock.patch.object(
                launch_module.subprocess,
                "Popen",
                return_value=process,
            ) as run,
            mock.patch.object(launch_module.signal, "signal"),
        ):
            self.assertEqual(launch_module.launch("work"), 0)

        arguments = run.call_args.args[0]
        bind = next(
            argument
            for argument in arguments
            if argument.endswith(":/home/user/Projects")
        )
        self.assertEqual(
            bind,
            f"--bind={self.host_home}/Projects:/home/user/Projects",
        )
        mounts = self.worker_class.call_args.args[4]
        self.assertEqual(len(mounts), 1)
        self.assertEqual(mounts[0].destination, "/home/user/Projects")
        self.assertEqual(
            self.worker_class.call_args.args[5],
            frozenset({1000}),
        )
        self.worker.start.assert_called()
        self.worker.attach.assert_called_once_with(process)
        self.worker.stop.assert_called()
        self.worker.join.assert_called()
        self.monitor.close.assert_called()

    def test_rootfs_fixup_logs_conflict_and_launches(self) -> None:
        self._write_info()
        var_home = self.rootfs / "var" / "home"
        var_home.mkdir(parents=True)
        process = mock.Mock()
        process.wait.return_value = 42

        with (
            mock.patch.object(
                launch_module.subprocess,
                "Popen",
                return_value=process,
            ) as run,
            mock.patch.object(launch_module.signal, "signal"),
            self.assertLogs(launch_module.logger, level="ERROR") as logs,
        ):
            result = launch_module.launch("work")

        self.assertEqual(result, 42)
        run.assert_called_once()
        self.assertTrue(var_home.is_dir())
        self.assertIn("the path exists and is not a symlink", logs.output[0])

    def test_rootfs_fixup_logs_os_error_and_launches(self) -> None:
        self._write_info()
        process = mock.Mock()
        process.wait.return_value = 0

        with (
            mock.patch.object(
                Path,
                "symlink_to",
                side_effect=PermissionError("not permitted"),
            ),
            mock.patch.object(
                launch_module.subprocess,
                "Popen",
                return_value=process,
            ) as run,
            mock.patch.object(launch_module.signal, "signal"),
            self.assertLogs(launch_module.logger, level="ERROR") as logs,
        ):
            result = launch_module.launch("work")

        self.assertEqual(result, 0)
        run.assert_called_once()
        self.assertIn("not permitted", logs.output[0])

    def test_rootfs_fixup_replaces_an_incorrect_symlink(self) -> None:
        self._write_info()
        var_home = self.rootfs / "var" / "home"
        var_home.parent.mkdir()
        var_home.symlink_to("/srv/home", target_is_directory=True)
        process = mock.Mock()
        process.wait.return_value = 0

        with (
            mock.patch.object(
                launch_module.subprocess,
                "Popen",
                return_value=process,
            ),
            mock.patch.object(launch_module.signal, "signal"),
        ):
            launch_module.launch("work")

        self.assertTrue(var_home.is_symlink())
        self.assertEqual(os.readlink(var_home), "/home")

    def test_rootfs_fixups_apply_each_configured_symlink(self) -> None:
        symlinks = [
            ("/var/home", "/home"),
            ("/srv/spaces/data", "/data"),
        ]

        with mock.patch.object(launch_module, "SYMLINKS", symlinks):
            launch_module._apply_rootfs_fixups(self.rootfs)

        for link_name, target in symlinks:
            link = self.rootfs / link_name.removeprefix("/")
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link), target)

    def test_rootfs_fixups_mask_netplan_configure(self) -> None:
        launch_module._apply_rootfs_fixups(self.rootfs)

        mask = self.rootfs / "etc" / "systemd" / "system" / "netplan-configure.service"
        self.assertTrue(mask.is_symlink())
        self.assertEqual(os.readlink(mask), "/dev/null")

    def test_rootfs_fixups_log_unexpected_error_and_continue(self) -> None:
        symlinks = [
            ("relative/path", "/invalid"),
            ("/var/home", "/home"),
        ]

        with (
            mock.patch.object(launch_module, "SYMLINKS", symlinks),
            self.assertLogs(launch_module.logger, level="ERROR") as logs,
        ):
            launch_module._apply_rootfs_fixups(self.rootfs)

        var_home = self.rootfs / "var" / "home"
        self.assertTrue(var_home.is_symlink())
        self.assertEqual(os.readlink(var_home), "/home")
        self.assertIn("relative/path", logs.output[0])

    def test_invalid_name_is_rejected_before_state_access(self) -> None:
        with (
            mock.patch.object(launch_module.subprocess, "Popen") as run,
            self.assertRaises(core.SpacesError),
        ):
            launch_module.launch("../work")
        run.assert_not_called()

    def test_missing_space_is_rejected(self) -> None:
        self.space.rename(self.state_root / "other")
        with (
            mock.patch.object(launch_module.subprocess, "Popen") as run,
            self.assertRaises(core.SpacesError),
        ):
            launch_module.launch("work")
        run.assert_not_called()

    def test_symlinked_space_is_rejected(self) -> None:
        target = self.state_root / "target"
        self.space.rename(target)
        self.space.symlink_to(target, target_is_directory=True)
        with self.assertRaises(core.SpacesError):
            launch_module.launch("work")

    def test_missing_or_symlinked_rootfs_is_rejected(self) -> None:
        self._write_info()
        shutil.rmtree(self.rootfs)
        with self.assertRaises(core.SpacesError):
            launch_module.launch("work")

        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        self.rootfs.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(core.SpacesError):
            launch_module.launch("work")

    def test_missing_or_symlinked_home_is_rejected(self) -> None:
        self.home.rmdir()
        with self.assertRaises(core.SpacesError):
            launch_module.launch("work")

        outside = Path(self.temporary.name) / "home"
        outside.mkdir()
        self.home.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(core.SpacesError):
            launch_module.launch("work")

    def test_symlinked_root_home_is_rejected(self) -> None:
        outside = Path(self.temporary.name) / "root"
        outside.mkdir()
        self.root_home.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(core.SpacesError):
            launch_module.launch("work")

    def test_missing_invalid_or_symlinked_info_is_rejected(self) -> None:
        with self.assertRaises(core.SpacesError):
            launch_module.launch("work")

        info_path = self.space / "info.json"
        info_path.write_text("{", encoding="utf-8")
        with self.assertRaises(core.SpacesError):
            launch_module.launch("work")

        info_path.unlink()
        outside = Path(self.temporary.name) / "info.json"
        outside.write_text("{}", encoding="utf-8")
        info_path.symlink_to(outside)
        with self.assertRaises(core.SpacesError):
            launch_module.launch("work")

    def test_metadata_name_must_match_space(self) -> None:
        self._write_info(name="other")
        with (
            mock.patch.object(launch_module.subprocess, "Popen") as run,
            self.assertRaises(core.SpacesError),
        ):
            launch_module.launch("work")
        run.assert_not_called()


class UserFixupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.rootfs = self.root / "rootfs"
        self.etc = self.rootfs / "etc"
        self.etc.mkdir(parents=True)
        self.space_home = self.root / "home"
        self.space_home.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _user(
        self,
        *,
        uid: int | None = None,
        gid: int | None = None,
        name: str = "alice",
        permitted: tuple[str, ...] = (),
        administrator: bool = True,
    ) -> launch_module.SpaceUser:
        fixed_uid = os.getuid() if uid is None else uid
        fixed_gid = os.getgid() if gid is None else gid
        return launch_module.SpaceUser(
            uid=fixed_uid,
            gid=fixed_gid,
            name=name,
            host_home=self.root / "host" / name,
            space_home=self.space_home / name,
            guest_home=launch_module.PurePosixPath(f"/home/{name}"),
            permitted_home=permitted,
            administrator=administrator,
        )

    def _write_accounts(
        self,
        *,
        passwd_text: str,
        group_text: str,
        shadow_text: str = "",
        gshadow_text: str = "",
    ) -> None:
        (self.etc / "passwd").write_text(passwd_text, encoding="utf-8")
        (self.etc / "group").write_text(group_text, encoding="utf-8")
        if shadow_text:
            (self.etc / "shadow").write_text(shadow_text, encoding="utf-8")
        if gshadow_text:
            (self.etc / "gshadow").write_text(gshadow_text, encoding="utf-8")

    def test_reconcile_renames_and_locks_existing_uid(self) -> None:
        uid = os.getuid()
        gid = os.getgid()
        self._write_accounts(
            passwd_text=f"root:x:0:0::/root:/bin/sh\nold:x:{uid}:55::/old:/bin/zsh\n",
            group_text=f"root:x:0:\nstaff:x:{gid}:old\n",
            shadow_text="root:!:::::::\nold:$6$hash:::::::\n",
            gshadow_text="root:!::\nstaff:!::old\n",
        )
        user = self._user(name="alice")

        launch_module._reconcile_accounts(self.rootfs, (user,))

        passwd_records = launch_module._read_database(self.etc / "passwd", 7)
        account = next(record for record in passwd_records if record[0] == "alice")
        self.assertEqual(
            account,
            [
                "alice",
                "x",
                str(uid),
                str(gid),
                "",
                "/home/alice",
                "/bin/sh",
            ],
        )
        shadow = launch_module._read_database(self.etc / "shadow", 9)
        shadow_account = next(
            record for record in shadow if record[0] == "alice"
        )
        self.assertEqual(shadow_account[1], "!")
        group = launch_module._read_database(self.etc / "group", 4)
        group_account = next(record for record in group if record[0] == "staff")
        self.assertEqual(group_account[3], "alice")
        gshadow = launch_module._read_database(self.etc / "gshadow", 4)
        gshadow_account = next(
            record for record in gshadow if record[0] == "staff"
        )
        self.assertEqual(gshadow_account[3], "alice")

    def test_reconcile_creates_account_and_primary_group(self) -> None:
        self._write_accounts(
            passwd_text="root:!:0:0::/root:/bin/sh\n",
            group_text="root:x:0:\n",
        )
        user = self._user(uid=12345, gid=12346)

        launch_module._reconcile_accounts(self.rootfs, (user,))

        passwd_records = launch_module._read_database(self.etc / "passwd", 7)
        self.assertIn(
            ["alice", "!", "12345", "12346", "", "/home/alice", "/bin/sh"],
            passwd_records,
        )
        self.assertIn(
            ["alice", "x", "12346", ""],
            launch_module._read_database(self.etc / "group", 4),
        )

    def test_reconcile_adds_administrator_to_wheel_group(self) -> None:
        self._write_accounts(
            passwd_text=(
                "root:x:0:0::/root:/bin/sh\n"
                "alice:x:12345:12346::/home/alice:/bin/sh\n"
            ),
            group_text="root:x:0:\nwheel:x:10:bob\nalice:x:12346:\n",
            shadow_text="root:!:::::::\nalice:!:::::::\n",
            gshadow_text="root:!::\nwheel:!::bob\nalice:!::\n",
        )

        launch_module._reconcile_accounts(
            self.rootfs,
            (self._user(uid=12345, gid=12346, administrator=True),),
        )

        group = launch_module._read_database(self.etc / "group", 4)
        wheel = next(record for record in group if record[0] == "wheel")
        self.assertEqual(wheel[3], "bob,alice")
        gshadow = launch_module._read_database(self.etc / "gshadow", 4)
        wheel_shadow = next(record for record in gshadow if record[0] == "wheel")
        self.assertEqual(wheel_shadow[3], "bob,alice")

    def test_reconcile_uses_distribution_administrator_group(self) -> None:
        self._write_accounts(
            passwd_text=(
                "root:x:0:0::/root:/bin/sh\n"
                "alice:x:12345:12346::/home/alice:/bin/sh\n"
            ),
            group_text="root:x:0:\nsudo:x:27:\nalice:x:12346:\n",
            shadow_text="root:!:::::::\nalice:!:::::::\n",
            gshadow_text="root:!::\nsudo:!::\nalice:!::\n",
        )

        launch_module._reconcile_accounts(
            self.rootfs,
            (self._user(uid=12345, gid=12346, administrator=True),),
            "sudo",
        )

        group = launch_module._read_database(self.etc / "group", 4)
        sudo = next(record for record in group if record[0] == "sudo")
        self.assertEqual(sudo[3], "alice")
        self.assertNotIn("wheel", {record[0] for record in group})
        gshadow = launch_module._read_database(self.etc / "gshadow", 4)
        sudo_shadow = next(record for record in gshadow if record[0] == "sudo")
        self.assertEqual(sudo_shadow[3], "alice")

    def test_reconcile_creates_missing_administrator_group(self) -> None:
        self._write_accounts(
            passwd_text="root:x:0:0::/root:/bin/sh\n",
            group_text="root:x:0:\nreserved:x:999:\n",
            shadow_text="root:!:::::::\n",
            gshadow_text="root:!::\nreserved:!::\n",
        )

        launch_module._reconcile_accounts(
            self.rootfs,
            (self._user(uid=12345, gid=998, administrator=True),),
            "sudo",
        )

        group = launch_module._read_database(self.etc / "group", 4)
        sudo = next(record for record in group if record[0] == "sudo")
        self.assertEqual(sudo, ["sudo", "x", "997", "alice"])
        gshadow = launch_module._read_database(self.etc / "gshadow", 4)
        sudo_shadow = next(record for record in gshadow if record[0] == "sudo")
        self.assertEqual(sudo_shadow, ["sudo", "!", "", "alice"])

    def test_reconcile_removes_non_administrator_from_wheel_group(self) -> None:
        self._write_accounts(
            passwd_text=(
                "root:x:0:0::/root:/bin/sh\n"
                "alice:x:12345:12346::/home/alice:/bin/sh\n"
            ),
            group_text="root:x:0:\nwheel:x:10:bob,alice\nalice:x:12346:\n",
            shadow_text="root:!:::::::\nalice:!:::::::\n",
            gshadow_text="root:!::\nwheel:!::bob,alice\nalice:!::\n",
        )

        launch_module._reconcile_accounts(
            self.rootfs,
            (self._user(uid=12345, gid=12346, administrator=False),),
        )

        group = launch_module._read_database(self.etc / "group", 4)
        wheel = next(record for record in group if record[0] == "wheel")
        self.assertEqual(wheel[3], "bob")
        gshadow = launch_module._read_database(self.etc / "gshadow", 4)
        wheel_shadow = next(record for record in gshadow if record[0] == "wheel")
        self.assertEqual(wheel_shadow[3], "bob")

    def test_reconcile_uses_bash_when_available(self) -> None:
        binary = self.rootfs / "bin" / "bash"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o755)
        self._write_accounts(
            passwd_text=(
                "root:x:0:0::/root:/bin/sh\n"
                "alice:x:12345:12346::/old:/bin/sh\n"
            ),
            group_text="root:x:0:\nalice:x:12346:\n",
        )
        user = self._user(uid=12345, gid=12346)

        launch_module._reconcile_accounts(self.rootfs, (user,))

        passwd_records = launch_module._read_database(
            self.etc / "passwd",
            7,
        )
        account = next(record for record in passwd_records if record[0] == "alice")
        self.assertEqual(account[6], "/bin/bash")

    def test_reconcile_rejects_name_and_uid_on_different_accounts(self) -> None:
        self._write_accounts(
            passwd_text=(
                "root:x:0:0::/root:/bin/sh\n"
                "alice:x:2001:2001::/home/alice:/bin/sh\n"
                "other:x:2000:2000::/home/other:/bin/sh\n"
            ),
            group_text="root:x:0:\nalice:x:2001:\nother:x:2000:\n",
        )
        user = self._user(uid=2000, gid=2000)

        with self.assertRaises(core.SpacesError):
            launch_module._reconcile_accounts(self.rootfs, (user,))

    def test_reconcile_rejects_symlinked_account_database(self) -> None:
        outside = self.root / "passwd"
        outside.write_text("root:x:0:0::/root:/bin/sh\n", encoding="utf-8")
        (self.etc / "passwd").symlink_to(outside)
        (self.etc / "group").write_text("root:x:0:\n", encoding="utf-8")
        with self.assertRaises(core.SpacesError):
            launch_module._reconcile_accounts(self.rootfs, (self._user(),))

    def test_root_user_does_not_require_account_database_changes(self) -> None:
        root = launch_module.SpaceUser(
            uid=0,
            gid=0,
            name="root",
            host_home=Path("/root"),
            space_home=self.space_home / "root",
            guest_home=launch_module.PurePosixPath("/root"),
            permitted_home=(),
        )
        launch_module._reconcile_accounts(self.rootfs, (root,))

    def test_new_home_copies_skeleton_once(self) -> None:
        skeleton = self.etc / "skel"
        nested = skeleton / ".config"
        nested.mkdir(parents=True)
        profile = skeleton / ".profile"
        profile.write_text("profile\n", encoding="utf-8")
        profile.chmod(0o640)
        (nested / "settings").write_text("first\n", encoding="utf-8")
        (skeleton / "config-link").symlink_to(".config")
        user = self._user()

        launch_module._ensure_user_homes(self.rootfs, (user,))

        self.assertEqual((user.space_home / ".profile").read_text(), "profile\n")
        self.assertEqual((user.space_home / ".profile").stat().st_mode & 0o777, 0o640)
        self.assertTrue((user.space_home / "config-link").is_symlink())
        self.assertEqual(os.readlink(user.space_home / "config-link"), ".config")
        self.assertEqual(user.space_home.stat().st_mode & 0o777, 0o700)

        (nested / "settings").write_text("second\n", encoding="utf-8")
        launch_module._ensure_user_homes(self.rootfs, (user,))
        self.assertEqual(
            (user.space_home / ".config" / "settings").read_text(),
            "first\n",
        )

    def test_missing_skeleton_creates_empty_home(self) -> None:
        user = self._user()
        launch_module._ensure_user_homes(self.rootfs, (user,))
        self.assertEqual(list(user.space_home.iterdir()), [])

    def test_symlinked_skeleton_is_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (self.etc / "skel").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(core.SpacesError):
            launch_module._ensure_user_homes(self.rootfs, (self._user(),))

    def test_failed_skeleton_copy_leaves_no_home(self) -> None:
        skeleton = self.etc / "skel"
        skeleton.mkdir()
        (skeleton / "file").write_text("data", encoding="utf-8")
        user = self._user()
        with (
            mock.patch.object(
                launch_module.shutil,
                "copy2",
                side_effect=OSError("copy failed"),
            ),
            self.assertRaises(core.SpacesError),
        ):
            launch_module._ensure_user_homes(self.rootfs, (user,))
        self.assertFalse(user.space_home.exists())
        self.assertEqual(list(self.space_home.iterdir()), [])

    def test_mount_plan_and_nspawn_escaping(self) -> None:
        user = self._user(permitted=("Project:One", "linked"))
        user.host_home.mkdir(parents=True)
        (user.host_home / "Project:One").mkdir()
        (user.host_home / "linked").symlink_to(
            user.host_home / "Project:One",
            target_is_directory=True,
        )
        user.space_home.mkdir()

        with self.assertLogs(launch_module.logger, level="WARNING"):
            available = launch_module._prepare_mounts((user,))
        mounts = launch_module._plan_mounts(
            available,
            frozenset({user.uid}),
        )
        self.assertEqual(len(mounts), 1)
        self.assertEqual(mounts[0].destination, "/home/alice/Project:One")
        self.assertEqual(
            mounts[0].source,
            (user.host_home / "Project:One").resolve(),
        )
        self.assertEqual(
            launch_module._bind_argument(mounts[0]),
            (
                f"--bind={user.host_home}/Project\\:One:"
                "/home/alice/Project\\:One"
            ),
        )
        self.assertTrue((user.space_home / "Project:One").is_dir())
        self.assertEqual(
            launch_module._plan_mounts(available, frozenset()),
            (),
        )

    def test_mount_destination_preparation_failure_is_skipped(self) -> None:
        user = self._user(permitted=("Projects",))
        user.host_home.mkdir(parents=True)
        (user.host_home / "Projects").mkdir()
        user.space_home.mkdir()
        original_close = os.close

        with (
            mock.patch.object(
                launch_module.os,
                "chown",
                side_effect=PermissionError("not permitted"),
            ),
            mock.patch.object(
                launch_module.os,
                "close",
                wraps=original_close,
            ) as close,
            self.assertLogs(launch_module.logger, level="WARNING"),
        ):
            available = launch_module._prepare_mounts((user,))

        self.assertEqual(available, ())
        close.assert_called_once()
        self.assertFalse((user.space_home / "Projects").exists())


class LoginAndMountWorkerTests(unittest.TestCase):
    def _user(self, uid: int) -> launch_module.SpaceUser:
        return launch_module.SpaceUser(
            uid=uid,
            gid=uid,
            name=f"user{uid}",
            host_home=Path(f"/home/user{uid}"),
            space_home=Path(f"/space/home/user{uid}"),
            guest_home=launch_module.PurePosixPath(f"/home/user{uid}"),
            permitted_home=(),
        )

    def test_eligible_states_include_lingering(self) -> None:
        users = tuple(self._user(uid) for uid in range(1000, 1005))
        monitor = mock.Mock()
        monitor.state.side_effect = [
            "active",
            "online",
            "lingering",
            "closing",
            "offline",
        ]
        self.assertEqual(
            launch_module._eligible_uids(monitor, users),
            frozenset({1000, 1001, 1002}),
        )

    def test_unknown_login_state_is_rejected(self) -> None:
        monitor = mock.Mock()
        monitor.state.return_value = "future-state"
        with self.assertRaises(core.SpacesError):
            launch_module._eligible_uids(monitor, (self._user(1000),))

    def test_lingering_keeps_mount_until_user_is_closing(self) -> None:
        user = self._user(1000)
        mount = launch_module.HomeMount(
            destination="/home/user1000/Projects",
            source=Path("/host/Projects"),
            uid=1000,
        )
        monitor = mock.Mock()
        monitor.state.return_value = "lingering"
        worker = launch_module._MountWorker(
            "work",
            (user,),
            monitor,
            (mount,),
            (mount,),
            frozenset({user.uid}),
        )
        worker._registered = True
        worker._process = mock.Mock()

        with (
            mock.patch.object(worker, "_remove") as remove,
            self.assertLogs(launch_module.logger, level="INFO"),
        ):
            worker._reconcile()
            remove.assert_not_called()
            monitor.state.return_value = "closing"
            worker._reconcile()
            remove.assert_called_once_with(mount)

    def test_login_and_logout_log_user_and_mounts(self) -> None:
        user = self._user(1000)
        mount = launch_module.HomeMount(
            destination="/home/user1000/Projects",
            source=Path("/host/Projects"),
            uid=1000,
        )
        monitor = mock.Mock()
        monitor.state.return_value = "offline"
        worker = launch_module._MountWorker(
            "work",
            (user,),
            monitor,
            (mount,),
            (),
            frozenset(),
        )
        worker._registered = True
        worker._process = mock.Mock()

        with (
            mock.patch.object(worker, "_add"),
            mock.patch.object(worker, "_remove"),
            self.assertLogs(
                launch_module.logger,
                level="INFO",
            ) as logs,
        ):
            monitor.state.return_value = "active"
            worker._reconcile()
            monitor.state.return_value = "offline"
            worker._reconcile()

        self.assertEqual(
            logs.output,
            [
                (
                    "INFO:spaces.launch:User user1000 (1000:1000) logged in; "
                    "mounted: /host/Projects -> /home/user1000/Projects."
                ),
                (
                    "INFO:spaces.launch:User user1000 (1000:1000) logged out; "
                    "unmounted: /host/Projects -> /home/user1000/Projects."
                ),
            ],
        )

    def test_initial_user_mounts_are_logged_after_registration(self) -> None:
        user = self._user(1000)
        mount = launch_module.HomeMount(
            destination="/home/user1000/Projects",
            source=Path("/host/Projects"),
            uid=1000,
        )
        monitor = mock.Mock()
        monitor.state.return_value = "active"
        monitor.wait.return_value = False
        worker = launch_module._MountWorker(
            "work",
            (user,),
            monitor,
            (mount,),
            (mount,),
            frozenset({user.uid}),
        )
        worker._registered = True
        process = mock.Mock()
        process.poll.return_value = None
        worker.attach(process)

        with self.assertLogs(
            launch_module.logger,
            level="INFO",
        ) as logs:
            worker._run()

        self.assertEqual(
            logs.output,
            [
                (
                    "INFO:spaces.launch:User user1000 (1000:1000) mounted at "
                    "space launch: /host/Projects -> "
                    "/home/user1000/Projects."
                )
            ],
        )

    def test_failed_runtime_mount_is_skipped(self) -> None:
        user = self._user(1000)
        failed_mount = launch_module.HomeMount(
            destination="/home/user1000/Documents",
            source=Path("/host/Documents"),
            uid=1000,
        )
        mounted = launch_module.HomeMount(
            destination="/home/user1000/Projects",
            source=Path("/host/Projects"),
            uid=1000,
        )
        monitor = mock.Mock()
        monitor.state.return_value = "active"
        worker = launch_module._MountWorker(
            "work",
            (user,),
            monitor,
            (failed_mount, mounted),
            (),
            frozenset(),
        )
        worker._registered = True
        worker._process = mock.Mock()

        with (
            mock.patch.object(
                worker,
                "_add",
                side_effect=[
                    launch_module.subprocess.CalledProcessError(
                        1,
                        ["machinectl", "bind"],
                    ),
                    None,
                ],
            ) as add,
            self.assertLogs(launch_module.logger, level="WARNING"),
        ):
            worker._reconcile()

        self.assertEqual(
            add.call_args_list,
            [mock.call(failed_mount), mock.call(mounted)],
        )
        self.assertEqual(worker._mounted, {mounted})

    def test_remove_uses_systemd_lazy_unmount(self) -> None:
        monitor = mock.Mock()
        worker = launch_module._MountWorker(
            "work",
            (),
            monitor,
            (),
            (),
            frozenset(),
        )
        mount = launch_module.HomeMount(
            destination="/home/alice/Projects",
            source=Path("/host/Projects"),
            uid=1000,
        )
        completed = SimpleNamespace(stdout="home-alice-Projects.mount\n")
        with mock.patch.object(
            launch_module.subprocess,
            "run",
            side_effect=[completed, mock.DEFAULT, mock.DEFAULT],
        ) as run:
            worker._remove(mount)

        self.assertEqual(
            run.call_args_list,
            [
                mock.call(
                    [
                        launch_module.SYSTEMD_ESCAPE,
                        "--path",
                        "--suffix=mount",
                        "/home/alice/Projects",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ),
                mock.call(
                    [
                        launch_module.SYSTEMCTL,
                        "--machine=work",
                        "--no-ask-password",
                        "set-property",
                        "--runtime",
                        "home-alice-Projects.mount",
                        "LazyUnmount=yes",
                    ],
                    check=True,
                ),
                mock.call(
                    [
                        launch_module.SYSTEMD_UMOUNT,
                        "--machine=work",
                        "--no-ask-password",
                        "--quiet",
                        "/home/alice/Projects",
                    ],
                    check=True,
                ),
            ],
        )

    def test_add_uses_machinectl_bind(self) -> None:
        worker = launch_module._MountWorker(
            "work",
            (),
            mock.Mock(),
            (),
            (),
            frozenset(),
        )
        mount = launch_module.HomeMount(
            destination="/home/alice/Projects",
            source=Path("/host/Projects"),
            uid=1000,
        )
        with mock.patch.object(launch_module.subprocess, "run") as run:
            worker._add(mount)
        run.assert_called_once_with(
            [
                launch_module.MACHINECTL,
                "--quiet",
                "--no-ask-password",
                "--mkdir",
                "bind",
                "work",
                "/host/Projects",
                "/home/alice/Projects",
            ],
            check=True,
        )


if __name__ == "__main__":
    unittest.main()
