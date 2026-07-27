"""Launch and supervise a Spaces machine."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from . import _
from . import core


NSPAWN = "/usr/bin/systemd-nspawn"
API_VFS_WRITABLE = "SYSTEMD_NSPAWN_API_VFS_WRITABLE"
BASE_CAPS = (
    # This is the initial set by nspawn and should be trimmed
    "CAP_AUDIT_CONTROL",
    "CAP_AUDIT_WRITE",
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
    "CAP_SYS_ADMIN", # todo: slowly work towards removing this by default
    "CAP_SYS_BOOT",
    "CAP_SYS_CHROOT",
    "CAP_SYS_NICE",
    "CAP_SYS_PTRACE",
    "CAP_SYS_RESOURCE",
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


def _load_space(space_name: str) -> tuple[Path, dict[str, Any]]:
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
    return rootfs, info


def _command(space_name: str, rootfs: Path, network: str) -> list[str]:
    capabilities = (*BASE_CAPS, *NETWORK_CAPS[network])
    return [
        NSPAWN,
        "--quiet",
        f"--directory={rootfs}",
        f"--machine={space_name}",
        "--boot",
        "--setenv=SYSTEMD_GETTY_AUTO=no",
        "--console=read-only",
        "--private-users=no",
        "--keep-unit",
        "--settings=no",
        "--notify-ready=yes",
        "--drop-capability=all",
        f"--capability={','.join(capabilities)}",
    ]


def launch(space_name: str) -> int:
    """Run a space until its nspawn machine exits."""

    rootfs, info = _load_space(space_name)
    network = info["permissions"]["system"]["network"]
    environment = os.environ.copy()
    environment.pop(API_VFS_WRITABLE, None)
    if network == "admin":
        environment[API_VFS_WRITABLE] = "network"

    # Future session and mount workers must start before this blocking call.
    completed = subprocess.run(
        _command(space_name, rootfs, network),
        check=False,
        env=environment,
    )
    return completed.returncode
