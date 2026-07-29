"""Prepared chroot helpers shared by Debian-family distributions."""

from __future__ import annotations

import contextlib
import stat
from pathlib import Path
from typing import Iterator

from .. import _
from .model import DistributionError
from .mounts import mounted_api_filesystems
from .pam import atomic_write, safe_directory


GUEST_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
POLICY_RC_D = Path("usr/sbin/policy-rc.d")
POLICY_RC_D_CONTENT = b"#!/bin/sh\nexit 101\n"


def chroot_command(rootfs: Path, *arguments: str) -> list[str]:
    return [
        "chroot",
        str(rootfs),
        "/usr/bin/env",
        f"PATH={GUEST_PATH}",
        "DEBIAN_FRONTEND=noninteractive",
        *arguments,
    ]


def _ensure_directory(rootfs: Path, relative: Path, label: str) -> Path:
    current = rootfs
    for part in relative.parts:
        current /= part
        try:
            status = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o755)
            continue
        if not stat.S_ISDIR(status.st_mode) or current.is_symlink():
            raise DistributionError(_("Unsafe {label} directory.", label=label))
    return safe_directory(rootfs, relative, label)


@contextlib.contextmanager
def prepared_chroot(
    rootfs: Path,
    distribution_name: str,
) -> Iterator[None]:
    policy_path = rootfs / POLICY_RC_D
    previous_policy: tuple[bytes, int] | None = None
    try:
        policy_status = policy_path.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(policy_status.st_mode) or policy_path.is_symlink():
            raise DistributionError(
                _(
                    "Unsafe {distribution} service policy path.",
                    distribution=distribution_name,
                )
            )
        previous_policy = (
            policy_path.read_bytes(),
            stat.S_IMODE(policy_status.st_mode),
        )

    _ensure_directory(
        rootfs,
        POLICY_RC_D.parent,
        _(
            "{distribution} service policy",
            distribution=distribution_name,
        ),
    )
    atomic_write(policy_path, POLICY_RC_D_CONTENT, 0o755)
    try:
        with mounted_api_filesystems(rootfs, distribution_name):
            yield
    finally:
        if previous_policy is None:
            policy_path.unlink(missing_ok=True)
        else:
            content, mode = previous_policy
            atomic_write(policy_path, content, mode)
