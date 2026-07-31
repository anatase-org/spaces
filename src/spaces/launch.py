"""Launch and supervise a Spaces machine."""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import logging
import os
import pwd
import select
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import _
from . import auth
from . import core
from . import devices
from . import host_config
from . import session
from . import shortcuts
from .distro import get_driver
from .logging import configure_logging


logger = logging.getLogger(__name__)

NSPAWN = "/usr/bin/systemd-nspawn"
MACHINECTL = "/usr/bin/machinectl"
SYSTEMD_RUN = "/usr/bin/systemd-run"
UMOUNT = "/usr/bin/umount"
BUSCTL = "/usr/bin/busctl"
API_VFS_WRITABLE = "SYSTEMD_NSPAWN_API_VFS_WRITABLE"
SELINUXFS = Path("/sys/fs/selinux")
SELINUX_GUEST_PATH = Path("/sys/fs/selinux")
SELINUX_POLICY_PACKAGE = Path("/usr/share/selinux/packages/spaces.pp")
SELINUX_PROCESS_CONTEXT = "system_u:system_r:spaces_container_t:s0"
SELINUX_APIFS_CONTEXT = "system_u:object_r:spaces_apifs_file_t:s0"
PING_GROUP_RANGE = Path("/proc/sys/net/ipv4/ping_group_range")
HOST_MEDIA_ROOT = Path("/run/media")
UNPRIVILEGED_PING_GROUP_RANGE = (0, 2_147_483_647)
PING_EXECUTABLES = ("/usr/bin/ping", "/bin/ping")
CAPABILITY_XATTR = "security.capability"
ELIGIBLE_USER_STATES = frozenset({"active", "online", "lingering"})
INELIGIBLE_USER_STATES = frozenset({"closing", "offline"})
LOGIN_RECONCILE_INTERVAL_SECONDS = 1.0
SESSION_RECONCILE_INTERVAL_SECONDS = 5.0
BASE_DEVICE_ALLOW = (
    ("/dev/net/tun", "rwm"),
    ("char-pts", "rw"),
    ("/dev/fuse", "rwm"),
)
ROOTFS_SYMLINKS = (("/var/home", "/home"),)
ZSH_SKELETON_PATH = Path("/etc/skel/.zshrc")
MASKED_UNIT_DESTINATIONS = (
    # Avoid messing with the network
    "/etc/systemd/system/netplan-configure.service",
    "/etc/systemd/system/NetworkManager-config-initrd.service",
    "/etc/systemd/system/NetworkManager-dispatcher.service",
    "/etc/systemd/system/NetworkManager-initrd.service",
    "/etc/systemd/system/NetworkManager-ovs.service",
    "/etc/systemd/system/NetworkManager-wait-online-initrd.service",
    "/etc/systemd/system/NetworkManager-wait-online.service",
    "/etc/systemd/system/NetworkManager.service",
    "/etc/systemd/system/nm-cloud-setup.service",
    "/etc/systemd/system/nm-cloud-setup.timer",
    "/etc/systemd/system/nm-priv-helper.service",
    # Avoid claiming host Bluetooth adapters.
    "/etc/systemd/system/bluetooth-mesh.service",
    "/etc/systemd/system/bluetooth.service",
    "/etc/systemd/system/bluetooth.target",
    "/etc/systemd/system/dbus-org.bluez.service",
    "/etc/systemd/user/dbus-org.bluez.obex.service",
    "/etc/systemd/user/obex.service",
    # Guest audio daemons must not connect to and manage the forwarded host
    # PipeWire instance. Applications use the forwarded sockets directly.
    "/etc/systemd/user/filter-chain.service",
    "/etc/systemd/user/pipewire-media-session.service",
    "/etc/systemd/user/pipewire-pulse.service",
    "/etc/systemd/user/pipewire-pulse.socket",
    "/etc/systemd/user/pipewire-session-manager.service",
    "/etc/systemd/user/pipewire.service",
    "/etc/systemd/user/pipewire.socket",
    "/etc/systemd/user/pulseaudio.service",
    "/etc/systemd/user/pulseaudio.socket",
    "/etc/systemd/user/wireplumber.service",
    "/etc/systemd/user/wireplumber@.service",
    # PrivateNetwork makes rtkit mount another sysfs instance. Avoid granting
    # the whole guest permission to mount host sysfs for this redundant daemon.
    "/etc/systemd/system/rtkit-daemon.service",
)
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
DEVELOPMENT_KERNEL_CAPS = (
    "CAP_AUDIT_CONTROL",
    "CAP_AUDIT_WRITE",
    "CAP_SYS_PTRACE",
    "CAP_PERFMON",
    "CAP_BPF",
)
KERNEL_CAPS = {
    "basic": (),
    "development": DEVELOPMENT_KERNEL_CAPS,
    "admin": DEVELOPMENT_KERNEL_CAPS,
}


@dataclass(frozen=True)
class SpaceUser:
    """A configured user resolved against the host passwd database."""

    uid: int
    gid: int
    name: str
    host_home: Path
    space_home: Path
    guest_home: PurePosixPath
    permitted_home: tuple[str, ...]
    administrator: bool = True
    desktop: bool = True
    credential_agents: bool = False
    mounted_drives: bool = False


@dataclass(frozen=True, order=True)
class HomeMount:
    """One permitted host home entry and its destination in the space."""

    destination: str
    source: Path
    uid: int


def _drop_ping_capability(rootfs: Path) -> None:
    """Let ping use ICMP sockets when every group is permitted to use them."""

    try:
        ping_group_range = tuple(
            int(value) for value in PING_GROUP_RANGE.read_text().split()
        )
    except (OSError, ValueError):
        return
    if ping_group_range != UNPRIVILEGED_PING_GROUP_RANGE:
        return

    resolved_rootfs = rootfs.resolve(strict=True)
    handled_files: set[tuple[int, int]] = set()
    for executable in PING_EXECUTABLES:
        candidate = rootfs / Path(executable).relative_to("/")
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not resolved.is_relative_to(resolved_rootfs):
            logger.error(
                _(
                    "Could not remove ping capability from {path}: "
                    "the path resolves outside the rootfs.",
                    path=candidate,
                )
            )
            continue

        try:
            descriptor = os.open(
                resolved,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
        except OSError as error:
            logger.error(
                _(
                    "Could not open ping executable {path}: {error}",
                    path=candidate,
                    error=error,
                )
            )
            continue

        try:
            opened_path = Path(f"/proc/self/fd/{descriptor}").resolve(
                strict=True
            )
            file_stat = os.fstat(descriptor)
            if (
                not opened_path.is_relative_to(resolved_rootfs)
                or not stat.S_ISREG(file_stat.st_mode)
            ):
                logger.error(
                    _(
                        "Could not remove ping capability from {path}: "
                        "the opened path is not a regular rootfs file.",
                        path=candidate,
                    )
                )
                continue

            identity = (file_stat.st_dev, file_stat.st_ino)
            if identity in handled_files:
                continue
            handled_files.add(identity)
            try:
                os.removexattr(descriptor, CAPABILITY_XATTR)
            except OSError as error:
                if error.errno != errno.ENODATA:
                    logger.error(
                        _(
                            "Could not remove ping capability from "
                            "{path}: {error}",
                            path=candidate,
                            error=error,
                        )
                    )
        except (OSError, RuntimeError) as error:
            logger.error(
                _(
                    "Could not inspect ping executable {path}: {error}",
                    path=candidate,
                    error=error,
                )
            )
        finally:
            os.close(descriptor)


def _apply_rootfs_fixups(rootfs: Path) -> None:
    """Apply persistent compatibility fixups to a space rootfs."""

    for link_name, target in ROOTFS_SYMLINKS:
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
        except Exception as error:
            logger.error(
                _(
                    "Could not apply rootfs symlink {link}: {error}",
                    link=link_name,
                    error=error,
                )
            )

    try:
        skeleton = rootfs / ZSH_SKELETON_PATH.parent.relative_to("/")
        if skeleton.is_symlink() or not skeleton.is_dir():
            raise OSError(_("the skeleton directory is missing or unsafe"))
        zshrc = skeleton / ZSH_SKELETON_PATH.name
        try:
            descriptor = os.open(
                zshrc,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | os.O_CLOEXEC,
                0o644,
            )
        except FileExistsError:
            if zshrc.is_symlink() or not zshrc.is_file():
                raise OSError(_("the skeleton file is unsafe"))
        else:
            os.close(descriptor)
    except (OSError, RuntimeError, ValueError) as error:
        logger.error(
            _(
                "Could not prepare rootfs Zsh skeleton file {path}: {error}",
                path=ZSH_SKELETON_PATH,
                error=error,
            )
        )

    _drop_ping_capability(rootfs)


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


def _safe_user_name(name: str) -> bool:
    return (
        bool(name)
        and name not in {".", ".."}
        and not any(character in name for character in "/:\0\n\r")
    )


def _resolve_users(info: dict[str, Any], home: Path) -> tuple[SpaceUser, ...]:
    users: list[SpaceUser] = []
    for uid_key, record in info["permissions"]["users"].items():
        user_permissions = core.effective_user_permissions(record)
        uid = int(uid_key)
        try:
            host_user = pwd.getpwuid(uid)
        except KeyError:
            logger.warning(
                _(
                    "Configured UID {uid} does not exist on the host; "
                    "skipping it.",
                    uid=uid,
                )
            )
            continue
        if not _safe_user_name(host_user.pw_name):
            raise core.SpacesError(
                _("Host UID {uid} has an unsafe user name.", uid=uid)
            )
        host_home = Path(host_user.pw_dir)
        if not host_home.is_absolute():
            raise core.SpacesError(
                _("Host UID {uid} has a non-absolute home path.", uid=uid)
            )

        is_root = uid == 0
        name = host_user.pw_name
        space_home = home / ("root" if is_root else name)
        guest_home = PurePosixPath("/root" if is_root else f"/home/{name}")
        users.append(
            SpaceUser(
                uid=uid,
                gid=record["gid"],
                name=name,
                host_home=host_home,
                space_home=space_home,
                guest_home=guest_home,
                permitted_home=tuple(user_permissions["home"]),
                administrator=user_permissions.get("administrator", True),
                desktop=user_permissions.get("desktop", True),
                credential_agents=user_permissions.get(
                    "credential_agents", True
                ),
                mounted_drives=user_permissions.get(
                    "mounted_drives", True
                ),
            )
        )
    return tuple(users)


def _read_database(path: Path, fields: int) -> list[list[str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise core.SpacesError(
            _(
                "Could not read account database {path}: {error}",
                path=path,
                error=error,
            )
        ) from error

    records: list[list[str]] = []
    for line in lines:
        record = line.split(":")
        if len(record) != fields:
            raise core.SpacesError(
                _("Account database {path} contains an invalid record.", path=path)
            )
        records.append(record)
    return records


def _atomic_write_database(path: Path, records: list[list[str]]) -> None:
    try:
        metadata = path.stat()
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                for record in records:
                    output.write(":".join(record))
                    output.write("\n")
                output.flush()
                os.fsync(output.fileno())
                os.fchmod(output.fileno(), metadata.st_mode & 0o7777)
                os.fchown(output.fileno(), metadata.st_uid, metadata.st_gid)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError as error:
        raise core.SpacesError(
            _(
                "Could not update account database {path}: {error}",
                path=path,
                error=error,
            )
        ) from error


def _numeric_field(record: list[str], index: int, path: Path) -> int:
    try:
        return int(record[index])
    except ValueError as error:
        raise core.SpacesError(
            _("Account database {path} contains a non-numeric ID.", path=path)
        ) from error


def _rename_members(
    records: list[list[str]],
    old_name: str,
    new_name: str,
    *fields: int,
) -> None:
    if old_name == new_name:
        return
    for record in records:
        for field in fields:
            members = record[field].split(",") if record[field] else []
            if old_name not in members:
                continue
            record[field] = ",".join(
                new_name if member == old_name else member for member in members
            )


def _set_group_member(
    records: list[list[str]],
    group_name: str,
    user_name: str,
    enabled: bool,
    field: int,
) -> None:
    group = next((record for record in records if record[0] == group_name), None)
    if group is None:
        return
    members = group[field].split(",") if group[field] else []
    if enabled and user_name not in members:
        members.append(user_name)
    elif not enabled:
        members = [member for member in members if member != user_name]
    group[field] = ",".join(members)


def _ensure_administrator_group(
    group_records: list[list[str]],
    gshadow_records: list[list[str]] | None,
    users: tuple[SpaceUser, ...],
    group_path: Path,
    group_name: str,
) -> None:
    if not any(user.administrator for user in users):
        return

    if not any(record[0] == group_name for record in group_records):
        used_gids = {
            _numeric_field(record, 2, group_path) for record in group_records
        }
        used_gids.update(user.gid for user in users)
        gid = next(
            (
                candidate
                for candidates in (range(999, 0, -1), range(1000, 60000))
                for candidate in candidates
                if candidate not in used_gids
            ),
            None,
        )
        if gid is None:
            raise core.SpacesError(
                _(
                    "Could not allocate a GID for the {group} group.",
                    group=group_name,
                )
            )
        group_records.append([group_name, "x", str(gid), ""])

    if gshadow_records is not None and not any(
        record[0] == group_name for record in gshadow_records
    ):
        gshadow_records.append([group_name, "!", "", ""])


def _account_shell(rootfs: Path) -> str:
    resolved_rootfs = rootfs.resolve(strict=True)
    for shell in ("/bin/bash", "/usr/bin/bash"):
        candidate = rootfs / shell.removeprefix("/")
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if (
            resolved.is_relative_to(resolved_rootfs)
            and resolved.is_file()
            and os.access(resolved, os.X_OK)
        ):
            return shell
    return "/bin/sh"


def _reconcile_accounts(
    rootfs: Path,
    users: tuple[SpaceUser, ...],
    administrator_group: str = "wheel",
) -> None:
    non_root_users = tuple(user for user in users if user.uid != 0)
    if not non_root_users:
        return

    etc = rootfs / "etc"
    if etc.is_symlink() or not etc.is_dir():
        raise core.SpacesError(
            _("Unsafe rootfs account directory: {path}.", path=etc)
        )
    passwd_path = etc / "passwd"
    group_path = etc / "group"
    for path in (passwd_path, group_path):
        if path.is_symlink() or not path.is_file():
            raise core.SpacesError(
                _("Unsafe account database path: {path}.", path=path)
            )
    passwd_records = _read_database(passwd_path, 7)
    group_records = _read_database(group_path, 4)

    shadow_path = etc / "shadow"
    shadow_records = (
        _read_database(shadow_path, 9)
        if shadow_path.exists() and not shadow_path.is_symlink()
        else None
    )
    if shadow_path.is_symlink():
        raise core.SpacesError(
            _("Unsafe account database path: {path}.", path=shadow_path)
        )

    gshadow_path = etc / "gshadow"
    gshadow_records = (
        _read_database(gshadow_path, 4)
        if gshadow_path.exists() and not gshadow_path.is_symlink()
        else None
    )
    if gshadow_path.is_symlink():
        raise core.SpacesError(
            _("Unsafe account database path: {path}.", path=gshadow_path)
        )

    shell = _account_shell(rootfs)
    _ensure_administrator_group(
        group_records,
        gshadow_records,
        non_root_users,
        group_path,
        administrator_group,
    )
    for user in non_root_users:
        names = {record[0]: index for index, record in enumerate(passwd_records)}
        uids = {
            _numeric_field(record, 2, passwd_path): index
            for index, record in enumerate(passwd_records)
        }
        name_index = names.get(user.name)
        uid_index = uids.get(user.uid)
        if name_index is not None and uid_index is not None and name_index != uid_index:
            raise core.SpacesError(
                _(
                    "Rootfs user name {name!r} and UID {uid} belong to "
                    "different accounts.",
                    name=user.name,
                    uid=user.uid,
                )
            )

        index = uid_index if uid_index is not None else name_index
        if index is None:
            old_name = user.name
            passwd_records.append(
                [
                    user.name,
                    "x" if shadow_records is not None else "!",
                    str(user.uid),
                    str(user.gid),
                    "",
                    str(user.guest_home),
                    shell,
                ]
            )
        else:
            account = passwd_records[index]
            old_name = account[0]
            account[0] = user.name
            account[1] = "x" if shadow_records is not None else "!"
            account[2] = str(user.uid)
            account[3] = str(user.gid)
            account[5] = str(user.guest_home)
            account[6] = shell

        gids = {
            _numeric_field(record, 2, group_path): index
            for index, record in enumerate(group_records)
        }
        if user.gid not in gids:
            group_name_index = next(
                (
                    index
                    for index, record in enumerate(group_records)
                    if record[0] == user.name
                ),
                None,
            )
            if group_name_index is None:
                group_records.append([user.name, "x", str(user.gid), ""])
                if gshadow_records is not None:
                    gshadow_records.append([user.name, "!", "", ""])
            else:
                group_records[group_name_index][2] = str(user.gid)

        _rename_members(group_records, old_name, user.name, 3)
        if gshadow_records is not None:
            _rename_members(gshadow_records, old_name, user.name, 2, 3)
        _set_group_member(
            group_records,
            administrator_group,
            user.name,
            user.administrator,
            3,
        )
        if gshadow_records is not None:
            _set_group_member(
                gshadow_records,
                administrator_group,
                user.name,
                user.administrator,
                3,
            )

        if shadow_records is not None:
            shadow_names = {
                record[0]: index for index, record in enumerate(shadow_records)
            }
            old_shadow_index = shadow_names.get(old_name)
            new_shadow_index = shadow_names.get(user.name)
            if (
                old_name != user.name
                and old_shadow_index is not None
                and new_shadow_index is not None
                and old_shadow_index != new_shadow_index
            ):
                raise core.SpacesError(
                    _(
                        "Rootfs shadow records conflict for user {name!r}.",
                        name=user.name,
                    )
                )
            shadow_index = (
                old_shadow_index
                if old_shadow_index is not None
                else new_shadow_index
            )
            if shadow_index is None:
                shadow_records.append(
                    [user.name, "!", "", "", "", "", "", "", ""]
                )
            else:
                shadow_records[shadow_index][0] = user.name
                shadow_records[shadow_index][1] = "!"

    _atomic_write_database(passwd_path, passwd_records)
    _atomic_write_database(group_path, group_records)
    if shadow_records is not None:
        _atomic_write_database(shadow_path, shadow_records)
    if gshadow_records is not None:
        _atomic_write_database(gshadow_path, gshadow_records)


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    os.chown(path, uid, gid, follow_symlinks=False)
    for parent, directories, files in os.walk(path, followlinks=False):
        parent_path = Path(parent)
        for name in (*directories, *files):
            os.chown(parent_path / name, uid, gid, follow_symlinks=False)


def _copy_skeleton(rootfs: Path, target: Path, user: SpaceUser) -> None:
    etc = rootfs / "etc"
    if etc.is_symlink() or not etc.is_dir():
        raise core.SpacesError(
            _("Unsafe rootfs skeleton directory: {path}.", path=etc)
        )
    skeleton = etc / "skel"
    if skeleton.is_symlink() or (skeleton.exists() and not skeleton.is_dir()):
        raise core.SpacesError(_("Unsafe skeleton path: {path}.", path=skeleton))

    staging = Path(
        tempfile.mkdtemp(prefix=f".{user.name}.", dir=target.parent)
    )
    try:
        if skeleton.is_dir():
            for source in skeleton.iterdir():
                destination = staging / source.name
                if source.is_symlink():
                    destination.symlink_to(os.readlink(source))
                elif source.is_dir():
                    shutil.copytree(source, destination, symlinks=True)
                else:
                    shutil.copy2(source, destination, follow_symlinks=False)
        _chown_tree(staging, user.uid, user.gid)
        os.chmod(staging, 0o700)
        staging.rename(target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _ensure_user_homes(rootfs: Path, users: tuple[SpaceUser, ...]) -> None:
    for user in users:
        if user.uid == 0:
            continue
        home = user.space_home
        if home.is_symlink() or (home.exists() and not home.is_dir()):
            raise core.SpacesError(_("Unsafe user home path: {path}.", path=home))
        if not home.exists():
            try:
                _copy_skeleton(rootfs, home, user)
            except OSError as error:
                raise core.SpacesError(
                    _(
                        "Could not create user home {path}: {error}",
                        path=home,
                        error=error,
                    )
                ) from error
        else:
            os.chown(home, user.uid, user.gid)
            os.chmod(home, 0o700)
        session.prepare_user_paths(
            user.space_home,
            user.uid,
            user.gid,
            user.name,
        )


def _prepare_mounts(users: tuple[SpaceUser, ...]) -> tuple[HomeMount, ...]:
    mounts: list[HomeMount] = []
    for user in users:
        for name in user.permitted_home:
            source = user.host_home / name
            try:
                source_fd = os.open(
                    source,
                    os.O_PATH
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC,
                )
                try:
                    source_stat = os.fstat(source_fd)
                    source_is_directory = stat.S_ISDIR(source_stat.st_mode)
                    source_is_file = stat.S_ISREG(source_stat.st_mode)
                    if not source_is_directory and not source_is_file:
                        raise OSError(
                            _("Permitted home source has an unsupported type.")
                        )
                    if source_is_directory and (
                        name.startswith(".") or "/" in name
                    ):
                        raise OSError(
                            _("Hidden or nested home directories are not permitted.")
                        )

                    source_parent = user.host_home
                    for component in PurePosixPath(name).parts[:-1]:
                        source_parent /= component
                        if (
                            source_parent.is_symlink()
                            or not source_parent.is_dir()
                        ):
                            raise OSError(
                                _("Permitted home source has an unsafe parent.")
                            )

                    resolved_home = user.host_home.resolve(strict=True)
                    resolved_source = source.resolve(strict=True)
                    resolved_stat = resolved_source.stat()
                    if (
                        not resolved_source.is_relative_to(resolved_home)
                        or source_stat.st_mode != resolved_stat.st_mode
                        or source_stat.st_dev != resolved_stat.st_dev
                        or source_stat.st_ino != resolved_stat.st_ino
                    ):
                        raise OSError(
                            _("Permitted home source changed during validation.")
                        )
                finally:
                    os.close(source_fd)
            except OSError:
                logger.warning(
                    _(
                        "Permitted home source {path} is missing or unsafe; "
                        "skipping it.",
                        path=source,
                    )
                )
                continue

            persistent_target = user.space_home / name
            created_paths: list[Path] = []
            try:
                target_parent = user.space_home
                for component in PurePosixPath(name).parts[:-1]:
                    target_parent /= component
                    try:
                        target_parent.mkdir(mode=0o700)
                    except FileExistsError:
                        pass
                    else:
                        created_paths.append(target_parent)
                        os.chown(target_parent, user.uid, user.gid)
                    if target_parent.is_symlink() or not target_parent.is_dir():
                        raise OSError(
                            _("Space home destination has an unsafe parent.")
                        )

                wrong_target_type = persistent_target.is_symlink() or (
                    persistent_target.exists()
                    and (
                        (source_is_directory and not persistent_target.is_dir())
                        or (source_is_file and not persistent_target.is_file())
                    )
                )
                if wrong_target_type:
                    logger.warning(
                        _(
                            "Space home destination {path} is unsafe; "
                            "skipping it.",
                            path=persistent_target,
                        )
                    )
                    continue
                if not persistent_target.exists():
                    if source_is_directory:
                        persistent_target.mkdir(mode=0o700)
                    else:
                        target_fd = os.open(
                            persistent_target,
                            os.O_WRONLY
                            | os.O_CREAT
                            | os.O_EXCL
                            | os.O_NOFOLLOW
                            | os.O_CLOEXEC,
                            0o600,
                        )
                        os.close(target_fd)
                    created_paths.append(persistent_target)
                    os.chown(persistent_target, user.uid, user.gid)
            except OSError as error:
                logger.warning(
                    _(
                        "Could not safely prepare space home destination "
                        "{path}; skipping it: {error}",
                        path=persistent_target,
                        error=error,
                    )
                )
                for created_path in reversed(created_paths):
                    try:
                        if created_path.is_dir():
                            created_path.rmdir()
                        else:
                            created_path.unlink()
                    except OSError:
                        pass
                continue

            mounts.append(
                HomeMount(
                    destination=str(user.guest_home / name),
                    source=resolved_source,
                    uid=user.uid,
                )
            )
    return tuple(sorted(mounts))


def _prepare_mounted_drive_mounts(
    users: tuple[SpaceUser, ...],
    media_root: Path | None = None,
) -> tuple[HomeMount, ...]:
    """Create and expose each permitted user's host media directory."""

    permitted_users = tuple(user for user in users if user.mounted_drives)
    if not permitted_users:
        return ()
    if media_root is None:
        media_root = HOST_MEDIA_ROOT

    try:
        media_root.mkdir(mode=0o755)
    except FileExistsError:
        pass
    except OSError as error:
        raise core.SpacesError(
            _(
                "Could not prepare mounted drive directory {path}: {error}",
                path=media_root,
                error=error,
            )
        ) from error

    try:
        root_fd = os.open(
            media_root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as error:
        raise core.SpacesError(
            _(
                "Mounted drive root {path} is unsafe: {error}",
                path=media_root,
                error=error,
            )
        ) from error

    mounts: list[HomeMount] = []
    try:
        root_stat = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or root_stat.st_uid != os.geteuid()
        ):
            raise core.SpacesError(
                _("Mounted drive root {path} is not root-owned.", path=media_root)
            )
        for user in permitted_users:
            created = False
            try:
                os.mkdir(user.name, mode=0o755, dir_fd=root_fd)
                created = True
            except FileExistsError:
                pass
            try:
                user_fd = os.open(
                    user.name,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC,
                    dir_fd=root_fd,
                )
            except OSError as error:
                raise core.SpacesError(
                    _(
                        "Mounted drive directory for {user} is unsafe: {error}",
                        user=user.name,
                        error=error,
                    )
                ) from error
            try:
                user_stat = os.fstat(user_fd)
                if (
                    not stat.S_ISDIR(user_stat.st_mode)
                    or user_stat.st_uid != os.geteuid()
                ):
                    raise core.SpacesError(
                        _(
                            "Mounted drive directory for {user} is not root-owned.",
                            user=user.name,
                        )
                    )
                if created:
                    os.fchmod(user_fd, 0o755)
            finally:
                os.close(user_fd)

            source = media_root / user.name
            mounts.append(
                HomeMount(
                    destination=str(PurePosixPath("/run/media") / user.name),
                    source=source,
                    uid=user.uid,
                )
            )
    finally:
        os.close(root_fd)
    return tuple(sorted(mounts))


def _plan_mounts(
    available_mounts: tuple[HomeMount, ...],
    eligible_uids: frozenset[int],
) -> tuple[HomeMount, ...]:
    return tuple(
        mount for mount in available_mounts if mount.uid in eligible_uids
    )


def _mount_summary(mounts: Iterable[HomeMount]) -> str:
    descriptions = [
        f"{mount.source} -> {mount.destination}" for mount in mounts
    ]
    return ", ".join(descriptions) if descriptions else _("none")


def _bind_argument(mount: HomeMount) -> str:
    return _path_bind_argument(mount.source, mount.destination)


def _path_bind_argument(
    source: Path,
    destination: str | PurePosixPath,
    *,
    read_only: bool = False,
) -> str:
    def escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace(":", "\\:")

    option = "--bind-ro" if read_only else "--bind"
    return f"{option}={escape(str(source))}:{escape(str(destination))}"


def _prepare_custom_mounts(
    rootfs: Path,
    mounts: tuple[host_config.Mount, ...],
) -> tuple[str, ...]:
    """Create safe bind targets and return read-only nspawn arguments."""

    arguments: list[str] = []
    resolved_rootfs = rootfs.resolve(strict=True)
    for mount in mounts:
        try:
            if not mount.source.exists():
                continue
            if not mount.source.is_dir() and not mount.source.is_file():
                raise OSError(_("the source is not a regular file or directory"))
            relative = Path(str(mount.destination)).relative_to("/")
            parent = rootfs
            for component in relative.parts[:-1]:
                parent /= component
                try:
                    parent.mkdir(mode=0o755)
                except FileExistsError:
                    pass
                if parent.is_symlink() or not parent.is_dir():
                    raise OSError(
                        _("unsafe destination parent {path}", path=parent)
                    )
                if not parent.resolve(strict=True).is_relative_to(resolved_rootfs):
                    raise OSError(
                        _("destination parent escapes the rootfs")
                    )

            destination = parent / relative.name
            if mount.source.is_dir():
                try:
                    destination.mkdir(mode=0o755)
                except FileExistsError:
                    pass
                if destination.is_symlink() or not destination.is_dir():
                    raise OSError(
                        _("destination is not a safe directory")
                    )
            else:
                try:
                    descriptor = os.open(
                        destination,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | os.O_NOFOLLOW
                        | os.O_CLOEXEC,
                        0o644,
                    )
                except FileExistsError:
                    if destination.is_symlink() or not destination.is_file():
                        raise OSError(
                            _("destination is not a safe regular file")
                        )
                else:
                    os.close(descriptor)
            arguments.append(
                _path_bind_argument(
                    mount.source,
                    mount.destination,
                    read_only=True,
                )
            )
        except (OSError, RuntimeError, ValueError) as error:
            logger.warning(
                _(
                    "Could not prepare read-only mount {source} at "
                    "{destination}; skipping it: {error}",
                    source=mount.source,
                    destination=mount.destination,
                    error=error,
                )
            )
    return tuple(arguments)


def _prepare_custom_overlays(
    rootfs: Path,
    overlays: tuple[host_config.Overlay, ...],
) -> tuple[str, ...]:
    """Expose overlay entries without making their whole destination read-only."""

    def raise_walk_error(error: OSError) -> None:
        raise error

    arguments: list[str] = []
    resolved_rootfs = rootfs.resolve(strict=True)
    for overlay in overlays:
        try:
            if not overlay.source.exists():
                continue
            if not overlay.source.is_dir():
                raise OSError(_("the overlay source is not a directory"))
            relative = Path(str(overlay.destination)).relative_to("/")
            destination = rootfs
            destination_available = True
            for component in relative.parts:
                destination /= component
                if destination.is_symlink():
                    raise OSError(
                        _(
                            "overlay destination is unsafe: {path}",
                            path=destination,
                        )
                    )
                if not destination.exists():
                    destination_available = False
                    break
                if not destination.is_dir():
                    raise OSError(
                        _(
                            "overlay destination is not a directory: {path}",
                            path=destination,
                        )
                    )
                if not destination.resolve(strict=True).is_relative_to(
                    resolved_rootfs
                ):
                    raise OSError(_("overlay destination escapes the rootfs"))
            if not destination_available:
                continue
            mounts: list[host_config.Mount] = []
            for parent, directories, files in os.walk(
                overlay.source,
                followlinks=False,
                onerror=raise_walk_error,
            ):
                parent_path = Path(parent)
                directories.sort()
                files.sort()
                linked_directories = {
                    name
                    for name in directories
                    if (parent_path / name).is_symlink()
                }
                directories[:] = [
                    name for name in directories if name not in linked_directories
                ]
                for name in sorted((*files, *linked_directories)):
                    source = parent_path / name
                    relative_source = source.relative_to(overlay.source)
                    mounts.append(
                        host_config.Mount(
                            source,
                            overlay.destination.joinpath(
                                *relative_source.parts
                            ),
                        )
                    )
            arguments.extend(
                _prepare_custom_mounts(rootfs, tuple(mounts))
            )
        except (OSError, RuntimeError, ValueError) as error:
            logger.warning(
                _(
                    "Could not prepare overlay {source} at "
                    "{destination}; skipping it: {error}",
                    source=overlay.source,
                    destination=overlay.destination,
                    error=error,
                )
            )
    return tuple(arguments)


def _unit_mask_bind_arguments() -> tuple[str, ...]:
    """Mask guest units for this launch without changing the rootfs."""

    return tuple(
        _path_bind_argument(
            Path("/dev/null"),
            destination,
            read_only=True,
        )
        for destination in MASKED_UNIT_DESTINATIONS
    )


def _set_device_policy(
    space_name: str,
    level: str,
    nodes: Iterable[devices.DeviceNode] = (),
) -> None:
    """Atomically replace the service instance's device cgroup policy."""

    unit = f"spaces@{space_name}.service"
    if level == "full":
        policy = "auto"
        allowed: tuple[tuple[str, str], ...] = ()
    else:
        policy = "closed"
        allowed = (
            *BASE_DEVICE_ALLOW,
            *((node.allow_spec, "rw") for node in sorted(nodes)),
        )
    subprocess.run(
        [
            BUSCTL,
            "call",
            "org.freedesktop.systemd1",
            "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager",
            "SetUnitProperties",
            "sba(sv)",
            unit,
            "true",
            "2",
            "DevicePolicy",
            "s",
            policy,
            "DeviceAllow",
            "a(ss)",
            str(len(allowed)),
            *(
                value
                for device_allow in allowed
                for value in device_allow
            ),
        ],
        check=True,
    )


class _LoginMonitor:
    """Small ctypes wrapper around systemd's sd-login monitor."""

    def __init__(self) -> None:
        library_name = ctypes.util.find_library("systemd") or "libsystemd.so.0"
        self._library = ctypes.CDLL(library_name, use_errno=True)
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._configure_functions()
        self._monitor = ctypes.c_void_p()
        self._read_fd = -1
        self._write_fd = -1
        try:
            self._raise_for_result(
                self._library.sd_login_monitor_new(
                    None, ctypes.byref(self._monitor)
                ),
                _("Could not create the systemd login monitor."),
            )
            self._read_fd, self._write_fd = os.pipe2(
                os.O_CLOEXEC | os.O_NONBLOCK
            )
            monitor_fd = self._library.sd_login_monitor_get_fd(self._monitor)
            self._raise_for_result(
                monitor_fd,
                _("Could not get the systemd login monitor descriptor."),
            )
            events = self._library.sd_login_monitor_get_events(self._monitor)
            self._raise_for_result(
                events,
                _("Could not get the systemd login monitor events."),
            )
            self._poll = select.poll()
            self._poll.register(monitor_fd, events)
            self._poll.register(self._read_fd, select.POLLIN)
        except Exception:
            self.close()
            raise

    def _configure_functions(self) -> None:
        self._library.sd_login_monitor_new.argtypes = [
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._library.sd_login_monitor_new.restype = ctypes.c_int
        self._library.sd_login_monitor_unref.argtypes = [ctypes.c_void_p]
        self._library.sd_login_monitor_unref.restype = ctypes.c_void_p
        self._library.sd_login_monitor_flush.argtypes = [ctypes.c_void_p]
        self._library.sd_login_monitor_flush.restype = ctypes.c_int
        self._library.sd_login_monitor_get_fd.argtypes = [ctypes.c_void_p]
        self._library.sd_login_monitor_get_fd.restype = ctypes.c_int
        self._library.sd_login_monitor_get_events.argtypes = [ctypes.c_void_p]
        self._library.sd_login_monitor_get_events.restype = ctypes.c_int
        self._library.sd_login_monitor_get_timeout.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint64),
        ]
        self._library.sd_login_monitor_get_timeout.restype = ctypes.c_int
        self._library.sd_uid_get_state.argtypes = [
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._library.sd_uid_get_state.restype = ctypes.c_int
        self._library.sd_uid_get_sessions.argtypes = [
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        ]
        self._library.sd_uid_get_sessions.restype = ctypes.c_int
        for name in (
            "sd_session_is_active",
            "sd_session_is_remote",
        ):
            function = getattr(self._library, name)
            function.argtypes = [ctypes.c_char_p]
            function.restype = ctypes.c_int
        for name in (
            "sd_session_get_type",
            "sd_session_get_class",
        ):
            function = getattr(self._library, name)
            function.argtypes = [
                ctypes.c_char_p,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            function.restype = ctypes.c_int
        self._libc.free.argtypes = [ctypes.c_void_p]
        self._libc.free.restype = None

    @staticmethod
    def _raise_for_result(result: int, message: str) -> None:
        if result < 0:
            raise core.SpacesError(f"{message} {os.strerror(-result)}")

    def state(self, uid: int) -> str:
        value = ctypes.c_void_p()
        self._raise_for_result(
            self._library.sd_uid_get_state(uid, ctypes.byref(value)),
            _("Could not query login state for UID {uid}.", uid=uid),
        )
        try:
            return ctypes.string_at(value).decode("utf-8")
        finally:
            self._libc.free(value)

    def _session_string(self, function_name: str, session_id: bytes) -> str:
        value = ctypes.c_void_p()
        function = getattr(self._library, function_name)
        self._raise_for_result(
            function(session_id, ctypes.byref(value)),
            _("Could not query systemd login session properties."),
        )
        try:
            return ctypes.string_at(value).decode("utf-8")
        finally:
            self._libc.free(value)

    def sessions(self, uid: int) -> tuple[session.LoginSession, ...]:
        values = ctypes.POINTER(ctypes.c_void_p)()
        count = self._library.sd_uid_get_sessions(
            uid, 0, ctypes.byref(values)
        )
        self._raise_for_result(
            count,
            _("Could not enumerate login sessions for UID {uid}.", uid=uid),
        )
        sessions: list[session.LoginSession] = []
        try:
            for index in range(count):
                pointer = values[index]
                session_id = ctypes.string_at(pointer)
                active = self._library.sd_session_is_active(session_id)
                remote = self._library.sd_session_is_remote(session_id)
                if active < 0 or remote < 0:
                    continue
                try:
                    session_type = self._session_string(
                        "sd_session_get_type", session_id
                    )
                    session_class = self._session_string(
                        "sd_session_get_class", session_id
                    )
                except core.SpacesError:
                    # Sessions can disappear between enumeration and property
                    # reads. A vanished record is not a monitor failure.
                    continue
                sessions.append(
                    session.LoginSession(
                        session_id=session_id.decode("utf-8"),
                        active=bool(active),
                        remote=bool(remote),
                        session_type=session_type,
                        session_class=session_class,
                    )
                )
        finally:
            for index in range(max(count, 0)):
                self._libc.free(values[index])
            if values:
                self._libc.free(values)
        return tuple(sessions)

    def wait(self, maximum_seconds: float | None = None) -> bool:
        timeout_ms = self._timeout_ms()
        if maximum_seconds is not None:
            maximum_ms = max(0, round(maximum_seconds * 1000))
            timeout_ms = (
                maximum_ms
                if timeout_ms is None
                else min(timeout_ms, maximum_ms)
            )
        monitor_fd = self._library.sd_login_monitor_get_fd(self._monitor)
        for descriptor, _events in self._poll.poll(timeout_ms):
            if descriptor == self._read_fd:
                return False
            if descriptor == monitor_fd:
                self._flush()
                return True
        # sd-login timeouts are notifications too. Flush them so an expired
        # absolute timeout cannot make the worker spin on zero-length polls.
        self._flush()
        return True

    def _timeout_ms(self) -> int | None:
        timeout_usec = ctypes.c_uint64()
        self._raise_for_result(
            self._library.sd_login_monitor_get_timeout(
                self._monitor, ctypes.byref(timeout_usec)
            ),
            _("Could not query the login monitor timeout."),
        )
        if timeout_usec.value == (1 << 64) - 1:
            timeout_ms = None
        else:
            now_usec = time.monotonic_ns() // 1000
            timeout_ms = max(
                0, (timeout_usec.value - now_usec + 999) // 1000
            )
        return timeout_ms

    def _flush(self) -> None:
        self._raise_for_result(
            self._library.sd_login_monitor_flush(self._monitor),
            _("Could not flush the login monitor."),
        )

    def stop(self) -> None:
        if self._write_fd < 0:
            return
        try:
            os.write(self._write_fd, b"\0")
        except (BlockingIOError, OSError):
            pass

    def close(self) -> None:
        if self._monitor:
            self._library.sd_login_monitor_unref(self._monitor)
            self._monitor = ctypes.c_void_p()
        if self._read_fd >= 0:
            os.close(self._read_fd)
            self._read_fd = -1
        if self._write_fd >= 0:
            os.close(self._write_fd)
            self._write_fd = -1


def _eligible_uids(
    monitor: _LoginMonitor,
    users: tuple[SpaceUser, ...],
) -> frozenset[int]:
    eligible: set[int] = set()
    for user in users:
        state = monitor.state(user.uid)
        if state in ELIGIBLE_USER_STATES:
            eligible.add(user.uid)
        elif state not in INELIGIBLE_USER_STATES:
            raise core.SpacesError(
                _(
                    "Systemd returned unknown login state {state!r} for UID {uid}.",
                    state=state,
                    uid=user.uid,
                )
            )
    return frozenset(eligible)


@dataclass(frozen=True)
class _LoginSnapshot:
    eligible_uids: frozenset[int]
    graphical_sessions: tuple[
        tuple[int, tuple[session.LoginSession, ...]], ...
    ]

    def sessions(self, uid: int) -> tuple[session.LoginSession, ...]:
        for item_uid, records in self.graphical_sessions:
            if item_uid == uid:
                return records
        return ()


def _login_snapshot(
    monitor: _LoginMonitor,
    users: tuple[SpaceUser, ...],
) -> _LoginSnapshot:
    eligible_uids = _eligible_uids(monitor, users)
    graphical_sessions: list[
        tuple[int, tuple[session.LoginSession, ...]]
    ] = []
    for user in users:
        if (
            not user.desktop
            or user.uid == 0
            or user.uid not in eligible_uids
        ):
            continue
        records = tuple(
            sorted(
                (
                    item
                    for item in monitor.sessions(user.uid)
                    if item.active
                    and not item.remote
                    and item.session_class == "user"
                    and item.session_type in {"wayland", "x11"}
                ),
                key=lambda item: item.session_id,
            )
        )
        graphical_sessions.append((user.uid, records))
    return _LoginSnapshot(
        eligible_uids=eligible_uids,
        graphical_sessions=tuple(graphical_sessions),
    )


class _MountWorker:
    """Reconcile planned mounts as configured users change login state."""

    def __init__(
        self,
        space_name: str,
        users: tuple[SpaceUser, ...],
        monitor: _LoginMonitor,
        available_mounts: tuple[HomeMount, ...],
        initial_mounts: tuple[HomeMount, ...],
        initial_eligible_uids: frozenset[int],
        portals_enabled: bool = True,
    ) -> None:
        self._space_name = space_name
        self._users = users
        self._monitor = monitor
        self._available_mounts = available_mounts
        self._mounted = set(initial_mounts)
        self._eligible_uids = initial_eligible_uids
        self._process: subprocess.Popen[Any] | None = None
        self._attached = threading.Event()
        self._stopping = threading.Event()
        self._registered = False
        self._login_snapshot: _LoginSnapshot | None = None
        self._last_reconcile_at: float | None = None
        self._portals_enabled = portals_enabled
        self._desktop = session.DesktopController(
            space_name,
            users,
            portals_enabled=portals_enabled,
        )
        self._thread = threading.Thread(
            target=self._run,
            name=f"spaces-{space_name}-mounts",
        )

    def start(self) -> None:
        self._thread.start()

    def attach(self, process: subprocess.Popen[Any]) -> None:
        self._process = process
        self._attached.set()

    def stop(self) -> None:
        self._stopping.set()
        self._attached.set()
        self._monitor.stop()

    def join(self) -> None:
        self._thread.join()

    def _run(self) -> None:
        try:
            self._attached.wait()
            if self._stopping.is_set() or self._process is None:
                return
            if not self._wait_until_registered():
                return
            if self._eligible_uids:
                self._log_initial_mounts()
            self._reconcile_retryable()
            self._last_reconcile_at = time.monotonic()
            while (
                not self._stopping.is_set()
                and self._monitor.wait(
                    SESSION_RECONCILE_INTERVAL_SECONDS
                    if (
                        self._portals_enabled
                        or any(
                            user.credential_agents
                            for user in self._users
                        )
                    )
                    else None
                )
            ):
                if self._stopping.is_set():
                    break
                if not self._wait_for_reconcile_slot():
                    break
                self._reconcile_retryable()
                self._last_reconcile_at = time.monotonic()
        except Exception as error:
            logger.error(_("User mount monitor failed: {error}", error=error))
            process = self._process
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGTERM)
        finally:
            try:
                process = self._process
                if process is None or process.poll() is not None:
                    self._desktop.abandon()
                else:
                    self._desktop.close()
            except Exception as error:
                logger.error(
                    _("Host session forwarding cleanup failed: {error}", error=error)
                )
                process = self._process
                if process is not None and process.poll() is None:
                    process.send_signal(signal.SIGTERM)

    def _reconcile_retryable(self) -> None:
        try:
            self._reconcile()
        except session.SessionResourceChangedError as error:
            # Login-session sockets can legitimately be replaced while the
            # host user manager is restarting.  Keep nspawn alive and rebuild
            # the complete plan on the next periodic reconciliation instead
            # of treating this narrow TOCTOU check as a worker failure.
            self._login_snapshot = None
            logger.warning(
                _(
                    "Host session resources changed during reconciliation; "
                    "retrying without stopping the space: {error}",
                    error=error,
                )
            )

    def _wait_for_reconcile_slot(self) -> bool:
        previous = self._last_reconcile_at
        if previous is None:
            return not self._stopping.is_set()
        remaining = max(
            0.0,
            previous
            + LOGIN_RECONCILE_INTERVAL_SECONDS
            - time.monotonic(),
        )
        return not self._stopping.wait(remaining)

    def _wait_until_registered(self) -> bool:
        if self._registered:
            return True
        process = self._process
        assert process is not None
        while not self._stopping.is_set() and process.poll() is None:
            machine = subprocess.run(
                [
                    MACHINECTL,
                    "--quiet",
                    "--no-ask-password",
                    "show",
                    "--property=Leader",
                    "--value",
                    self._space_name,
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if machine.returncode != 0:
                self._stopping.wait(0.05)
                continue
            guest_shell = subprocess.run(
                [
                    MACHINECTL,
                    "--quiet",
                    "--no-ask-password",
                    "--uid=root",
                    "--",
                    "shell",
                    self._space_name,
                    "/usr/bin/true",
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Registration precedes the guest system bus during early boot.
            # Desktop setup uses machinectl shell, so starting it before both
            # probes succeed leaves forwarding inactive until another login
            # event happens to trigger reconciliation.
            if guest_shell.returncode == 0:
                self._registered = True
                return True
            self._stopping.wait(0.05)
        return False

    def _reconcile(self) -> None:
        snapshot = _login_snapshot(self._monitor, self._users)
        if snapshot == self._login_snapshot:
            if any(user.credential_agents for user in self._users):
                if not self._reconcile_desktops(snapshot):
                    self._login_snapshot = None
            self._desktop.reconcile_portals()
            return
        eligible_uids = snapshot.eligible_uids
        desired = set(
            _plan_mounts(
                self._available_mounts,
                eligible_uids,
            )
        )
        logged_in = eligible_uids - self._eligible_uids
        logged_out = self._eligible_uids - eligible_uids
        additions = sorted(desired - self._mounted)
        removals = sorted(
            self._mounted - desired,
            key=lambda mount: mount.destination.count("/"),
            reverse=True,
        )
        if not additions and not removals:
            self._log_user_transitions(logged_in, logged_out, (), ())
            self._eligible_uids = eligible_uids
            self._login_snapshot = (
                snapshot if self._reconcile_desktops(snapshot) else None
            )
            return
        if not self._wait_until_registered():
            return
        removed: list[HomeMount] = []
        for mount in removals:
            self._remove(mount)
            self._mounted.remove(mount)
            removed.append(mount)
        added: list[HomeMount] = []
        for mount in additions:
            try:
                self._add(mount)
            except (OSError, subprocess.CalledProcessError) as error:
                logger.warning(
                    _(
                        "Could not safely add mount {source} at {destination}; "
                        "skipping it: {error}",
                        source=mount.source,
                        destination=mount.destination,
                        error=error,
                    )
                )
                continue
            self._mounted.add(mount)
            added.append(mount)
        self._log_user_transitions(logged_in, logged_out, added, removed)
        self._eligible_uids = eligible_uids
        self._login_snapshot = (
            snapshot if self._reconcile_desktops(snapshot) else None
        )

    def _reconcile_desktops(self, snapshot: _LoginSnapshot) -> bool:
        successful = True
        for user in self._users:
            try:
                arguments = (
                    user,
                    snapshot.sessions(user.uid),
                    tuple(
                        sorted(
                            session.OpenPathMapping(
                                mount.destination,
                                mount.source,
                            )
                            for mount in self._mounted
                            if mount.uid == user.uid
                        )
                    ),
                )
                if user.credential_agents:
                    self._desktop.reconcile(
                        *arguments,
                        session_active=user.uid in snapshot.eligible_uids,
                    )
                else:
                    self._desktop.reconcile(*arguments)
            except session.DesktopSetupError as error:
                successful = False
                logger.warning(
                    _(
                        "Could not enable host session forwarding for {user}: "
                        "{error}",
                        user=user.name,
                        error=error,
                    )
                )
        return successful

    def _log_initial_mounts(self) -> None:
        for user in self._users:
            if user.uid not in self._eligible_uids:
                continue
            mounts = _mount_summary(
                mount for mount in self._mounted if mount.uid == user.uid
            )
            identity = f"{user.name} ({user.uid}:{user.gid})"
            logger.info(
                _(
                    "User {user} mounted at space launch: {mounts}.",
                    user=identity,
                    mounts=mounts,
                )
            )

    def _log_user_transitions(
        self,
        logged_in: frozenset[int],
        logged_out: frozenset[int],
        added: tuple[HomeMount, ...] | list[HomeMount],
        removed: tuple[HomeMount, ...] | list[HomeMount],
    ) -> None:
        for user in self._users:
            identity = f"{user.name} ({user.uid}:{user.gid})"
            if user.uid in logged_out:
                mounts = _mount_summary(
                    mount for mount in removed if mount.uid == user.uid
                )
                logger.info(
                    _(
                        "User {user} logged out; unmounted: {mounts}.",
                        user=identity,
                        mounts=mounts,
                    )
                )
            if user.uid in logged_in:
                mounts = _mount_summary(
                    mount for mount in added if mount.uid == user.uid
                )
                logger.info(
                    _(
                        "User {user} logged in; mounted: {mounts}.",
                        user=identity,
                        mounts=mounts,
                    )
                )

    def _add(self, mount: HomeMount) -> None:
        subprocess.run(
            [
                MACHINECTL,
                "--quiet",
                "--no-ask-password",
                "--mkdir",
                "bind",
                self._space_name,
                str(mount.source),
                mount.destination,
            ],
            check=True,
        )

    def _remove(self, mount: HomeMount) -> None:
        _unmount_in_machine(self._space_name, mount.destination)


def _unmount_in_machine(space_name: str, destination: str) -> None:
    """Lazily revoke one runtime bind from a running space."""

    # LazyUnmount= is a mount-unit setting, but systemd does not expose it
    # through systemctl set-property. Run the stable umount(8) interface in
    # the guest manager instead; every supported systemd has these
    # systemd-run options (the newest, --pipe, was added in systemd 235).
    subprocess.run(
        [
            SYSTEMD_RUN,
            f"--machine={space_name}",
            "--no-ask-password",
            "--quiet",
            "--wait",
            "--pipe",
            "--collect",
            "--service-type=exec",
            "--",
            UMOUNT,
            "--lazy",
            "--",
            destination,
        ],
        check=True,
    )


class _DeviceWorker:
    """Reconcile filtered device binds when udev reports host changes."""

    def __init__(
        self,
        space_name: str,
        level: str,
        udev: devices.Udev,
        monitor: devices.UdevMonitor,
        initial_nodes: tuple[devices.DeviceNode, ...],
    ) -> None:
        self._space_name = space_name
        self._level = level
        self._udev = udev
        self._monitor = monitor
        self._mounted = set(initial_nodes)
        self._process: subprocess.Popen[Any] | None = None
        self._attached = threading.Event()
        self._stopping = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"spaces-{space_name}-devices",
        )

    def start(self) -> None:
        self._thread.start()

    def attach(self, process: subprocess.Popen[Any]) -> None:
        self._process = process
        self._attached.set()

    def stop(self) -> None:
        self._stopping.set()
        self._attached.set()
        self._monitor.stop()

    def join(self) -> None:
        self._thread.join()

    def _run(self) -> None:
        try:
            self._attached.wait()
            if self._stopping.is_set() or self._process is None:
                return
            if not self._wait_until_registered():
                return
            self._reconcile()
            while not self._stopping.is_set() and self._monitor.wait():
                if self._stopping.is_set():
                    break
                self._reconcile()
        except Exception as error:
            logger.error(
                _("Device monitor failed; stopping the space: {error}", error=error)
            )
            process = self._process
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGTERM)

    def _wait_until_registered(self) -> bool:
        process = self._process
        assert process is not None
        while not self._stopping.is_set() and process.poll() is None:
            machine = subprocess.run(
                [
                    MACHINECTL,
                    "--quiet",
                    "--no-ask-password",
                    "show",
                    "--property=Leader",
                    "--value",
                    self._space_name,
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if machine.returncode != 0:
                self._stopping.wait(0.05)
                continue
            guest_shell = subprocess.run(
                [
                    MACHINECTL,
                    "--quiet",
                    "--no-ask-password",
                    "--uid=root",
                    "--",
                    "shell",
                    self._space_name,
                    "/usr/bin/true",
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if guest_shell.returncode == 0:
                return True
            self._stopping.wait(0.05)
        return False

    def _reconcile(self) -> None:
        desired = set(
            devices.discover(
                self._level,
                metadata_reader=self._udev.metadata,
            )
        )
        removals = sorted(
            self._mounted - desired,
            key=lambda node: len(node.destination.parts),
            reverse=True,
        )
        for node in removals:
            _unmount_in_machine(self._space_name, str(node.destination))
            self._mounted.remove(node)
        if removals:
            _set_device_policy(
                self._space_name, self._level, self._mounted
            )

        for node in sorted(desired - self._mounted):
            proposed = {*self._mounted, node}
            try:
                _set_device_policy(self._space_name, self._level, proposed)
                subprocess.run(
                    [
                        MACHINECTL,
                        "--quiet",
                        "--no-ask-password",
                        "--mkdir",
                        "bind",
                        self._space_name,
                        str(node.source),
                        str(node.destination),
                    ],
                    check=True,
                )
            except (OSError, subprocess.CalledProcessError) as error:
                _set_device_policy(
                    self._space_name, self._level, self._mounted
                )
                logger.warning(
                    _(
                        "Could not expose device {device}; skipping it: {error}",
                        device=node.source,
                        error=error,
                    )
                )
                continue
            self._mounted.add(node)


def _device_bind_arguments(
    level: str,
    nodes: Iterable[devices.DeviceNode],
) -> tuple[str, ...]:
    return tuple(
        _path_bind_argument(node.source, node.destination)
        for node in sorted(nodes)
    )


def _selinux_arguments() -> tuple[str, ...]:
    """Hide host SELinux state and select labels when SELinux is active."""

    if not (SELINUXFS / "enforce").is_file():
        return ()
    mask = f"--inaccessible={SELINUX_GUEST_PATH}"
    if not SELINUX_POLICY_PACKAGE.is_file():
        logger.warning(
            _(
                "SELinux is active, but the Spaces policy package is "
                "missing; launching without an explicit container context."
            )
        )
        return (mask,)
    return (
        mask,
        f"--selinux-context={SELINUX_PROCESS_CONTEXT}",
        f"--selinux-apifs-context={SELINUX_APIFS_CONTEXT}",
    )


def _command(
    space_name: str,
    rootfs: Path,
    home: Path,
    network: str,
    kernel_capabilities: str,
    mounts: tuple[HomeMount, ...] = (),
    authentication_binds: tuple[str, ...] = (),
    custom_binds: tuple[str, ...] = (),
    custom_overlays: tuple[str, ...] = (),
) -> list[str]:
    network_caps = NETWORK_CAPS[network]
    kernel_caps = KERNEL_CAPS[kernel_capabilities]
    kept_caps = (*KEPT_CAPS, *network_caps, *kernel_caps)
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
        *_unit_mask_bind_arguments(),
        *authentication_binds,
        *custom_binds,
        *custom_overlays,
        *(_bind_argument(mount) for mount in mounts),
        "--boot",
        "--setenv=SYSTEMD_GETTY_AUTO=no",
        "--console=read-only",
        "--private-users=no",
        "--keep-unit",
        "--settings=no",
        "--notify-ready=yes",
        "--resolv-conf=bind-host",
        *_selinux_arguments(),
        *(
            ("--system-call-filter=perf_event_open",)
            if kernel_caps
            else ()
        ),
        f"--drop-capability={','.join(dropped_caps)}",
        f"--capability={','.join(kept_caps)}",
    ]


def launch(space_name: str) -> int:
    """Run a space until its nspawn machine exits."""

    configure_logging(rich=False)
    rootfs, home, info = _load_space(space_name)
    configuration = host_config.load()
    system_permissions = core.effective_system_permissions(
        info["permissions"]["system"]
    )
    network = system_permissions["network"]
    kernel_capabilities = system_permissions.get(
        "kernel_capabilities",
        "basic",
    )
    device_level = system_permissions.get("devices", "basic")
    host_authentication = system_permissions.get(
        "host_authentication",
        True,
    )
    shortcut_export = system_permissions.get("shortcuts", True)
    environment = os.environ.copy()
    environment.pop(API_VFS_WRITABLE, None)
    if kernel_capabilities == "admin":
        environment[API_VFS_WRITABLE] = "yes"
    elif network == "admin":
        environment[API_VFS_WRITABLE] = "network"

    _apply_rootfs_fixups(rootfs)
    users = _resolve_users(info, home)
    driver = get_driver(info["distribution"]["id"])
    administrator_group = (
        driver.administrator_group if driver is not None else "wheel"
    )
    _reconcile_accounts(
        rootfs,
        users,
        administrator_group,
    )
    distro_id = info["distribution"]["id"]
    custom_binds = _prepare_custom_mounts(
        rootfs,
        configuration.mounts_for(distro_id),
    )
    custom_overlays = _prepare_custom_overlays(
        rootfs,
        configuration.overlays_for(distro_id),
    )
    _ensure_user_homes(rootfs, users)
    session.initialize_status(space_name, users)
    authentication_supported = (
        driver.reconcile_host_authentication(
            rootfs,
            host_authentication,
        )
        if driver is not None
        else not host_authentication
    )
    if not authentication_supported:
        raise core.SpacesError(
            _(
                "Host authentication is enabled for {space}, but its "
                "distribution does not support PAM integration.",
                space=space_name,
            )
        )

    available_mounts = (
        *_prepare_mounts(users),
        *_prepare_mounted_drive_mounts(users),
    )
    monitor: _LoginMonitor | None = None
    worker: _MountWorker | None = None
    device_udev: devices.Udev | None = None
    device_monitor: devices.UdevMonitor | None = None
    device_worker: _DeviceWorker | None = None
    shortcut_monitor: shortcuts.InotifyMonitor | None = None
    shortcut_worker: shortcuts.ShortcutWorker | None = None
    device_policy_set = False
    authentication: auth.AuthenticationService | None = None
    authentication_binds: tuple[str, ...] = ()
    portal_binds = (
        session.portal_bind_arguments(rootfs)
        if any(user.desktop and user.uid != 0 for user in users)
        else ()
    )
    try:
        if shortcut_export:
            try:
                shortcuts.reconcile(
                    space_name,
                    rootfs,
                    info["distribution"]["id"],
                )
                shortcut_monitor = shortcuts.InotifyMonitor(rootfs)
                shortcut_worker = shortcuts.ShortcutWorker(
                    space_name,
                    rootfs,
                    info["distribution"]["id"],
                    shortcut_monitor,
                )
                shortcut_worker.start()
            except Exception as error:
                logger.error(
                    _(
                        "Could not initialize application shortcuts: {error}",
                        error=error,
                    )
                )
                if shortcut_monitor is not None:
                    shortcut_monitor.close()
                    shortcut_monitor = None
        else:
            try:
                shortcuts.remove(space_name)
            except Exception as error:
                logger.error(
                    _(
                        "Could not remove disabled application shortcuts: "
                        "{error}",
                        error=error,
                    )
                )
        native_required = host_authentication or any(
            user.desktop and user.uid != 0 for user in users
        )
        if native_required:
            auth.validate_native_runtime(rootfs)
            authentication_binds = (auth.native_bind_argument(),)
        if host_authentication:
            authentication_runtime = auth.prepare_runtime(
                space_name,
                rootfs,
            )
            authentication = auth.AuthenticationService(
                space_name,
                authentication_runtime,
                {
                    user.uid: user.administrator
                    for user in users
                },
            )
            authentication.start()
            authentication_binds += authentication_runtime.bind_arguments
        initial_devices: tuple[devices.DeviceNode, ...] = ()
        if device_level in {"basic", "admin", "full"}:
            device_udev = devices.Udev()
            device_monitor = device_udev.monitor()
            initial_devices = devices.discover(
                device_level,
                metadata_reader=device_udev.metadata,
            )
            device_worker = _DeviceWorker(
                space_name,
                device_level,
                device_udev,
                device_monitor,
                initial_devices,
            )
            device_worker.start()
        _set_device_policy(space_name, device_level, initial_devices)
        device_policy_set = True
        monitor = _LoginMonitor()
        initial_eligible_uids = _eligible_uids(monitor, users)
        initial_mounts = _plan_mounts(
            available_mounts,
            initial_eligible_uids,
        )
        worker = _MountWorker(
            space_name,
            users,
            monitor,
            available_mounts,
            initial_mounts,
            initial_eligible_uids,
            bool(portal_binds),
        )
        worker.start()
        process = subprocess.Popen(
            _command(
                space_name,
                rootfs,
                home,
                network,
                kernel_capabilities,
                initial_mounts,
                (
                    *authentication_binds,
                    *portal_binds,
                    *_device_bind_arguments(
                        device_level,
                        initial_devices,
                    ),
                ),
                custom_binds,
                custom_overlays,
            ),
            env=environment,
        )
        worker.attach(process)
        if device_worker is not None:
            device_worker.attach(process)

        def stop_authentication() -> None:
            nonlocal authentication
            if authentication is not None:
                authentication.stop()
                authentication = None

        def forward_signal(signum: int, _frame: object) -> None:
            stop_authentication()
            process.send_signal(signum)

        signal.signal(signal.SIGTERM, forward_signal)
        return process.wait()
    finally:
        if shortcut_worker is not None:
            shortcut_worker.stop()
        if device_worker is not None:
            device_worker.stop()
        if worker is not None:
            worker.stop()
        if shortcut_worker is not None:
            shortcut_worker.join()
        if device_worker is not None:
            device_worker.join()
        if worker is not None:
            worker.join()
        if shortcut_monitor is not None:
            shortcut_monitor.close()
        if monitor is not None:
            monitor.close()
        if device_monitor is not None:
            device_monitor.close()
        if device_udev is not None:
            device_udev.close()
        if authentication is not None:
            authentication.stop()
        if device_policy_set:
            _set_device_policy(space_name, "disabled")
