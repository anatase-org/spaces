"""Persistent per-space storage helpers."""

from __future__ import annotations

import contextlib
import shutil
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path

from . import _
from . import core


CACHE_PATH = Path("var/cache")


def _directory(path: Path, label: str, *, create: bool = False) -> Path:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if not create:
            raise
        path.mkdir(mode=0o755)
        metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise core.SpacesError(
            _("Unsafe {label} path: {path}.", label=label, path=path)
        )
    return path


def prepare_persistent_cache(space: Path) -> Path:
    """Migrate and prepare a space's persistent /var/cache storage."""

    rootfs = _directory(space / "rootfs", _("rootfs"))
    var = _directory(rootfs / "var", _("rootfs /var"), create=True)
    guest_cache = var / "cache"
    cache_root = _directory(
        core.CACHE_ROOT,
        _("Spaces cache root"),
        create=True,
    )
    cache = cache_root / space.name

    if cache.exists() or cache.is_symlink():
        _directory(cache, _("persistent cache"))
    elif guest_cache.exists() or guest_cache.is_symlink():
        _directory(guest_cache, _("rootfs /var/cache"))
        shutil.move(guest_cache, cache)
    else:
        cache.mkdir(mode=0o755)

    _directory(cache, _("persistent cache"))
    _directory(guest_cache, _("rootfs /var/cache"), create=True)
    return cache


@contextlib.contextmanager
def mounted_persistent_cache(rootfs: Path, cache: Path) -> Iterator[None]:
    """Bind persistent cache storage into a rootfs while it is bootstrapped."""

    target = rootfs / CACHE_PATH
    subprocess.run(
        ["mount", "--bind", str(cache), str(target)],
        check=True,
    )
    try:
        yield
    finally:
        subprocess.run(["umount", str(target)], check=True)
