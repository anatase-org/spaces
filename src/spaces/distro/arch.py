"""Arch Linux distribution driver."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .. import _
from .model import Distribution, DistributionError
from .mounts import mounted_rootfs
from .pam import (
    atomic_write,
    reconcile_managed_pam_file,
    safe_directory,
)


PACKAGES = (
    "base",
    "openssh",
    "git",
    "nano",
    "sudo",
    # auth
    "polkit",
    "polkit-kde-agent",
    # desktop integration
    "breeze",
    "plasma-integration",
    "kde-cli-tools",
    "kwallet",
    "file",
    "pipewire",
    "xdg-desktop-portal",
    "xdg-desktop-portal-kde",
)
AUR_BUILD_PACKAGES = ("base-devel",)
AUR_REPOSITORIES = {
    "yay": "https://aur.archlinux.org/yay.git",
    "shelly": "https://aur.archlinux.org/shelly.git",
}
AUR_PACKAGE_NAMES = {
    "yay": "yay",
    "shelly": "Shelly",
}
BUILDER = "spaces-build"
SYSTEM_AUTH = Path("etc/pam.d/system-auth")
SUDOERS_DROP_IN = Path("etc/sudoers.d/10-spaces-wheel")
AUR_SUDOERS_DROP_IN = Path("etc/sudoers.d/10-spaces-build")
SUDOERS_CONTENT = (
    "# Managed by Spaces: administrator group\n"
    "%wheel ALL=(ALL:ALL) ALL\n"
).encode()
AUR_SUDOERS_CONTENT = (
    "# Managed by Spaces: temporary AUR package builder\n"
    f"{BUILDER} ALL=(root) NOPASSWD: /usr/bin/pacman\n"
).encode()
OPTIONS = {
    "yay": _("yay — AUR helper (built from community source)"),
    "shelly": _("Shelly — graphical package manager"),
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
    command = ["arch-chroot"]
    if user is not None:
        command.extend(["-u", user])
    subprocess.run([*command, str(rootfs), *arguments], check=True)


def _arch_chroot_output(
    rootfs: Path,
    *arguments: str,
    user: str | None = None,
) -> str:
    command = ["arch-chroot"]
    if user is not None:
        command.extend(["-u", user])
    completed = subprocess.run(
        [*command, str(rootfs), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return completed.stdout


def _install_aur_package(
    rootfs: Path,
    package: str,
    repository: str,
) -> None:
    home = Path("/home") / BUILDER
    checkout = home / package
    temporary = home / ".tmp"
    package_directory = home / "packages"
    sudoers = rootfs / AUR_SUDOERS_DROP_IN
    build_environment = (
        "/usr/bin/env",
        f"HOME={home}",
        f"TMPDIR={temporary}",
        f"PKGDEST={package_directory}",
    )
    created = False
    sudoers_created = False
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
            "/usr/bin/mkdir",
            "--mode=0700",
            str(package_directory),
            user=BUILDER,
        )
        safe_directory(
            rootfs,
            AUR_SUDOERS_DROP_IN.parent,
            "Arch AUR builder sudoers",
        )
        try:
            sudoers.lstat()
        except FileNotFoundError:
            pass
        else:
            raise DistributionError(_("Unsafe AUR builder sudoers drop-in."))
        atomic_write(sudoers, AUR_SUDOERS_CONTENT, 0o440)
        sudoers_created = True
        _arch_chroot(
            rootfs,
            *build_environment,
            "/usr/bin/git",
            "clone",
            "--depth=1",
            repository,
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
            "--syncdeps",
            "--rmdeps",
            "--dir",
            str(checkout),
            user=BUILDER,
        )
        package_files = [
            path
            for path in (rootfs / package_directory.relative_to("/")).glob(
                "*.pkg.tar.*"
            )
            if (
                path.is_file()
                and not path.is_symlink()
                and path.suffix != ".sig"
            )
        ]
        matching_packages = []
        for path in package_files:
            guest_path = "/" + str(path.relative_to(rootfs))
            built_package = _arch_chroot_output(
                rootfs,
                "/usr/bin/pacman",
                "--query",
                "--file",
                guest_path,
            ).split(maxsplit=1)[0]
            if built_package == package:
                matching_packages.append(guest_path)
        if len(matching_packages) != 1:
            raise DistributionError(
                _(
                    "The AUR build did not produce exactly one {package} package.",
                    package=package,
                )
            )
        _arch_chroot(
            rootfs,
            "/usr/bin/pacman",
            "--noconfirm",
            "-U",
            matching_packages[0],
        )
    except BaseException as caught:
        error = caught
        raise
    finally:
        cleanup_error: BaseException | None = None
        if sudoers_created:
            try:
                sudoers.unlink()
            except OSError as caught:
                cleanup_error = caught
        if created:
            try:
                _arch_chroot(
                    rootfs,
                    "/usr/bin/userdel",
                    BUILDER,
                )
            except (OSError, subprocess.CalledProcessError) as caught:
                cleanup_error = cleanup_error or caught
            builder_home = rootfs / home.relative_to("/")
            if builder_home.exists() or builder_home.is_symlink():
                if builder_home.is_dir() and not builder_home.is_symlink():
                    try:
                        shutil.rmtree(builder_home)
                    except OSError as caught:
                        cleanup_error = cleanup_error or caught
                else:
                    cleanup_error = cleanup_error or DistributionError(
                        _("Unsafe AUR builder home.")
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
        if {"yay", "shelly"} & set(metadata["options"]):
            packages.extend(AUR_BUILD_PACKAGES)
        return ["pacstrap", "-K", str(rootfs), *packages]

    def bootstrap(self, metadata: Mapping[str, Any], rootfs: Path) -> None:
        self.validate(metadata)
        print(_("Bootstrapping Arch Linux..."), flush=True)
        subprocess.run(self.command(metadata, rootfs), check=True)
        options = metadata["options"]
        if options:
            with mounted_rootfs(rootfs, "Arch"):
                for package in options:
                    package_name = AUR_PACKAGE_NAMES[package]
                    print(
                        _(
                            "Building {package} from the AUR...",
                            package=package_name,
                        ),
                        flush=True,
                    )
                    try:
                        _install_aur_package(
                            rootfs,
                            package,
                            AUR_REPOSITORIES[package],
                        )
                    except (
                        OSError,
                        subprocess.CalledProcessError,
                    ) as error:
                        raise DistributionError(
                            _(
                                "Could not build {package}.",
                                package=package_name,
                            )
                        ) from error

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
