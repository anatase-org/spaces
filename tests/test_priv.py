from __future__ import annotations

import ast
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spaces import core, priv
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
                    ],
                    check=True,
                ),
                mock.call(
                    [
                        "chroot",
                        str(space / "rootfs"),
                        "/usr/bin/env",
                        "DEBIAN_FRONTEND=noninteractive",
                        "apt-get",
                        "install",
                        "--yes",
                        "--no-install-recommends",
                        "openssh-client",
                        "python3",
                        "nano",
                        "sudo",
                        "polkitd",
                    ],
                    check=True,
                ),
            ]
        )
        self.assertEqual(run.call_count, 3)
        self.assertEqual(
            print_output.call_args_list,
            [
                mock.call("Bootstrapping Ubuntu Resolute (26.04)...", flush=True),
                mock.call(
                    "Adding additional packages:\n"
                    "openssh-client, python3, nano, sudo, polkitd",
                    flush=True,
                ),
            ],
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

    def test_rebuild_removes_rootfs_and_preserves_home(self) -> None:
        with mock.patch.object(ubuntu.subprocess, "run"):
            priv.create(self.info)
        space = self.state_root / "ubuntu"
        (space / "rootfs" / "partial").write_text("remove", encoding="utf-8")
        (space / "home" / "keep").write_text("preserve", encoding="utf-8")
        with mock.patch.object(ubuntu.subprocess, "run"):
            priv.create(self.info)
        self.assertFalse((space / "rootfs" / "partial").exists())
        self.assertEqual(
            (space / "home" / "keep").read_text(encoding="utf-8"), "preserve"
        )

    def test_bootstrap_failure_leaves_partial_space(self) -> None:
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
        space = self.state_root / "ubuntu"
        self.assertTrue((space / "rootfs").is_dir())
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

    def test_delete_removes_entire_space(self) -> None:
        space = self.state_root / "work"
        (space / "rootfs").mkdir(parents=True)
        (space / "home").mkdir()
        (space / "home" / "file").write_text("delete", encoding="utf-8")

        priv.delete({"name": "work"})

        self.assertFalse(space.exists())

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

    def test_enter_validates_user_then_starts_and_runs_machinectl(self) -> None:
        info = core.create_info(
            "work",
            {"id": "custom"},
            core.Identity(1000, 1000, Path("/home/alice")),
            "basic",
            [],
            host_authentication=False,
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

    def test_enter_without_command_uses_machinectl_default_shell(self) -> None:
        info = core.create_info(
            "work",
            {"id": "custom"},
            core.Identity(1000, 1000, Path("/home/alice")),
            "basic",
            [],
            host_authentication=False,
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

    def test_enter_parser_passes_verified_subject_metadata(self) -> None:
        with (
            mock.patch.object(priv.os, "geteuid", return_value=0),
            mock.patch.object(priv, "enter", return_value=0) as enter,
        ):
            self.assertEqual(
                priv.main(
                    [
                        "enter",
                        "--subject-pid=12",
                        "--subject-start-time=34",
                        "--subject-session=c1",
                        "alice@work",
                    ]
                ),
                0,
            )

        enter.assert_called_once_with(
            "alice@work",
            [],
            subject_pid=12,
            subject_start_time=34,
            subject_session="c1",
        )

    def test_machine_shell_injects_only_opaque_session_and_runs_direct(
        self,
    ) -> None:
        completed = subprocess.CompletedProcess([], 0)
        with mock.patch.object(
            priv.subprocess, "run", return_value=completed
        ) as run:
            self.assertEqual(
                priv._machine_shell(
                    "alice", "work", ["id", "-u"], "opaque-token"
                ),
                0,
            )

        self.assertEqual(
            run.call_args.args[0],
            [
                "/usr/bin/machinectl",
                "--quiet",
                "--uid=alice",
                "--setenv=SPACES_AUTH_SESSION=opaque-token",
                "--",
                "shell",
                "work",
                "id",
                "-u",
            ],
        )

    def test_verified_subject_must_be_live_unprivileged_ancestor(
        self,
    ) -> None:
        account = mock.Mock(pw_gid=1000)
        with (
            mock.patch.object(priv, "_caller_uid", return_value=1000),
            mock.patch.object(
                priv.Path,
                "stat",
                return_value=mock.Mock(st_uid=1000),
            ),
            mock.patch.object(priv.os, "getpid", return_value=300),
            mock.patch.object(
                priv.auth,
                "process_parent",
                side_effect=lambda pid: {300: 200, 200: 123}[pid],
            ),
            mock.patch.object(
                priv.auth, "process_start_time", return_value=456
            ),
            mock.patch.object(
                priv.auth, "process_session_matches", return_value=True
            ),
            mock.patch.object(priv.pwd, "getpwuid", return_value=account),
        ):
            subject = priv._verified_subject(123, 456, "c1")

        self.assertEqual(
            subject,
            priv.auth.LeaseSubject(123, 456, 1000, 1000, "c1"),
        )

    def test_enter_as_user_uses_privileged_target_without_host_lookup(
        self,
    ) -> None:
        space = self.state_root / "ubuntu"
        space.mkdir(parents=True)
        self.info["permissions"]["system"]["host_authentication"] = False
        priv._write_info(space, self.info)
        unavailable = subprocess.CompletedProcess([], 1)
        started = subprocess.CompletedProcess([], 0)
        entered = subprocess.CompletedProcess([], 42)
        with (
            mock.patch.object(priv.pwd, "getpwnam") as getpwnam,
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

        getpwnam.assert_not_called()
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

    def test_enter_as_root_does_not_depend_on_guest_authentication_agent(
        self,
    ) -> None:
        space = self.state_root / "ubuntu"
        space.mkdir(parents=True)
        priv._write_info(space, self.info)
        available = subprocess.CompletedProcess([], 0)
        entered = subprocess.CompletedProcess([], 42)
        with mock.patch.object(
            priv.subprocess,
            "run",
            side_effect=[available, entered],
        ) as run:
            self.assertEqual(
                priv.enter_as_user(
                    "root",
                    "ubuntu",
                    ["apt-get", "install", "polkitd"],
                ),
                42,
            )

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
