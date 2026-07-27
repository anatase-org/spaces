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
                        "ssh",
                        "python3",
                        "nano",
                    ],
                    check=True,
                ),
            ]
        )
        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            print_output.call_args_list,
            [
                mock.call("Bootstrapping Ubuntu Resolute (26.04)...", flush=True),
                mock.call(
                    "Adding additional packages:\nssh, python3, nano",
                    flush=True,
                ),
            ],
        )

    def test_custom_does_not_bootstrap(self) -> None:
        info = core.create_info(
            "work", {"id": "custom"}, self.identity, "basic", ["Projects"]
        )
        with mock.patch.object(ubuntu.subprocess, "run") as run:
            priv.create(info)
        run.assert_not_called()

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
            mock.patch.object(ubuntu.subprocess, "run", side_effect=error),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            priv.create(self.info)
        space = self.state_root / "ubuntu"
        self.assertTrue((space / "rootfs").is_dir())
        self.assertTrue((space / "home").is_dir())
        self.assertTrue((space / "info.json").is_file())

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
        priv.configure(patch)
        updated = json.loads(info_path.read_text(encoding="utf-8"))
        self.assertEqual(updated["distribution"], self.info["distribution"])
        self.assertEqual(updated["permissions"]["system"]["network"], "admin")
        self.assertIn("1001", updated["permissions"]["users"])
        self.assertEqual(
            updated["permissions"]["users"]["0"]["permissions"]["future"],
            {"enabled": True},
        )

    def test_user_only_configure_preserves_system(self) -> None:
        with mock.patch.object(ubuntu.subprocess, "run"):
            priv.create(self.info)
        priv.configure(
            {
                "schema_version": 1,
                "name": "ubuntu",
                "permissions": {
                    "user": {
                        "uid": 0,
                        "gid": 0,
                        "permissions": {"home": []},
                    }
                },
            }
        )
        updated = json.loads(
            (self.state_root / "ubuntu" / "info.json").read_text(encoding="utf-8")
        )
        self.assertEqual(updated["permissions"]["system"]["network"], "basic")

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
