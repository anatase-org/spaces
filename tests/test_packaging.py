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
            "org.anatase.spaces.start": ("start", "yes"),
            "org.anatase.spaces.enter": ("enter", "yes"),
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
        self.assertIn("'python-textual'", pkgbuild)
        self.assertIn("'ubuntu-keyring'", pkgbuild)
        self.assertIn("'systemd'", pkgbuild)
        self.assertIn(
            "/usr/bin/python -m unittest discover -s tests -v", pkgbuild
        )

    def test_pkgbuild_is_not_in_source_manifest(self) -> None:
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertNotIn("PKGBUILD", manifest)
        self.assertFalse((ROOT / "src" / "spaces" / "utils.py").exists())


if __name__ == "__main__":
    unittest.main()
