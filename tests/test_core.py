from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spaces import core
from spaces.distro import ubuntu
from spaces.distro.model import Distribution


class CoreTests(unittest.TestCase):
    def test_ubuntu_display_and_command_mapping(self) -> None:
        self.assertIsInstance(ubuntu.DISTRIBUTION, Distribution)
        self.assertEqual(
            ubuntu.DISTRIBUTION.choices(),
            [
                ("Noble (24.04)", "noble"),
                ("Resolute (26.04)", "resolute"),
            ],
        )
        self.assertEqual(ubuntu.DISTRIBUTION.default_option, "resolute")
        self.assertEqual(
            ubuntu.DISTRIBUTION.command(
                {"id": "ubuntu", "version": "noble"}, Path("/rootfs")
            ),
            [
                "debootstrap",
                "noble",
                "/rootfs",
            ],
        )

    def test_discover_home_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "Documents").mkdir()
            (home / ".ssh").mkdir()
            (home / "file.txt").write_text("not a folder")
            (home / "linked").symlink_to(home / "Documents", target_is_directory=True)
            self.assertEqual(
                core.discover_home_folders(home),
                ["Documents", "Downloads", "Projects"],
            )

    def test_new_space_defaults(self) -> None:
        network, selected_home = core.defaults_from_info(
            None,
            core.Identity(1000, 1000, Path("/home/user")),
        )
        self.assertEqual(network, "basic")
        self.assertEqual(selected_home, ["Projects", "Downloads"])

    def test_space_name_validation(self) -> None:
        self.assertEqual(core.validate_space_name("project-1"), "project-1")
        with self.assertRaises(core.SpacesError):
            core.validate_space_name("../project")
        with self.assertRaises(core.SpacesError):
            core.validate_space_name("ubuntu", allow_reserved=False)

    def test_space_location_uses_rootfs_and_shared_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            (state / "work" / "rootfs").mkdir(parents=True)
            (state / "work" / "home").mkdir()
            with mock.patch.object(core, "STATE_ROOT", state):
                self.assertEqual(
                    core.resolve_space_location("work:/etc/hosts"),
                    str(state / "work" / "rootfs" / "etc" / "hosts"),
                )
                self.assertEqual(
                    core.resolve_space_location("work:/home/alice/file"),
                    str(state / "work" / "home" / "alice" / "file"),
                )
                self.assertEqual(
                    core.resolve_space_location("work:/var/home/alice/file"),
                    str(state / "work" / "home" / "alice" / "file"),
                )

    def test_space_location_cannot_escape_space(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            (state / "work" / "rootfs").mkdir(parents=True)
            (state / "work" / "home").mkdir()
            with (
                mock.patch.object(core, "STATE_ROOT", state),
                self.assertRaises(core.SpacesError),
            ):
                core.resolve_space_location("work:/../../outside")

    def test_host_location_is_preserved(self) -> None:
        self.assertEqual(
            core.resolve_space_location("../source"),
            "../source",
        )

    def test_cp_payload_requires_arguments(self) -> None:
        with self.assertRaises(core.SpacesError):
            core.validate_cp_request({"arguments": []})

    def test_initiating_identity_uses_sudo_ids(self) -> None:
        passwd = mock.Mock(pw_dir="/home/alice")
        with (
            mock.patch.object(core.os, "geteuid", return_value=0),
            mock.patch.dict(
                os.environ, {"SUDO_UID": "1001", "SUDO_GID": "1002"}, clear=True
            ),
            mock.patch.object(core.pwd, "getpwuid", return_value=passwd),
        ):
            identity = core.initiating_identity()
        self.assertEqual(identity, core.Identity(1001, 1002, Path("/home/alice")))

    def test_initiating_identity_direct_root_defaults_to_root(self) -> None:
        passwd = mock.Mock(pw_dir="/root")
        with (
            mock.patch.object(core.os, "geteuid", return_value=0),
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(core.pwd, "getpwuid", return_value=passwd),
        ):
            identity = core.initiating_identity()
        self.assertEqual(identity, core.Identity(0, 0, Path("/root")))

    def test_create_info_has_only_initiating_user(self) -> None:
        info = core.create_info(
            "ubuntu",
            {"id": "ubuntu", "version": "noble"},
            core.Identity(1000, 1000, Path("/home/user")),
            "advanced",
            ["Projects", "Documents"],
        )
        self.assertEqual(info["distribution"]["version"], "noble")
        self.assertEqual(set(info["permissions"]["users"]), {"1000"})

    def test_custom_info_omits_version(self) -> None:
        info = core.create_info(
            "work",
            {"id": "custom"},
            core.Identity(1000, 1000, Path("/home/user")),
            "basic",
            ["Projects"],
        )
        self.assertEqual(info["distribution"], {"id": "custom"})

    def test_creation_rejects_unsupported_distribution(self) -> None:
        info = core.create_info(
            "work",
            {"id": "custom"},
            core.Identity(1000, 1000, Path("/home/user")),
            "basic",
            [],
        )
        info["distribution"] = {"id": "arch"}
        with self.assertRaises(core.SpacesError):
            core.validate_creation_info(info)

    def test_distribution_name_does_not_constrain_space_name(self) -> None:
        info = core.create_info(
            "work",
            {"id": "ubuntu", "version": "noble"},
            core.Identity(1000, 1000, Path("/home/user")),
            "basic",
            [],
        )
        self.assertEqual(info["name"], "work")

    def test_boolean_schema_version_is_rejected(self) -> None:
        info = core.create_info(
            "work",
            {"id": "custom"},
            core.Identity(1000, 1000, Path("/home/user")),
            "basic",
            [],
        )
        info["schema_version"] = True
        with self.assertRaises(core.SpacesError):
            core.validate_info(info)

    def test_invalid_home_permission_is_rejected(self) -> None:
        info = core.create_info(
            "work",
            {"id": "custom"},
            core.Identity(1000, 1000, Path("/home/user")),
            "basic",
            ["Projects"],
        )
        info["permissions"]["users"]["1000"]["permissions"]["home"] = ["../secret"]
        with self.assertRaises(core.SpacesError):
            core.validate_info(info)

    def test_load_info_returns_none_for_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "info.json"
            path.write_text("{broken", encoding="utf-8")
            self.assertIsNone(core.load_info(path))


if __name__ == "__main__":
    unittest.main()
