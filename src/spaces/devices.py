"""Host device discovery and udev monitoring for Spaces."""

from __future__ import annotations

import ctypes
import ctypes.util
import grp
import os
import re
import select
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from . import _
from .core import SpacesError


DEV_ROOT = Path("/dev")
NSPAWN_MANAGED_DIRECTORIES = frozenset({"mqueue", "pts", "shm"})
NSPAWN_MANAGED_DEVICES = frozenset(
    {
        PurePosixPath("/dev/console"),
        PurePosixPath("/dev/full"),
        PurePosixPath("/dev/kmsg"),
        PurePosixPath("/dev/null"),
        PurePosixPath("/dev/ptmx"),
        PurePosixPath("/dev/random"),
        PurePosixPath("/dev/tty"),
        PurePosixPath("/dev/urandom"),
        PurePosixPath("/dev/zero"),
    }
)
# Never give a guest the host's terminal or virtual-console devices. nspawn
# supplies its own console and PTYs; exposing these character majors lets a
# guest getty operate the host VT that owns a graphical login session.
HOST_TERMINAL_CHARACTER_MAJORS = frozenset({4, 5, 7})
VIDEO_SUBSYSTEMS = frozenset(
    {"cec", "drm", "dvb", "graphics", "media", "video4linux"}
)
SECURITY_SUBSYSTEMS = frozenset(
    {"tee", "tpm", "tpmrm", "vtpm", "vtpm_proxy"}
)
SECURITY_PROPERTIES = frozenset(
    {"ID_HARDWARE_WALLET", "ID_SECURITY_TOKEN", "ID_SMARTCARD_READER"}
)
CAPTURE_PROPERTIES = frozenset(
    {
        "ID_INPUT_KEYBOARD",
        "ID_INPUT_MOUSE",
        "ID_INPUT_POINTINGSTICK",
        "ID_INPUT_TABLET",
        "ID_INPUT_TABLET_PAD",
        "ID_INPUT_TOUCHPAD",
        "ID_INPUT_TOUCHSCREEN",
    }
)
CONTROLLER_PROPERTIES = frozenset(
    {"ID_INPUT_GAMEPAD", "ID_INPUT_JOYSTICK"}
)


@dataclass(frozen=True)
class DeviceMetadata:
    """Relevant udev data collected from a device and all its ancestors."""

    tags: frozenset[str] = frozenset()
    properties: frozenset[str] = frozenset()
    subsystems: frozenset[str] = frozenset()


@dataclass(frozen=True, order=True)
class DeviceNode:
    """One canonical host device node and its container destination."""

    destination: PurePosixPath
    source: Path
    kind: str
    major: int
    minor: int

    @property
    def allow_spec(self) -> str:
        family = "char" if self.kind == "c" else "block"
        return f"/dev/{family}/{self.major}:{self.minor}"


class Udev:
    """Minimal ctypes wrapper around libudev."""

    def __init__(self) -> None:
        library_name = ctypes.util.find_library("udev") or "libudev.so.1"
        try:
            self._library = ctypes.CDLL(library_name, use_errno=True)
        except OSError as error:
            raise SpacesError(
                _("Could not load libudev: {error}", error=error)
            ) from error
        self._configure_functions()
        self._context = ctypes.c_void_p(self._library.udev_new())
        if not self._context:
            raise SpacesError(_("Could not initialize libudev."))

    def _configure_functions(self) -> None:
        library = self._library
        library.udev_new.argtypes = []
        library.udev_new.restype = ctypes.c_void_p
        library.udev_unref.argtypes = [ctypes.c_void_p]
        library.udev_unref.restype = ctypes.c_void_p
        library.udev_device_new_from_devnum.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char,
            ctypes.c_ulong,
        ]
        library.udev_device_new_from_devnum.restype = ctypes.c_void_p
        library.udev_device_unref.argtypes = [ctypes.c_void_p]
        library.udev_device_unref.restype = ctypes.c_void_p
        library.udev_device_get_parent.argtypes = [ctypes.c_void_p]
        library.udev_device_get_parent.restype = ctypes.c_void_p
        library.udev_device_get_subsystem.argtypes = [ctypes.c_void_p]
        library.udev_device_get_subsystem.restype = ctypes.c_char_p
        library.udev_device_get_property_value.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
        ]
        library.udev_device_get_property_value.restype = ctypes.c_char_p
        library.udev_device_has_tag.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
        ]
        library.udev_device_has_tag.restype = ctypes.c_int
        library.udev_monitor_new_from_netlink.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
        ]
        library.udev_monitor_new_from_netlink.restype = ctypes.c_void_p
        library.udev_monitor_enable_receiving.argtypes = [ctypes.c_void_p]
        library.udev_monitor_enable_receiving.restype = ctypes.c_int
        library.udev_monitor_get_fd.argtypes = [ctypes.c_void_p]
        library.udev_monitor_get_fd.restype = ctypes.c_int
        library.udev_monitor_receive_device.argtypes = [ctypes.c_void_p]
        library.udev_monitor_receive_device.restype = ctypes.c_void_p
        library.udev_monitor_unref.argtypes = [ctypes.c_void_p]
        library.udev_monitor_unref.restype = ctypes.c_void_p

    @staticmethod
    def _decode(value: bytes | None) -> str | None:
        if value is None:
            return None
        return value.decode("utf-8", errors="surrogateescape")

    def metadata(self, kind: str, device_number: int) -> DeviceMetadata:
        pointer = ctypes.c_void_p(
            self._library.udev_device_new_from_devnum(
                self._context,
                kind.encode("ascii"),
                device_number,
            )
        )
        if not pointer:
            return DeviceMetadata()
        tags: set[str] = set()
        properties: set[str] = set()
        subsystems: set[str] = set()
        try:
            current = pointer
            while current:
                for tag in ("security-device", "uaccess"):
                    if self._library.udev_device_has_tag(
                        current, tag.encode("ascii")
                    ) > 0:
                        tags.add(tag)
                subsystem = self._decode(
                    self._library.udev_device_get_subsystem(current)
                )
                if subsystem:
                    subsystems.add(subsystem)
                for name in (
                    *SECURITY_PROPERTIES,
                    *CAPTURE_PROPERTIES,
                    *CONTROLLER_PROPERTIES,
                ):
                    value = self._decode(
                        self._library.udev_device_get_property_value(
                            current, name.encode("ascii")
                        )
                    )
                    if value and value != "0":
                        properties.add(name)
                current = ctypes.c_void_p(
                    self._library.udev_device_get_parent(current)
                )
            return DeviceMetadata(
                tags=frozenset(tags),
                properties=frozenset(properties),
                subsystems=frozenset(subsystems),
            )
        finally:
            self._library.udev_device_unref(pointer)

    def monitor(self) -> UdevMonitor:
        return UdevMonitor(self)

    def close(self) -> None:
        if self._context:
            self._library.udev_unref(self._context)
            self._context = ctypes.c_void_p()


class UdevMonitor:
    """Wake on udev changes, with a pipe for prompt worker shutdown."""

    def __init__(self, udev: Udev) -> None:
        self._udev = udev
        self._monitor = ctypes.c_void_p(
            udev._library.udev_monitor_new_from_netlink(
                udev._context, b"udev"
            )
        )
        if not self._monitor:
            raise SpacesError(_("Could not create a udev monitor."))
        self._read_fd = -1
        self._write_fd = -1
        try:
            result = udev._library.udev_monitor_enable_receiving(self._monitor)
            if result < 0:
                raise SpacesError(
                    _(
                        "Could not start the udev monitor: {error}",
                        error=os.strerror(-result),
                    )
                )
            monitor_fd = udev._library.udev_monitor_get_fd(self._monitor)
            if monitor_fd < 0:
                raise SpacesError(_("Could not get the udev monitor descriptor."))
            self._read_fd, self._write_fd = os.pipe2(
                os.O_CLOEXEC | os.O_NONBLOCK
            )
            self._poll = select.poll()
            self._poll.register(monitor_fd, select.POLLIN)
            self._poll.register(self._read_fd, select.POLLIN)
            self._monitor_fd = monitor_fd
        except Exception:
            self.close()
            raise

    def wait(self) -> bool:
        for descriptor, events in self._poll.poll():
            if events & (select.POLLERR | select.POLLHUP | select.POLLNVAL):
                raise SpacesError(_("The udev monitor failed."))
            if descriptor == self._read_fd:
                return False
            if descriptor == self._monitor_fd:
                device = ctypes.c_void_p(
                    self._udev._library.udev_monitor_receive_device(
                        self._monitor
                    )
                )
                if device:
                    self._udev._library.udev_device_unref(device)
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
            self._udev._library.udev_monitor_unref(self._monitor)
            self._monitor = ctypes.c_void_p()
        if self._read_fd >= 0:
            os.close(self._read_fd)
            self._read_fd = -1
        if self._write_fd >= 0:
            os.close(self._write_fd)
            self._write_fd = -1


MetadataReader = Callable[[str, int], DeviceMetadata]


def _is_security_device(
    destination: PurePosixPath,
    metadata: DeviceMetadata,
) -> bool:
    name = destination.name
    return (
        "security-device" in metadata.tags
        or bool(metadata.properties & SECURITY_PROPERTIES)
        or bool(metadata.subsystems & SECURITY_SUBSYSTEMS)
        or name == "vtpmx"
        or re.fullmatch(r"(?:tee|teepriv|tpm|tpmrm)\d+", name) is not None
    )


def _is_capture_device(metadata: DeviceMetadata) -> bool:
    if metadata.properties & CONTROLLER_PROPERTIES:
        return False
    return bool(
        metadata.properties & CAPTURE_PROPERTIES
        or "input" in metadata.subsystems
    )


def discover(
    level: str,
    *,
    device_root: Path = DEV_ROOT,
    metadata_reader: MetadataReader | None = None,
) -> tuple[DeviceNode, ...]:
    """Return host device paths permitted by the selected access level."""

    if level not in {"basic", "admin", "full"}:
        return ()
    resolved_device_root = device_root.resolve(strict=True)
    owned_udev: Udev | None = None
    if metadata_reader is None:
        owned_udev = Udev()
        metadata_reader = owned_udev.metadata
    try:
        video_gid: int | None = None
        if level == "basic":
            try:
                video_gid = grp.getgrnam("video").gr_gid
            except KeyError:
                pass
        nodes: list[DeviceNode] = []
        for parent, directories, files in os.walk(
            device_root, followlinks=False
        ):
            parent_path = Path(parent)
            directories[:] = [
                name
                for name in directories
                if (
                    not (parent_path / name).is_symlink()
                    and not (
                        parent_path == device_root
                        and name in NSPAWN_MANAGED_DIRECTORIES
                    )
                )
            ]
            for name in files:
                source = parent_path / name
                try:
                    metadata_stat = source.lstat()
                except OSError:
                    continue
                bind_source = source
                if stat.S_ISLNK(metadata_stat.st_mode):
                    if level != "full":
                        continue
                    try:
                        bind_source = source.resolve(strict=True)
                        bind_source.relative_to(resolved_device_root)
                        metadata_stat = bind_source.lstat()
                    except (OSError, RuntimeError, ValueError):
                        continue
                if stat.S_ISCHR(metadata_stat.st_mode):
                    kind = "c"
                elif stat.S_ISBLK(metadata_stat.st_mode):
                    kind = "b"
                else:
                    continue
                major = os.major(metadata_stat.st_rdev)
                minor = os.minor(metadata_stat.st_rdev)
                if (
                    kind == "c"
                    and major in HOST_TERMINAL_CHARACTER_MAJORS
                ):
                    continue
                try:
                    relative = source.relative_to(device_root)
                except ValueError:
                    continue
                destination = PurePosixPath("/dev", *relative.parts)
                if destination in NSPAWN_MANAGED_DEVICES:
                    continue
                assert metadata_reader is not None
                metadata = metadata_reader(kind, metadata_stat.st_rdev)
                # Watchdogs control host-wide resets and must never be
                # exposed, even with full device access or through aliases.
                if kind == "c" and (
                    "watchdog" in metadata.subsystems
                    or (major, minor) == (10, 130)
                    or re.fullmatch(r"watchdog\d*", destination.name)
                    or re.fullmatch(r"watchdog\d*", bind_source.name)
                ):
                    continue
                if level != "full":
                    if _is_security_device(destination, metadata):
                        continue
                    if level == "basic" and (
                        _is_capture_device(metadata)
                        or not (
                            "uaccess" in metadata.tags
                            or (
                                video_gid is not None
                                and metadata_stat.st_gid == video_gid
                            )
                            or bool(metadata.subsystems & VIDEO_SUBSYSTEMS)
                        )
                    ):
                        continue
                nodes.append(
                    DeviceNode(
                        destination=destination,
                        source=bind_source,
                        kind=kind,
                        major=major,
                        minor=minor,
                    )
                )
        return tuple(sorted(nodes))
    finally:
        if owned_udev is not None:
            owned_udev.close()
