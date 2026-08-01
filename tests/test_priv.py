from __future__ import annotations

import ast
import io
import json
import os
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from spaces import core, host_config, priv, session, shortcuts
from spaces.distro import ubuntu


class PrivilegedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state_root = Path(self.temporary.name) / "spaces"
        self.identity = core.Identity(0, 0, Path("/root"))
        self.info = core.create_info(
            "ubuntu",
            {"id": "ubuntu", "version": "resolute"},
            self.identity,
            "basic",
            ["Projects"],
        )
        self.patches = [
            mock.patch.object(core, "STATE_ROOT", self.state_root),
            mock.patch.object(
                shortcuts,
                "APPLICATIONS_ROOT",
                Path(self.temporary.name) / "applications",
            ),
            mock.patch.object(priv.os, "chown"),
            mock.patch.object(priv.os, "fchown"),
            mock.patch.dict(os.environ, {}, clear=True),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temporary.cleanup()

    def test_create_writes_metadata_and_bootstraps(self) -> None:
        with (
            mock.patch.object(ubuntu.subprocess, "run") as run,
            mock.patch.object(ubuntu, "print") as print_output,
        ):
            priv.create(self.info)
        space = self.state_root / "ubuntu"
        self.assertTrue((space / "rootfs").is_dir())
        self.assertTrue((space / "home").is_dir())
        stored = json.loads((space / "info.json").read_text(encoding="utf-8"))
        self.assertEqual(stored, self.info)
        self.assertEqual(stat.S_IMODE(space.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((space / "home").stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((space / "info.json").stat().st_mode), 0o644)
        run.assert_has_calls(
            [
                mock.call(
                    [
                        "/usr/bin/systemctl",
                        "stop",
                        "spaces@ubuntu.service",
                    ],
                    check=True,
                ),
                mock.call(
                    [
                        "debootstrap",
                        "resolute",
                        str(space / "rootfs"),
                        "",
                        "gutsy",
                    ],
                    check=True,
                ),
                mock.call(
                    [
                        "mount",
                        "--types",
                        "proc",
                        "--options",
                        "nosuid,noexec,nodev",
                        "proc",
                        str(space / "rootfs" / "proc"),
                    ],
                    check=True,
                ),
                mock.call(
                    [
                        "mount",
                        "--types",
                        "sysfs",
                        "--options",
                        "ro,nosuid,noexec,nodev",
                        "sysfs",
                        str(space / "rootfs" / "sys"),
                    ],
                    check=True,
                ),
                mock.call(
                    ubuntu.chroot_command(
                        space / "rootfs",
                        "apt-get",
                        "update",
                    ),
                    check=True,
                ),
            ]
        )

        self.assertEqual(run.call_count, 8)
        self.assertEqual(
            run.call_args_list[-2],
            mock.call(
                ["umount", str(space / "rootfs" / "sys")],
                check=True,
            ),
        )
        self.assertEqual(
            run.call_args,
            mock.call(
                ["umount", str(space / "rootfs" / "proc")],
                check=True,
            ),
        )
        self.assertFalse(
            (
                space
                / "rootfs"
                / "usr"
                / "sbin"
                / "policy-rc.d"
            ).exists()
        )
        self.assertEqual(
            (
                space
                / "rootfs"
                / "etc"
                / "apt"
                / "sources.list.d"
                / "ubuntu.sources"
            ).read_text(encoding="utf-8"),
            "Types: deb\n"
            "URIs: http://archive.ubuntu.com/ubuntu\n"
            "Suites: resolute resolute-updates resolute-backports\n"
            "Components: main restricted universe multiverse\n"
            "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n"
            "\n"
            "Types: deb\n"
            "URIs: http://security.ubuntu.com/ubuntu\n"
            "Suites: resolute-security\n"
            "Components: main restricted universe multiverse\n"
            "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n",
        )
        self.assertEqual(
            (space / "rootfs" / "etc" / "apt" / "sources.list").read_text(
                encoding="utf-8"
            ),
            "# Ubuntu sources have moved to "
            "/etc/apt/sources.list.d/ubuntu.sources\n",
        )
        print_output.assert_any_call(
            "Bootstrapping Ubuntu Resolute (26.04)...",
            flush=True,
        )

    def test_create_passes_only_selected_distro_packages(self) -> None:
        driver = mock.Mock()
        configuration = host_config.HostConfig(
            version=1,
            distros={
                "arch": host_config.DistroConfig(packages=("screen",)),
                "ubuntu": host_config.DistroConfig(
                    packages=("tmux", "zsh")
                ),
            },
        )
        with (
            mock.patch.object(priv, "get_driver", return_value=driver),
            mock.patch.object(host_config, "load", return_value=configuration),
            mock.patch.object(priv.subprocess, "run"),
        ):
            priv.create(self.info)

        driver.bootstrap.assert_called_once_with(
            self.info["distribution"],
            self.state_root / "ubuntu" / "rootfs",
            additional_packages=("tmux", "zsh"),
        )

    def test_space_lock_owns_the_specific_space_directory(self) -> None:
        space = self.state_root / "work"
        targets: list[Path] = []

        def flock(descriptor: int, operation: int) -> None:
            if operation == priv.fcntl.LOCK_EX:
                targets.append(
                    Path(os.readlink(f"/proc/self/fd/{descriptor}"))
                )

        with mock.patch.object(priv.fcntl, "flock", side_effect=flock):
            with priv._space_lock(space, create=True):
                pass

        self.assertEqual(targets, [space])

    def test_different_space_locks_can_run_concurrently(self) -> None:
        release = threading.Event()
        entered = {
            "first": threading.Event(),
            "second": threading.Event(),
        }
        errors: list[BaseException] = []

        def hold(name: str) -> None:
            try:
                with priv._space_lock(
                    self.state_root / name,
                    create=True,
                ):
                    entered[name].set()
                    release.wait(2)
            except BaseException as error:
                errors.append(error)

        threads = [
            threading.Thread(target=hold, args=(name,))
            for name in ("first", "second")
        ]
        try:
            threads[0].start()
            self.assertTrue(entered["first"].wait(1))
            threads[1].start()
            self.assertTrue(entered["second"].wait(1))
        finally:
            release.set()
            for thread in threads:
                thread.join(2)

        self.assertEqual(errors, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))

    def test_same_space_locks_remain_serialized(self) -> None:
        space = self.state_root / "work"
        release = threading.Event()
        first_entered = threading.Event()
        second_started = threading.Event()
        second_entered = threading.Event()
        errors: list[BaseException] = []

        def first() -> None:
            try:
                with priv._space_lock(space, create=True):
                    first_entered.set()
                    release.wait(2)
            except BaseException as error:
                errors.append(error)

        def second() -> None:
            try:
                second_started.set()
                with priv._space_lock(space, create=True):
                    second_entered.set()
            except BaseException as error:
                errors.append(error)

        first_thread = threading.Thread(target=first)
        second_thread = threading.Thread(target=second)
        try:
            first_thread.start()
            self.assertTrue(first_entered.wait(1))
            second_thread.start()
            self.assertTrue(second_started.wait(1))
            self.assertFalse(second_entered.wait(0.1))
        finally:
            release.set()
            first_thread.join(2)
            second_thread.join(2)

        self.assertTrue(second_entered.is_set())
        self.assertEqual(errors, [])
        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())

    def test_create_bootstraps_different_spaces_concurrently(self) -> None:
        release = threading.Event()
        entered = {
            "first": threading.Event(),
            "second": threading.Event(),
        }
        errors: list[BaseException] = []
        driver = mock.Mock()

        def bootstrap(
            metadata: dict[str, object],
            rootfs: Path,
        ) -> None:
            entered[rootfs.parent.name].set()
            release.wait(2)

        driver.bootstrap.side_effect = bootstrap

        def create(name: str) -> None:
            try:
                priv.create(
                    core.create_info(
                        name,
                        {"id": "custom"},
                        self.identity,
                        "basic",
                        [],
                    )
                )
            except BaseException as error:
                errors.append(error)

        threads = [
            threading.Thread(target=create, args=(name,))
            for name in ("first", "second")
        ]
        with (
            mock.patch.object(priv, "get_driver", return_value=driver),
            mock.patch.object(priv.subprocess, "run"),
        ):
            try:
                threads[0].start()
                self.assertTrue(entered["first"].wait(1))
                threads[1].start()
                self.assertTrue(entered["second"].wait(1))
            finally:
                release.set()
                for thread in threads:
                    thread.join(2)

        self.assertEqual(errors, [])
        self.assertEqual(driver.bootstrap.call_count, 2)
        for name in ("first", "second"):
            self.assertTrue(
                (self.state_root / name / "info.json").is_file()
            )

    def test_custom_stops_service_without_bootstrapping(self) -> None:
        info = core.create_info(
            "work", {"id": "custom"}, self.identity, "basic", ["Projects"]
        )
        with mock.patch.object(ubuntu.subprocess, "run") as run:
            priv.create(info)
        run.assert_called_once_with(
            [
                "/usr/bin/systemctl",
                "stop",
                "spaces@work.service",
            ],
            check=True,
        )

    def test_create_enables_service_for_initiating_user(self) -> None:
        info = core.create_info(
            "work", {"id": "custom"}, self.identity, "basic", []
        )
        request = {**info, "enable": True}
        with (
            mock.patch.object(ubuntu.subprocess, "run"),
            mock.patch.object(priv, "_enable_user_service") as enable,
        ):
            priv.create(request)

        enable.assert_called_once_with("work", 0, 0)

    def test_enable_user_service_uses_target_user_manager(self) -> None:
        account = mock.Mock(
            pw_dir="/home/alice",
            pw_gid=1002,
            pw_name="alice",
        )
        with (
            mock.patch.object(priv.pwd, "getpwuid", return_value=account),
            mock.patch.object(priv.subprocess, "run") as run,
        ):
            priv._enable_user_service("work", 1001, 1002)

        run.assert_called_once_with(
            [
                "/usr/bin/systemctl",
                "--machine=alice@.host",
                "--user",
                "--no-reload",
                "reenable",
                "spaces@work.service",
            ],
            check=True,
        )

    def test_rebuild_removes_existing_shortcut_versions(self) -> None:
        info = core.create_info(
            "work", {"id": "custom"}, self.identity, "basic", ["Projects"]
        )
        with (
            mock.patch.object(ubuntu.subprocess, "run"),
            mock.patch.object(shortcuts, "remove") as remove,
        ):
            priv.create(info)
        remove.assert_called_once_with("work")

    def test_rebuild_removes_rootfs_and_preserves_home(self) -> None:
        with mock.patch.object(ubuntu.subprocess, "run"):
            priv.create(self.info)
        space = self.state_root / "ubuntu"
        (space / "rootfs" / "partial").write_text("remove", encoding="utf-8")
        (space / "rootfs.fail").mkdir()
        (space / "rootfs.fail" / "previous").write_text(
            "remove",
            encoding="utf-8",
        )
        (space / "home" / "keep").write_text("preserve", encoding="utf-8")
        with mock.patch.object(ubuntu.subprocess, "run"):
            priv.create(self.info)
        self.assertFalse((space / "rootfs" / "partial").exists())
        self.assertFalse((space / "rootfs.fail").exists())
        self.assertEqual(
            (space / "home" / "keep").read_text(encoding="utf-8"), "preserve"
        )

    def test_rebuild_with_purge_removes_home_contents(self) -> None:
        with mock.patch.object(ubuntu.subprocess, "run"):
            priv.create(self.info)
        space = self.state_root / "ubuntu"
        (space / "home" / "remove").write_text("remove", encoding="utf-8")
        request = {**self.info, "purge": True}

        with mock.patch.object(ubuntu.subprocess, "run"):
            priv.create(request)

        self.assertTrue((space / "home").is_dir())
        self.assertFalse((space / "home" / "remove").exists())
        stored = json.loads(
            (space / "info.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("purge", stored)

    def test_bootstrap_failure_moves_rootfs_to_failed_path(self) -> None:
        space = self.state_root / "ubuntu"
        (space / "rootfs.fail").mkdir(parents=True)
        (space / "rootfs.fail" / "previous").write_text(
            "remove",
            encoding="utf-8",
        )
        error = subprocess.CalledProcessError(42, ["debootstrap"])
        with (
            mock.patch.object(
                ubuntu.subprocess,
                "run",
                side_effect=[
                    subprocess.CompletedProcess(
                        [
                            "/usr/bin/systemctl",
                            "stop",
                            "spaces@ubuntu.service",
                        ],
                        0,
                    ),
                    error,
                ],
            ),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            priv.create(self.info)
        self.assertFalse((space / "rootfs").exists())
        self.assertTrue((space / "rootfs.fail").is_dir())
        self.assertFalse((space / "rootfs.fail" / "previous").exists())
        self.assertTrue((space / "home").is_dir())
        self.assertTrue((space / "info.json").is_file())

    def test_bootstrap_keyboard_interrupt_moves_rootfs_to_failed_path(
        self,
    ) -> None:
        space = self.state_root / "ubuntu"
        with (
            mock.patch.object(
                ubuntu.subprocess,
                "run",
                side_effect=[
                    subprocess.CompletedProcess(
                        [
                            "/usr/bin/systemctl",
                            "stop",
                            "spaces@ubuntu.service",
                        ],
                        0,
                    ),
                    KeyboardInterrupt,
                ],
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            priv.create(self.info)

        self.assertFalse((space / "rootfs").exists())
        self.assertTrue((space / "rootfs.fail").is_dir())
        self.assertTrue((space / "home").is_dir())
        self.assertTrue((space / "info.json").is_file())

    def test_create_stop_failure_preserves_existing_space(self) -> None:
        space = self.state_root / "ubuntu"
        rootfs = space / "rootfs"
        rootfs.mkdir(parents=True)
        existing = rootfs / "keep"
        existing.write_text("preserve", encoding="utf-8")
        error = subprocess.CalledProcessError(
            1,
            [
                "/usr/bin/systemctl",
                "stop",
                "spaces@ubuntu.service",
            ],
        )

        with (
            mock.patch.object(ubuntu.subprocess, "run", side_effect=error),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            priv.create(self.info)

        self.assertEqual(existing.read_text(encoding="utf-8"), "preserve")

    def test_keyboard_interrupt_returns_130(self) -> None:
        payload = json.dumps(self.info)
        with (
            mock.patch.object(priv.os, "geteuid", return_value=0),
            mock.patch.object(priv, "create", side_effect=KeyboardInterrupt),
            mock.patch.object(priv, "print") as print_output,
        ):
            self.assertEqual(priv.main(["create", payload]), 130)
        print_output.assert_called_once_with(
            "Exiting due to Ctrl+C", file=priv.sys.stderr
        )

    def test_configure_service_failure_does_not_claim_rootfs_was_moved(
        self,
    ) -> None:
        error = subprocess.CalledProcessError(
            5,
            [
                "/usr/bin/systemctl",
                "try-restart",
                "spaces@ubuntu.service",
            ],
        )
        with (
            mock.patch.object(priv.os, "geteuid", return_value=0),
            mock.patch.object(priv, "configure", side_effect=error),
            mock.patch.object(priv, "print") as print_output,
        ):
            self.assertEqual(priv.main(["configure", "{}"]), 5)

        print_output.assert_called_once_with(
            "Configuration saved but restarting the space failed.",
            file=priv.sys.stderr,
        )

    def test_configure_merges_user_and_system(self) -> None:
        with mock.patch.object(ubuntu.subprocess, "run"):
            priv.create(self.info)
        info_path = self.state_root / "ubuntu" / "info.json"
        stored = json.loads(info_path.read_text(encoding="utf-8"))
        stored["permissions"]["users"]["1001"] = {
            "gid": 1001,
            "permissions": {"home": ["Documents"]},
        }
        stored["permissions"]["users"]["0"]["permissions"]["future"] = {
            "enabled": True
        }
        priv._write_info(info_path.parent, stored)

        patch = {
            "schema_version": 1,
            "name": "ubuntu",
            "permissions": {
                "system": {"network": "admin"},
                "user": {
                    "uid": 0,
                    "gid": 0,
                    "permissions": {"home": ["Projects", "Documents"]},
                },
            },
        }
        with mock.patch.object(priv.subprocess, "run") as run:
            priv.configure(patch)
        updated = json.loads(info_path.read_text(encoding="utf-8"))
        self.assertEqual(updated["distribution"], self.info["distribution"])
        self.assertEqual(updated["permissions"]["system"]["network"], "admin")
        self.assertIn("1001", updated["permissions"]["users"])
        self.assertEqual(
            updated["permissions"]["users"]["0"]["permissions"]["future"],
            {"enabled": True},
        )
        run.assert_called_once_with(
            [
                "/usr/bin/systemctl",
                "try-restart",
                "spaces@ubuntu.service",
            ],
            check=True,
        )

    def test_user_only_configure_preserves_system(self) -> None:
        with mock.patch.object(ubuntu.subprocess, "run"):
            priv.create(self.info)
        with mock.patch.object(priv.subprocess, "run"):
            priv.configure(
                {
                    "schema_version": 1,
                    "name": "ubuntu",
                    "permissions": {
                        "user": {
                            "uid": 0,
                            "gid": 0,
                            "permissions": {"home": []},
                        },
                    },
                }
            )
        updated = json.loads(
            (self.state_root / "ubuntu" / "info.json").read_text(encoding="utf-8")
        )
        self.assertEqual(updated["permissions"]["system"]["network"], "basic")

    def test_configure_disabled_shortcuts_removes_exports(self) -> None:
        with mock.patch.object(ubuntu.subprocess, "run"):
            priv.create(self.info)
        patch = {
            "schema_version": 1,
            "name": "ubuntu",
            "permissions": {
                "system": {"network": "basic", "shortcuts": False},
                "user": {
                    "uid": 0,
                    "gid": 0,
                    "permissions": {"home": ["Projects"]},
                },
            },
        }
        with (
            mock.patch.object(priv.subprocess, "run"),
            mock.patch.object(shortcuts, "remove") as remove,
            mock.patch.object(shortcuts, "reconcile") as reconcile,
        ):
            priv.configure(patch)
        remove.assert_called_once_with("ubuntu")
        reconcile.assert_not_called()

    def test_configure_can_target_another_user(self) -> None:
        with mock.patch.object(ubuntu.subprocess, "run"):
            priv.create(self.info)

        with mock.patch.object(priv.subprocess, "run"):
            priv.configure(
                {
                    "schema_version": 1,
                    "name": "ubuntu",
                    "permissions": {
                        "user": {
                            "uid": 1001,
                            "gid": 1002,
                            "permissions": {
                                "home": ["Documents"],
                                "administrator": False,
                            },
                        },
                    },
                }
            )

        updated = json.loads(
            (self.state_root / "ubuntu" / "info.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            updated["permissions"]["users"]["1001"],
            {
                "gid": 1002,
                "permissions": {
                    "home": ["Documents"],
                    "administrator": False,
                },
            },
        )

    def test_configure_enables_service_for_target_user(self) -> None:
        with mock.patch.object(ubuntu.subprocess, "run"):
            priv.create(self.info)
        patch = {
            "schema_version": 1,
            "name": "ubuntu",
            "enable": True,
            "permissions": {
                "user": {
                    "uid": 1001,
                    "gid": 1002,
                    "permissions": {"home": ["Documents"]},
                },
            },
        }
        with (
            mock.patch.object(priv.subprocess, "run"),
            mock.patch.object(priv, "_enable_user_service") as enable,
        ):
            priv.configure(patch)

        enable.assert_called_once_with("ubuntu", 1001, 1002)

    def test_delete_preserves_home_and_removes_space_contents(self) -> None:
        space = self.state_root / "work"
        (space / "rootfs").mkdir(parents=True)
        (space / "home").mkdir()
        (space / "home" / "file").write_text("preserve", encoding="utf-8")
        (space / "info.json").write_text("{}", encoding="utf-8")

        with mock.patch.object(priv.subprocess, "run") as run:
            priv.delete({"name": "work"})

        self.assertTrue(space.is_dir())
        self.assertFalse((space / "rootfs").exists())
        self.assertFalse((space / "info.json").exists())
        self.assertEqual(
            (space / "home" / "file").read_text(encoding="utf-8"),
            "preserve",
        )
        run.assert_called_once_with(
            [
                "/usr/bin/systemctl",
                "stop",
                "spaces@work.service",
            ],
            check=True,
        )

    def test_delete_with_purge_removes_entire_space(self) -> None:
        space = self.state_root / "work"
        (space / "rootfs").mkdir(parents=True)
        (space / "home").mkdir()
        (space / "home" / "file").write_text("delete", encoding="utf-8")

        with mock.patch.object(priv.subprocess, "run"):
            priv.delete({"name": "work", "purge": True})

        self.assertFalse(space.exists())

    def test_delete_removes_shortcut_versions(self) -> None:
        space = self.state_root / "work"
        (space / "rootfs").mkdir(parents=True)
        with (
            mock.patch.object(priv.subprocess, "run"),
            mock.patch.object(shortcuts, "remove") as remove,
        ):
            priv.delete({"name": "work"})
        remove.assert_called_once_with("work")

    def test_delete_stop_failure_preserves_space(self) -> None:
        space = self.state_root / "work"
        (space / "rootfs").mkdir(parents=True)
        error = subprocess.CalledProcessError(
            1,
            [
                "/usr/bin/systemctl",
                "stop",
                "spaces@work.service",
            ],
        )

        with (
            mock.patch.object(priv.subprocess, "run", side_effect=error),
            mock.patch.object(shortcuts, "remove") as remove,
            self.assertRaises(subprocess.CalledProcessError),
        ):
            priv.delete({"name": "work"})

        self.assertTrue(space.is_dir())
        remove.assert_not_called()

    def test_delete_rejects_symlink(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        self.state_root.mkdir()
        (self.state_root / "work").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(core.SpacesError):
            priv.delete({"name": "work"})

        self.assertTrue(outside.is_dir())

    def test_delete_refuses_space_with_mount(self) -> None:
        space = self.state_root / "work"
        (space / "rootfs").mkdir(parents=True)
        mountinfo = f"1 0 0:1 / {space}/rootfs/proc rw - proc proc rw\n"

        with (
            mock.patch.object(priv.subprocess, "run"),
            mock.patch.object(Path, "read_text", return_value=mountinfo),
            self.assertRaises(core.SpacesError),
        ):
            priv.delete({"name": "work"})

        self.assertTrue(space.is_dir())

    def test_copy_runs_cp_with_authorized_arguments(self) -> None:
        completed = subprocess.CompletedProcess([], 42)
        with mock.patch.object(
            priv.subprocess, "run", return_value=completed
        ) as run:
            returncode = priv.copy(
                {
                    "arguments": [
                        "/var/lib/spaces/work/rootfs/source",
                        "/tmp/destination",
                        "--recursive",
                    ]
                }
            )

        self.assertEqual(returncode, 42)
        run.assert_called_once_with(
            [
                "/usr/bin/cp",
                "/var/lib/spaces/work/rootfs/source",
                "/tmp/destination",
                "--recursive",
            ],
            check=False,
        )

    def test_start_validates_configured_user_and_starts_space(self) -> None:
        info = core.create_info(
            "work",
            {"id": "custom"},
            core.Identity(1000, 1000, Path("/home/alice")),
            "basic",
            [],
        )
        space = self.state_root / "work"
        space.mkdir(parents=True)
        priv._write_info(space, info)
        with (
            mock.patch.dict(os.environ, {"PKEXEC_UID": "1000"}, clear=True),
            mock.patch.object(
                priv,
                "_ensure_space_started",
                return_value=42,
            ) as ensure_started,
        ):
            self.assertEqual(priv.start("work"), 42)

        ensure_started.assert_called_once_with("work")

    def test_start_succeeds_when_space_is_already_running(self) -> None:
        info = core.create_info(
            "work",
            {"id": "custom"},
            core.Identity(1000, 1000, Path("/home/alice")),
            "basic",
            [],
        )
        space = self.state_root / "work"
        space.mkdir(parents=True)
        priv._write_info(space, info)
        available = subprocess.CompletedProcess([], 0)
        with (
            mock.patch.dict(os.environ, {"PKEXEC_UID": "1000"}, clear=True),
            mock.patch.object(
                priv.subprocess,
                "run",
                return_value=available,
            ) as run,
        ):
            self.assertEqual(priv.start("work"), 0)

        run.assert_called_once_with(
            ["/usr/bin/machinectl", "--quiet", "show", "work"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def test_enter_status_is_only_shown_while_starting_space(self) -> None:
        class TerminalOutput(io.StringIO):
            def isatty(self) -> bool:
                return True

        for active, expected in (
            (True, ""),
            (
                False,
                "Starting space work and entering it...\n"
                "\033[F\033[2K",
            ),
        ):
            output = TerminalOutput()
            completed = [subprocess.CompletedProcess([], 0)]
            if not active:
                completed = [
                    subprocess.CompletedProcess([], 1),
                    subprocess.CompletedProcess([], 0),
                ]
            with (
                self.subTest(active=active),
                mock.patch.object(priv.sys, "stdout", output),
                mock.patch.object(
                    priv.subprocess,
                    "run",
                    side_effect=completed,
                ) as run,
            ):
                returncode = priv._ensure_space_started(
                    "work", entering=True
                )
                self.assertEqual(returncode, 0)
                self.assertEqual(output.getvalue(), expected)
            if active:
                self.assertEqual(run.call_count, 1)
            else:
                self.assertEqual(run.call_count, 2)
                self.assertEqual(
                    run.call_args_list[-1],
                    mock.call(
                        [
                            "/usr/bin/systemctl",
                            "start",
                            "spaces@work.service",
                        ],
                        check=False,
                    ),
                )

    def test_enter_status_is_hidden_when_output_is_not_a_terminal(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(priv.sys, "stdout", output),
            mock.patch.object(
                priv.subprocess,
                "run",
                side_effect=[
                    subprocess.CompletedProcess([], 1),
                    subprocess.CompletedProcess([], 0),
                ],
            ),
        ):
            returncode = priv._ensure_space_started("work", entering=True)

        self.assertEqual(returncode, 0)
        self.assertEqual(output.getvalue(), "")

    def test_start_propagates_system_service_failure(self) -> None:
        info = core.create_info(
            "work",
            {"id": "custom"},
            core.Identity(1000, 1000, Path("/home/alice")),
            "basic",
            [],
        )
        space = self.state_root / "work"
        space.mkdir(parents=True)
        priv._write_info(space, info)
        unavailable = subprocess.CompletedProcess([], 1)
        failed = subprocess.CompletedProcess([], 42)
        with (
            mock.patch.dict(os.environ, {"PKEXEC_UID": "1000"}, clear=True),
            mock.patch.object(
                priv.subprocess,
                "run",
                side_effect=[unavailable, failed],
            ) as run,
        ):
            self.assertEqual(priv.start("work"), 42)

        self.assertEqual(
            run.call_args_list[-1],
            mock.call(
                [
                    "/usr/bin/systemctl",
                    "start",
                    "spaces@work.service",
                ],
                check=False,
            ),
        )

    def test_start_rejects_unconfigured_user_without_starting(self) -> None:
        space = self.state_root / "ubuntu"
        space.mkdir(parents=True)
        priv._write_info(space, self.info)
        with (
            mock.patch.dict(os.environ, {"PKEXEC_UID": "1000"}, clear=True),
            mock.patch.object(priv.subprocess, "run") as run,
            self.assertRaises(core.SpacesError),
        ):
            priv.start("ubuntu")

        run.assert_not_called()

    def test_start_rejects_missing_or_invalid_space_without_starting(
        self,
    ) -> None:
        mismatched = self.state_root / "work"
        mismatched.mkdir(parents=True)
        priv._write_info(mismatched, self.info)
        invalid = self.state_root / "invalid"
        invalid.mkdir()
        (invalid / "info.json").write_text("{}\n", encoding="utf-8")

        for name in ("../work", "missing", "work", "invalid"):
            with (
                self.subTest(name=name),
                mock.patch.object(priv.subprocess, "run") as run,
                self.assertRaises(core.SpacesError),
            ):
                priv.start(name)
            run.assert_not_called()

    def test_start_parser_dispatches_space(self) -> None:
        with (
            mock.patch.object(priv.os, "geteuid", return_value=0),
            mock.patch.object(priv, "start", return_value=42) as start,
        ):
            self.assertEqual(priv.main(["start", "work"]), 42)

        start.assert_called_once_with("work")

    def test_enter_validates_user_then_starts_and_runs_machinectl(self) -> None:
        info = core.create_info(
            "work",
            {"id": "custom"},
            core.Identity(1000, 1000, Path("/home/alice")),
            "basic",
            [],
            host_authentication=False,
            desktop=False,
            credential_agents=False,
        )
        space = self.state_root / "work"
        space.mkdir(parents=True)
        priv._write_info(space, info)
        unavailable = subprocess.CompletedProcess([], 1)
        started = subprocess.CompletedProcess([], 0)
        entered = subprocess.CompletedProcess([], 42)
        with (
            mock.patch.dict(os.environ, {"PKEXEC_UID": "1000"}, clear=True),
            mock.patch.object(
                priv.pwd,
                "getpwnam",
                return_value=mock.Mock(
                    pw_uid=1000,
                    pw_name="alice@example",
                ),
            ) as getpwnam,
            mock.patch.object(
                priv.subprocess,
                "run",
                side_effect=[unavailable, started, entered],
            ) as run,
        ):
            self.assertEqual(
                priv.enter(
                    "alice@example@work",
                    ["--help", "literal value", "$HOME"],
                ),
                42,
            )

        getpwnam.assert_called_once_with("alice@example")
        self.assertEqual(
            run.call_args_list,
            [
                mock.call(
                    ["/usr/bin/machinectl", "--quiet", "show", "work"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ),
                mock.call(
                    [
                        "/usr/bin/systemctl",
                        "start",
                        "spaces@work.service",
                    ],
                    check=False,
                ),
                mock.call(
                    [
                        "/usr/bin/machinectl",
                        "--quiet",
                        "--uid=alice@example",
                        "--",
                        "shell",
                        "work",
                        "--help",
                        "literal value",
                        "$HOME",
                    ],
                    check=False,
                ),
            ],
        )

    def test_enter_without_command_uses_machinectl_shell(self) -> None:
        info = core.create_info(
            "work",
            {"id": "custom"},
            core.Identity(1000, 1000, Path("/home/alice")),
            "basic",
            [],
            host_authentication=False,
            desktop=False,
        )
        space = self.state_root / "work"
        space.mkdir(parents=True)
        priv._write_info(space, info)
        with (
            mock.patch.dict(os.environ, {"PKEXEC_UID": "1000"}, clear=True),
            mock.patch.object(
                priv.pwd,
                "getpwnam",
                return_value=mock.Mock(pw_uid=1000, pw_name="alice"),
            ),
            mock.patch.object(
                priv.subprocess,
                "run",
                side_effect=[
                    subprocess.CompletedProcess([], 0),
                    subprocess.CompletedProcess([], 0),
                ],
            ) as run,
        ):
            self.assertEqual(priv.enter("alice@work", []), 0)

        self.assertEqual(
            run.call_args_list[1].args[0][-2:],
            ["shell", "work"],
        )

    def test_enter_rejects_another_or_unconfigured_user(self) -> None:
        space = self.state_root / "ubuntu"
        space.mkdir(parents=True)
        self.info["permissions"]["system"]["host_authentication"] = False
        priv._write_info(space, self.info)
        with (
            mock.patch.dict(os.environ, {"PKEXEC_UID": "1000"}, clear=True),
            mock.patch.object(
                priv.pwd,
                "getpwnam",
                return_value=mock.Mock(pw_uid=1001),
            ),
            mock.patch.object(priv.subprocess, "run") as run,
            self.assertRaises(core.SpacesError),
        ):
            priv.enter("alice@ubuntu", [])
        run.assert_not_called()

        with (
            mock.patch.dict(os.environ, {"PKEXEC_UID": "1000"}, clear=True),
            mock.patch.object(
                priv.pwd,
                "getpwnam",
                return_value=mock.Mock(pw_uid=1000),
            ),
            mock.patch.object(priv.subprocess, "run") as run,
            self.assertRaises(core.SpacesError),
        ):
            priv.enter("alice@ubuntu", [])
        run.assert_not_called()

    def test_enter_rejects_malformed_target_or_unknown_user(self) -> None:
        for target in ("alice", "@work", "alice@"):
            with (
                self.subTest(target=target),
                mock.patch.object(priv.pwd, "getpwnam") as getpwnam,
                mock.patch.object(priv.subprocess, "run") as run,
                self.assertRaises(core.SpacesError),
            ):
                priv.enter(target, [])
            getpwnam.assert_not_called()
            run.assert_not_called()

        with (
            mock.patch.object(
                priv.pwd,
                "getpwnam",
                side_effect=KeyError("alice"),
            ),
            mock.patch.object(priv.subprocess, "run") as run,
            self.assertRaises(core.SpacesError),
        ):
            priv.enter("alice@work", [])
        run.assert_not_called()

    def test_enter_parser_removes_argument_separator(self) -> None:
        with (
            mock.patch.object(priv.os, "geteuid", return_value=0),
            mock.patch.object(priv, "enter", return_value=42) as enter,
        ):
            self.assertEqual(
                priv.main(["enter", "alice@work", "--", "--help"]),
                42,
            )

        enter.assert_called_once_with("alice@work", ["--help"])

    def test_enter_parser_forwards_graphical_launch_environment(self) -> None:
        with (
            mock.patch.object(priv.os, "geteuid", return_value=0),
            mock.patch.object(priv, "enter", return_value=42) as enter,
        ):
            self.assertEqual(
                priv.main(
                    [
                        "enter",
                        "--launch-environment="
                        '{"DESKTOP_STARTUP_ID":"x11-id",'
                        '"XDG_ACTIVATION_TOKEN":"wayland-token"}',
                        "alice@work",
                        "--",
                        "/usr/bin/kate",
                    ]
                ),
                42,
            )

        enter.assert_called_once_with(
            "alice@work",
            ["/usr/bin/kate"],
            launch_environment={
                "DESKTOP_STARTUP_ID": "x11-id",
                "XDG_ACTIVATION_TOKEN": "wayland-token",
            },
        )

    def test_graphical_launch_environment_rejects_other_names(self) -> None:
        with self.assertRaisesRegex(
            core.SpacesError,
            "Invalid graphical launch environment",
        ):
            priv._validate_launch_environment({"DISPLAY": ":0"})

    def test_enter_as_user_parser_rejects_launch_environment(self) -> None:
        with self.assertRaises(SystemExit):
            priv.build_parser().parse_args(
                [
                    "enter-as-user",
                    "--launch-environment={}",
                    "root",
                    "work",
                ]
            )

    def test_enter_parser_has_no_session_manifest_option(self) -> None:
        with self.assertRaises(SystemExit):
            priv.build_parser().parse_args(
                ["enter", "--session", "{}", "alice@work"]
            )

    def test_enter_reads_root_environment_file_and_uses_launcher(self) -> None:
        info = core.create_info(
            "work",
            {"id": "custom"},
            core.Identity(1000, 1000, Path("/home/alice")),
            "basic",
            [],
            host_authentication=False,
        )
        account = mock.Mock(
            pw_uid=1000,
            pw_gid=1000,
            pw_name="alice",
            pw_dir="/home/alice",
        )
        with (
            mock.patch.object(priv, "_caller_uid", return_value=1000),
            mock.patch.object(priv.pwd, "getpwnam", return_value=account),
            mock.patch.object(priv, "_space_info", return_value=info),
            mock.patch.object(
                priv,
                "_ensure_space_started",
                return_value=0,
            ),
            mock.patch.object(
                session,
                "desktop_environment",
                return_value={"DISPLAY": ":0"},
            ) as desktop_environment,
            mock.patch.object(session, "polkit_agent", return_value="/agent"),
            mock.patch.object(priv, "_space_directory", return_value=Path("/space")),
            mock.patch.object(priv, "_machine_shell", return_value=42) as shell,
        ):
            self.assertEqual(
                priv.enter(
                    "alice@work",
                    ["/usr/bin/kate"],
                    launch_environment={
                        "XDG_ACTIVATION_TOKEN": "wayland-token"
                    },
                ),
                42,
            )

        desktop_environment.assert_called_once_with("work", 1000)
        shell.assert_called_once_with(
            "alice",
            "work",
            ["/usr/bin/kate"],
            environment={"DISPLAY": ":0"},
            launch_environment={
                "XDG_ACTIVATION_TOKEN": "wayland-token"
            },
            launcher=True,
            agent="/agent",
        )

    def test_disabled_desktop_uses_launcher_without_environment(self) -> None:
        info = core.create_info(
            "work",
            {"id": "custom"},
            core.Identity(1000, 1000, Path("/home/alice")),
            "basic",
            [],
            host_authentication=False,
            desktop=False,
            credential_agents=False,
        )
        account = mock.Mock(
            pw_uid=1000,
            pw_name="alice",
        )
        with (
            mock.patch.object(priv, "_caller_uid", return_value=1000),
            mock.patch.object(priv.pwd, "getpwnam", return_value=account),
            mock.patch.object(priv, "_space_info", return_value=info),
            mock.patch.object(
                priv,
                "_ensure_space_started",
                return_value=0,
            ),
            mock.patch.object(session, "desktop_environment") as environment,
            mock.patch.object(
                priv,
                "_machine_shell",
                return_value=0,
            ) as shell,
        ):
            self.assertEqual(priv.enter("alice@work", []), 0)
        environment.assert_not_called()
        shell.assert_called_once_with("alice", "work", [])

    def test_credential_only_enter_injects_agent_without_gui_launcher(
        self,
    ) -> None:
        info = core.create_info(
            "work",
            {"id": "custom"},
            core.Identity(1000, 1000, Path("/home/alice")),
            "basic",
            [],
            desktop=False,
            credential_agents=True,
        )
        account = mock.Mock(pw_uid=1000, pw_name="alice")
        forwarded = {
            "SSH_AUTH_SOCK": "/run/spaces/credentials/1000/ssh-agent"
        }
        with (
            mock.patch.object(priv, "_caller_uid", return_value=1000),
            mock.patch.object(priv.pwd, "getpwnam", return_value=account),
            mock.patch.object(priv, "_space_info", return_value=info),
            mock.patch.object(
                priv,
                "_ensure_space_started",
                return_value=0,
            ),
            mock.patch.object(
                session,
                "desktop_environment",
                return_value=forwarded,
            ),
            mock.patch.object(priv, "_machine_shell", return_value=0) as shell,
        ):
            self.assertEqual(priv.enter("alice@work", ["ssh", "host"]), 0)

        shell.assert_called_once_with(
            "alice",
            "work",
            ["ssh", "host"],
            environment=forwarded,
        )

    def test_machine_shell_runs_direct(self) -> None:
        completed = subprocess.CompletedProcess([], 0)
        with mock.patch.object(
            priv.subprocess, "run", return_value=completed
        ) as run:
            self.assertEqual(
                priv._machine_shell("alice", "work", ["id", "-u"]),
                0,
            )

        self.assertEqual(
            run.call_args.args[0],
            [
                "/usr/bin/machinectl",
                "--quiet",
                "--uid=alice",
                "--",
                "shell",
                "work",
                "id",
                "-u",
            ],
        )

    def test_machine_shell_passes_file_environment_to_pam_command(self) -> None:
        completed = subprocess.CompletedProcess([], 0)
        with mock.patch.object(
            priv.subprocess, "run", return_value=completed
        ) as run:
            self.assertEqual(
                priv._machine_shell(
                    "alice",
                    "work",
                    ["/usr/bin/code", "--reuse-window"],
                    environment={
                        "WAYLAND_DISPLAY": (
                            "/run/spaces/desktop/1000/wayland/wayland-0"
                        ),
                        "DISPLAY": ":0",
                        "XDG_DATA_DIRS": (
                            "/run/spaces/desktop/1000/open-data:"
                            "/usr/local/share:/usr/share"
                        ),
                    },
                    launcher=True,
                    agent="/usr/libexec/polkit-agent",
                ),
                0,
            )

        self.assertEqual(
            run.call_args.args[0],
            [
                "/usr/bin/machinectl",
                "--quiet",
                "--uid=alice",
                "--setenv=DISPLAY=:0",
                (
                    "--setenv=WAYLAND_DISPLAY="
                    "/run/spaces/desktop/1000/wayland/wayland-0"
                ),
                (
                    "--setenv=XDG_DATA_DIRS="
                    "/run/spaces/desktop/1000/open-data:"
                    "/usr/local/share:/usr/share"
                ),
                "--",
                "shell",
                "work",
                "/run/spaces-host/bin/spaces",
                "--dbus-env",
                "DISPLAY",
                "--dbus-env",
                "WAYLAND_DISPLAY",
                "--dbus-env-path",
                "XDG_DATA_DIRS",
                "--agent",
                "/usr/libexec/polkit-agent",
                "--",
                "/usr/bin/code",
                "--reuse-window",
            ],
        )

    def test_machine_shell_keeps_activation_token_launch_scoped(self) -> None:
        completed = subprocess.CompletedProcess([], 0)
        with mock.patch.object(
            priv.subprocess, "run", return_value=completed
        ) as run:
            self.assertEqual(
                priv._machine_shell(
                    "alice",
                    "work",
                    ["/usr/bin/kate"],
                    environment={"DISPLAY": ":0"},
                    launch_environment={
                        "DESKTOP_STARTUP_ID": "x11-id",
                        "XDG_ACTIVATION_TOKEN": "wayland-token",
                    },
                    launcher=True,
                ),
                0,
            )

        self.assertEqual(
            run.call_args.args[0],
            [
                "/usr/bin/machinectl",
                "--quiet",
                "--uid=alice",
                "--setenv=DESKTOP_STARTUP_ID=x11-id",
                "--setenv=DISPLAY=:0",
                "--setenv=XDG_ACTIVATION_TOKEN=wayland-token",
                "--",
                "shell",
                "work",
                "/run/spaces-host/bin/spaces",
                "--dbus-env",
                "DISPLAY",
                "--",
                "/usr/bin/kate",
            ],
        )

    def test_enter_as_user_checks_target_instead_of_privileged_caller(
        self,
    ) -> None:
        space = self.state_root / "ubuntu"
        space.mkdir(parents=True)
        info = core.create_info(
            "ubuntu",
            {"id": "ubuntu", "version": "resolute"},
            core.Identity(1001, 1001, Path("/home/builder")),
            "basic",
            [],
            host_authentication=False,
        )
        priv._write_info(space, info)
        unavailable = subprocess.CompletedProcess([], 1)
        started = subprocess.CompletedProcess([], 0)
        entered = subprocess.CompletedProcess([], 42)
        with (
            mock.patch.object(
                priv.pwd,
                "getpwnam",
                return_value=mock.Mock(pw_uid=1001),
            ) as getpwnam,
            mock.patch.object(
                priv,
                "_caller_uid",
                side_effect=AssertionError("caller UID must not be checked"),
            ),
            mock.patch.object(
                priv.subprocess,
                "run",
                side_effect=[unavailable, started, entered],
            ) as run,
        ):
            self.assertEqual(
                priv.enter_as_user(
                    "builder",
                    "ubuntu",
                    ["id", "-u"],
                ),
                42,
        )

        getpwnam.assert_called_once_with("builder")
        self.assertEqual(
            run.call_args_list,
            [
                mock.call(
                    ["/usr/bin/machinectl", "--quiet", "show", "ubuntu"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ),
                mock.call(
                    [
                        "/usr/bin/systemctl",
                        "start",
                        "spaces@ubuntu.service",
                    ],
                    check=False,
                ),
                mock.call(
                    [
                        "/usr/bin/machinectl",
                        "--quiet",
                        "--uid=builder",
                        "--",
                        "shell",
                        "ubuntu",
                        "id",
                        "-u",
                    ],
                    check=False,
                ),
            ],
        )

    def test_enter_as_user_rejects_unconfigured_target(self) -> None:
        with (
            mock.patch.object(priv, "_space_info", return_value=self.info),
            mock.patch.object(
                priv.pwd,
                "getpwnam",
                return_value=mock.Mock(pw_uid=1001),
            ),
            mock.patch.object(priv, "_ensure_space_started") as start,
            self.assertRaisesRegex(
                core.SpacesError,
                "User 'builder' is not configured",
            ),
        ):
            priv.enter_as_user("builder", "ubuntu", [])

        start.assert_not_called()

    def test_enter_as_user_rejects_unknown_host_target(self) -> None:
        with (
            mock.patch.object(priv, "_space_info", return_value=self.info),
            mock.patch.object(priv.pwd, "getpwnam", side_effect=KeyError),
            mock.patch.object(priv, "_ensure_space_started") as start,
            self.assertRaisesRegex(
                core.SpacesError,
                "Host user 'builder' does not exist",
            ),
        ):
            priv.enter_as_user("builder", "ubuntu", [])

        start.assert_not_called()

    def test_enter_as_root_does_not_depend_on_guest_authentication_agent(
        self,
    ) -> None:
        space = self.state_root / "ubuntu"
        space.mkdir(parents=True)
        priv._write_info(space, self.info)
        available = subprocess.CompletedProcess([], 0)
        entered = subprocess.CompletedProcess([], 42)
        with (
            mock.patch.object(
                priv.subprocess,
                "run",
                side_effect=[available, entered],
            ) as run,
            mock.patch.object(priv.pwd, "getpwnam") as getpwnam,
        ):
            self.assertEqual(
                priv.enter_as_user(
                    "root",
                    "ubuntu",
                    ["apt-get", "install", "polkitd"],
                ),
                42,
            )

        getpwnam.assert_not_called()
        self.assertEqual(
            run.call_args_list[1],
            mock.call(
                [
                    "/usr/bin/machinectl",
                    "--quiet",
                    "--uid=root",
                    "--",
                    "shell",
                    "ubuntu",
                    "apt-get",
                    "install",
                    "polkitd",
                ],
                check=False,
            ),
        )

    def test_enter_as_user_rejects_unsafe_user_name(self) -> None:
        for user_name in ("", ".", "..", "../root", "bad:name", "bad\nname"):
            with (
                self.subTest(user_name=user_name),
                mock.patch.object(priv.subprocess, "run") as run,
                self.assertRaises(core.SpacesError),
            ):
                priv.enter_as_user(user_name, "ubuntu", [])
            run.assert_not_called()

    def test_enter_as_user_parser_removes_argument_separator(self) -> None:
        with (
            mock.patch.object(priv.os, "geteuid", return_value=0),
            mock.patch.object(
                priv,
                "enter_as_user",
                return_value=42,
            ) as enter_as_user,
        ):
            self.assertEqual(
                priv.main(
                    [
                        "enter-as-user",
                        "root",
                        "work",
                        "--",
                        "--help",
                    ]
                ),
                42,
            )

        enter_as_user.assert_called_once_with(
            "root",
            "work",
            ["--help"],
        )

    def test_launch_return_code_is_propagated(self) -> None:
        with (
            mock.patch.object(priv.os, "geteuid", return_value=0),
            mock.patch.object(priv, "launch", return_value=42) as launch,
        ):
            self.assertEqual(priv.main(["launch", "work"]), 42)

        launch.assert_called_once_with("work")

    def test_launch_rejects_invalid_name(self) -> None:
        with (
            mock.patch.object(priv.os, "geteuid", return_value=0),
            mock.patch.object(priv, "print") as print_output,
        ):
            self.assertEqual(priv.main(["launch", "../work"]), 1)

        print_output.assert_called_once_with(
            mock.ANY,
            file=priv.sys.stderr,
        )

    def test_launch_os_error_is_reported(self) -> None:
        with (
            mock.patch.object(priv.os, "geteuid", return_value=0),
            mock.patch.object(priv, "launch", side_effect=OSError("missing")),
            mock.patch.object(priv, "print") as print_output,
        ):
            self.assertEqual(priv.main(["launch", "work"]), 1)

        print_output.assert_called_once_with(
            "spaces.priv: missing",
            file=priv.sys.stderr,
        )

    def test_creation_rejects_another_uid(self) -> None:
        info = core.create_info(
            "work",
            {"id": "custom"},
            core.Identity(1000, 1000, Path("/home/user")),
            "basic",
            [],
        )
        with self.assertRaises(core.SpacesError):
            priv.create(info)

    def test_rootfs_with_mount_is_not_removed(self) -> None:
        rootfs = self.state_root / "ubuntu" / "rootfs"
        rootfs.mkdir(parents=True)
        mountinfo = (
            f"1 0 0:1 / {rootfs}/proc rw - proc proc rw\n"
        )
        with (
            mock.patch.object(Path, "read_text", return_value=mountinfo),
            self.assertRaises(core.SpacesError),
        ):
            priv._remove_rootfs(rootfs)

    def test_priv_module_does_not_import_textual_or_rich(self) -> None:
        path = Path(priv.__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".", 1)[0])
        self.assertTrue({"textual", "rich"}.isdisjoint(imports))


if __name__ == "__main__":
    unittest.main()
