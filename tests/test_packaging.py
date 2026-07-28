from __future__ import annotations

import configparser
import tomllib
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_console_scripts_and_package_data(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        scripts = project["project"]["entry-points"]["console_scripts"]
        self.assertEqual(scripts["spaces"], "spaces.__main__:main")
        self.assertEqual(scripts["spaces.priv"], "spaces.priv:main")
        package_data = project["tool"]["setuptools"]["package-data"]["spaces"]
        self.assertNotIn("distro/*.toml", package_data)
        self.assertTrue((ROOT / "src" / "spaces" / "distro" / "ubuntu.py").is_file())
        self.assertFalse(
            (ROOT / "src" / "spaces" / "distro" / "ubuntu.toml").exists()
        )
        self.assertTrue((ROOT / "src" / "spaces" / "auth.py").is_file())
        self.assertFalse((ROOT / "src" / "spaces" / "auth").exists())
        core_source = (ROOT / "src" / "spaces" / "core.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("ubuntu", core_source.casefold())

    def test_systemd_template_launches_unescaped_space_name(self) -> None:
        service = ROOT / "data" / "spaces@.service"
        unit = configparser.ConfigParser(interpolation=None)
        unit.read(service, encoding="utf-8")

        self.assertEqual(unit["Service"]["Type"], "notify")
        self.assertEqual(unit["Service"]["NotifyAccess"], "all")
        self.assertEqual(unit["Service"]["Delegate"], "yes")
        self.assertEqual(unit["Service"]["KillMode"], "mixed")
        self.assertEqual(unit["Service"]["SyslogIdentifier"], "spaces-%I")
        self.assertEqual(
            unit["Service"]["ExecStart"],
            "/usr/bin/spaces.priv launch %I",
        )
        self.assertEqual(unit["Install"]["WantedBy"], "multi-user.target")

        project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        data_files = project["tool"]["setuptools"]["data-files"]
        self.assertEqual(
            data_files["lib/systemd/system"],
            ["data/spaces@.service"],
        )

        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertIn("include data/spaces@.service", manifest)

        spec = (ROOT / "spaces.spec").read_text(encoding="utf-8")
        self.assertIn("%{_unitdir}/spaces@.service", spec)
        self.assertIn("%systemd_post spaces@.service", spec)
        self.assertIn("%systemd_preun spaces@.service", spec)
        self.assertIn("%systemd_postun_with_restart spaces@.service", spec)
        self.assertIn("Requires:       systemd\n", spec)
        self.assertIn("Requires:       systemd-container\n", spec)

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
        self.assertIn("'ubuntu-keyring'", pkgbuild)
        self.assertIn("'systemd'", pkgbuild)
        self.assertIn(
            "/usr/bin/python -m unittest discover -s tests -v", pkgbuild
        )

    def test_native_authentication_assets_are_packaged_per_architecture(
        self,
    ) -> None:
        spec = (ROOT / "spaces.spec").read_text(encoding="utf-8")
        self.assertIn("ExclusiveArch:  x86_64 aarch64", spec)
        self.assertNotIn("BuildArch:      noarch", spec)
        self.assertIn("%make_build -C native", spec)
        self.assertIn("%{_sysconfdir}/pam.d/spaces", spec)
        self.assertIn("%{_datadir}/spaces/pam/spaces.ubuntu", spec)

        makefile = (ROOT / "native" / "Makefile").read_text(encoding="utf-8")
        self.assertIn("install: check-guest-abi", makefile)
        self.assertIn("check_guest_abi.py", makefile)
        for name in (
            "pam_spaces.so",
            "spaces-pam-worker",
        ):
            self.assertIn(name, makefile)
        self.assertNotIn("spaces-polkit", makefile)
        self.assertNotIn("polkit-agent-1", spec)
        self.assertNotIn("spaces-session", makefile)
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
