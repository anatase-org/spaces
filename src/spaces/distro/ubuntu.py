"""Ubuntu distribution driver."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .. import _
from .model import Distribution, DistributionError


PACKAGES = (
    "openssh-client",
    "nano",
    "sudo",
    # auth
    "pkexec",
    "polkit-kde-agent-1",
    "polkitd",
    # Used to initialize an installed guest KWallet over its session bus.
    "libglib2.0-bin",
    # these are required for plasma applications to look correct
    "breeze",
    "plasma-integration",
)
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


def _pam_profile_directory(rootfs: Path) -> Path:
    directory = rootfs / GUEST_AUTHENTICATION_PROFILE.parent
    try:
        resolved_root = rootfs.resolve(strict=True)
        resolved_directory = directory.resolve(strict=True)
    except OSError as error:
        raise DistributionError(
            _("Ubuntu PAM profile directory is missing.")
        ) from error
    if (
        directory.is_symlink()
        or not directory.is_dir()
        or not resolved_directory.is_relative_to(resolved_root)
    ):
        raise DistributionError(
            _("Unsafe Ubuntu PAM profile directory.")
        )
    return directory


def _write_pam_profile(destination: Path, profile: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=".spaces-",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(profile)
            temporary.flush()
            os.fchmod(temporary.fileno(), 0o644)
            os.fsync(temporary.fileno())
        temporary_path.replace(destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


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
        print(
            _(
                "Bootstrapping Ubuntu {version}...",
                version=RELEASES.get(version, version),
            ),
            flush=True,
        )
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

    def reconcile_host_authentication(
        self,
        rootfs: Path,
        enabled: bool,
    ) -> bool:
        destination = rootfs / GUEST_AUTHENTICATION_PROFILE
        if not enabled:
            try:
                destination.lstat()
            except FileNotFoundError:
                return True
            if destination.is_symlink() or not destination.is_file():
                raise DistributionError(
                    _(
                        "Unsafe Spaces PAM profile: {path}.",
                        path=destination,
                    )
                )

        try:
            profile = HOST_AUTHENTICATION_PROFILE.read_bytes()
        except OSError as error:
            raise DistributionError(
                _(
                    "Spaces PAM profile is missing: {path}.",
                    path=HOST_AUTHENTICATION_PROFILE,
                )
            ) from error
        _pam_profile_directory(rootfs)
        if enabled:
            _write_pam_profile(destination, profile)
        else:
            destination.unlink()

        try:
            subprocess.run(
                [
                    "chroot",
                    str(rootfs),
                    "/usr/bin/env",
                    "DEBIAN_FRONTEND=noninteractive",
                    "/usr/sbin/pam-auth-update",
                    "--package",
                ],
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            if not enabled:
                try:
                    _write_pam_profile(destination, profile)
                except OSError:
                    pass
            raise DistributionError(
                _("Could not update Ubuntu PAM configuration.")
            ) from error
        return True


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
