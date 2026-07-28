"""Launch and supervise a Spaces machine."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from . import _
from . import core
from .logging import configure_logging


logger = logging.getLogger(__name__)

NSPAWN = "/usr/bin/systemd-nspawn"
API_VFS_WRITABLE = "SYSTEMD_NSPAWN_API_VFS_WRITABLE"
SYMLINKS = [
    ("/var/home", "/home"),
]
KEPT_CAPS = (
    "CAP_CHOWN",
    "CAP_DAC_OVERRIDE",
    "CAP_DAC_READ_SEARCH",
    "CAP_FOWNER",
    "CAP_FSETID",
    "CAP_IPC_OWNER",
    "CAP_KILL",
    "CAP_LEASE",
    "CAP_LINUX_IMMUTABLE",
    "CAP_MKNOD",
    "CAP_SETFCAP",
    "CAP_SETGID",
    "CAP_SETPCAP",
    "CAP_SETUID",
    "CAP_SYS_ADMIN",
    "CAP_SYS_BOOT",
    "CAP_SYS_CHROOT",
    "CAP_SYS_NICE",
    "CAP_SYS_RESOURCE",
)
DROPPED_CAPS = (
    "CAP_AUDIT_CONTROL",
    "CAP_AUDIT_WRITE",
    "CAP_NET_BIND_SERVICE",
    "CAP_NET_BROADCAST",
    "CAP_NET_RAW",
    "CAP_SYS_PTRACE",
    "CAP_SYS_TTY_CONFIG",
)
NETWORK_CAPS = {
    "basic": (),
    "advanced": ("CAP_NET_BIND_SERVICE",),
    "admin": (
        "CAP_NET_BIND_SERVICE",
        "CAP_NET_RAW",
        "CAP_NET_ADMIN",
    ),
}


def _apply_rootfs_fixups(rootfs: Path) -> None:
    """Apply persistent compatibility fixups to a space rootfs."""

    for link_name, target in SYMLINKS:
        try:
            relative_link = Path(link_name).relative_to("/")
            parent = rootfs
            parent_is_safe = True
            for component in relative_link.parts[:-1]:
                parent /= component
                try:
                    parent.mkdir()
                except FileExistsError:
                    pass
                if parent.is_symlink() or not parent.is_dir():
                    logger.error(
                        _(
                            "Could not create rootfs symlink {link}: "
                            "unsafe parent path {parent}.",
                            link=link_name,
                            parent=parent,
                        )
                    )
                    parent_is_safe = False
                    break
            if not parent_is_safe:
                continue

            link = parent / relative_link.name
            if link.is_symlink():
                if os.readlink(link) == target:
                    continue
                link.unlink()
            elif link.exists():
                logger.error(
                    _(
                        "Could not create rootfs symlink {link}: "
                        "the path exists and is not a symlink.",
                        link=link,
                    )
                )
                continue
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            logger.error(
                _(
                    "Could not create rootfs symlink {link}: {error}",
                    link=link_name,
                    error=error,
                )
            )


def _load_space(space_name: str) -> tuple[Path, Path, dict[str, Any]]:
    core.validate_space_name(space_name)
    space = core.STATE_ROOT / space_name
    if space.is_symlink() or not space.is_dir():
        raise core.SpacesError(
            _("Space {name!r} does not exist.", name=space_name)
        )

    rootfs = space / "rootfs"
    if rootfs.is_symlink() or not rootfs.is_dir():
        raise core.SpacesError(
            _("Space {name!r} has an unsafe or missing rootfs.", name=space_name)
        )

    home = space / "home"
    if home.is_symlink() or not home.is_dir():
        raise core.SpacesError(
            _("Space {name!r} has an unsafe or missing home.", name=space_name)
        )
    root_home = home / "root"
    try:
        root_home.mkdir(mode=0o700)
    except FileExistsError:
        pass
    if root_home.is_symlink() or not root_home.is_dir():
        raise core.SpacesError(
            _("Space {name!r} has an unsafe root home.", name=space_name)
        )

    info_path = space / "info.json"
    if info_path.is_symlink() or not info_path.is_file():
        raise core.SpacesError(
            _("Unsafe space information path: {path}.", path=info_path)
        )
    info = core.load_info(info_path)
    if info is None:
        raise core.SpacesError(
            _("Space {name!r} has an invalid info.json.", name=space_name)
        )
    if info["name"] != space_name:
        raise core.SpacesError(_("Space name does not match its info.json."))
    return rootfs, home, info


def _command(
    space_name: str,
    rootfs: Path,
    home: Path,
    network: str,
) -> list[str]:
    network_caps = NETWORK_CAPS[network]
    kept_caps = (*KEPT_CAPS, *network_caps)
    dropped_caps = tuple(
        capability
        for capability in DROPPED_CAPS
        if capability not in kept_caps
    )
    return [
        NSPAWN,
        "--quiet",
        f"--directory={rootfs}",
        f"--machine={space_name}",
        f"--bind={home}:/home",
        f"--bind={home / 'root'}:/root",
        "--boot",
        "--setenv=SYSTEMD_GETTY_AUTO=no",
        "--console=read-only",
        "--private-users=no",
        "--keep-unit",
        "--settings=no",
        "--notify-ready=yes",
        f"--drop-capability={','.join(dropped_caps)}",
        f"--capability={','.join(kept_caps)}",
    ]


def launch(space_name: str) -> int:
    """Run a space until its nspawn machine exits."""

    configure_logging(rich=False)
    rootfs, home, info = _load_space(space_name)
    network = info["permissions"]["system"]["network"]
    environment = os.environ.copy()
    environment.pop(API_VFS_WRITABLE, None)
    if network == "admin":
        environment[API_VFS_WRITABLE] = "network"

    _apply_rootfs_fixups(rootfs)

    # Future session and mount workers must start before this blocking call.
    completed = subprocess.run(
        _command(space_name, rootfs, home, network),
        check=False,
        env=environment,
    )
    return completed.returncode
