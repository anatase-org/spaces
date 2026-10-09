"""Guest unit suppression and RPM upgrades in a private mount namespace.

Run the RPM regression as root with PYTHONPATH=src. It only installs fixture
packages into a temporary rootfs and never changes the host RPM database.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from spaces import launch


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "data/system-bridge/disabled-unit.conf"


class UnitConditionTests(unittest.TestCase):
    def test_condition_is_packaged_under_existing_bridge_policy(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertIn(
            str(CONFIG.relative_to(ROOT)),
            project["tool"]["setuptools"]["data-files"]["share/spaces/system-bridge"],
        )

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze required")
    def test_condition_prevents_guest_unit_start(self):
        conditions = [
            line for line in CONFIG.read_text().splitlines()
            if line.startswith("Condition")
        ]
        self.assertEqual(conditions, ["ConditionPathExists=!/"])
        result = subprocess.run(
            ["systemd-analyze", "condition", *conditions],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("ConditionPathExists=!/ failed", result.stderr)


@unittest.skipUnless(
    os.geteuid() == 0
    and all(shutil.which(command) for command in ("unshare", "rpm", "rpmbuild")),
    "root, unshare, rpm and rpmbuild required",
)
class UnitUpgradeTests(unittest.TestCase):
    def test_rpm_upgrade_can_replace_wireplumber_and_its_aliases(self):
        with tempfile.TemporaryDirectory(prefix="spaces-unit-upgrade-") as temporary:
            directory = Path(temporary)
            packages = []
            for release in (1, 2):
                spec = directory / "fixture.spec"
                spec.write_text(f"""Name: spaces-unit-upgrade-test
Version: 1
Release: {release}
Summary: Spaces unit upgrade fixture
License: MIT
BuildArch: noarch
%description
Private fixture for testing unit file replacement.
%install
mkdir -p %{{buildroot}}/usr/lib/systemd/user %{{buildroot}}/etc/systemd/user
echo 'release {release}' > %{{buildroot}}/usr/lib/systemd/user/wireplumber.service
ln -s wireplumber.service %{{buildroot}}/usr/lib/systemd/user/pipewire-session-manager.service
ln -s ../../../usr/lib/systemd/user/pipewire-session-manager.service %{{buildroot}}/etc/systemd/user/pipewire-session-manager.service
%files
/usr/lib/systemd/user/wireplumber.service
/usr/lib/systemd/user/pipewire-session-manager.service
/etc/systemd/user/pipewire-session-manager.service
""")
                build = subprocess.run(
                    [
                        "rpmbuild", "-bb", "--define", f"_topdir {directory}",
                        "--define", "_build_id_links none", str(spec),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
                packages.append(str(next((directory / "RPMS").rglob(f"*-{release}*.rpm"))))

            rootfs = directory / "rootfs"
            rootfs.mkdir()
            with mock.patch.object(launch, "DISABLED_UNIT_CONFIG", CONFIG):
                bindings = launch._disabled_unit_bind_arguments()
            script = """import json, pathlib, subprocess, sys
rootfs, packages, bindings = json.loads(sys.argv[1])
subprocess.run(['mount', '--make-rprivate', '/'], check=True)
rpm = ['rpm', '--root', rootfs, '--dbpath', '/var/lib/rpm',
       '--nodeps', '--noscripts', '--nosignature']
subprocess.run(rpm + ['-i', packages[0]], check=True)
for binding in bindings:
    source, destination = binding.removeprefix('--bind-ro=').split(':', 1)
    target = pathlib.Path(rootfs) / destination.lstrip('/')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()
    subprocess.run(['mount', '--bind', source, str(target)], check=True)
    subprocess.run(['mount', '-o', 'remount,bind,ro', str(target)], check=True)
subprocess.run(rpm + ['-U', packages[1]], check=True)
unit = pathlib.Path(rootfs) / 'usr/lib/systemd/user/wireplumber.service'
assert unit.read_text().strip() == 'release 2'
alias = pathlib.Path(rootfs) / 'etc/systemd/user/pipewire-session-manager.service'
assert alias.is_symlink() and alias.resolve() == unit
"""
            result = subprocess.run(
                [
                    "unshare", "--mount", sys.executable, "-c", script,
                    json.dumps([str(rootfs), packages, bindings]),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
