"""Ubuntu distribution driver."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Mapping

from .. import _
from .model import Distribution


PACKAGES = ("openssh-client", "python3", "nano", "sudo", "polkitd")
APT_COMPONENTS = ("main", "restricted", "universe", "multiverse")
RELEASES = {
    "noble": _("Noble (24.04)"),
    "resolute": _("Resolute (26.04)"),
}


def _configure_apt_sources(rootfs: Path, release: str) -> None:
    apt_directory = rootfs / "etc" / "apt"
    legacy_sources_path = apt_directory / "sources.list"
    mirror = "http://archive.ubuntu.com/ubuntu"
    if legacy_sources_path.is_file():
        for line in legacy_sources_path.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] == "deb":
                mirror = fields[1].rstrip("/")
                break

    security_mirror = (
        mirror if "ports.ubuntu.com" in mirror else "http://security.ubuntu.com/ubuntu"
    )
    components = " ".join(APT_COMPONENTS)
    sources = (
        "Types: deb\n"
        f"URIs: {mirror}\n"
        f"Suites: {release} {release}-updates {release}-backports\n"
        f"Components: {components}\n"
        "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n"
        "\n"
        "Types: deb\n"
        f"URIs: {security_mirror}\n"
        f"Suites: {release}-security\n"
        f"Components: {components}\n"
        "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n"
    )
    deb822_sources_path = apt_directory / "sources.list.d" / "ubuntu.sources"
    deb822_sources_path.parent.mkdir(parents=True, exist_ok=True)
    deb822_sources_path.write_text(sources, encoding="utf-8")
    legacy_sources_path.write_text(
        "# Ubuntu sources have moved to "
        "/etc/apt/sources.list.d/ubuntu.sources\n",
        encoding="utf-8",
    )


class UbuntuDistribution(Distribution):
    def describe(self, metadata: Mapping[str, Any]) -> str:
        self.validate(metadata)
        version = str(metadata["version"])
        return _(
            "Ubuntu {version}",
            version=RELEASES.get(version, version),
        )

    def command(self, metadata: Mapping[str, Any], rootfs: Path) -> list[str]:
        self.validate(metadata)
        release = str(metadata["version"])
        return [
            "debootstrap",
            release,
            str(rootfs),
        ]

    def bootstrap(self, metadata: Mapping[str, Any], rootfs: Path) -> None:
        version = str(metadata["version"])
        print(_("Bootstrapping Ubuntu {version}...", version=RELEASES.get(version, version)), flush=True)
        subprocess.run(self.command(metadata, rootfs), check=True)
        _configure_apt_sources(rootfs, version)
        subprocess.run(
            [
                "chroot",
                str(rootfs),
                "apt-get",
                "update",
            ],
            check=True,
        )
        print(
            _(
                "Adding additional packages:\n{packages}",
                packages=", ".join(PACKAGES),
            ),
            flush=True,
        )
        subprocess.run(
            [
                "chroot",
                str(rootfs),
                "/usr/bin/env",
                "DEBIAN_FRONTEND=noninteractive",
                "apt-get",
                "install",
                "--yes",
                "--no-install-recommends",
                *PACKAGES,
            ],
            check=True,
        )


DISTRIBUTION = UbuntuDistribution(
    id="ubuntu",
    default_name="ubuntu",
    administrator_group="sudo",
    shared_pam_policy="/etc/pam.d/common-auth",
    shared_pam_session_policy="/etc/pam.d/common-session",
    configuration_title=_("Ubuntu version"),
    configuration_description=_("Choose the Ubuntu release to bootstrap."),
    option_key="version",
    configuration_options=RELEASES,
    default_option="resolute",
)
