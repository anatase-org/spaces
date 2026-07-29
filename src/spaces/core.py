"""Shared, standard-library-only data handling for Spaces."""

from __future__ import annotations

import json
import os
import pwd
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import _
from .distro import DistributionError, KNOWN_IDS, get_driver


SCHEMA_VERSION = 1
STATE_ROOT = Path("/var/lib/spaces")
KNOWN_DISTRIBUTIONS = KNOWN_IDS
RESERVED_NAMES = frozenset(KNOWN_DISTRIBUTIONS)
NETWORK_LEVELS = ("basic", "advanced", "admin")
DEVICE_LEVELS = ("disabled", "basic", "admin", "full")
DEFAULT_HOME_FOLDERS = ("Projects", "Downloads")
SPACE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class SpacesError(ValueError):
    """A user-facing Spaces configuration error."""


@dataclass(frozen=True)
class Identity:
    uid: int
    gid: int
    home: Path


def _environment_id(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise SpacesError(_("{name} must be a numeric ID.", name=name)) from error
    if value < 0:
        raise SpacesError(_("{name} must not be negative.", name=name))
    return value


def initiating_identity() -> Identity:
    """Return the user for whom Spaces is being configured."""

    if os.geteuid() == 0:
        uid = _environment_id("SUDO_UID", 0)
        gid = _environment_id("SUDO_GID", 0)
    else:
        uid = os.getuid()
        gid = os.getgid()

    try:
        home = Path(pwd.getpwuid(uid).pw_dir)
    except KeyError as error:
        raise SpacesError(
            _("No passwd entry exists for UID {uid}.", uid=uid)
        ) from error
    return Identity(uid=uid, gid=gid, home=home)


def validate_space_name(name: object, *, allow_reserved: bool = True) -> str:
    if not isinstance(name, str) or not SPACE_NAME_PATTERN.fullmatch(name):
        raise SpacesError(
            _(
                "Space names must use lowercase letters, digits, hyphens, or "
                "underscores, start with a letter or digit, and be at most 63 "
                "characters long."
            )
        )
    if not allow_reserved and name in RESERVED_NAMES:
        raise SpacesError(
            _("{name!r} is reserved for a known distribution.", name=name)
        )
    return name


def validate_home_name(name: object) -> str:
    if (
        not isinstance(name, str)
        or not name
        or name.startswith(".")
        or name in {".", ".."}
        or "/" in name
        or "\0" in name
    ):
        raise SpacesError(
            _("Invalid home folder permission: {name!r}.", name=name)
        )
    return name


def discover_home_folders(home: Path) -> list[str]:
    """Find visible, immediate real directories and offer common defaults."""

    names = set(DEFAULT_HOME_FOLDERS)
    try:
        for entry in home.iterdir():
            if (
                not entry.name.startswith(".")
                and entry.is_dir()
                and not entry.is_symlink()
            ):
                names.add(entry.name)
    except OSError as error:
        raise SpacesError(
            _(
                "Could not inspect home directory {home}: {error}",
                home=home,
                error=error,
            )
        ) from error
    return sorted(names, key=str.casefold)


def _require_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SpacesError(_("{label} must be a JSON object.", label=label))
    return value


def _validate_distribution(value: object) -> dict[str, Any]:
    distribution = _require_mapping(value, "distribution")
    distro_id = distribution.get("id")
    if distro_id not in KNOWN_DISTRIBUTIONS:
        raise SpacesError(
            _("Unknown distribution ID: {distro_id!r}.", distro_id=distro_id)
        )
    driver = get_driver(str(distro_id))
    if driver is not None:
        try:
            driver.validate(distribution)
        except DistributionError as error:
            raise SpacesError(str(error)) from error
    return distribution


def _validate_system_permissions(value: object) -> dict[str, Any]:
    system = _require_mapping(value, "system permissions")
    network = system.get("network")
    if network not in NETWORK_LEVELS:
        raise SpacesError(
            _("Unknown network permission: {network!r}.", network=network)
        )
    devices = system.get("devices", "basic")
    if devices not in DEVICE_LEVELS:
        raise SpacesError(
            _("Unknown device permission: {devices!r}.", devices=devices)
        )
    host_authentication = system.get("host_authentication", True)
    if not isinstance(host_authentication, bool):
        raise SpacesError(
            _("Host authentication permission must be a boolean.")
        )
    return system


def _validate_user_record(value: object, uid_key: str) -> dict[str, Any]:
    record = _require_mapping(value, f"user {uid_key}")
    gid = record.get("gid")
    if not isinstance(gid, int) or isinstance(gid, bool) or gid < 0:
        raise SpacesError(
            _("User {uid} has an invalid GID.", uid=uid_key)
        )
    permissions = _require_mapping(
        record.get("permissions"), f"user {uid_key} permissions"
    )
    home = permissions.get("home")
    if not isinstance(home, list):
        raise SpacesError(
            _("User {uid} home permissions must be a list.", uid=uid_key)
        )
    validated = [validate_home_name(name) for name in home]
    if len(validated) != len(set(validated)):
        raise SpacesError(
            _("User {uid} home permissions contain duplicates.", uid=uid_key)
        )
    administrator = permissions.get("administrator", True)
    if not isinstance(administrator, bool):
        raise SpacesError(
            _("User {uid} administrator permission must be a boolean.", uid=uid_key)
        )
    desktop = permissions.get("desktop", True)
    if not isinstance(desktop, bool):
        raise SpacesError(
            _("User {uid} desktop permission must be a boolean.", uid=uid_key)
        )
    return record


def validate_info(value: object) -> dict[str, Any]:
    info = _require_mapping(value, "space information")
    schema_version = info.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != SCHEMA_VERSION
    ):
        raise SpacesError(_("Unsupported or missing schema_version."))
    validate_space_name(info.get("name"))
    _validate_distribution(info.get("distribution"))
    permissions = _require_mapping(info.get("permissions"), "permissions")
    _validate_system_permissions(permissions.get("system"))
    users = _require_mapping(permissions.get("users"), "users")
    if not users:
        raise SpacesError(_("At least one user permission entry is required."))
    for uid_key, record in users.items():
        if not isinstance(uid_key, str) or not uid_key.isdecimal():
            raise SpacesError(
                _("Invalid user ID key: {uid!r}.", uid=uid_key)
            )
        _validate_user_record(record, uid_key)
    return info


def validate_creation_info(value: object) -> dict[str, Any]:
    info = validate_info(value)
    distro_id = info["distribution"]["id"]
    driver = get_driver(distro_id)
    if driver is None:
        raise SpacesError(
            _("Distribution {distro_id!r} is not implemented.", distro_id=distro_id)
        )
    return info


def validate_configure_patch(value: object) -> dict[str, Any]:
    patch = _require_mapping(value, "configure payload")
    schema_version = patch.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != SCHEMA_VERSION
    ):
        raise SpacesError(_("Unsupported or missing schema_version."))
    validate_space_name(patch.get("name"))
    permissions = _require_mapping(patch.get("permissions"), "permissions")
    allowed = {"system", "user"}
    if not set(permissions).issubset(allowed):
        raise SpacesError(
            _("Configure payload contains unknown permission sections.")
        )
    if "system" in permissions:
        _validate_system_permissions(permissions["system"])
    user = _require_mapping(permissions.get("user"), "user update")
    uid = user.get("uid")
    if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
        raise SpacesError(_("User update has an invalid UID."))
    _validate_user_record(
        {"gid": user.get("gid"), "permissions": user.get("permissions")}, str(uid)
    )
    return patch


def validate_delete_request(value: object) -> dict[str, Any]:
    request = _require_mapping(value, "delete payload")
    if set(request) != {"name"}:
        raise SpacesError(_("Delete payload must contain only a space name."))
    validate_space_name(request["name"])
    return request


def validate_cp_request(value: object) -> dict[str, Any]:
    request = _require_mapping(value, "cp payload")
    if set(request) != {"arguments"}:
        raise SpacesError(_("cp payload must contain only arguments."))
    arguments = request["arguments"]
    if not isinstance(arguments, list) or not arguments:
        raise SpacesError(_("cp arguments must be a list."))
    if any(
        not isinstance(argument, str) or "\0" in argument
        for argument in arguments
    ):
        raise SpacesError(_("cp arguments must be strings without null bytes."))
    return request


def resolve_space_location(value: str) -> str:
    if value.startswith("-"):
        return value
    name, separator, location = value.partition(":")
    if not separator:
        return value

    validate_space_name(name)
    space = STATE_ROOT / name
    if space.is_symlink() or not space.is_dir():
        raise SpacesError(_("Space {name!r} does not exist.", name=name))

    if location == "/home" or location.startswith("/home/"):
        root = space / "home"
        relative = location.removeprefix("/home").lstrip("/")
    elif location == "/var/home" or location.startswith("/var/home/"):
        root = space / "home"
        relative = location.removeprefix("/var/home").lstrip("/")
    else:
        root = space / "rootfs"
        relative = location.lstrip("/")

    if root.is_symlink() or not root.is_dir():
        raise SpacesError(
            _("Space {name!r} has an unsafe or missing filesystem.", name=name)
        )
    fixed_root = root.absolute()
    fixed_location = Path(os.path.abspath(fixed_root / relative))
    if not fixed_location.is_relative_to(fixed_root):
        raise SpacesError(
            _(
                "Space location escapes {name!r}: {location!r}.",
                name=name,
                location=location,
            )
        )
    return str(fixed_location)


def load_info(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return validate_info(value)
    except (OSError, json.JSONDecodeError, SpacesError):
        return None


def defaults_from_info(
    info: Mapping[str, Any] | None, identity: Identity
) -> tuple[str, str, bool, list[str], bool, bool]:
    network = "basic"
    devices = "basic"
    host_authentication = True
    selected_home = list(DEFAULT_HOME_FOLDERS)
    administrator = True
    desktop = True
    if not info:
        return (
            network,
            devices,
            host_authentication,
            selected_home,
            administrator,
            desktop,
        )

    permissions = info.get("permissions", {})
    system_permissions = permissions.get("system", {})
    existing_network = system_permissions.get("network")
    if existing_network in NETWORK_LEVELS:
        network = existing_network
    existing_devices = system_permissions.get("devices")
    if existing_devices in DEVICE_LEVELS:
        devices = existing_devices
    existing_host_authentication = system_permissions.get(
        "host_authentication"
    )
    if isinstance(existing_host_authentication, bool):
        host_authentication = existing_host_authentication
    user = permissions.get("users", {}).get(str(identity.uid), {})
    user_permissions = user.get("permissions", {})
    home = user_permissions.get("home")
    if isinstance(home, list):
        selected_home = [
            name
            for name in home
            if isinstance(name, str)
            and not name.startswith(".")
            and "/" not in name
        ]
    existing_administrator = user_permissions.get("administrator")
    if isinstance(existing_administrator, bool):
        administrator = existing_administrator
    existing_desktop = user_permissions.get("desktop")
    if isinstance(existing_desktop, bool):
        desktop = existing_desktop
    return (
        network,
        devices,
        host_authentication,
        selected_home,
        administrator,
        desktop,
    )


def create_info(
    name: str,
    distribution: dict[str, Any],
    identity: Identity,
    network: str,
    home: list[str],
    administrator: bool = True,
    host_authentication: bool = True,
    desktop: bool = True,
    devices: str = "basic",
) -> dict[str, Any]:
    value = {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "distribution": distribution,
        "permissions": {
            "system": {
                "network": network,
                "devices": devices,
                "host_authentication": host_authentication,
            },
            "users": {
                str(identity.uid): {
                    "gid": identity.gid,
                    "permissions": {
                        "home": sorted(home, key=str.casefold),
                        "administrator": administrator,
                        "desktop": desktop,
                    },
                }
            },
        },
    }
    return validate_creation_info(value)
