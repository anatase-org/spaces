from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spaces import core
from spaces.distro import ubuntu
from spaces.distro.model import Distribution, DistributionError


class CoreTests(unittest.TestCase):
    def test_ubuntu_display_and_command_mapping(self) -> None:
        self.assertIsInstance(ubuntu.DISTRIBUTION, Distribution)
        self.assertEqual(ubuntu.DISTRIBUTION.administrator_group, "sudo")
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
                "",
                "gutsy",
            ],
        )
        self.assertIn("pkexec", ubuntu.PACKAGES)
        self.assertIn("polkit-kde-agent-1", ubuntu.PACKAGES)
        self.assertIn("breeze", ubuntu.PACKAGES)
        self.assertIn("plasma-integration", ubuntu.PACKAGES)
        self.assertIn("pipewire", ubuntu.PACKAGES)
        self.assertIn("xdg-desktop-portal", ubuntu.PACKAGES)
        self.assertIn("xdg-desktop-portal-kde", ubuntu.PACKAGES)
        self.assertIn("file", ubuntu.PACKAGES)
        self.assertEqual(
            ubuntu.SECRET_PACKAGES,
            {
                "noble": (
                    "libkf5wallet-bin",
                    "libqca-qt5-2-plugins",
                ),
                "resolute": (
                    "kwallet6",
                    "libqca-qt6-plugins",
                    "qt6-wayland",
                ),
            },
        )
        self.assertNotIn("python3", ubuntu.PACKAGES)

    def test_ubuntu_reconciles_host_authentication_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rootfs = root / "rootfs"
            profile_directory = rootfs / "usr" / "share" / "pam-configs"
            profile_directory.mkdir(parents=True)
            source = root / "spaces.ubuntu"
            source.write_text("Name: Spaces auth\n", encoding="utf-8")
            command = [
                "chroot",
                str(rootfs),
                "/usr/bin/env",
                "DEBIAN_FRONTEND=noninteractive",
                "/usr/sbin/pam-auth-update",
                "--package",
            ]

            with (
                mock.patch.object(
                    ubuntu,
                    "HOST_AUTHENTICATION_PROFILE",
                    source,
                ),
                mock.patch.object(ubuntu.subprocess, "run") as run,
            ):
                self.assertTrue(
                    ubuntu.DISTRIBUTION.reconcile_host_authentication(
                        rootfs,
                        True,
                    )
                )
                destination = profile_directory / "spaces"
                self.assertEqual(
                    destination.read_text(encoding="utf-8"),
                    "Name: Spaces auth\n",
                )
                self.assertEqual(
                    destination.stat().st_mode & 0o777,
                    0o644,
                )
                run.assert_called_once_with(command, check=True)

                run.reset_mock()
                self.assertTrue(
                    ubuntu.DISTRIBUTION.reconcile_host_authentication(
                        rootfs,
                        False,
                    )
                )
                self.assertFalse(destination.exists())
                run.assert_called_once_with(command, check=True)

    def test_disabled_ubuntu_authentication_skips_absent_profile_fixup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rootfs = Path(temporary)
            with mock.patch.object(ubuntu.subprocess, "run") as run:
                self.assertTrue(
                    ubuntu.DISTRIBUTION.reconcile_host_authentication(
                        rootfs,
                        False,
                    )
                )

            run.assert_not_called()

    def test_failed_disabled_fixup_restores_profile_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rootfs = root / "rootfs"
            profile_directory = rootfs / "usr" / "share" / "pam-configs"
            profile_directory.mkdir(parents=True)
            destination = profile_directory / "spaces"
            destination.write_text("old profile\n", encoding="utf-8")
            source = root / "spaces.ubuntu"
            source.write_text("canonical profile\n", encoding="utf-8")

            with (
                mock.patch.object(
                    ubuntu,
                    "HOST_AUTHENTICATION_PROFILE",
                    source,
                ),
                mock.patch.object(
                    ubuntu.subprocess,
                    "run",
                    side_effect=ubuntu.subprocess.CalledProcessError(
                        1,
                        "pam-auth-update",
                    ),
                ),
                self.assertRaises(DistributionError),
            ):
                ubuntu.DISTRIBUTION.reconcile_host_authentication(
                    rootfs,
                    False,
                )

            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                "canonical profile\n",
            )

    def test_ubuntu_apt_sources_keep_ports_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rootfs = Path(temporary)
            sources_path = rootfs / "etc" / "apt" / "sources.list"
            sources_path.parent.mkdir(parents=True)
            sources_path.write_text(
                "deb http://ports.ubuntu.com/ubuntu-ports noble main\n",
                encoding="utf-8",
            )

            ubuntu._configure_apt_sources(rootfs, "noble")

            self.assertEqual(
                (
                    rootfs
                    / "etc"
                    / "apt"
                    / "sources.list.d"
                    / "ubuntu.sources"
                ).read_text(encoding="utf-8"),
                "Types: deb\n"
                "URIs: http://ports.ubuntu.com/ubuntu-ports\n"
                "Suites: noble noble-updates noble-backports\n"
                "Components: main restricted universe multiverse\n"
                "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n"
                "\n"
                "Types: deb\n"
                "URIs: http://ports.ubuntu.com/ubuntu-ports\n"
                "Suites: noble-security\n"
                "Components: main restricted universe multiverse\n"
                "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n",
            )
            self.assertEqual(
                sources_path.read_text(encoding="utf-8"),
                "# Ubuntu sources have moved to "
                "/etc/apt/sources.list.d/ubuntu.sources\n",
            )

    def test_discover_home_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "Documents").mkdir()
            (home / ".ssh").mkdir()
            (home / "file.txt").write_text("not a folder")
            (home / ".hidden").write_text("hidden file")
            (home / "linked").symlink_to(home / "Documents", target_is_directory=True)
            (home / "linked-file").symlink_to(home / "file.txt")
            self.assertEqual(
                core.discover_home_folders(home),
                [
                    "Documents",
                    "Downloads",
                    "Projects",
                    ".bash_history",
                    ".hidden",
                    ".ssh/config",
                    ".zhistory",
                    "file.txt",
                ],
            )

    def test_new_space_defaults(self) -> None:
        (
            network,
            kernel_capabilities,
            devices,
            host_authentication,
            shortcuts,
            selected_home,
            administrator,
            desktop,
            credential_agents,
            mounted_drives,
        ) = core.defaults_from_info(
            None,
            core.Identity(1000, 1000, Path("/home/user")),
        )
        self.assertEqual(network, "basic")
        self.assertEqual(kernel_capabilities, "basic")
        self.assertEqual(devices, "basic")
        self.assertTrue(host_authentication)
        self.assertTrue(shortcuts)
        self.assertEqual(selected_home, ["Downloads"])
        self.assertNotIn(".bashrc", selected_home)
        self.assertNotIn(".zshrc", selected_home)
        self.assertTrue(administrator)
        self.assertTrue(desktop)
        self.assertFalse(credential_agents)
        self.assertTrue(mounted_drives)

    def test_permission_presets_have_expected_effective_values(self) -> None:
        self.assertEqual(
            core.PERMISSION_PRESETS,
            {
                "basic": {
                    "system": {
                        "network": "basic",
                        "kernel_capabilities": "basic",
                        "devices": "basic",
                        "host_authentication": True,
                        "shortcuts": True,
                    },
                    "user": {
                        "home": ["Downloads"],
                        "administrator": True,
                        "desktop": True,
                        "credential_agents": False,
                        "mounted_drives": True,
                    },
                },
                "develop": {
                    "system": {
                        "network": "admin",
                        "kernel_capabilities": "development",
                        "devices": "admin",
                        "host_authentication": True,
                        "shortcuts": True,
                    },
                    "user": {
                        "home": [
                            "Downloads",
                            "Projects",
                            ".bashrc",
                            ".zshrc",
                            ".bash_history",
                            ".zhistory",
                            ".ssh/config",
                        ],
                        "administrator": True,
                        "desktop": True,
                        "credential_agents": True,
                        "mounted_drives": True,
                    },
                },
            },
        )
        identity = core.Identity(1000, 1000, Path("/home/user"))
        info = core.create_info(
            "work",
            {"id": "custom"},
            identity,
            "advanced",
            ["Documents"],
            preset="develop",
        )

        self.assertEqual(
            core.effective_system_permissions(info["permissions"]["system"]),
            core.PERMISSION_PRESETS["develop"]["system"],
        )
        self.assertEqual(
            core.effective_user_permissions(
                info["permissions"]["users"]["1000"]
            ),
            core.PERMISSION_PRESETS["develop"]["user"],
        )
        self.assertEqual(core.selected_preset(info, identity), "develop")

    def test_legacy_and_unknown_presets(self) -> None:
        identity = core.Identity(1000, 1000, Path("/home/user"))
        info = core.create_info(
            "work", {"id": "custom"}, identity, "basic", []
        )
        del info["permissions"]["system"]["preset"]
        del info["permissions"]["users"]["1000"]["permissions"]["preset"]
        core.validate_info(info)
        self.assertEqual(core.selected_preset(info, identity), "custom")

        info["permissions"]["system"]["preset"] = "unknown"
        with self.assertRaises(core.SpacesError):
            core.validate_info(info)

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
        self.assertTrue(
            info["permissions"]["users"]["1000"]["permissions"]["administrator"]
        )
        self.assertTrue(
            info["permissions"]["system"]["host_authentication"]
        )
        self.assertTrue(info["permissions"]["system"]["shortcuts"])
        self.assertEqual(
            info["permissions"]["system"]["kernel_capabilities"],
            "basic",
        )
        self.assertEqual(
            info["permissions"]["system"]["devices"],
            "basic",
        )
        self.assertTrue(
            info["permissions"]["users"]["1000"]["permissions"]["desktop"]
        )
        self.assertTrue(
            info["permissions"]["users"]["1000"]["permissions"][
                "credential_agents"
            ]
        )
        self.assertTrue(
            info["permissions"]["users"]["1000"]["permissions"][
                "mounted_drives"
            ]
        )

    def test_existing_administrator_permission_is_used_as_default(self) -> None:
        identity = core.Identity(1000, 1000, Path("/home/user"))
        info = core.create_info(
            "work",
            {"id": "custom"},
            identity,
            "basic",
            [],
            administrator=False,
        )

        (
            _network,
            _kernel_capabilities,
            _devices,
            _host_auth,
            _shortcuts,
            _home,
            administrator,
            _desktop,
            _credential_agents,
            _mounted_drives,
        ) = core.defaults_from_info(info, identity)

        self.assertFalse(administrator)

    def test_missing_administrator_permission_defaults_to_true(self) -> None:
        identity = core.Identity(1000, 1000, Path("/home/user"))
        info = core.create_info(
            "work",
            {"id": "custom"},
            identity,
            "basic",
            [],
        )
        del info["permissions"]["users"]["1000"]["permissions"]["administrator"]

        core.validate_info(info)
        (
            _network,
            _kernel_capabilities,
            _devices,
            _host_auth,
            _shortcuts,
            _home,
            administrator,
            _desktop,
            _credential_agents,
            _mounted_drives,
        ) = core.defaults_from_info(info, identity)

        self.assertTrue(administrator)

    def test_missing_host_authentication_defaults_to_true(self) -> None:
        identity = core.Identity(1000, 1000, Path("/home/user"))
        info = core.create_info(
            "work", {"id": "custom"}, identity, "basic", []
        )
        del info["permissions"]["system"]["host_authentication"]

        core.validate_info(info)
        (
            _network,
            _kernel_capabilities,
            _devices,
            host_auth,
            _shortcuts,
            _home,
            _administrator,
            _desktop,
            _credential_agents,
            _mounted_drives,
        ) = core.defaults_from_info(info, identity)

        self.assertTrue(host_auth)

    def test_non_boolean_host_authentication_is_rejected(self) -> None:
        info = core.create_info(
            "work",
            {"id": "custom"},
            core.Identity(1000, 1000, Path("/home/user")),
            "basic",
            [],
        )
        info["permissions"]["system"]["host_authentication"] = 1

        with self.assertRaises(core.SpacesError):
            core.validate_info(info)

    def test_credential_agent_permission_defaults_validates_and_is_preserved(
        self,
    ) -> None:
        identity = core.Identity(1000, 1000, Path("/home/user"))
        info = core.create_info(
            "work",
            {"id": "custom"},
            identity,
            "basic",
            [],
            credential_agents=False,
        )
        self.assertFalse(core.defaults_from_info(info, identity)[8])

        del info["permissions"]["users"]["1000"]["permissions"][
            "credential_agents"
        ]
        core.validate_info(info)
        self.assertTrue(core.defaults_from_info(info, identity)[8])

        info["permissions"]["users"]["1000"]["permissions"][
            "credential_agents"
        ] = 1
        with self.assertRaises(core.SpacesError):
            core.validate_info(info)

    def test_mounted_drives_permission_defaults_validates_and_is_preserved(
        self,
    ) -> None:
        identity = core.Identity(1000, 1000, Path("/home/user"))
        info = core.create_info(
            "work",
            {"id": "custom"},
            identity,
            "basic",
            [],
            mounted_drives=False,
        )
        self.assertFalse(core.defaults_from_info(info, identity)[9])

        del info["permissions"]["users"]["1000"]["permissions"][
            "mounted_drives"
        ]
        core.validate_info(info)
        self.assertTrue(core.defaults_from_info(info, identity)[9])

        info["permissions"]["users"]["1000"]["permissions"][
            "mounted_drives"
        ] = 1
        with self.assertRaises(core.SpacesError):
            core.validate_info(info)

    def test_non_boolean_administrator_permission_is_rejected(self) -> None:
        info = core.create_info(
            "work",
            {"id": "custom"},
            core.Identity(1000, 1000, Path("/home/user")),
            "basic",
            [],
        )
        info["permissions"]["users"]["1000"]["permissions"]["administrator"] = 1

        with self.assertRaises(core.SpacesError):
            core.validate_info(info)

    def test_desktop_permission_defaults_true_and_requires_boolean(self) -> None:
        identity = core.Identity(1000, 1000, Path("/home/user"))
        info = core.create_info(
            "work", {"id": "custom"}, identity, "basic", [], desktop=False
        )
        self.assertFalse(core.defaults_from_info(info, identity)[7])
        del info["permissions"]["users"]["1000"]["permissions"]["desktop"]
        core.validate_info(info)
        self.assertTrue(core.defaults_from_info(info, identity)[7])
        info["permissions"]["users"]["1000"]["permissions"]["desktop"] = 1
        with self.assertRaises(core.SpacesError):
            core.validate_info(info)

    def test_shortcuts_permission_defaults_true_and_requires_boolean(self) -> None:
        identity = core.Identity(1000, 1000, Path("/home/user"))
        info = core.create_info(
            "work",
            {"id": "custom"},
            identity,
            "basic",
            [],
            shortcuts=False,
        )
        self.assertFalse(core.defaults_from_info(info, identity)[4])
        del info["permissions"]["system"]["shortcuts"]
        core.validate_info(info)
        self.assertTrue(core.defaults_from_info(info, identity)[4])
        info["permissions"]["system"]["shortcuts"] = 1
        with self.assertRaises(core.SpacesError):
            core.validate_info(info)

    def test_device_permission_defaults_validates_and_is_preserved(self) -> None:
        identity = core.Identity(1000, 1000, Path("/home/user"))
        info = core.create_info(
            "work",
            {"id": "custom"},
            identity,
            "basic",
            [],
            devices="admin",
        )
        self.assertEqual(core.defaults_from_info(info, identity)[2], "admin")

        del info["permissions"]["system"]["devices"]
        core.validate_info(info)
        self.assertEqual(core.defaults_from_info(info, identity)[2], "basic")

        info["permissions"]["system"]["devices"] = "unknown"
        with self.assertRaises(core.SpacesError):
            core.validate_info(info)

    def test_kernel_capabilities_default_validates_and_is_preserved(self) -> None:
        identity = core.Identity(1000, 1000, Path("/home/user"))
        info = core.create_info(
            "work",
            {"id": "custom"},
            identity,
            "basic",
            [],
            kernel_capabilities="admin",
        )
        self.assertEqual(
            core.defaults_from_info(info, identity)[1],
            "admin",
        )

        del info["permissions"]["system"]["kernel_capabilities"]
        core.validate_info(info)
        self.assertEqual(core.defaults_from_info(info, identity)[1], "basic")

        info["permissions"]["system"]["kernel_capabilities"] = "unknown"
        with self.assertRaises(core.SpacesError):
            core.validate_info(info)

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

    def test_create_request_extracts_boolean_purge_option(self) -> None:
        info = core.create_info(
            "work",
            {"id": "custom"},
            core.Identity(1000, 1000, Path("/home/user")),
            "basic",
            [],
        )
        validated, purge = core.validate_create_request(
            {**info, "purge": True}
        )
        self.assertTrue(purge)
        self.assertNotIn("purge", validated)

        with self.assertRaises(core.SpacesError):
            core.validate_create_request({**info, "purge": "yes"})

    def test_delete_request_validates_optional_purge_option(self) -> None:
        self.assertEqual(
            core.validate_delete_request({"name": "work"}),
            {"name": "work"},
        )
        self.assertEqual(
            core.validate_delete_request({"name": "work", "purge": True}),
            {"name": "work", "purge": True},
        )
        for request in (
            {"name": "work", "purge": "yes"},
            {"name": "work", "unknown": False},
            {},
        ):
            with self.subTest(request=request), self.assertRaises(
                core.SpacesError
            ):
                core.validate_delete_request(request)

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

    def test_hidden_files_and_ssh_config_are_valid_home_permissions(self) -> None:
        info = core.create_info(
            "work",
            {"id": "custom"},
            core.Identity(1000, 1000, Path("/home/user")),
            "basic",
            [".bashrc", ".ssh/config"],
        )
        self.assertEqual(
            info["permissions"]["users"]["1000"]["permissions"]["home"],
            [".bashrc", ".ssh/config"],
        )

    def test_other_nested_home_permissions_are_rejected(self) -> None:
        info = core.create_info(
            "work",
            {"id": "custom"},
            core.Identity(1000, 1000, Path("/home/user")),
            "basic",
            [],
        )
        info["permissions"]["users"]["1000"]["permissions"]["home"] = [
            ".ssh/id_ed25519"
        ]
        with self.assertRaises(core.SpacesError):
            core.validate_info(info)

    def test_load_info_returns_none_for_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "info.json"
            path.write_text("{broken", encoding="utf-8")
            self.assertIsNone(core.load_info(path))


if __name__ == "__main__":
    unittest.main()
