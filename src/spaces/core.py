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
KERNEL_CAPABILITY_LEVELS = (
    "basic",
    "development",
    "admin",
)
DEVICE_LEVELS = ("disabled", "basic", "admin", "full")
DEFAULT_HOME_FOLDERS = ("Projects", "Downloads")
DEFAULT_HOME_FILES = (
    ".ssh/config",
    ".zhistory",
    ".bash_history",
)
DEFAULT_HOME_MOUNTS = (*DEFAULT_HOME_FOLDERS, *DEFAULT_HOME_FILES)
PRESET_NAMES = ("basic", "develop", "custom")
PERMISSION_PRESETS: dict[str, dict[str, dict[str, Any]]] = {
    "basic": {
        "system": {
            "network": "basic",
            "kernel_capabilities": "basic",
            "devices": "basic",
            "host_authentication": True,
            "shortcuts": True,
        },
        "user": {
            "home": ["Downloads"],
            "administrator": True,
            "desktop": True,
            "credential_agents": False,
            "mounted_drives": True,
        },
    },
    "develop": {
        "system": {
            "network": "admin",
            "kernel_capabilities": "development",
            "devices": "admin",
            "host_authentication": True,
            "shortcuts": True,
        },
        "user": {
            "home": [
                "Downloads",
                "Projects",
                ".bashrc",
                ".zshrc",
                ".bash_history",
                ".zhistory",
                ".ssh/config",
            ],
            "administrator": True,
            "desktop": True,
            "credential_agents": True,
            "mounted_drives": True,
        },
    },
}
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
        or name in {".", ".."}
        or "\0" in name
        or ("/" in name and name != ".ssh/config")
    ):
        raise SpacesError(
            _("Invalid home mount permission: {name!r}.", name=name)
        )
    return name


def discover_home_folders(home: Path) -> list[str]:
    """Find mountable home directories and files, with files listed last."""

    directories = set(DEFAULT_HOME_FOLDERS)
    files = set(DEFAULT_HOME_FILES)
    try:
        for entry in home.iterdir():
            if entry.is_symlink():
                continue
            if not entry.name.startswith(".") and entry.is_dir():
                directories.add(entry.name)
            elif entry.is_file():
                files.add(entry.name)
    except OSError as error:
        raise SpacesError(
            _(
                "Could not inspect home directory {home}: {error}",
                home=home,
                error=error,
            )
        ) from error
    return [
        *sorted(directories, key=str.casefold),
        *sorted(files, key=str.casefold),
    ]


def _require_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SpacesError(_("{label} must be a JSON object.", label=label))
    return value


def _validate_preset(value: Mapping[str, Any], label: str) -> str:
    preset = value.get("preset", "custom")
    if preset not in PRESET_NAMES:
        raise SpacesError(
            _("Unknown {label} preset: {preset!r}.", label=label, preset=preset)
        )
    return str(preset)


def effective_system_permissions(
    permissions: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the system permissions that should be applied at runtime."""

    preset = _validate_preset(permissions, "system permission")
    if preset == "custom":
        return dict(permissions)
    return dict(PERMISSION_PRESETS[preset]["system"])


def effective_user_permissions(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return the user permissions that should be applied at runtime."""

    permissions = record.get("permissions", {})
    if not isinstance(permissions, dict):
        return {}
    preset = _validate_preset(permissions, "user permission")
    if preset == "custom":
        return dict(permissions)
    effective = dict(PERMISSION_PRESETS[preset]["user"])
    effective["home"] = list(effective["home"])
    return effective


def selected_preset(
    info: Mapping[str, Any] | None,
    identity: Identity,
) -> str:
    """Return the user preset initially selected by the permission wizard."""

    if info is None:
        return "basic"
    record = (
        info.get("permissions", {})
        .get("users", {})
        .get(str(identity.uid))
    )
    if not isinstance(record, dict):
        return "basic"
    permissions = record.get("permissions", {})
    if not isinstance(permissions, dict):
        return "basic"
    return _validate_preset(permissions, "user permission")


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
    _validate_preset(system, "system permission")
    network = system.get("network")
    if network not in NETWORK_LEVELS:
        raise SpacesError(
            _("Unknown network permission: {network!r}.", network=network)
        )
    kernel_capabilities = system.get("kernel_capabilities", "basic")
    if kernel_capabilities not in KERNEL_CAPABILITY_LEVELS:
        raise SpacesError(
            _(
                "Unknown kernel capabilities permission: "
                "{kernel_capabilities!r}.",
                kernel_capabilities=kernel_capabilities,
            )
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
    shortcuts = system.get("shortcuts", True)
    if not isinstance(shortcuts, bool):
        raise SpacesError(
            _("Application shortcuts permission must be a boolean.")
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
    _validate_preset(permissions, f"user {uid_key} permission")
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
    mounted_drives = permissions.get("mounted_drives", True)
    if not isinstance(mounted_drives, bool):
        raise SpacesError(
            _(
                "User {uid} mounted drives permission must be a boolean.",
                uid=uid_key,
            )
        )
    credential_agents = permissions.get("credential_agents", True)
    if not isinstance(credential_agents, bool):
        raise SpacesError(
            _(
                "User {uid} credential agent permission must be a boolean.",
                uid=uid_key,
            )
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


def validate_create_request(value: object) -> tuple[dict[str, Any], bool]:
    """Validate creation metadata and its non-persistent purge option."""

    request = dict(_require_mapping(value, "create payload"))
    purge = request.pop("purge", False)
    if not isinstance(purge, bool):
        raise SpacesError(_("Create purge option must be a boolean."))
    return validate_creation_info(request), purge


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
    if "name" not in request or not set(request).issubset({"name", "purge"}):
        raise SpacesError(
            _("Delete payload contains unknown settings.")
        )
    validate_space_name(request["name"])
    purge = request.get("purge", False)
    if not isinstance(purge, bool):
        raise SpacesError(_("Delete purge option must be a boolean."))
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
) -> tuple[str, str, str, bool, bool, list[str], bool, bool, bool, bool]:
    network = "basic"
    kernel_capabilities = "basic"
    devices = "basic"
    host_authentication = True
    shortcuts = True
    selected_home = list(PERMISSION_PRESETS["basic"]["user"]["home"])
    administrator = True
    desktop = True
    credential_agents = False
    mounted_drives = True
    if not info:
        return (
            network,
            kernel_capabilities,
            devices,
            host_authentication,
            shortcuts,
            selected_home,
            administrator,
            desktop,
            credential_agents,
            mounted_drives,
        )

    permissions = info.get("permissions", {})
    system_permissions = effective_system_permissions(
        permissions.get("system", {})
    )
    existing_network = system_permissions.get("network")
    if existing_network in NETWORK_LEVELS:
        network = existing_network
    existing_kernel_capabilities = system_permissions.get(
        "kernel_capabilities"
    )
    if existing_kernel_capabilities in KERNEL_CAPABILITY_LEVELS:
        kernel_capabilities = existing_kernel_capabilities
    existing_devices = system_permissions.get("devices")
    if existing_devices in DEVICE_LEVELS:
        devices = existing_devices
    existing_host_authentication = system_permissions.get(
        "host_authentication"
    )
    if isinstance(existing_host_authentication, bool):
        host_authentication = existing_host_authentication
    existing_shortcuts = system_permissions.get("shortcuts")
    if isinstance(existing_shortcuts, bool):
        shortcuts = existing_shortcuts
    user = permissions.get("users", {}).get(str(identity.uid))
    user_permissions = (
        effective_user_permissions(user)
        if isinstance(user, dict)
        else dict(PERMISSION_PRESETS["basic"]["user"])
    )
    home = user_permissions.get("home")
    if isinstance(home, list):
        selected_home = []
        for name in home:
            try:
                selected_home.append(validate_home_name(name))
            except SpacesError:
                continue
    existing_administrator = user_permissions.get("administrator")
    if isinstance(existing_administrator, bool):
        administrator = existing_administrator
    existing_desktop = user_permissions.get("desktop")
    if isinstance(existing_desktop, bool):
        desktop = existing_desktop
    existing_credential_agents = user_permissions.get("credential_agents")
    if isinstance(existing_credential_agents, bool):
        credential_agents = existing_credential_agents
    elif isinstance(user, dict):
        credential_agents = True
    existing_mounted_drives = user_permissions.get("mounted_drives")
    if isinstance(existing_mounted_drives, bool):
        mounted_drives = existing_mounted_drives
    return (
        network,
        kernel_capabilities,
        devices,
        host_authentication,
        shortcuts,
        selected_home,
        administrator,
        desktop,
        credential_agents,
        mounted_drives,
    )


def create_info(
    name: str,
    distribution: dict[str, Any],
    identity: Identity,
    network: str,
    home: list[str],
    administrator: bool = True,
    host_authentication: bool = True,
    shortcuts: bool = True,
    desktop: bool = True,
    credential_agents: bool = True,
    mounted_drives: bool = True,
    devices: str = "basic",
    kernel_capabilities: str = "basic",
    preset: str = "custom",
) -> dict[str, Any]:
    value = {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "distribution": distribution,
        "permissions": {
            "system": {
                "preset": preset,
                "network": network,
                "kernel_capabilities": kernel_capabilities,
                "devices": devices,
                "host_authentication": host_authentication,
                "shortcuts": shortcuts,
            },
            "users": {
                str(identity.uid): {
                    "gid": identity.gid,
                    "permissions": {
                        "preset": preset,
                        "home": sorted(home, key=str.casefold),
                        "administrator": administrator,
                        "desktop": desktop,
                        "credential_agents": credential_agents,
                        "mounted_drives": mounted_drives,
                    },
                }
            },
        },
    }
    return validate_creation_info(value)
