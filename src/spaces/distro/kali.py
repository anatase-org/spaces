"""Kali Linux distribution driver."""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .. import _
from .debian import (
    POLICY_RC_D,
    chroot_command as _chroot_command,
    prepared_chroot,
)
from .model import Distribution, DistributionError
from .pam import reconcile_pam_auth_update


PACKAGES = (
    "openssh-client",
    "nano",
    "sudo",
    # auth
    "pkexec",
    "polkit-kde-agent-1",
    "polkitd",
    # desktop integration
    "breeze",
    "plasma-integration",
    "kde-cli-tools",
    "kwallet6",
    "libqca-qt6-plugins",
    "qt6-wayland",
    "file",
    "pipewire",
    "xdg-desktop-portal",
    "xdg-desktop-portal-kde",
)
METAPACKAGE = "kali-linux-default"
RELEASE = "kali-rolling"
MIRROR = "http://http.kali.org/kali"
HOST_KEYRING = Path(
    "/usr/share/spaces/keys/kali-archive-key.gpg.base64"
)
HOST_AUTHENTICATION_PROFILE = Path(
    "/usr/share/spaces/pam/spaces.kali"
)
GUEST_AUTHENTICATION_PROFILE = Path(
    "usr/share/pam-configs/spaces"
)


def _configure_apt_sources(rootfs: Path) -> None:
    sources = (
        "Types: deb\n"
        f"URIs: {MIRROR}/\n"
        f"Suites: {RELEASE}\n"
        "Components: main contrib non-free non-free-firmware\n"
        "Signed-By: /usr/share/keyrings/kali-archive-keyring.gpg\n"
    )
    apt_directory = rootfs / "etc" / "apt"
    sources_path = apt_directory / "sources.list.d" / "kali.sources"
    sources_path.parent.mkdir(parents=True, exist_ok=True)
    sources_path.write_text(sources, encoding="utf-8")
    (apt_directory / "sources.list").write_text(
        "# Kali sources have moved to /etc/apt/sources.list.d/kali.sources\n",
        encoding="utf-8",
    )


class KaliDistribution(Distribution):
    def describe(self, metadata: Mapping[str, Any]) -> str:
        self.validate(metadata)
        return _("Kali Linux")

    def command(
        self,
        metadata: Mapping[str, Any],
        rootfs: Path,
        keyring: Path,
    ) -> list[str]:
        self.validate(metadata)
        return [
            "debootstrap",
            "--force-check-gpg",
            f"--keyring={keyring}",
            RELEASE,
            str(rootfs),
            MIRROR,
        ]

    def bootstrap(self, metadata: Mapping[str, Any], rootfs: Path) -> None:
        self.validate(metadata)
        print(_("Bootstrapping Kali Linux..."), flush=True)
        try:
            encoded_keyring = b"".join(HOST_KEYRING.read_bytes().split())
            keyring_data = base64.b64decode(encoded_keyring, validate=True)
        except (OSError, ValueError) as error:
            raise DistributionError(
                _("The Kali archive keyring is missing or invalid.")
            ) from error
        with tempfile.NamedTemporaryFile(
            prefix="spaces-kali-",
            suffix=".gpg",
        ) as keyring:
            keyring.write(keyring_data)
            keyring.flush()
            os.fchmod(keyring.fileno(), 0o600)
            subprocess.run(
                self.command(metadata, rootfs, Path(keyring.name)),
                check=True,
            )
        _configure_apt_sources(rootfs)
        with prepared_chroot(rootfs, "Kali"):
            subprocess.run(
                _chroot_command(rootfs, "apt-get", "update"),
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
                _chroot_command(
                    rootfs,
                    "apt-get",
                    "install",
                    "--yes",
                    "--no-install-recommends",
                    *PACKAGES,
                ),
                check=True,
            )
            print(
                _(
                    "Installing Kali default toolset: {package}",
                    package=METAPACKAGE,
                ),
                flush=True,
            )
            subprocess.run(
                _chroot_command(
                    rootfs,
                    "apt-get",
                    "install",
                    "--yes",
                    METAPACKAGE,
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
            distribution_name="Kali Linux",
        )


DISTRIBUTION = KaliDistribution(
    id="kali",
    default_name="kali",
    administrator_group="sudo",
)
