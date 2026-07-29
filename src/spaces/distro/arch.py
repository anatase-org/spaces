"""Arch Linux distribution driver."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .. import _
from .model import Distribution, DistributionError
from .pam import (
    atomic_write,
    reconcile_managed_pam_file,
    safe_directory,
)


PACKAGES = (
    "base",
    "openssh",
    "nano",
    "sudo",
    # auth
    "polkit",
    "polkit-kde-agent",
    # desktop integration
    "breeze",
    "plasma-integration",
    "kde-cli-tools",
    "pipewire",
    "xdg-desktop-portal",
    "xdg-desktop-portal-kde",
)
AUR_BUILD_PACKAGES = ("base-devel", "git", "go")
YAY_REPOSITORY = "https://aur.archlinux.org/yay.git"
BUILDER = "spaces-build"
SYSTEM_AUTH = Path("etc/pam.d/system-auth")
SUDOERS_DROP_IN = Path("etc/sudoers.d/10-spaces-wheel")
SUDOERS_CONTENT = (
    "# Managed by Spaces: administrator group\n"
    "%wheel ALL=(ALL:ALL) ALL\n"
).encode()
OPTIONS = {
    "yay": _("yay — AUR helper (built from community source)"),
}


def _configure_sudoers(rootfs: Path) -> None:
    safe_directory(
        rootfs,
        SUDOERS_DROP_IN.parent,
        "Arch sudoers",
    )
    destination = rootfs / SUDOERS_DROP_IN
    try:
        destination.lstat()
    except FileNotFoundError:
        pass
    else:
        if destination.is_symlink() or not destination.is_file():
            raise DistributionError(_("Unsafe Arch sudoers drop-in."))
    atomic_write(destination, SUDOERS_CONTENT, 0o440)


def _arch_chroot(
    rootfs: Path,
    *arguments: str,
    user: str | None = None,
) -> None:
    command = ["arch-chroot", "-S"]
    if user is not None:
        command.extend(["-u", user])
    subprocess.run([*command, str(rootfs), *arguments], check=True)


def _install_yay(rootfs: Path) -> None:
    home = Path("/home") / BUILDER
    checkout = home / "yay"
    temporary = home / ".tmp"
    build_environment = (
        "/usr/bin/env",
        f"TMPDIR={temporary}",
    )
    created = False
    error: BaseException | None = None
    try:
        _arch_chroot(
            rootfs,
            "/usr/bin/useradd",
            "--create-home",
            "--user-group",
            "--shell",
            "/bin/bash",
            BUILDER,
        )
        created = True
        _arch_chroot(
            rootfs,
            "/usr/bin/mkdir",
            "--mode=0700",
            str(temporary),
            user=BUILDER,
        )
        _arch_chroot(
            rootfs,
            *build_environment,
            "/usr/bin/git",
            "clone",
            "--depth=1",
            YAY_REPOSITORY,
            str(checkout),
            user=BUILDER,
        )
        _arch_chroot(
            rootfs,
            *build_environment,
            "/usr/bin/makepkg",
            "--clean",
            "--cleanbuild",
            "--noconfirm",
            "--dir",
            str(checkout),
            user=BUILDER,
        )
        packages = [
            path
            for path in (rootfs / checkout.relative_to("/")).glob(
                "yay-*.pkg.tar.zst"
            )
            if (
                path.is_file()
                and not path.is_symlink()
                and not path.name.startswith("yay-debug-")
            )
        ]
        if len(packages) != 1:
            raise DistributionError(
                _("The yay build did not produce exactly one package.")
            )
        package = "/" + str(packages[0].relative_to(rootfs))
        _arch_chroot(
            rootfs,
            "/usr/bin/pacman",
            "--noconfirm",
            "-U",
            package,
        )
        _arch_chroot(
            rootfs,
            "/usr/bin/pacman",
            "--noconfirm",
            "-Rns",
            "go",
        )
    except BaseException as caught:
        error = caught
        raise
    finally:
        if created:
            cleanup_error: BaseException | None = None
            try:
                _arch_chroot(
                    rootfs,
                    "/usr/bin/userdel",
                    BUILDER,
                )
            except (OSError, subprocess.CalledProcessError) as caught:
                cleanup_error = caught
            builder_home = rootfs / home.relative_to("/")
            if builder_home.exists() or builder_home.is_symlink():
                if builder_home.is_dir() and not builder_home.is_symlink():
                    try:
                        shutil.rmtree(builder_home)
                    except OSError as caught:
                        cleanup_error = cleanup_error or caught
                else:
                    cleanup_error = cleanup_error or DistributionError(
                        _("Unsafe yay builder home.")
                    )
            if cleanup_error is not None and error is None:
                raise cleanup_error


class ArchDistribution(Distribution):
    def describe(self, metadata: Mapping[str, Any]) -> str:
        self.validate(metadata)
        return _("Arch Linux")

    def command(self, metadata: Mapping[str, Any], rootfs: Path) -> list[str]:
        self.validate(metadata)
        packages = list(PACKAGES)
        if "yay" in metadata["options"]:
            packages.extend(AUR_BUILD_PACKAGES)
        return ["pacstrap", "-K", str(rootfs), *packages]

    def bootstrap(self, metadata: Mapping[str, Any], rootfs: Path) -> None:
        self.validate(metadata)
        print(_("Bootstrapping Arch Linux..."), flush=True)
        subprocess.run(self.command(metadata, rootfs), check=True)
        if "yay" in metadata["options"]:
            print(_("Building yay from the AUR..."), flush=True)
            try:
                _install_yay(rootfs)
            except (OSError, subprocess.CalledProcessError) as error:
                raise DistributionError(_("Could not build yay.")) from error

    def reconcile_host_authentication(
        self,
        rootfs: Path,
        enabled: bool,
    ) -> bool:
        reconciled = reconcile_managed_pam_file(
            rootfs,
            enabled,
            relative_path=SYSTEM_AUTH,
            distribution_name="Arch Linux",
        )
        _configure_sudoers(rootfs)
        return reconciled


DISTRIBUTION = ArchDistribution(
    id="arch",
    default_name="arch",
    configuration_title=_("Arch options"),
    configuration_description=_(
        "Choose optional software to install while bootstrapping Arch Linux."
    ),
    option_key="options",
    configuration_options=OPTIONS,
    multiple_options=True,
    default_options=("yay",),
)
