"""Optional host-wide Spaces integration configuration."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from . import _


logger = logging.getLogger(__name__)

CONFIG_PATH = Path("/etc/spaces/config.json")
SUPPORTED_VERSION = 1
DISTRO_IDS = frozenset({"arch", "fedora", "ubuntu", "kali"})
PACKAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.:@-]*$")
UNSAFE_DESTINATIONS = (
    PurePosixPath("/dev"),
    PurePosixPath("/home"),
    PurePosixPath("/proc"),
    PurePosixPath("/root"),
    PurePosixPath("/sys"),
)


@dataclass(frozen=True)
class Mount:
    source: Path
    destination: PurePosixPath


@dataclass(frozen=True)
class Overlay:
    source: Path
    destination: PurePosixPath


@dataclass(frozen=True)
class DistroConfig:
    packages: tuple[str, ...] = ()
    mounts: tuple[Mount, ...] = ()
    overlays: tuple[Overlay, ...] = ()


@dataclass(frozen=True)
class HostConfig:
    version: int | None = None
    default_shell: str | None = None
    distros: Mapping[str, DistroConfig] = field(default_factory=dict)

    def packages_for(self, distro_id: str) -> tuple[str, ...]:
        distro = self.distros.get(distro_id)
        return distro.packages if distro is not None else ()

    def mounts_for(self, distro_id: str) -> tuple[Mount, ...]:
        distro = self.distros.get(distro_id)
        return distro.mounts if distro is not None else ()

    def overlays_for(self, distro_id: str) -> tuple[Overlay, ...]:
        distro = self.distros.get(distro_id)
        return distro.overlays if distro is not None else ()


def _warning(message: str, *arguments: object) -> None:
    logger.warning(message, *arguments)


def _guest_path(value: object, description: str) -> PurePosixPath | None:
    if not isinstance(value, str) or not value or "\0" in value:
        _warning(_("Ignoring invalid %s: %r."), description, value)
        return None
    path = PurePosixPath(value)
    if not path.is_absolute() or str(path) != value:
        _warning(
            _("Ignoring non-absolute or non-normalized %s: %r."),
            description,
            value,
        )
        return None
    return path


def _safe_mount_destination(value: object) -> PurePosixPath | None:
    destination = _guest_path(value, _("mount destination"))
    if destination is None:
        return None
    if destination == PurePosixPath("/") or any(
        destination == unsafe or unsafe in destination.parents
        for unsafe in UNSAFE_DESTINATIONS
    ):
        _warning(_("Ignoring unsafe mount destination: %s."), destination)
        return None
    return destination


def _parse_packages(value: object, distro_id: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        _warning(
            _("Ignoring invalid package list for distribution %s."),
            distro_id,
        )
        return ()
    packages: list[str] = []
    seen: set[str] = set()
    for package in value:
        if not isinstance(package, str) or not PACKAGE_PATTERN.fullmatch(package):
            _warning(
                _("Ignoring invalid package name for distribution %s: %r."),
                distro_id,
                package,
            )
            continue
        if package in seen:
            _warning(
                _("Ignoring duplicate package for distribution %s: %s."),
                distro_id,
                package,
            )
            continue
        seen.add(package)
        packages.append(package)
    return tuple(packages)


def _parse_overlays(
    value: object,
    distro_id: str,
) -> tuple[Overlay, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        _warning(
            _("Ignoring invalid overlays for distribution %s."),
            distro_id,
        )
        return ()
    overlays: list[Overlay] = []
    destinations: set[PurePosixPath] = set()
    for index, overlay in enumerate(value):
        if not isinstance(overlay, dict):
            _warning(
                _("Ignoring invalid overlay %d for distribution %s."),
                index + 1,
                distro_id,
            )
            continue
        for key in overlay:
            if key not in {"source", "destination"}:
                _warning(
                    _("Ignoring unknown option on overlay %s.%d: %s."),
                    distro_id,
                    index + 1,
                    key,
                )
        source_value = overlay.get("source")
        destination = _safe_mount_destination(overlay.get("destination"))
        if (
            not isinstance(source_value, str)
            or not source_value
            or "\0" in source_value
            or not os.path.isabs(source_value)
            or os.path.normpath(source_value) != source_value
        ):
            _warning(_("Ignoring invalid overlay source: %r."), source_value)
            continue
        try:
            source = Path(source_value).resolve(strict=True)
        except FileNotFoundError:
            continue
        except (OSError, RuntimeError) as error:
            _warning(
                _("Ignoring invalid overlay source %s: %s."),
                source_value,
                error,
            )
            continue
        if not source.is_dir():
            _warning(
                _("Ignoring non-directory overlay source: %s."),
                source,
            )
            continue
        if destination is None:
            continue
        if destination in destinations:
            _warning(
                _("Ignoring duplicate overlay destination for %s: %s."),
                distro_id,
                destination,
            )
            continue
        destinations.add(destination)
        overlays.append(Overlay(source, destination))
    return tuple(overlays)


def _parse_distros(value: object) -> dict[str, DistroConfig]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        _warning(_("Ignoring invalid distros configuration."))
        return {}
    distros: dict[str, DistroConfig] = {}
    for distro_id, distro in value.items():
        if distro_id not in DISTRO_IDS:
            _warning(_("Ignoring unsupported distribution %r."), distro_id)
            continue
        if not isinstance(distro, dict):
            _warning(
                _("Ignoring invalid configuration for distribution %s."),
                distro_id,
            )
            continue
        for key in distro:
            if key not in {"packages", "mounts", "overlays"}:
                _warning(
                    _("Ignoring unknown distribution option %s.%s."),
                    distro_id,
                    key,
                )
        distros[distro_id] = DistroConfig(
            packages=_parse_packages(
                distro.get("packages", []),
                distro_id,
            ),
            mounts=_parse_mounts(distro.get("mounts"), distro_id),
            overlays=_parse_overlays(
                distro.get("overlays"),
                distro_id,
            ),
        )
    return distros


def _parse_mounts(
    value: object,
    distro_id: str,
) -> tuple[Mount, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        _warning(
            _("Ignoring invalid mounts for distribution %s."),
            distro_id,
        )
        return ()
    mounts: list[Mount] = []
    destinations: set[PurePosixPath] = set()
    for index, mount in enumerate(value):
        if not isinstance(mount, dict):
            _warning(
                _("Ignoring invalid mount %d for distribution %s."),
                index + 1,
                distro_id,
            )
            continue
        for key in mount:
            if key not in {"source", "destination"}:
                _warning(
                    _("Ignoring unknown option on mount %s.%d: %s."),
                    distro_id,
                    index + 1,
                    key,
                )
        source_value = mount.get("source")
        destination = _safe_mount_destination(mount.get("destination"))
        if (
            not isinstance(source_value, str)
            or not source_value
            or "\0" in source_value
            or not os.path.isabs(source_value)
            or os.path.normpath(source_value) != source_value
        ):
            _warning(_("Ignoring invalid mount source: %r."), source_value)
            continue
        try:
            source = Path(source_value).resolve(strict=True)
        except FileNotFoundError:
            continue
        except (OSError, RuntimeError) as error:
            _warning(
                _("Ignoring invalid mount source %s: %s."),
                source_value,
                error,
            )
            continue
        if not source.is_dir() and not source.is_file():
            _warning(_("Ignoring unsupported mount source: %s."), source)
            continue
        if destination is None:
            continue
        if destination in destinations:
            _warning(
                _("Ignoring duplicate mount destination for %s: %s."),
                distro_id,
                destination,
            )
            continue
        destinations.add(destination)
        mounts.append(Mount(source, destination))
    return tuple(mounts)


def _parse(data: object) -> HostConfig:
    if not isinstance(data, dict):
        _warning(_("Ignoring invalid Spaces host configuration."))
        return HostConfig()
    version = data.get("version")
    if isinstance(version, bool) or version != SUPPORTED_VERSION:
        _warning(
            _("Ignoring unsupported Spaces configuration version: %r."),
            version,
        )
        return HostConfig()
    for key in data:
        if key not in {
            "version",
            "default_shell",
            "distros",
        }:
            _warning(_("Ignoring unknown Spaces configuration option: %s."), key)

    shell_path = (
        _guest_path(data["default_shell"], _("default shell"))
        if "default_shell" in data
        else None
    )
    return HostConfig(
        version=SUPPORTED_VERSION,
        default_shell=str(shell_path) if shell_path is not None else None,
        distros=_parse_distros(data.get("distros")),
    )


def load(path: Path | None = None) -> HostConfig:
    """Load the optional versioned host configuration."""

    config_path = CONFIG_PATH if path is None else path
    try:
        with config_path.open(encoding="utf-8") as config_file:
            data: Any = json.load(config_file)
    except FileNotFoundError:
        return HostConfig()
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        _warning(
            _("Ignoring invalid Spaces host configuration %s: %s."),
            config_path,
            error,
        )
        return HostConfig()
    return _parse(data)
