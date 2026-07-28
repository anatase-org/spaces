from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
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
        self.identity = core.Identity(1000, 1000, Path("/home/user"))
        self.state_root_patch = mock.patch.object(
            core, "STATE_ROOT", self.state_root
        )
        self.state_root_patch.start()

    def tearDown(self) -> None:
        self.state_root_patch.stop()
        self.temporary.cleanup()

    def _write_info(self, network: str = "basic", name: str = "work") -> None:
        info = core.create_info(
            name,
            {"id": "custom"},
            self.identity,
            network,
            [],
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
        self.rootfs.rmdir()
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


if __name__ == "__main__":
    unittest.main()
