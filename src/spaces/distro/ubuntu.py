"""Ubuntu distribution driver."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from .. import _
from .debian import chroot_command, prepared_chroot
from .model import Distribution
from .pam import reconcile_pam_auth_update


PACKAGES = (
    "openssh-client",
    "git",
    "nano",
    "sudo",
    # auth
    "pkexec",
    "polkit-kde-agent-1",
    "polkitd",
    # these are required for plasma applications to look correct
    "breeze",
    "plasma-integration",
    "kde-cli-tools",
    "dconf-gsettings-backend",
    "file",
    "pipewire",
    "xdg-desktop-portal",
    "xdg-desktop-portal-kde",
)
SECRET_PACKAGES = {
    "noble": ("libkf5wallet-bin", "libqca-qt5-2-plugins"),
    "resolute": (
        "kwallet6",
        "libqca-qt6-plugins",
        "qt6-wayland",
    ),
}
HOST_AUTHENTICATION_PROFILE = Path(
    "/usr/share/spaces/pam/spaces.ubuntu"
)
GUEST_AUTHENTICATION_PROFILE = Path(
    "usr/share/pam-configs/spaces"
)
APT_COMPONENTS = ("main", "restricted", "universe", "multiverse")
RELEASES = {
    "noble": _("Noble (24.04)"),
    "resolute": _("Resolute (26.04)"),
}
DEBOOTSTRAP_SCRIPT = "gutsy"


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


def _available_additional_packages(
    rootfs: Path,
    packages: Sequence[str],
) -> tuple[str, ...]:
    available: list[str] = []
    for package in packages:
        result = subprocess.run(
            chroot_command(
                rootfs,
                "apt-cache",
                "show",
                "--no-all-versions",
                package,
            ),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            available.append(package)
        else:
            print(
                _(
                    "Skipping unavailable Ubuntu package: {package}",
                    package=package,
                ),
                flush=True,
            )
    return tuple(available)


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
            "",
            DEBOOTSTRAP_SCRIPT,
        ]

    def bootstrap(
        self,
        metadata: Mapping[str, Any],
        rootfs: Path,
        additional_packages: Sequence[str] = (),
    ) -> None:
        version = str(metadata["version"])
        print(
            _(
                "Bootstrapping Ubuntu {version}...",
                version=RELEASES.get(version, version),
            ),
            flush=True,
        )
        subprocess.run(self.command(metadata, rootfs), check=True)
        _configure_apt_sources(rootfs, version)
        with prepared_chroot(rootfs, "Ubuntu"):
            subprocess.run(
                chroot_command(rootfs, "apt-get", "update"),
                check=True,
            )
            packages = (
                *PACKAGES,
                *SECRET_PACKAGES[version],
                *_available_additional_packages(rootfs, additional_packages),
            )
            print(
                _(
                    "Adding additional packages:\n{packages}",
                    packages=", ".join(packages),
                ),
                flush=True,
            )
            subprocess.run(
                chroot_command(
                    rootfs,
                    "apt-get",
                    "install",
                    "--yes",
                    "--no-install-recommends",
                    *packages,
                ),
                check=True,
            )

    def reconcile_host_authentication(
        self,
        rootfs: Path,
        enabled: bool,
    ) -> bool:
        return reconcile_pam_auth_update(
            rootfs,
            enabled,
            host_profile=HOST_AUTHENTICATION_PROFILE,
            guest_profile=GUEST_AUTHENTICATION_PROFILE,
            distribution_name="Ubuntu",
        )


DISTRIBUTION = UbuntuDistribution(
    id="ubuntu",
    default_name="ubuntu",
    administrator_group="sudo",
    configuration_title=_("Ubuntu version"),
    configuration_description=_("Choose the Ubuntu release to bootstrap."),
    option_key="version",
    configuration_options=RELEASES,
    default_option="resolute",
)
