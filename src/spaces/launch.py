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
from .distro import get_driver
from .logging import configure_logging


logger = logging.getLogger(__name__)

NSPAWN = "/usr/bin/systemd-nspawn"
MACHINECTL = "/usr/bin/machinectl"
SYSTEMCTL = "/usr/bin/systemctl"
SYSTEMD_ESCAPE = "/usr/bin/systemd-escape"
SYSTEMD_UMOUNT = "/usr/bin/systemd-umount"
API_VFS_WRITABLE = "SYSTEMD_NSPAWN_API_VFS_WRITABLE"
PING_GROUP_RANGE = Path("/proc/sys/net/ipv4/ping_group_range")
UNPRIVILEGED_PING_GROUP_RANGE = (0, 2_147_483_647)
PING_EXECUTABLES = ("/usr/bin/ping", "/bin/ping")
CAPABILITY_XATTR = "security.capability"
ELIGIBLE_USER_STATES = frozenset({"active", "online", "lingering"})
INELIGIBLE_USER_STATES = frozenset({"closing", "offline"})
SYMLINKS = [
    # Ostree system weirdness
    ("/var/home", "/home"),
    # Services that should not run
    ("/etc/systemd/system/netplan-configure.service", "/dev/null"),
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


@dataclass(frozen=True, order=True)
class HomeMount:
    """One permitted host directory and its destination in the space."""

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

    for fixup in SYMLINKS:
        try:
            link_name, target = fixup
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
                    "Could not apply rootfs symlink fixup {fixup}: {error}",
                    fixup=fixup,
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
                permitted_home=tuple(record["permissions"]["home"]),
                administrator=record["permissions"].get("administrator", True),
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


def _default_user_shell(rootfs: Path) -> str:
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

    shell = _default_user_shell(rootfs)
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


def _prepare_mounts(users: tuple[SpaceUser, ...]) -> tuple[HomeMount, ...]:
    mounts: list[HomeMount] = []
    for user in users:
        for name in user.permitted_home:
            source = user.host_home / name
            try:
                source_fd = os.open(
                    source,
                    os.O_PATH
                    | os.O_DIRECTORY
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC,
                )
                try:
                    source_stat = os.fstat(source_fd)
                    resolved_source = source.resolve(strict=True)
                    resolved_stat = resolved_source.stat()
                    if (
                        source_stat.st_dev != resolved_stat.st_dev
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
            created_target = False
            try:
                if persistent_target.is_symlink() or (
                    persistent_target.exists()
                    and not persistent_target.is_dir()
                ):
                    logger.warning(
                        _(
                            "Space home destination {path} is unsafe; "
                            "skipping it.",
                            path=persistent_target,
                        )
                    )
                    continue
                if not persistent_target.exists():
                    persistent_target.mkdir(mode=0o700)
                    created_target = True
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
                if created_target:
                    try:
                        persistent_target.rmdir()
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
    def escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace(":", "\\:")

    return f"--bind={escape(str(mount.source))}:{escape(mount.destination)}"


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
                    b"uid", ctypes.byref(self._monitor)
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

    def wait(self) -> bool:
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
        monitor_fd = self._library.sd_login_monitor_get_fd(self._monitor)
        for descriptor, _events in self._poll.poll(timeout_ms):
            if descriptor == self._read_fd:
                return False
            if descriptor == monitor_fd:
                self._raise_for_result(
                    self._library.sd_login_monitor_flush(self._monitor),
                    _("Could not flush the login monitor."),
                )
                return True
        return True

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
            if self._eligible_uids:
                if not self._wait_until_registered():
                    return
                self._log_initial_mounts()
            self._reconcile()
            while not self._stopping.is_set() and self._monitor.wait():
                if self._stopping.is_set():
                    break
                self._reconcile()
        except Exception as error:
            logger.error(_("User mount monitor failed: {error}", error=error))
            process = self._process
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGTERM)

    def _wait_until_registered(self) -> bool:
        if self._registered:
            return True
        process = self._process
        assert process is not None
        while not self._stopping.is_set() and process.poll() is None:
            completed = subprocess.run(
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
            if completed.returncode == 0:
                self._registered = True
                return True
            self._stopping.wait(0.05)
        return False

    def _reconcile(self) -> None:
        eligible_uids = _eligible_uids(self._monitor, self._users)
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
        escaped = subprocess.run(
            [
                SYSTEMD_ESCAPE,
                "--path",
                "--suffix=mount",
                mount.destination,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if not escaped:
            raise core.SpacesError(
                _("Could not derive a mount unit for {path}.", path=mount.destination)
            )
        subprocess.run(
            [
                SYSTEMCTL,
                f"--machine={self._space_name}",
                "--no-ask-password",
                "set-property",
                "--runtime",
                escaped,
                "LazyUnmount=yes",
            ],
            check=True,
        )
        subprocess.run(
            [
                SYSTEMD_UMOUNT,
                f"--machine={self._space_name}",
                "--no-ask-password",
                "--quiet",
                mount.destination,
            ],
            check=True,
        )


def _command(
    space_name: str,
    rootfs: Path,
    home: Path,
    network: str,
    mounts: tuple[HomeMount, ...] = (),
    authentication_binds: tuple[str, ...] = (),
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
        *authentication_binds,
        *(_bind_argument(mount) for mount in mounts),
        "--boot",
        "--setenv=SYSTEMD_GETTY_AUTO=no",
        "--console=read-only",
        "--private-users=no",
        "--keep-unit",
        "--settings=no",
        "--notify-ready=yes",
        "--resolv-conf=bind-host",
        f"--drop-capability={','.join(dropped_caps)}",
        f"--capability={','.join(kept_caps)}",
    ]


def launch(space_name: str) -> int:
    """Run a space until its nspawn machine exits."""

    configure_logging(rich=False)
    rootfs, home, info = _load_space(space_name)
    network = info["permissions"]["system"]["network"]
    host_authentication = info["permissions"]["system"].get(
        "host_authentication",
        True,
    )
    environment = os.environ.copy()
    environment.pop(API_VFS_WRITABLE, None)
    if network == "admin":
        environment[API_VFS_WRITABLE] = "network"

    _apply_rootfs_fixups(rootfs)
    users = _resolve_users(info, home)
    driver = get_driver(info["distribution"]["id"])
    administrator_group = (
        driver.administrator_group if driver is not None else "wheel"
    )
    _reconcile_accounts(rootfs, users, administrator_group)
    _ensure_user_homes(rootfs, users)

    available_mounts = _prepare_mounts(users)
    monitor: _LoginMonitor | None = None
    worker: _MountWorker | None = None
    authentication: auth.AuthenticationService | None = None
    authentication_binds: tuple[str, ...] = ()
    try:
        if host_authentication:
            policy_path = (
                driver.shared_pam_policy if driver is not None else None
            )
            session_policy_path = (
                driver.shared_pam_session_policy
                if driver is not None
                else None
            )
            if policy_path is None or session_policy_path is None:
                raise core.SpacesError(
                    _(
                        "Host authentication is enabled for {space}, but its "
                        "distribution does not declare shared PAM policies.",
                        space=space_name,
                    )
                )
            else:
                authentication_runtime = auth.prepare_runtime(
                    space_name,
                    rootfs,
                    policy_path,
                    session_policy_path,
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
                authentication_binds = (
                    authentication_runtime.bind_arguments
                )
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
        )
        worker.start()
        process = subprocess.Popen(
            _command(
                space_name,
                rootfs,
                home,
                network,
                initial_mounts,
                authentication_binds,
            ),
            env=environment,
        )
        worker.attach(process)

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
        if worker is not None:
            worker.stop()
            worker.join()
        if monitor is not None:
            monitor.close()
        if authentication is not None:
            authentication.stop()
