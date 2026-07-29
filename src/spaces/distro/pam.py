"""Safe PAM configuration helpers shared by distribution drivers."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from .. import _
from .model import DistributionError


SPACES_PAM_BLOCK = (
    "# Managed by Spaces: host authentication\n"
    "auth [success=done open_err=ignore default=die] "
    "/run/spaces-host/bin/pam_spaces.so\n"
)


def safe_directory(rootfs: Path, relative: Path, label: str) -> Path:
    directory = rootfs / relative
    try:
        resolved_root = rootfs.resolve(strict=True)
        resolved_directory = directory.resolve(strict=True)
    except OSError as error:
        raise DistributionError(
            _("{label} directory is missing.", label=label)
        ) from error
    if (
        directory.is_symlink()
        or not directory.is_dir()
        or not resolved_directory.is_relative_to(resolved_root)
    ):
        raise DistributionError(
            _("Unsafe {label} directory.", label=label)
        )
    return directory


def safe_file(rootfs: Path, relative: Path, label: str) -> Path:
    path = rootfs / relative
    try:
        resolved_root = rootfs.resolve(strict=True)
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise DistributionError(
            _("{label} file is missing.", label=label)
        ) from error
    if (
        path.is_symlink()
        or not path.is_file()
        or not resolved.is_relative_to(resolved_root)
    ):
        raise DistributionError(_("Unsafe {label} file.", label=label))
    return path


def atomic_write(destination: Path, content: bytes, mode: int = 0o644) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=".spaces-",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fchmod(temporary.fileno(), mode)
            os.fsync(temporary.fileno())
        temporary_path.replace(destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def reconcile_pam_auth_update(
    rootfs: Path,
    enabled: bool,
    *,
    host_profile: Path,
    guest_profile: Path,
    distribution_name: str,
) -> bool:
    destination = rootfs / guest_profile
    if not enabled:
        try:
            destination.lstat()
        except FileNotFoundError:
            return True
        if destination.is_symlink() or not destination.is_file():
            raise DistributionError(
                _("Unsafe Spaces PAM profile: {path}.", path=destination)
            )

    try:
        profile = host_profile.read_bytes()
    except OSError as error:
        raise DistributionError(
            _("Spaces PAM profile is missing: {path}.", path=host_profile)
        ) from error
    safe_directory(
        rootfs,
        guest_profile.parent,
        _("{distribution} PAM profile", distribution=distribution_name),
    )
    if enabled:
        atomic_write(destination, profile)
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
                atomic_write(destination, profile)
            except OSError:
                pass
        raise DistributionError(
            _(
                "Could not update {distribution} PAM configuration.",
                distribution=distribution_name,
            )
        ) from error
    return True


def reconcile_managed_pam_file(
    rootfs: Path,
    enabled: bool,
    *,
    relative_path: Path,
    distribution_name: str,
) -> bool:
    path = safe_file(
        rootfs,
        relative_path,
        _("{distribution} PAM", distribution=distribution_name),
    )
    content = path.read_text(encoding="utf-8")
    block_count = content.count(SPACES_PAM_BLOCK)
    marker_present = "# Managed by Spaces: host authentication" in content
    module_present = "/run/spaces-host/bin/pam_spaces.so" in content
    if block_count > 1 or (block_count == 0 and (marker_present or module_present)):
        raise DistributionError(
            _(
                "Could not safely reconcile {distribution} PAM configuration.",
                distribution=distribution_name,
            )
        )
    if enabled and block_count == 1:
        return True
    if not enabled and block_count == 0:
        return True

    if enabled:
        lines = content.splitlines(keepends=True)
        insertion = 1 if lines and lines[0].startswith("#%PAM-") else 0
        lines.insert(insertion, SPACES_PAM_BLOCK)
        updated = "".join(lines)
    else:
        updated = content.replace(SPACES_PAM_BLOCK, "", 1)
    atomic_write(path, updated.encode("utf-8"), path.stat().st_mode & 0o777)
    return True
