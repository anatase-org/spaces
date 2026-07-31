from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spaces.distro import arch, fedora, get_driver, kali, mounts, ubuntu
from spaces.distro.model import DistributionError
from spaces.distro.pam import SPACES_PAM_BLOCK


ROOT = Path(__file__).resolve().parents[1]


class DistributionDriverTests(unittest.TestCase):
    def test_configured_packages_append_without_changing_builtin_tuples(
        self,
    ) -> None:
        snapshots = {
            "arch": arch.PACKAGES,
            "fedora": fedora.PACKAGES,
            "ubuntu": ubuntu.PACKAGES,
            "kali": kali.PACKAGES,
        }
        arch_command = arch.DISTRIBUTION.command(
            {"id": "arch", "options": []},
            Path("/rootfs"),
            ("screen",),
        )
        fedora_command = fedora.DISTRIBUTION.command(
            {"id": "fedora", "version": "44"},
            Path("/rootfs"),
            ("tmux",),
        )
        self.assertEqual(arch_command[-1], "screen")
        self.assertEqual(fedora_command[-1], "tmux")
        self.assertEqual(arch.PACKAGES, snapshots["arch"])
        self.assertEqual(fedora.PACKAGES, snapshots["fedora"])
        self.assertEqual(ubuntu.PACKAGES, snapshots["ubuntu"])
        self.assertEqual(kali.PACKAGES, snapshots["kali"])

    def test_desktop_drivers_install_xdg_mime_detector(self) -> None:
        for packages in (
            arch.PACKAGES,
            fedora.PACKAGES,
            kali.PACKAGES,
            ubuntu.PACKAGES,
        ):
            self.assertIn("file", packages)

    def test_desktop_drivers_install_gsettings_backend(self) -> None:
        for packages, backend in (
            (arch.PACKAGES, "dconf"),
            (fedora.PACKAGES, "dconf"),
            (kali.PACKAGES, "dconf-gsettings-backend"),
            (ubuntu.PACKAGES, "dconf-gsettings-backend"),
        ):
            self.assertIn(backend, packages)

    def test_managed_distros_install_git(self) -> None:
        for packages in (
            arch.PACKAGES,
            fedora.PACKAGES,
            kali.PACKAGES,
            ubuntu.PACKAGES,
        ):
            self.assertIn("git", packages)

    def test_drivers_are_registered_and_describe_metadata(self) -> None:
        self.assertIs(get_driver("arch"), arch.DISTRIBUTION)
        self.assertIs(get_driver("fedora"), fedora.DISTRIBUTION)
        self.assertIs(get_driver("kali"), kali.DISTRIBUTION)
        self.assertEqual(
            arch.DISTRIBUTION.describe({"id": "arch", "options": ["yay"]}),
            "Arch Linux",
        )
        self.assertEqual(
            fedora.DISTRIBUTION.describe({"id": "fedora", "version": "44"}),
            "Fedora 44",
        )
        self.assertEqual(kali.DISTRIBUTION.describe({"id": "kali"}), "Kali Linux")

    def test_fedora_configuration_and_command(self) -> None:
        driver = fedora.DISTRIBUTION
        self.assertEqual(driver.choices(), [("44", "44")])
        self.assertEqual(driver.default_option, "44")
        self.assertEqual(driver.metadata("44"), {"id": "fedora", "version": "44"})
        command = driver.command(
            {"id": "fedora", "version": "44"},
            Path("/rootfs"),
        )
        self.assertEqual(command[:4], [
            "dnf5",
            "--assumeyes",
            "--installroot=/rootfs",
            "--releasever=44",
        ])
        self.assertIn("--setopt=install_weak_deps=False", command)
        self.assertIn("--setopt=tsflags=nocontexts", command)
        self.assertIn(
            "--setopt=reposdir=/usr/share/spaces/repos",
            command,
        )
        self.assertIn("fedora-release-container", command)
        self.assertIn("dnf5", command)
        self.assertIn("systemd-pam", command)
        self.assertIn("dbus-tools", command)
        self.assertIn("qca-qt6-ossl", command)
        self.assertIn("qt6-qtwayland", command)
        with self.assertRaises(DistributionError):
            driver.validate({"id": "fedora", "version": "45"})

    def test_arch_options_are_plural_optional_and_default_to_yay(self) -> None:
        driver = arch.DISTRIBUTION
        self.assertTrue(driver.multiple_options)
        self.assertEqual(driver.option_key, "options")
        self.assertEqual(
            driver.choices(),
            [
                ("yay — AUR helper (built from community source)", "yay"),
                ("Shelly — graphical package manager", "shelly"),
            ],
        )
        self.assertEqual(driver.default_options, ("yay",))
        self.assertEqual(driver.selected_options(None), ["yay"])
        self.assertEqual(
            driver.metadata(["yay", "shelly"]),
            {"id": "arch", "options": ["yay", "shelly"]},
        )
        self.assertEqual(driver.metadata([]), {"id": "arch", "options": []})
        self.assertEqual(
            driver.selected_options({"id": "arch", "options": []}),
            [],
        )
        with self.assertRaises(DistributionError):
            driver.validate({"id": "arch", "options": ["yay", "yay"]})
        with self.assertRaises(DistributionError):
            driver.validate({"id": "arch", "options": ["paru"]})
        with self.assertRaises(DistributionError):
            driver.validate({"id": "arch", "packages": ["yay"]})

    def test_arch_command_installs_aur_build_dependencies_only_when_selected(
        self,
    ) -> None:
        yay = arch.DISTRIBUTION.command(
            {"id": "arch", "options": ["yay"]}, Path("/rootfs")
        )
        shelly = arch.DISTRIBUTION.command(
            {"id": "arch", "options": ["shelly"]}, Path("/rootfs")
        )
        opted_out = arch.DISTRIBUTION.command(
            {"id": "arch", "options": []},
            Path("/rootfs"),
        )
        self.assertEqual(yay[:3], ["pacstrap", "-K", "/rootfs"])
        self.assertTrue(set(arch.AUR_BUILD_PACKAGES).issubset(yay))
        self.assertTrue(set(arch.AUR_BUILD_PACKAGES).issubset(shelly))
        self.assertTrue(set(arch.AUR_BUILD_PACKAGES).isdisjoint(opted_out))
        for package in (
            "base",
            "sudo",
            "polkit",
            "pipewire",
            "xdg-desktop-portal-kde",
            "kwallet",
        ):
            self.assertIn(package, yay)

    def test_kali_has_no_configuration_and_only_id_metadata(self) -> None:
        driver = kali.DISTRIBUTION
        self.assertEqual(driver.choices(), [])
        self.assertIsNone(driver.option_key)
        self.assertEqual(driver.metadata(None), {"id": "kali"})
        driver.validate({"id": "kali"})
        command = driver.command(
            {"id": "kali"},
            Path("/rootfs"),
            Path("/keyring.gpg"),
        )
        self.assertEqual(
            command,
            [
                "debootstrap",
                "--force-check-gpg",
                "--keyring=/keyring.gpg",
                "kali-rolling",
                "/rootfs",
                "http://http.kali.org/kali",
            ],
        )

    def test_ubuntu_skips_unavailable_configured_packages(self) -> None:
        metadata = {"id": "ubuntu", "version": "noble"}
        with tempfile.TemporaryDirectory() as temporary:
            rootfs = Path(temporary) / "rootfs"
            rootfs.mkdir()
            commands: list[list[str]] = []

            def run(
                command: list[str],
                **_kwargs: object,
            ) -> subprocess.CompletedProcess:
                commands.append(command)
                return subprocess.CompletedProcess(
                    command,
                    1
                    if "apt-cache" in command and command[-1] == "fastfetch"
                    else 0,
                )

            with mock.patch.object(ubuntu.subprocess, "run", side_effect=run):
                ubuntu.DISTRIBUTION.bootstrap(
                    metadata,
                    rootfs,
                    ("fastfetch", "screen"),
                )

        package_checks = [
            command
            for command in commands
            if "apt-cache" in command
        ]
        self.assertEqual(
            [command[-1] for command in package_checks],
            ["fastfetch", "screen"],
        )
        install = next(
            command
            for command in commands
            if "apt-get" in command and "install" in command
        )
        self.assertNotIn("fastfetch", install)
        self.assertIn("screen", install)
        self.assertTrue(set(ubuntu.PACKAGES).issubset(install))
        self.assertTrue(set(ubuntu.SECRET_PACKAGES["noble"]).issubset(install))

    def test_fedora_bootstrap_uses_driver_command(self) -> None:
        metadata = {"id": "fedora", "version": "44"}
        with tempfile.TemporaryDirectory() as temporary:
            rootfs = Path(temporary) / "rootfs"
            rootfs.mkdir()
            with mock.patch.object(fedora.subprocess, "run") as run:
                fedora.DISTRIBUTION.bootstrap(metadata, rootfs)
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                [
                    "mount",
                    "--types",
                    "proc",
                    "--options",
                    "nosuid,noexec,nodev",
                    "proc",
                    str(rootfs / "proc"),
                ],
                [
                    "mount",
                    "--types",
                    "sysfs",
                    "--options",
                    "ro,nosuid,noexec,nodev",
                    "sysfs",
                    str(rootfs / "sys"),
                ],
                fedora.DISTRIBUTION.command(metadata, rootfs),
                ["umount", str(rootfs / "sys")],
                ["umount", str(rootfs / "proc")],
            ],
        )
        self.assertEqual(
            Path(run.call_args_list[2].kwargs["env"]["XDG_CONFIG_HOME"]).parent,
            rootfs,
        )
        self.assertTrue(run.call_args_list[2].kwargs["check"])
        self.assertTrue(all(
            call.kwargs == {"check": True}
            for index, call in enumerate(run.call_args_list)
            if index != 2
        ))

    def test_fedora_rpm_environment_uses_guest_database_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rootfs = Path(temporary) / "rootfs"
            rootfs.mkdir()
            with fedora._rpm_environment(rootfs) as environment:
                config = Path(environment["XDG_CONFIG_HOME"])
                self.assertEqual(config.parent, rootfs)
                self.assertEqual(
                    (config / "rpm/macros").read_text(encoding="utf-8"),
                    "%_dbpath /usr/lib/sysimage/rpm\n",
                )
                self.assertEqual(
                    {
                        key: value
                        for key, value in environment.items()
                        if key != "XDG_CONFIG_HOME"
                    },
                    {
                        key: value
                        for key, value in os.environ.items()
                        if key != "XDG_CONFIG_HOME"
                    },
                )
            self.assertFalse(config.exists())

    def test_fedora_bootstrap_unmounts_api_filesystems_after_failure(self) -> None:
        metadata = {"id": "fedora", "version": "44"}
        with tempfile.TemporaryDirectory() as temporary:
            rootfs = Path(temporary) / "rootfs"
            rootfs.mkdir()
            commands: list[list[str]] = []

            def run(
                command: list[str],
                *,
                check: bool,
                env: dict[str, str] | None = None,
            ) -> subprocess.CompletedProcess:
                commands.append(command)
                if command[0] == "dnf5":
                    self.assertIsNotNone(env)
                    raise subprocess.CalledProcessError(1, command)
                return subprocess.CompletedProcess(command, 0)

            with (
                mock.patch.object(fedora.subprocess, "run", side_effect=run),
                self.assertRaises(subprocess.CalledProcessError),
            ):
                fedora.DISTRIBUTION.bootstrap(metadata, rootfs)

        self.assertEqual(commands[-2:], [
            ["umount", str(rootfs / "sys")],
            ["umount", str(rootfs / "proc")],
        ])

    def test_fedora_bootstrap_hides_host_selinux_mount(self) -> None:
        with (
            mock.patch.object(Path, "is_mount", return_value=True),
            mock.patch.object(mounts.os, "open", return_value=17) as open_namespace,
            mock.patch.object(mounts.os, "unshare") as unshare,
            mock.patch.object(mounts.os, "setns") as setns,
            mock.patch.object(mounts.os, "close") as close,
            mock.patch.object(mounts.subprocess, "run") as run,
        ):
            with mounts.hidden_selinuxfs():
                pass

        open_namespace.assert_called_once_with(
            "/proc/self/ns/mnt",
            mounts.os.O_RDONLY | mounts.os.O_CLOEXEC,
        )
        unshare.assert_called_once_with(mounts.os.CLONE_NEWNS)
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["mount", "--make-rprivate", "/"],
                ["umount", "/sys/fs/selinux"],
            ],
        )
        self.assertTrue(
            all(call.kwargs == {"check": True} for call in run.call_args_list)
        )
        setns.assert_called_once_with(17, mounts.os.CLONE_NEWNS)
        close.assert_called_once_with(17)

    def test_rootfs_mountpoint_is_private_and_temporary(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(mounts.os, "open", return_value=17) as open_namespace,
            mock.patch.object(mounts.os, "unshare") as unshare,
            mock.patch.object(mounts.os, "setns") as setns,
            mock.patch.object(mounts.os, "close") as close,
            mock.patch.object(mounts.subprocess, "run") as run,
        ):
            rootfs = Path(temporary)
            with mounts.mounted_rootfs(rootfs, "Arch"):
                pass

        open_namespace.assert_called_once_with(
            "/proc/self/ns/mnt",
            mounts.os.O_RDONLY | mounts.os.O_CLOEXEC,
        )
        unshare.assert_called_once_with(mounts.os.CLONE_NEWNS)
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["mount", "--make-rprivate", "/"],
                ["mount", "--bind", str(rootfs), str(rootfs)],
            ],
        )
        setns.assert_called_once_with(17, mounts.os.CLONE_NEWNS)
        close.assert_called_once_with(17)

    def test_arch_bootstrap_honors_aur_package_opt_out(self) -> None:
        with (
            mock.patch.object(arch.subprocess, "run") as run,
            mock.patch.object(arch, "_install_aur_package") as install,
        ):
            arch.DISTRIBUTION.bootstrap(
                {"id": "arch", "options": []},
                Path("/rootfs"),
            )
        run.assert_called_once()
        install.assert_not_called()

    def test_arch_bootstrap_builds_each_selected_aur_package(self) -> None:
        with (
            mock.patch.object(arch.subprocess, "run"),
            mock.patch.object(arch, "_install_aur_package") as install,
            mock.patch.object(arch, "mounted_rootfs") as mounted_rootfs,
        ):
            arch.DISTRIBUTION.bootstrap(
                {"id": "arch", "options": ["yay", "shelly"]},
                Path("/rootfs"),
            )
        self.assertEqual(
            install.call_args_list,
            [
                mock.call(
                    Path("/rootfs"),
                    "yay",
                    arch.AUR_REPOSITORIES["yay"],
                ),
                mock.call(
                    Path("/rootfs"),
                    "shelly",
                    arch.AUR_REPOSITORIES["shelly"],
                ),
            ],
        )
        mounted_rootfs.assert_called_once_with(Path("/rootfs"), "Arch")

    def test_shelly_does_not_depend_on_yay_being_selected(self) -> None:
        with (
            mock.patch.object(arch.subprocess, "run"),
            mock.patch.object(arch, "_install_aur_package") as install,
            mock.patch.object(arch, "mounted_rootfs"),
        ):
            arch.DISTRIBUTION.bootstrap(
                {"id": "arch", "options": ["shelly"]},
                Path("/rootfs"),
            )
        install.assert_called_once_with(
            Path("/rootfs"),
            "shelly",
            arch.AUR_REPOSITORIES["shelly"],
        )

    def test_aur_packages_are_built_unprivileged_and_installed_by_name(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rootfs = Path(temporary)
            (rootfs / "etc" / "sudoers.d").mkdir(parents=True)
            commands: list[list[str]] = []

            def run(
                command: list[str],
                *,
                check: bool,
                stdout: int | None = None,
                text: bool = False,
            ) -> subprocess.CompletedProcess:
                commands.append(command)
                if "/usr/bin/useradd" in command:
                    (rootfs / "home" / arch.BUILDER).mkdir(parents=True)
                if "/usr/bin/makepkg" in command:
                    package_directory = (
                        rootfs / "home" / arch.BUILDER / "packages"
                    )
                    package_directory.mkdir()
                    (package_directory / "shelly-1.0-1-x86_64.pkg.tar.zst").touch()
                    (
                        package_directory
                        / "shelly-flatpak-backend-1.0-1-x86_64.pkg.tar.zst"
                    ).touch()
                output = None
                if stdout == subprocess.PIPE:
                    output = (
                        "shelly-flatpak-backend\n"
                        if "shelly-flatpak-backend-" in command[-1]
                        else "shelly\n"
                    )
                return subprocess.CompletedProcess(command, 0, stdout=output)

            with mock.patch.object(arch.subprocess, "run", side_effect=run):
                arch._install_aur_package(
                    rootfs,
                    "shelly",
                    arch.AUR_REPOSITORIES["shelly"],
                )

            self.assertFalse((rootfs / arch.AUR_SUDOERS_DROP_IN).exists())
            self.assertFalse((rootfs / "home" / arch.BUILDER).exists())

        self.assertTrue(
            all(command[0] == "arch-chroot" for command in commands)
        )
        self.assertTrue(all("-S" not in command for command in commands))
        self.assertEqual(commands[1][0:3], ["arch-chroot", "-u", arch.BUILDER])
        self.assertIn("/usr/bin/mkdir", commands[1])
        self.assertEqual(commands[3][0:3], ["arch-chroot", "-u", arch.BUILDER])
        self.assertIn("HOME=/home/spaces-build", commands[3])
        self.assertIn("TMPDIR=/home/spaces-build/.tmp", commands[3])
        self.assertIn("PKGDEST=/home/spaces-build/packages", commands[3])
        self.assertIn("/usr/bin/git", commands[3])
        self.assertIn(arch.AUR_REPOSITORIES["shelly"], commands[3])
        self.assertEqual(commands[4][0:3], ["arch-chroot", "-u", arch.BUILDER])
        self.assertIn("HOME=/home/spaces-build", commands[4])
        self.assertIn("/usr/bin/makepkg", commands[4])
        self.assertIn("--syncdeps", commands[4])
        self.assertIn("--rmdeps", commands[4])
        self.assertNotIn("/usr/bin/runuser", commands[3])
        install = next(command for command in commands if "-U" in command)
        self.assertIn("shelly-1.0-1-x86_64.pkg.tar.zst", install[-1])
        self.assertNotIn("flatpak-backend", install[-1])
        self.assertEqual(commands[-1][-2:], ["/usr/bin/userdel", arch.BUILDER])

    def test_aur_builder_is_removed_after_failure(self) -> None:
        commands: list[list[str]] = []

        def run(command: list[str], *, check: bool) -> subprocess.CompletedProcess:
            commands.append(command)
            if "/usr/bin/git" in command:
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0)

        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(arch.subprocess, "run", side_effect=run),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            rootfs = Path(temporary)
            (rootfs / "etc" / "sudoers.d").mkdir(parents=True)
            arch._install_aur_package(
                rootfs,
                "yay",
                arch.AUR_REPOSITORIES["yay"],
            )
        self.assertEqual(commands[-1][-2:], ["/usr/bin/userdel", arch.BUILDER])

    def test_kali_bootstrap_orders_signed_base_sources_and_default_tools(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rootfs = Path(temporary) / "rootfs"
            commands: list[list[str]] = []

            def run(command: list[str], *, check: bool) -> subprocess.CompletedProcess:
                commands.append(command)
                if command[0] == "debootstrap":
                    keyring = Path(command[2].split("=", 1)[1])
                    self.assertTrue(keyring.is_file())
                    (rootfs / "etc" / "apt").mkdir(parents=True)
                    (rootfs / "usr" / "sbin").mkdir(parents=True)
                    (rootfs / "proc").mkdir()
                if command[0] == "chroot":
                    self.assertTrue((rootfs / kali.POLICY_RC_D).is_file())
                return subprocess.CompletedProcess(command, 0)

            with (
                mock.patch.object(
                    kali,
                    "HOST_KEYRING",
                    ROOT / "data" / "keys" / "kali-archive-key.gpg.base64",
                ),
                mock.patch.object(kali.subprocess, "run", side_effect=run),
            ):
                kali.DISTRIBUTION.bootstrap({"id": "kali"}, rootfs)

            self.assertEqual(commands[0][0:2], ["debootstrap", "--force-check-gpg"])
            self.assertEqual(
                commands[1],
                [
                    "mount",
                    "--types",
                    "proc",
                    "--options",
                    "nosuid,noexec,nodev",
                    "proc",
                    str(rootfs / "proc"),
                ],
            )
            self.assertEqual(
                commands[2],
                [
                    "mount",
                    "--types",
                    "sysfs",
                    "--options",
                    "ro,nosuid,noexec,nodev",
                    "sysfs",
                    str(rootfs / "sys"),
                ],
            )
            self.assertEqual(
                commands[3],
                kali._chroot_command(rootfs, "apt-get", "update"),
            )
            self.assertIn("--no-install-recommends", commands[4])
            self.assertIn("kwallet6", commands[4])
            self.assertIn("libqca-qt6-plugins", commands[4])
            self.assertIn("qt6-wayland", commands[4])
            self.assertEqual(commands[5][-2:], ["--yes", "kali-linux-default"])
            self.assertEqual(commands[6], ["umount", str(rootfs / "sys")])
            self.assertEqual(commands[7], ["umount", str(rootfs / "proc")])
            self.assertFalse((rootfs / kali.POLICY_RC_D).exists())
            self.assertEqual(
                (
                    rootfs / "etc" / "apt" / "sources.list.d" / "kali.sources"
                ).read_text(encoding="utf-8"),
                "Types: deb\n"
                "URIs: http://http.kali.org/kali/\n"
                "Suites: kali-rolling\n"
                "Components: main contrib non-free non-free-firmware\n"
                "Signed-By: /usr/share/keyrings/kali-archive-keyring.gpg\n",
            )

    def test_kali_bootstrap_unmounts_proc_after_package_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rootfs = Path(temporary) / "rootfs"
            commands: list[list[str]] = []

            def run(command: list[str], *, check: bool) -> subprocess.CompletedProcess:
                commands.append(command)
                if command[0] == "debootstrap":
                    (rootfs / "etc" / "apt").mkdir(parents=True)
                    (rootfs / "usr" / "sbin").mkdir(parents=True)
                    (rootfs / "proc").mkdir()
                if "--no-install-recommends" in command:
                    raise subprocess.CalledProcessError(1, command)
                return subprocess.CompletedProcess(command, 0)

            with (
                mock.patch.object(
                    kali,
                    "HOST_KEYRING",
                    ROOT / "data" / "keys" / "kali-archive-key.gpg.base64",
                ),
                mock.patch.object(kali.subprocess, "run", side_effect=run),
                self.assertRaises(subprocess.CalledProcessError),
            ):
                kali.DISTRIBUTION.bootstrap({"id": "kali"}, rootfs)

            self.assertEqual(commands[-2:], [
                ["umount", str(rootfs / "sys")],
                ["umount", str(rootfs / "proc")],
            ])
            self.assertFalse((rootfs / kali.POLICY_RC_D).exists())


class AuthenticationTests(unittest.TestCase):
    def test_kali_uses_its_packaged_pam_auth_update_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rootfs = root / "rootfs"
            profile_directory = rootfs / "usr" / "share" / "pam-configs"
            profile_directory.mkdir(parents=True)
            source = root / "spaces.kali"
            source.write_text("Name: Kali Spaces auth\n", encoding="utf-8")

            with (
                mock.patch.object(kali, "HOST_AUTHENTICATION_PROFILE", source),
                mock.patch.object(kali.subprocess, "run") as run,
            ):
                self.assertTrue(
                    kali.DISTRIBUTION.reconcile_host_authentication(rootfs, True)
                )
                destination = profile_directory / "spaces"
                self.assertEqual(
                    destination.read_text(encoding="utf-8"),
                    "Name: Kali Spaces auth\n",
                )
                self.assertTrue(
                    kali.DISTRIBUTION.reconcile_host_authentication(rootfs, False)
                )

            self.assertFalse(destination.exists())
            self.assertEqual(run.call_count, 2)

    def test_arch_pam_rule_is_atomic_idempotent_and_reversible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rootfs = Path(temporary)
            system_auth = rootfs / arch.SYSTEM_AUTH
            system_auth.parent.mkdir(parents=True)
            (rootfs / arch.SUDOERS_DROP_IN.parent).mkdir(parents=True)
            original = "#%PAM-1.0\nauth required pam_unix.so\n"
            system_auth.write_text(original, encoding="utf-8")

            self.assertTrue(
                arch.DISTRIBUTION.reconcile_host_authentication(rootfs, True)
            )
            enabled = system_auth.read_text(encoding="utf-8")
            self.assertEqual(enabled.count(SPACES_PAM_BLOCK), 1)
            self.assertIn("auth required pam_unix.so\n", enabled)
            sudoers = rootfs / arch.SUDOERS_DROP_IN
            self.assertEqual(sudoers.read_bytes(), arch.SUDOERS_CONTENT)
            self.assertEqual(sudoers.stat().st_mode & 0o777, 0o440)
            self.assertTrue(
                arch.DISTRIBUTION.reconcile_host_authentication(rootfs, True)
            )
            self.assertEqual(system_auth.read_text(encoding="utf-8"), enabled)
            self.assertTrue(
                arch.DISTRIBUTION.reconcile_host_authentication(rootfs, False)
            )
            self.assertEqual(system_auth.read_text(encoding="utf-8"), original)
            self.assertEqual(sudoers.read_bytes(), arch.SUDOERS_CONTENT)

    def test_arch_pam_rejects_partial_or_unsafe_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rootfs = root / "rootfs"
            system_auth = rootfs / arch.SYSTEM_AUTH
            system_auth.parent.mkdir(parents=True)
            system_auth.write_text(
                "# Managed by Spaces: host authentication\n"
                "auth required pam_unix.so\n",
                encoding="utf-8",
            )
            with self.assertRaises(DistributionError):
                arch.DISTRIBUTION.reconcile_host_authentication(rootfs, True)

            system_auth.unlink()
            outside = root / "outside"
            outside.write_text("auth required pam_unix.so\n", encoding="utf-8")
            system_auth.symlink_to(outside)
            with self.assertRaises(DistributionError):
                arch.DISTRIBUTION.reconcile_host_authentication(rootfs, True)

    def test_arch_sudoers_rejects_unsafe_managed_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rootfs = root / "rootfs"
            destination = rootfs / arch.SUDOERS_DROP_IN
            destination.parent.mkdir(parents=True)
            outside = root / "outside"
            outside.write_text("unrelated\n", encoding="utf-8")
            destination.symlink_to(outside)

            with self.assertRaises(DistributionError):
                arch._configure_sudoers(rootfs)
            self.assertEqual(outside.read_text(encoding="utf-8"), "unrelated\n")

    def test_fedora_authselect_preserves_selection_and_is_reversible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rootfs = Path(temporary)
            (rootfs / "etc" / "authselect" / "custom").mkdir(parents=True)
            (rootfs / "var" / "lib").mkdir(parents=True)
            commands: list[tuple[str, ...]] = []

            def authselect(
                current_rootfs: Path,
                *arguments: str,
                capture: bool = False,
            ) -> str:
                self.assertEqual(current_rootfs, rootfs)
                commands.append(arguments)
                if arguments == ("current", "--raw"):
                    self.assertTrue(capture)
                    return "minimal with-faillock"
                if arguments[:2] == ("create-profile", "spaces"):
                    profile = rootfs / fedora.AUTHSELECT_PROFILE
                    profile.mkdir()
                    for name in fedora.AUTHSELECT_FILES:
                        (profile / name).write_text(
                            "#%PAM-1.0\nauth required pam_unix.so\n",
                            encoding="utf-8",
                        )
                return ""

            with mock.patch.object(fedora, "_authselect", side_effect=authselect):
                self.assertTrue(
                    fedora.DISTRIBUTION.reconcile_host_authentication(rootfs, True)
                )
                self.assertTrue(
                    fedora.DISTRIBUTION.reconcile_host_authentication(rootfs, True)
                )
                for name in fedora.AUTHSELECT_FILES:
                    content = (
                        rootfs / fedora.AUTHSELECT_PROFILE / name
                    ).read_text(encoding="utf-8")
                    self.assertEqual(content.count(SPACES_PAM_BLOCK), 1)
                    self.assertIn("pam_unix.so", content)
                self.assertEqual(
                    json.loads(
                        (rootfs / fedora.AUTHSELECT_STATE).read_text(
                            encoding="utf-8"
                        )
                    ),
                    {"selection": ["minimal", "with-faillock"]},
                )
                self.assertTrue(
                    fedora.DISTRIBUTION.reconcile_host_authentication(rootfs, False)
                )

            self.assertEqual(
                commands.count(("select", "custom/spaces", "with-faillock", "--force")),
                2,
            )
            self.assertIn(("select", "minimal", "with-faillock", "--force"), commands)
            self.assertFalse((rootfs / fedora.AUTHSELECT_STATE).exists())
            self.assertFalse((rootfs / fedora.AUTHSELECT_PROFILE).exists())

    def test_fedora_authselect_rolls_back_failed_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rootfs = Path(temporary)
            (rootfs / "etc" / "authselect" / "custom").mkdir(parents=True)
            (rootfs / "var" / "lib").mkdir(parents=True)
            commands: list[tuple[str, ...]] = []

            def authselect(
                unused_rootfs: Path,
                *arguments: str,
                capture: bool = False,
            ) -> str:
                commands.append(arguments)
                if arguments == ("current", "--raw"):
                    return "sssd with-mkhomedir"
                if arguments[:2] == ("create-profile", "spaces"):
                    profile = rootfs / fedora.AUTHSELECT_PROFILE
                    profile.mkdir()
                    for name in fedora.AUTHSELECT_FILES:
                        (profile / name).write_text("#%PAM-1.0\n", encoding="utf-8")
                if arguments[:2] == ("select", "custom/spaces"):
                    raise subprocess.CalledProcessError(1, arguments)
                return ""

            with (
                mock.patch.object(fedora, "_authselect", side_effect=authselect),
                self.assertRaises(DistributionError),
            ):
                fedora.DISTRIBUTION.reconcile_host_authentication(rootfs, True)

            self.assertIn(("select", "sssd", "with-mkhomedir", "--force"), commands)
            self.assertFalse((rootfs / fedora.AUTHSELECT_STATE).exists())
            self.assertFalse((rootfs / fedora.AUTHSELECT_PROFILE).exists())


if __name__ == "__main__":
    unittest.main()
