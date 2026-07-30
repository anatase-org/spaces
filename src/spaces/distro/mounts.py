"""Temporary API filesystem mounts for distribution bootstrapping."""

from __future__ import annotations

import contextlib
import os
import stat
import subprocess
from pathlib import Path
from typing import Iterator

from .. import _
from .model import DistributionError
from .pam import safe_directory


SELINUXFS = Path("/sys/fs/selinux")


@contextlib.contextmanager
def hidden_selinuxfs() -> Iterator[None]:
    """Run children in a mount namespace where host SELinux is undiscoverable."""

    if not SELINUXFS.is_mount():
        yield
        return

    namespace = os.open("/proc/self/ns/mnt", os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.unshare(os.CLONE_NEWNS)
        try:
            subprocess.run(["mount", "--make-rprivate", "/"], check=True)
            subprocess.run(["umount", str(SELINUXFS)], check=True)
            yield
        finally:
            os.setns(namespace, os.CLONE_NEWNS)
    finally:
        os.close(namespace)


def _ensure_directory(rootfs: Path, name: str, distribution_name: str) -> Path:
    directory = rootfs / name
    try:
        status = directory.lstat()
    except FileNotFoundError:
        directory.mkdir(mode=0o755)
    else:
        if not stat.S_ISDIR(status.st_mode) or directory.is_symlink():
            raise DistributionError(
                _(
                    "Unsafe {distribution} {name} directory.",
                    distribution=distribution_name,
                    name=name,
                )
            )
    return safe_directory(
        rootfs,
        Path(name),
        _(
            "{distribution} {name}",
            distribution=distribution_name,
            name=name,
        ),
    )


@contextlib.contextmanager
def mounted_api_filesystems(
    rootfs: Path,
    distribution_name: str,
) -> Iterator[None]:
    """Mount restricted proc and sysfs instances during package installation."""

    proc = _ensure_directory(rootfs, "proc", distribution_name)
    sysfs = _ensure_directory(rootfs, "sys", distribution_name)
    mounted: list[Path] = []
    try:
        for filesystem, options, target in (
            ("proc", "nosuid,noexec,nodev", proc),
            ("sysfs", "ro,nosuid,noexec,nodev", sysfs),
        ):
            subprocess.run(
                [
                    "mount",
                    "--types",
                    filesystem,
                    "--options",
                    options,
                    filesystem,
                    str(target),
                ],
                check=True,
            )
            mounted.append(target)
        yield
    finally:
        for target in reversed(mounted):
            subprocess.run(["umount", str(target)], check=True)
