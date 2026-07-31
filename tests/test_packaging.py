from __future__ import annotations

import base64
import configparser
import shutil
import subprocess
import tempfile
import tomllib
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from PIL import Image, ImageChops


ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_git_spec_tracks_checkout_without_changing_release_spec(self) -> None:
        release_spec = (ROOT / "spaces.spec").read_text(encoding="utf-8")
        git_spec = (ROOT / "spaces-git.spec").read_text(encoding="utf-8")
        sync = (ROOT / "sync.sh").read_text(encoding="utf-8")

        self.assertIn("Version:        0.0.1", release_spec)
        self.assertNotIn("%global commit", release_spec)
        self.assertIn("%global commit %(git rev-parse --verify HEAD)", git_spec)
        self.assertIn("%global shortcommit", git_spec)
        self.assertIn("%global gitversion", git_spec)
        self.assertIn("Version:        %{gitversion}", git_spec)
        self.assertIn(
            "Source:         %{url}/archive/%{commit}/"
            "%{name}-%{commit}.tar.gz",
            git_spec,
        )
        self.assertIn("%autosetup -n %{name}-%{commit}", git_spec)
        self.assertIn(
            "awk '$1 == \"Version:\" { print $2; exit }' spaces.spec",
            sync,
        )
        self.assertIn("-name 'spaces-selinux-*.rpm'", sync)
        self.assertIn(
            'scp "$spaces_rpm_path" "$selinux_rpm_path" "$remote_host:"',
            sync,
        )
        self.assertIn(
            "~/$spaces_rpm_name ~/$selinux_rpm_name",
            sync,
        )
        for spec in (release_spec, git_spec):
            self.assertIn(
                "Requires:       %{name}-selinux = "
                "%{version}-%{release}",
                spec,
            )
            self.assertIn("%package selinux", spec)
            self.assertIn("BuildArch:      noarch", spec)
            self.assertIn("Requires:       container-selinux", spec)
            self.assertIn("Requires:       selinux-policy-targeted", spec)
            self.assertIn("%description selinux", spec)

            main_post = spec.split("%post\n", 1)[1].split("%preun", 1)[0]
            self.assertNotIn("%selinux_modules_install", main_post)
            policy_post = spec.split("%post selinux\n", 1)[1].split(
                "%postun selinux",
                1,
            )[0]
            self.assertIn("%selinux_modules_install", policy_post)

            main_files = spec.split("%files\n", 1)[1].split(
                "%files selinux",
                1,
            )[0]
            self.assertNotIn("selinux/packages/spaces.pp", main_files)
            policy_files = spec.split("%files selinux\n", 1)[1]
            self.assertIn("selinux/packages/spaces.pp", policy_files)

    def test_console_scripts_and_package_data(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        scripts = project["project"]["entry-points"]["console_scripts"]
        self.assertEqual(scripts["spaces"], "spaces.__main__:main")
        self.assertEqual(scripts["spaces.priv"], "spaces.priv:main")
        package_data = project["tool"]["setuptools"]["package-data"]["spaces"]
        self.assertIn("Pillow>=11.0", project["project"]["dependencies"])
        self.assertNotIn("distro/*.toml", package_data)
        for distribution in ("arch", "fedora", "kali", "ubuntu"):
            self.assertTrue(
                (
                    ROOT / "src" / "spaces" / "distro" / f"{distribution}.py"
                ).is_file()
            )
        self.assertFalse(
            (ROOT / "src" / "spaces" / "distro" / "ubuntu.toml").exists()
        )
        self.assertTrue((ROOT / "src" / "spaces" / "auth.py").is_file())
        self.assertFalse((ROOT / "src" / "spaces" / "auth").exists())
        core_source = (ROOT / "src" / "spaces" / "core.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("ubuntu", core_source.casefold())

    def test_systemd_templates_launch_unescaped_space_name(self) -> None:
        service = ROOT / "data" / "spaces@.service"
        unit = configparser.ConfigParser(interpolation=None, strict=False)
        unit.read(service, encoding="utf-8")

        self.assertEqual(unit["Service"]["Type"], "notify")
        self.assertEqual(unit["Service"]["NotifyAccess"], "all")
        self.assertEqual(unit["Service"]["Delegate"], "yes")
        self.assertEqual(unit["Service"]["KillMode"], "mixed")
        self.assertEqual(unit["Service"]["SyslogIdentifier"], "spaces-%I")
        self.assertEqual(unit["Service"]["DevicePolicy"], "closed")
        service_text = service.read_text(encoding="utf-8")
        self.assertIn("DeviceAllow=/dev/net/tun rwm", service_text)
        self.assertIn("DeviceAllow=char-pts rw", service_text)
        self.assertIn("DeviceAllow=/dev/fuse rwm", service_text)
        self.assertEqual(
            unit["Service"]["Environment"],
            "PYTHONDONTWRITEBYTECODE=1",
        )
        self.assertEqual(
            unit["Service"]["ExecStart"],
            "/usr/bin/spaces.priv launch %I",
        )
        self.assertNotIn("ExecStartPost", unit["Service"])
        self.assertEqual(unit["Install"]["WantedBy"], "multi-user.target")

        project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        data_files = project["tool"]["setuptools"]["data-files"]
        self.assertEqual(
            data_files["lib/systemd/system"],
            ["data/spaces@.service"],
        )

        user_service = ROOT / "data" / "systemd" / "user" / "spaces@.service"
        user_unit = configparser.ConfigParser(
            interpolation=None,
            strict=False,
        )
        user_unit.read(user_service, encoding="utf-8")
        self.assertEqual(user_unit["Service"]["Type"], "oneshot")
        self.assertEqual(
            user_unit["Service"]["ExecStart"],
            "/usr/bin/pkexec /usr/bin/spaces.priv start %I",
        )
        self.assertNotIn("RemainAfterExit", user_unit["Service"])
        self.assertEqual(
            user_unit["Install"]["WantedBy"],
            "default.target",
        )
        self.assertEqual(
            data_files["lib/systemd/user"],
            ["data/systemd/user/spaces@.service"],
        )

        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertIn("include data/spaces@.service", manifest)
        self.assertIn(
            "include data/systemd/user/spaces@.service",
            manifest,
        )

        spec = (ROOT / "spaces.spec").read_text(encoding="utf-8")
        self.assertIn("%{_unitdir}/spaces@.service", spec)
        self.assertIn("%{_userunitdir}/spaces@.service", spec)
        self.assertIn("%dir %{_sysconfdir}/spaces", spec)
        self.assertIn("%systemd_post spaces@.service", spec)
        self.assertIn("%systemd_preun spaces@.service", spec)
        self.assertIn("%systemd_postun_with_restart spaces@.service", spec)
        self.assertIn("%systemd_user_post spaces@.service", spec)
        self.assertIn("%systemd_user_preun spaces@.service", spec)
        self.assertIn(
            "%systemd_user_postun_with_restart spaces@.service",
            spec,
        )
        self.assertIn("Requires:       systemd\n", spec)
        self.assertIn("Requires:       systemd-container\n", spec)
        self.assertIn("Requires:       container-selinux\n", spec)
        self.assertIn("BuildRequires:  container-selinux\n", spec)
        self.assertIn(
            "restorecon -RF %{_bindir}/spaces.priv "
            "%{_localstatedir}/lib/spaces",
            spec,
        )
        self.assertEqual(
            spec.count(
                "restorecon -F /home/*/.ssh/config /root/.ssh/config"
            ),
            2,
        )
        self.assertIn(
            "%{_prefix}/local/share/applications/spaces",
            spec,
        )
        self.assertIn(
            "/usr/lib/spaces/guest %{_datadir}/spaces/portal "
            "%{_rundir}/spaces",
            spec,
        )
        self.assertIn("selinux/spaces.pp", spec)
        self.assertTrue((ROOT / "selinux" / "spaces.te").is_file())
        self.assertTrue((ROOT / "selinux" / "spaces.fc").is_file())
        self.assertTrue((ROOT / "selinux" / "spaces.if").is_file())
        type_enforcement = (ROOT / "selinux" / "spaces.te").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "allow systemd_machined_t spaces_file_t:{ file dir } mounton;",
            type_enforcement,
        )
        self.assertIn(
            "allow systemd_machined_t "
            "spaces_apifs_file_t:{ file dir } mounton;",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_container_t ssh_home_config_t:file {",
            type_enforcement,
        )
        self.assertIn(
            "allow ssh_t ssh_home_config_t:file manage_file_perms;",
            type_enforcement,
        )
        self.assertNotIn(
            "allow spaces_container_t ssh_home_t:file",
            type_enforcement,
        )
        self.assertIn(
            "create_dirs_pattern(\n"
            "\tsystemd_machined_t,\n"
            "\tspaces_apifs_file_t,\n"
            "\tspaces_apifs_file_t\n"
            ")",
            type_enforcement,
        )
        self.assertIn(
            "create_files_pattern(\n"
            "\tsystemd_machined_t,\n"
            "\tspaces_apifs_file_t,\n"
            "\tspaces_apifs_file_t\n"
            ")",
            type_enforcement,
        )
        self.assertIn(
            "allow systemd_machined_t user_home_type:file getattr;",
            type_enforcement,
        )
        self.assertIn(
            "allow systemd_machined_t "
            "spaces_var_run_t:{ dir file sock_file } getattr;",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_container_t tmpfs_t:file mounton;",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_container_t cgroup_t:filesystem mount;",
            type_enforcement,
        )
        self.assertIn(
            "dev_mount_sysfs_fs(spaces_container_t)",
            type_enforcement,
        )
        self.assertIn(
            "dev_unmount_sysfs_fs(spaces_container_t)",
            type_enforcement,
        )
        self.assertIn(
            "term_mount_pty_fs(spaces_container_t)",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_container_t devpts_t:chr_file { mounton open };",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_container_t hugetlbfs_t:filesystem "
            "{ mount remount unmount };",
            type_enforcement,
        )
        self.assertIn(
            "manage_dirs_pattern("
            "spaces_container_t, hugetlbfs_t, hugetlbfs_t)",
            type_enforcement,
        )
        self.assertIn(
            "manage_files_pattern("
            "spaces_container_t, hugetlbfs_t, hugetlbfs_t)",
            type_enforcement,
        )
        self.assertIn(
            "manage_dirs_pattern(\n"
            "\tspaces_container_t,\n"
            "\tcontainer_runtime_tmpfs_t,\n"
            "\tcontainer_runtime_tmpfs_t\n"
            ")",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_container_t fixed_disk_device_t:blk_file "
            "rw_blk_file_perms;",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_container_t lvm_control_t:chr_file "
            "rw_chr_file_perms;",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_container_t sound_device_t:chr_file "
            "rw_chr_file_perms;",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_container_t cpu_device_t:chr_file "
            "{ open read };",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_container_t tun_tap_device_t:chr_file "
            "rw_chr_file_perms;",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_container_t vhost_device_t:chr_file "
            "rw_chr_file_perms;",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_container_t user_tty_device_t:chr_file {\n"
            "\topen watch watch_reads\n"
            "};",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_container_t fonts_t:dir "
            "{ watch watch_reads };",
            type_enforcement,
        )
        self.assertIn(
            "dontaudit spaces_container_t proc_security_t:file write;",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_container_t rpc_pipefs_t:filesystem mount;",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_container_t mtrr_device_t:file "
            "{ mounton write };",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_container_t tmpfs_t:file execute;",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_container_t tmpfs_t:chr_file relabelfrom;",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_container_t tmpfs_t:lnk_file relabelfrom;",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_container_t spaces_var_run_t:dir "
            "list_dir_perms;",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_container_t spaces_var_run_t:dir watch;",
            type_enforcement,
        )
        file_contexts = (ROOT / "selinux" / "spaces.fc").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "/usr/local/share/applications/spaces(/.*)?",
            file_contexts,
        )
        self.assertIn(
            "/usr/lib/spaces/guest(/.*)?",
            file_contexts,
        )
        self.assertIn(
            "/usr/share/spaces/portal(/.*)?",
            file_contexts,
        )
        self.assertIn(
            "HOME_DIR/\\.ssh/config",
            file_contexts,
        )
        self.assertIn(
            "/root/\\.ssh/config",
            file_contexts,
        )
        self.assertIn(
            "allow spaces_container_t spaces_file_t:dir "
            "{ watch watch_reads };",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_container_t spaces_apifs_file_t:dir "
            "{ watch watch_reads };",
            type_enforcement,
        )
        self.assertIn(
            "userdom_write_user_tmp_sockets(spaces_container_t)",
            type_enforcement,
        )
        self.assertIn(
            "spaces_container_t,\n\t\tgpg_agent_tmp_t,\n"
            "\t\tgpg_agent_tmp_t,\n\t\tgpg_agent_t",
            type_enforcement,
        )
        self.assertIn("attribute ssh_agent_type;", type_enforcement)
        self.assertIn(
            "spaces_container_t,\n\t\tssh_agent_tmp_t,\n"
            "\t\tssh_agent_tmp_t,\n\t\tssh_agent_type",
            type_enforcement,
        )
        self.assertIn(
            "dbus_connect_session_bus(spaces_t)",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_t userdomain:system start;",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_container_t user_tmp_t:file write;",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_container_t user_tmp_t:file map;",
            type_enforcement,
        )
        self.assertIn(
            "allow spaces_container_t config_home_t:file read_file_perms;",
            type_enforcement,
        )
        self.assertIn(
            "read_files_pattern(\n"
            "\tspaces_container_t,\n"
            "\tspaces_var_run_t,\n"
            "\tspaces_var_run_t\n"
            ")",
            type_enforcement,
        )
        self.assertIn(
            "xserver_stream_connect(spaces_container_t)",
            type_enforcement,
        )
        self.assertIn(
            "unconfined_stream_connect(spaces_container_t)",
            type_enforcement,
        )
        self.assertIn(
            "unconfined_use_fds(spaces_container_t)",
            type_enforcement,
        )
        self.assertIn(
            "dev_rw_dma_dev(spaces_container_t)",
            type_enforcement,
        )
        self.assertIn(
            "kernel_io_uring_use(spaces_container_t)",
            type_enforcement,
        )
        self.assertIn(
            "userdom_manage_user_home_content(spaces_container_t)",
            type_enforcement,
        )
        self.assertIn(
            "userdom_exec_user_home_content_files(spaces_container_t)",
            type_enforcement,
        )
        self.assertIn(
            "dbus_write_session_tmp_sock_files(spaces_t)",
            type_enforcement,
        )
        self.assertIn(
            "dbus_session_bus_client(spaces_t)",
            type_enforcement,
        )

    def test_polkit_policy_scopes_authorization_by_operation(self) -> None:
        policy = ROOT / "data" / "org.anatase.spaces.policy"
        root = ElementTree.parse(policy).getroot()
        actions = {
            action.attrib["id"]: action for action in root.findall("./action")
        }
        expected = {
            "org.anatase.spaces.create": ("create", "auth_admin"),
            "org.anatase.spaces.configure": ("configure", "auth_admin"),
            "org.anatase.spaces.delete": ("delete", "auth_admin"),
            "org.anatase.spaces.cp": ("cp", "auth_admin"),
            "org.anatase.spaces.start": ("start", "yes"),
            "org.anatase.spaces.enter": ("enter", "yes"),
            "org.anatase.spaces.enter-as-user": (
                "enter-as-user",
                "auth_admin",
            ),
        }
        self.assertEqual(set(actions), set(expected))
        for action_id, (operation, authorization) in expected.items():
            action = actions[action_id]
            defaults = action.find("./defaults")
            self.assertIsNotNone(defaults)
            self.assertEqual(
                [child.text for child in defaults],
                [authorization, authorization, authorization],
            )
            annotations = {
                item.attrib["key"]: item.text
                for item in action.findall("./annotate")
            }
            self.assertEqual(
                annotations,
                {
                    "org.freedesktop.policykit.exec.path": (
                        "/usr/bin/spaces.priv"
                    ),
                    "org.freedesktop.policykit.exec.argv1": operation,
                },
            )

    def test_pkgbuild_uses_current_checkout(self) -> None:
        pkgbuild = (ROOT / "pkg" / "PKGBUILD").read_text(encoding="utf-8")
        self.assertIn("source=()", pkgbuild)
        self.assertIn('_project_dir="$PWD/.."', pkgbuild)
        self.assertNotIn("archive/refs/tags", pkgbuild)
        self.assertIn("arch=('x86_64' 'aarch64')", pkgbuild)
        self.assertIn("make -C native", pkgbuild)
        self.assertIn(
            'data/pam/spaces.system-auth "$pkgdir/etc/pam.d/spaces"',
            pkgbuild,
        )
        self.assertIn("'python-textual'", pkgbuild)
        self.assertIn("'python-pillow'", pkgbuild)
        self.assertIn("'librsvg'", pkgbuild)
        self.assertNotIn("'ubuntu-keyring'", pkgbuild)
        self.assertIn("'debootstrap'", pkgbuild)
        self.assertIn("'dnf5'", pkgbuild)
        self.assertIn("'arch-install-scripts'", pkgbuild)
        self.assertIn("'systemd'", pkgbuild)
        self.assertIn(
            "/usr/bin/python -m unittest discover -s tests -v", pkgbuild
        )

    def test_distribution_launchers_are_packaged(self) -> None:
        spec = (ROOT / "spaces.spec").read_text(encoding="utf-8")
        pkgbuild = (ROOT / "pkg" / "PKGBUILD").read_text(encoding="utf-8")
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertIn("recursive-include data/applications *.desktop", manifest)
        self.assertIn("recursive-include data/icons *.png", manifest)
        self.assertIn(
            "%{_datadir}/applications/spaces-*.desktop",
            spec,
        )
        self.assertIn(
            "%{_datadir}/icons/hicolor/256x256/apps/spaces-*.png",
            spec,
        )
        self.assertIn(
            "data/applications/spaces-${distro}.desktop",
            spec,
        )
        self.assertIn(
            "data/icons/hicolor/256x256/apps/spaces-${distro}.png",
            spec,
        )
        self.assertIn(
            "data/applications/spaces-$distro.desktop",
            pkgbuild,
        )
        self.assertIn(
            "data/icons/hicolor/256x256/apps/spaces-$distro.png",
            pkgbuild,
        )

        expected_names = {
            "arch": "Space (Arch)",
            "fedora": "Space (Fedora)",
            "ubuntu": "Space (Ubuntu)",
        }
        icon_hashes: set[bytes] = set()
        for distribution, name in expected_names.items():
            desktop_path = (
                ROOT
                / "data"
                / "applications"
                / f"spaces-{distribution}.desktop"
            )
            desktop = configparser.ConfigParser(interpolation=None)
            desktop.read(desktop_path, encoding="utf-8")
            entry = desktop["Desktop Entry"]
            self.assertEqual(entry["Type"], "Application")
            self.assertEqual(entry["Name"], name)
            self.assertEqual(entry["TryExec"], "/usr/bin/spaces")
            self.assertEqual(
                entry["Exec"],
                f"/usr/bin/spaces enter {distribution}",
            )
            self.assertEqual(entry["Icon"], f"spaces-{distribution}")
            self.assertTrue(entry.getboolean("Terminal"))
            self.assertEqual(entry["Categories"], "Development;")

            icon_path = (
                ROOT
                / "data"
                / "icons"
                / "hicolor"
                / "256x256"
                / "apps"
                / f"spaces-{distribution}.png"
            )
            distribution_path = (
                ROOT / "art" / "distros" / f"{distribution}.png"
            )
            with (
                Image.open(icon_path) as icon_source,
                Image.open(distribution_path) as distribution_source,
            ):
                self.assertEqual(icon_source.mode, "RGBA")
                icon = icon_source.convert("RGBA")
                distribution_icon = distribution_source.convert("RGBA")
            self.assertEqual(icon.size, (256, 256))
            self.assertEqual(icon.getpixel((0, 0))[3], 0)
            self.assertEqual(icon.getpixel((255, 255))[3], 0)

            expected_distribution = Image.new("RGBA", icon.size)
            expected_distribution.alpha_composite(
                distribution_icon,
                (
                    (icon.width - distribution_icon.width) // 2,
                    (icon.height - distribution_icon.height) // 2,
                ),
            )
            difference = ImageChops.difference(icon, expected_distribution)
            badge_bounds = difference.getbbox()
            self.assertIsNotNone(badge_bounds)
            assert badge_bounds is not None
            self.assertGreaterEqual(badge_bounds[0], 146)
            self.assertGreaterEqual(badge_bounds[1], 146)
            icon_hashes.add(icon.tobytes())
        self.assertEqual(len(icon_hashes), len(expected_names))

    def test_native_authentication_assets_are_packaged_per_architecture(
        self,
    ) -> None:
        spec = (ROOT / "spaces.spec").read_text(encoding="utf-8")
        main_package = spec.split("%package selinux", 1)[0]
        self.assertIn("ExclusiveArch:  x86_64 aarch64", spec)
        self.assertNotIn("BuildArch:      noarch", main_package)
        self.assertIn("%make_build -C native", spec)
        self.assertIn("%{_sysconfdir}/pam.d/spaces", spec)
        self.assertIn("%{_datadir}/spaces/pam/spaces.ubuntu", spec)
        self.assertIn("%{_datadir}/spaces/pam/spaces.kali", spec)
        self.assertIn("Requires:       dnf5", spec)
        self.assertIn("Requires:       debootstrap", spec)
        self.assertNotIn("ubuntu-keyring", spec)
        self.assertIn("Requires:       arch-install-scripts", spec)

        makefile = (ROOT / "native" / "Makefile").read_text(encoding="utf-8")
        self.assertIn("install: check-guest-abi", makefile)
        self.assertIn("check_guest_abi.py", makefile)
        for name in (
            "pam_spaces.so",
            "spaces-portal",
            "spaces-open",
            "spaces-open-broker",
            "spaces-secret-helper",
            "spaces-pam-worker",
            "spaces-session-launcher",
        ):
            self.assertIn(name, makefile)
        self.assertNotIn("spaces-polkit", makefile)
        self.assertNotIn("polkit-agent-1", spec)
        self.assertIn(
            "/usr/lib/spaces/guest/spaces-session-launcher",
            spec,
        )
        self.assertIn("/usr/lib/spaces/guest/spaces-portal", spec)
        self.assertIn("/usr/lib/spaces/guest/spaces-open", spec)
        self.assertIn("/usr/lib/spaces/spaces-open-broker", spec)
        self.assertIn(
            "/usr/lib/spaces/guest/spaces-secret-helper", spec
        )
        self.assertIn("Requires:       glib2", spec)
        self.assertIn("Requires:       xdg-dbus-proxy", spec)
        self.assertIn("Requires:       python3-pillow", spec)
        self.assertIn("Requires:       librsvg2-tools", spec)
        self.assertIn("BuildRequires:  glib2-devel", spec)
        self.assertTrue(
            (ROOT / "native" / "spaces_session_launcher.c").exists()
        )
        self.assertFalse(
            (ROOT / "native" / "spaces_session.c").exists()
        )
        self.assertFalse(
            (ROOT / "native" / "spaces_polkit_worker.c").exists()
        )
        self.assertFalse(
            (ROOT / "native" / "spaces_polkit_agent.c").exists()
        )
        self.assertFalse((ROOT / "native" / "glibc_compat.c").exists())

        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertIn(
            "recursive-include native Makefile *.c *.h *.py", manifest
        )
        self.assertIn("recursive-include data/pam *", manifest)
        profile = (
            ROOT / "data" / "pam" / "spaces.ubuntu"
        ).read_text(encoding="utf-8")
        self.assertIn("Default: yes", profile)
        self.assertIn("open_err=ignore", profile)
        self.assertIn("default=die", profile)
        self.assertIn(
            "/run/spaces-host/bin/pam_spaces.so",
            profile,
        )
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"data/pam/spaces.ubuntu"', pyproject)
        self.assertIn('"data/pam/spaces.kali"', pyproject)
        self.assertIn('"data/repos/fedora.repo"', pyproject)
        self.assertIn(
            '"data/keys/RPM-GPG-KEY-fedora-44-primary"',
            pyproject,
        )
        self.assertIn(
            '"data/keys/kali-archive-key.gpg.base64"',
            pyproject,
        )
        for asset in (
            "org.freedesktop.secrets.service",
            "org.kde.secretservicecompat.service",
            "org.kde.kwalletd5.service",
            "data/portal/config/kwalletrc",
        ):
            self.assertIn(asset, pyproject)

    def test_packaged_bootstrap_keys_have_expected_fingerprints(self) -> None:
        if shutil.which("gpg") is None:
            self.skipTest("gpg is required to inspect packaged keys")

        def fingerprints(path: Path) -> set[str]:
            completed = subprocess.run(
                [
                    "gpg",
                    "--batch",
                    "--with-colons",
                    "--show-keys",
                    str(path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            return {
                fields[9]
                for line in completed.stdout.splitlines()
                if (fields := line.split(":"))[0] == "fpr"
            }

        fedora_key = (
            ROOT / "data" / "keys" / "RPM-GPG-KEY-fedora-44-primary"
        )
        kali_key = ROOT / "data" / "keys" / "kali-archive-key.asc"
        encoded_kali_key = (
            ROOT / "data" / "keys" / "kali-archive-key.gpg.base64"
        )
        self.assertIn(
            "36F612DCF27F7D1A48A835E4DBFCF71C6D9F90A6",
            fingerprints(fedora_key),
        )
        expected_kali = "827C8569F2518CC677FECA1AED65462EC8D5E4C5"
        self.assertIn(expected_kali, fingerprints(kali_key))
        with tempfile.TemporaryDirectory() as temporary:
            binary_key = Path(temporary) / "kali.gpg"
            binary_key.write_bytes(
                base64.b64decode(encoded_kali_key.read_bytes())
            )
            self.assertIn(expected_kali, fingerprints(binary_key))

    def test_distro_overlays_are_packaged_at_export_resolution(self) -> None:
        for name in ("arch", "fedora", "kali", "ubuntu"):
            path = ROOT / "src" / "spaces" / "overlay" / f"{name}.png"
            with Image.open(path) as image:
                self.assertEqual(image.size, (256, 256))

    def test_pam_module_has_no_session_registration(self) -> None:
        source = (ROOT / "native" / "pam_spaces.c").read_text(
            encoding="utf-8"
        )
        setcred = source.split("PAM_EXTERN int pam_sm_setcred(", 1)[1]
        setcred, account = setcred.split(
            "PAM_EXTERN int pam_sm_acct_mgmt(", 1
        )
        account, opening = account.split(
            "PAM_EXTERN int pam_sm_open_session(", 1
        )
        opening, closing = opening.split(
            "PAM_EXTERN int pam_sm_close_session(", 1
        )
        closing = closing.split("PAM_EXTERN int pam_sm_chauthtok(", 1)[0]

        self.assertNotIn("notify_session", setcred)
        self.assertNotIn("notify_session", account)
        self.assertNotIn("notify_session", opening)
        self.assertNotIn("launch_polkit_agent", source)
        self.assertNotIn("notify_session", closing)
        self.assertNotIn("SPACES_AUTH_SESSION", source)

    def test_pkgbuild_is_not_in_source_manifest(self) -> None:
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertNotIn("PKGBUILD", manifest)
        self.assertFalse((ROOT / "src" / "spaces" / "utils.py").exists())


if __name__ == "__main__":
    unittest.main()
