from __future__ import annotations

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

    def test_polkit_policy_requires_admin(self) -> None:
        policy = ROOT / "data" / "org.anatase.spaces.policy"
        root = ElementTree.parse(policy).getroot()
        defaults = root.find("./action/defaults")
        self.assertIsNotNone(defaults)
        self.assertEqual(
            [child.text for child in defaults],
            ["auth_admin", "auth_admin", "auth_admin"],
        )
        annotation = root.find("./action/annotate")
        self.assertEqual(annotation.text, "/usr/bin/spaces.priv")

    def test_pkgbuild_uses_current_checkout(self) -> None:
        pkgbuild = (ROOT / "pkg" / "PKGBUILD").read_text(encoding="utf-8")
        self.assertIn("source=()", pkgbuild)
        self.assertIn('_project_dir="$PWD/.."', pkgbuild)
        self.assertNotIn("archive/refs/tags", pkgbuild)
        self.assertIn("'python-textual'", pkgbuild)
        self.assertIn("'ubuntu-keyring'", pkgbuild)
        self.assertIn(
            "/usr/bin/python -m unittest discover -s tests -v", pkgbuild
        )

    def test_pkgbuild_is_not_in_source_manifest(self) -> None:
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertNotIn("PKGBUILD", manifest)
        self.assertFalse((ROOT / "src" / "spaces" / "utils.py").exists())


if __name__ == "__main__":
    unittest.main()
