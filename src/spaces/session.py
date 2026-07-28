"""Host desktop-session data passed to one ``spaces enter`` invocation.

This module is deliberately standard-library-only because it is imported by
the privileged helper as well as the user-facing CLI.
"""

from __future__ import annotations

import os
import pwd
import re
import secrets
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from . import _
from . import core


SESSION_VERSION = 1
MAX_ENVIRONMENT_VALUE = 4096
# systemd creates missing BindReadOnlyPaths destinations as root, even for a
# transient unit with private mounts. Prepare these persistent guest-home
# targets before the space starts so applications can write beside them.
CONFIG_DIRECTORIES = (
    "gtk-3.0",
    "gtk-4.0",
    "fontconfig",
)
CONFIG_FILES = ("kdeglobals",)
SYSTEMCTL = "/usr/bin/systemctl"
MACHINECTL = "/usr/bin/machinectl"
SYSTEMD_RUN = "/usr/bin/systemd-run"
RESOURCE_KINDS = frozenset(
    {"appearance", "pipewire", "pulseaudio", "wayland", "x11"}
)
LOCAL_DISPLAY_PATTERN = re.compile(
    r"^(?:(?:unix)/)?:(?P<number>[0-9]+)(?:\.[0-9]+)?$"
)
REMOTE_DISPLAY_PATTERN = re.compile(
    r"^[A-Za-z0-9_.-]+:[0-9]+(?:\.[0-9]+)?$"
)
SOCKET_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
WAYLAND_DISPLAY_PATTERN = SOCKET_NAME_PATTERN

# These values affect desktop-toolkit behaviour but do not identify arbitrary
# host paths. Path-bearing variables are handled separately by the privileged
# side.
DESKTOP_ENVIRONMENT = frozenset(
    {
        "COLORTERM",
        "DESKTOP_SESSION",
        "GDK_BACKEND",
        "GDK_DPI_SCALE",
        "GDK_SCALE",
        "GTK_IM_MODULE",
        "GTK_THEME",
        "KDE_APPLICATIONS_AS_SCOPE",
        "KDE_FULL_SESSION",
        "KDE_SESSION_UID",
        "KDE_SESSION_VERSION",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_ADDRESS",
        "LC_COLLATE",
        "LC_CTYPE",
        "LC_IDENTIFICATION",
        "LC_MEASUREMENT",
        "LC_MESSAGES",
        "LC_MONETARY",
        "LC_NAME",
        "LC_NUMERIC",
        "LC_PAPER",
        "LC_TELEPHONE",
        "LC_TIME",
        "PIPEWIRE_REMOTE",
        "PULSE_SERVER",
        "QT_AUTO_SCREEN_SCALE_FACTOR",
        "QT_ENABLE_HIGHDPI_SCALING",
        "QT_FONT_DPI",
        "QT_IM_MODULE",
        "QT_QPA_PLATFORM",
        "QT_QPA_PLATFORMTHEME",
        "QT_SCALE_FACTOR",
        "QT_SCREEN_SCALE_FACTORS",
        "QT_STYLE_OVERRIDE",
        "QT_WAYLAND_DISABLE_WINDOWDECORATION",
        "SDL_VIDEODRIVER",
        "TERM",
        "TERM_PROGRAM",
        "TERM_PROGRAM_VERSION",
        "VTE_VERSION",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "XDG_CONFIG_HOME",
        "XCURSOR_SIZE",
        "XCURSOR_THEME",
        "XDG_DATA_HOME",
        "XDG_CURRENT_DESKTOP",
        "XDG_MENU_PREFIX",
        "XDG_SESSION_CLASS",
        "XDG_SESSION_DESKTOP",
        "XDG_SESSION_TYPE",
        "XMODIFIERS",
    }
)
DISPLAY_ENVIRONMENT = frozenset({"DISPLAY", "WAYLAND_DISPLAY"})


def prepare_user_paths(
    home: Path,
    uid: int,
    gid: int,
    user_name: str,
) -> None:
    """Prepare safe guest-owned targets for appearance-data mounts."""

    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | os.O_CLOEXEC
    )
    home_descriptor: int | None = None
    config_descriptor: int | None = None
    try:
        home_descriptor = os.open(home, directory_flags)
        try:
            os.mkdir(".config", mode=0o700, dir_fd=home_descriptor)
        except FileExistsError:
            pass
        config_descriptor = os.open(
            ".config",
            directory_flags,
            dir_fd=home_descriptor,
        )
        os.close(home_descriptor)
        home_descriptor = None
        os.fchown(config_descriptor, uid, gid)
        for name in CONFIG_DIRECTORIES:
            try:
                os.mkdir(
                    name,
                    mode=0o700,
                    dir_fd=config_descriptor,
                )
            except FileExistsError:
                pass
            descriptor = os.open(
                name,
                directory_flags,
                dir_fd=config_descriptor,
            )
            try:
                os.fchown(descriptor, uid, gid)
            finally:
                os.close(descriptor)

        for name in CONFIG_FILES:
            created = False
            try:
                descriptor = os.open(
                    name,
                    os.O_RDONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC,
                    0o600,
                    dir_fd=config_descriptor,
                )
                created = True
            except FileExistsError:
                descriptor = os.open(
                    name,
                    os.O_RDONLY
                    | os.O_NONBLOCK
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC,
                    dir_fd=config_descriptor,
                )
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise core.SpacesError(
                        _(
                            "Unsafe user session path for {name!r}.",
                            name=user_name,
                        )
                    )
                os.fchown(descriptor, uid, gid)
                if created:
                    os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
    except OSError as error:
        raise core.SpacesError(
            _(
                "Could not prepare session paths for {name!r}: {error}",
                name=user_name,
                error=error,
            )
        ) from error
    finally:
        if config_descriptor is not None:
            os.close(config_descriptor)
        if home_descriptor is not None:
            os.close(home_descriptor)


def _safe_value(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= MAX_ENVIRONMENT_VALUE
        and "\0" not in value
        and "\n" not in value
        and "\r" not in value
    )


def _is_socket(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISSOCK(metadata.st_mode)


def _runtime_socket(runtime: Path, value: str) -> Path | None:
    candidate = Path(value)
    if candidate.is_absolute():
        try:
            resolved = candidate.resolve(strict=False)
            resolved_runtime = runtime.resolve(strict=False)
        except (OSError, RuntimeError):
            return None
        if not (
            resolved == resolved_runtime
            or resolved.is_relative_to(resolved_runtime)
        ):
            return None
    elif SOCKET_NAME_PATTERN.fullmatch(value):
        candidate = runtime / value
    else:
        return None
    return candidate if _is_socket(candidate) else None


def discover(
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Capture the allowlisted part of the launching terminal's session."""

    source = os.environ if environment is None else environment
    captured = {
        name: value
        for name in sorted(DESKTOP_ENVIRONMENT | DISPLAY_ENVIRONMENT)
        if (value := source.get(name)) and _safe_value(value)
    }
    resources: set[str] = set()
    runtime = Path(f"/run/user/{os.getuid()}")
    has_runtime = source.get("XDG_RUNTIME_DIR") == str(runtime)

    wayland = captured.get("WAYLAND_DISPLAY")
    if (
        wayland is not None
        and has_runtime
        and _runtime_socket(runtime, wayland) is not None
    ):
        resources.add("wayland")
    else:
        captured.pop("WAYLAND_DISPLAY", None)

    display = captured.get("DISPLAY")
    if display is not None:
        local = LOCAL_DISPLAY_PATTERN.fullmatch(display)
        if (
            local is not None
            and _is_socket(
                Path(f"/tmp/.X11-unix/X{local.group('number')}")
            )
        ) or REMOTE_DISPLAY_PATTERN.fullmatch(display) is not None:
            resources.add("x11")
        else:
            captured.pop("DISPLAY", None)
    if "x11" not in resources:
        captured.pop("XAUTHORITY", None)

    pulse_server = captured.get("PULSE_SERVER")
    if pulse_server is not None or (
        has_runtime and _is_socket(runtime / "pulse" / "native")
    ):
        resources.add("pulseaudio")

    pipewire_remote = captured.get("PIPEWIRE_REMOTE", "pipewire-0")
    if (
        captured.get("PIPEWIRE_REMOTE") is not None
        or (
            has_runtime
            and SOCKET_NAME_PATTERN.fullmatch(pipewire_remote)
            and _is_socket(runtime / pipewire_remote)
        )
    ):
        resources.add("pipewire")

    if not resources:
        return None
    resources.add("appearance")
    return {
        "version": SESSION_VERSION,
        "resources": sorted(resources),
        "environment": captured,
    }


def validate_manifest(value: object) -> dict[str, Any]:
    """Validate the untrusted manifest received by the root helper."""

    if not isinstance(value, dict) or set(value) != {
        "version",
        "resources",
        "environment",
    }:
        raise core.SpacesError(_("Invalid session passthrough manifest."))
    if value["version"] != SESSION_VERSION or isinstance(
        value["version"], bool
    ):
        raise core.SpacesError(
            _("Unsupported session passthrough manifest version.")
        )
    environment = value["environment"]
    if not isinstance(environment, dict):
        raise core.SpacesError(
            _("Session passthrough environment must be an object.")
        )
    allowed = DESKTOP_ENVIRONMENT | DISPLAY_ENVIRONMENT
    if not set(environment).issubset(allowed):
        raise core.SpacesError(
            _("Session passthrough contains an unsupported environment value.")
        )
    resources = value["resources"]
    if (
        not isinstance(resources, list)
        or not resources
        or any(not isinstance(item, str) for item in resources)
        or len(resources) != len(set(resources))
        or not set(resources).issubset(RESOURCE_KINDS)
        or "appearance" not in resources
    ):
        raise core.SpacesError(
            _("Session passthrough contains invalid resource kinds.")
        )
    for name, item in environment.items():
        if not isinstance(name, str) or not _safe_value(item) or not item:
            raise core.SpacesError(
                _("Session passthrough contains an invalid environment value.")
            )
    requirements = {
        "DISPLAY": "x11",
        "PIPEWIRE_REMOTE": "pipewire",
        "PULSE_SERVER": "pulseaudio",
        "WAYLAND_DISPLAY": "wayland",
        "XAUTHORITY": "x11",
        "XDG_CONFIG_HOME": "appearance",
        "XDG_DATA_HOME": "appearance",
    }
    if any(
        name in environment and kind not in resources
        for name, kind in requirements.items()
    ):
        raise core.SpacesError(
            _("Session passthrough environment does not match its resources.")
        )
    if (
        "wayland" in resources
        and "WAYLAND_DISPLAY" not in environment
    ) or ("x11" in resources and "DISPLAY" not in environment):
        raise core.SpacesError(
            _("Session passthrough resource has no selected endpoint.")
        )
    if not set(resources).intersection(
        {"pipewire", "pulseaudio", "wayland", "x11"}
    ):
        raise core.SpacesError(
            _("Session passthrough has no desktop-session endpoint.")
        )
    return {
        "version": SESSION_VERSION,
        "resources": list(resources),
        "environment": dict(environment),
    }


@dataclass(frozen=True)
class _SessionBind:
    source: Path
    staging: str
    destination: str
    device: int
    inode: int


@dataclass(frozen=True)
class _SessionPlan:
    binds: tuple[_SessionBind, ...]
    environment: dict[str, str]
    staging_root: str
    destination_root: str


@dataclass
class _SessionSignalState:
    process: subprocess.Popen[Any] | None = None
    signum: int | None = None


class _SessionInterrupted(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


@contextmanager
def _forward_session_signals(
    state: _SessionSignalState,
) -> Iterator[None]:
    previous_handlers: dict[int, Any] = {}

    def forward(signum: int, _frame: object) -> None:
        if state.process is None:
            if state.signum is not None:
                return
            state.signum = signum
            raise _SessionInterrupted(signum)
        state.signum = signum
        if state.process.poll() is None:
            state.process.send_signal(signum)

    try:
        for signum in (signal.SIGTERM, signal.SIGHUP):
            previous_handlers[signum] = signal.signal(signum, forward)
        yield
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def _validated_source(
    path: Path,
    *,
    roots: tuple[Path, ...],
    kinds: tuple[int, ...],
    owner: int | None = None,
    access_user: pwd.struct_passwd | None = None,
) -> tuple[Path, os.stat_result] | None:
    try:
        candidate = path.resolve(strict=False)
        resolved_roots = tuple(root.resolve(strict=False) for root in roots)
    except (OSError, RuntimeError) as error:
        raise core.SpacesError(
            _("Could not resolve session resource {path}.", path=path)
        ) from error
    if not any(
        candidate == root or candidate.is_relative_to(root)
        for root in resolved_roots
    ):
        raise core.SpacesError(
            _("Session resource escapes its allowed directory: {path}.", path=path)
    )
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise core.SpacesError(
            _("Could not resolve session resource {path}.", path=path)
        ) from error
    resolved_metadata = resolved.stat()
    if stat.S_IFMT(resolved_metadata.st_mode) not in kinds:
        raise core.SpacesError(
            _("Session resource has an unexpected file type: {path}.", path=path)
        )
    if owner is not None and resolved_metadata.st_uid != owner:
        raise core.SpacesError(
            _(
                "Session resource is not owned by the initiating user: "
                "{path}.",
                path=path,
            )
        )
    if access_user is not None:
        groups = set(
            os.getgrouplist(access_user.pw_name, access_user.pw_gid)
        )

        def permissions(item: os.stat_result) -> int:
            if item.st_uid == access_user.pw_uid:
                return (item.st_mode >> 6) & 0o7
            if item.st_gid in groups:
                return (item.st_mode >> 3) & 0o7
            return item.st_mode & 0o7

        required = (
            0o2
            if stat.S_ISSOCK(resolved_metadata.st_mode)
            else 0o5
            if stat.S_ISDIR(resolved_metadata.st_mode)
            else 0o4
        )
        if permissions(resolved_metadata) & required != required:
            raise core.SpacesError(
                _(
                    "Session resource is not accessible to the initiating "
                    "user: {path}.",
                    path=path,
                )
            )
        for parent in resolved.parents:
            parent_metadata = parent.stat()
            if permissions(parent_metadata) & 0o1 == 0:
                raise core.SpacesError(
                    _(
                        "Session resource is not accessible to the initiating "
                        "user: {path}.",
                        path=path,
                    )
                )
    return resolved, resolved_metadata


def _prepare_session_plan(
    manifest: dict[str, Any],
    user: pwd.struct_passwd,
    token: str,
    temporary: Path,
) -> _SessionPlan:
    environment = dict(manifest["environment"])
    resources = set(manifest["resources"])
    uid = user.pw_uid
    runtime = Path(f"/run/user/{uid}")
    home = Path(user.pw_dir)
    staging_root = f"/run/spaces-staging/{token}"
    destination_root = f"/tmp/.spaces-session-{token}"
    binds: list[_SessionBind] = []

    runtime_available = False
    try:
        runtime_metadata = runtime.stat()
        runtime_available = (
            runtime.is_dir()
            and not runtime.is_symlink()
            and runtime_metadata.st_uid == uid
        )
    except OSError:
        pass

    def add(
        label: str,
        source: Path,
        destination: str,
        *,
        roots: tuple[Path, ...],
        kinds: tuple[int, ...],
        owner: int | None = None,
        access_user: pwd.struct_passwd | None = user,
    ) -> bool:
        checked = _validated_source(
            source,
            roots=roots,
            kinds=kinds,
            owner=owner,
            access_user=access_user,
        )
        if checked is None:
            return False
        resolved, metadata = checked
        binds.append(
            _SessionBind(
                source=resolved,
                staging=f"{staging_root}/{label}",
                destination=destination,
                device=metadata.st_dev,
                inode=metadata.st_ino,
            )
        )
        return True

    wayland = environment.pop("WAYLAND_DISPLAY", None)
    if wayland is not None:
        wayland_source = Path(wayland)
        if not wayland_source.is_absolute():
            if not WAYLAND_DISPLAY_PATTERN.fullmatch(wayland):
                raise core.SpacesError(
                    _("Invalid Wayland session endpoint.")
                )
            wayland_source = runtime / wayland
        if not runtime_available:
            raise core.SpacesError(_("Invalid Wayland session endpoint."))
        destination = f"{destination_root}/wayland/socket"
        if add(
            "wayland",
            wayland_source,
            destination,
            roots=(runtime,),
            kinds=(stat.S_IFSOCK,),
            owner=uid,
        ):
            environment["WAYLAND_DISPLAY"] = destination

    display = environment.pop("DISPLAY", None)
    if display is not None:
        local_display = LOCAL_DISPLAY_PATTERN.fullmatch(display)
        if local_display is not None:
            number = local_display.group("number")
            socket_path = Path(f"/tmp/.X11-unix/X{number}")
            if add(
                "x11",
                socket_path,
                str(socket_path),
                roots=(Path("/tmp/.X11-unix"),),
                kinds=(stat.S_IFSOCK,),
            ):
                environment["DISPLAY"] = display
        elif REMOTE_DISPLAY_PATTERN.fullmatch(display):
            environment["DISPLAY"] = display
        else:
            raise core.SpacesError(_("Invalid X11 display value."))

    xauthority_value = environment.pop("XAUTHORITY", None)
    if "x11" in resources:
        xauthority = (
            Path(xauthority_value)
            if xauthority_value is not None
            else home / ".Xauthority"
        )
        if not xauthority.is_absolute():
            raise core.SpacesError(_("XAUTHORITY must be an absolute path."))
        xauthority_roots = (
            (home, runtime, Path("/tmp"))
            if runtime_available
            else (home, Path("/tmp"))
        )
        xauthority_destination = f"{destination_root}/xauthority"
        if add(
            "xauthority",
            xauthority,
            xauthority_destination,
            roots=xauthority_roots,
            kinds=(stat.S_IFREG,),
            owner=uid,
        ):
            environment["XAUTHORITY"] = xauthority_destination

    pulse_value = environment.pop("PULSE_SERVER", None)
    if "pulseaudio" in resources:
        pulse_source = runtime / "pulse" / "native"
        if pulse_value and pulse_value.startswith("unix:"):
            pulse_source = Path(pulse_value.removeprefix("unix:"))
            if not pulse_source.is_absolute():
                raise core.SpacesError(
                    _("Invalid PulseAudio server path.")
                )
        pulse_destination = f"{destination_root}/pulse/native"
        if runtime_available and add(
            "pulse",
            pulse_source,
            pulse_destination,
            roots=(runtime,),
            kinds=(stat.S_IFSOCK,),
            owner=uid,
        ):
            environment["PULSE_SERVER"] = f"unix:{pulse_destination}"
        elif pulse_value and not pulse_value.startswith("unix:"):
            environment["PULSE_SERVER"] = pulse_value

    pipewire_remote = environment.pop("PIPEWIRE_REMOTE", "pipewire-0")
    if "pipewire" in resources:
        if not WAYLAND_DISPLAY_PATTERN.fullmatch(pipewire_remote):
            raise core.SpacesError(_("Invalid PipeWire remote name."))
        pipewire_destination = f"{destination_root}/pipewire"
        if runtime_available and add(
            "pipewire",
            runtime / pipewire_remote,
            f"{pipewire_destination}/{pipewire_remote}",
            roots=(runtime,),
            kinds=(stat.S_IFSOCK,),
            owner=uid,
        ):
            environment["PIPEWIRE_RUNTIME_DIR"] = pipewire_destination
            environment["PIPEWIRE_REMOTE"] = pipewire_remote
            manager = runtime / f"{pipewire_remote}-manager"
            add(
                "pipewire-manager",
                manager,
                f"{pipewire_destination}/{pipewire_remote}-manager",
                roots=(runtime,),
                kinds=(stat.S_IFSOCK,),
                owner=uid,
            )

    guest_home = f"/home/{user.pw_name}"
    config_value = environment.pop("XDG_CONFIG_HOME", None)
    config = (
        Path(config_value)
        if config_value is not None
        else home / ".config"
    )
    if not config.is_absolute():
        raise core.SpacesError(_("XDG_CONFIG_HOME must be an absolute path."))
    add(
        "config-kdeglobals",
        config / "kdeglobals",
        f"{guest_home}/.config/kdeglobals",
        roots=(home,),
        kinds=(stat.S_IFREG,),
        owner=uid,
    )
    for name in ("gtk-3.0", "gtk-4.0"):
        add(
            f"config-{name}",
            config / name,
            f"{guest_home}/.config/{name}",
            roots=(home,),
            kinds=(stat.S_IFDIR,),
            owner=uid,
        )
    add(
        "config-fontconfig",
        config / "fontconfig",
        f"{guest_home}/.config/fontconfig",
        roots=(home,),
        kinds=(stat.S_IFDIR,),
        owner=uid,
    )

    data_roots: list[str] = []
    data_home_value = environment.pop("XDG_DATA_HOME", None)
    data_home = (
        Path(data_home_value)
        if data_home_value is not None
        else home / ".local" / "share"
    )
    if not data_home.is_absolute():
        raise core.SpacesError(_("XDG_DATA_HOME must be an absolute path."))
    appearance_sources = (
        ("user", data_home, (home,), uid),
        (
            "local",
            Path("/usr/local/share"),
            (Path("/usr/local/share"),),
            None,
        ),
        ("system", Path("/usr/share"), (Path("/usr/share"),), None),
    )
    for label, source_root, allowed_roots, owner in appearance_sources:
        destination_data_root = f"{destination_root}/data/{label}"
        found = False
        for name in ("icons", "themes", "color-schemes"):
            found = (
                add(
                    f"data-{label}-{name}",
                    source_root / name,
                    f"{destination_data_root}/{name}",
                    roots=allowed_roots,
                    kinds=(stat.S_IFDIR,),
                    owner=owner,
                )
                or found
            )
        if found:
            data_roots.append(destination_data_root)

    for label, source in (
        ("legacy-icons", home / ".icons"),
        ("legacy-themes", home / ".themes"),
    ):
        kind = label.removeprefix("legacy-")
        legacy_root = f"{destination_root}/data/{label}"
        if add(
            label,
            source,
            f"{legacy_root}/{kind}",
            roots=(home,),
            kinds=(stat.S_IFDIR,),
            owner=uid,
        ):
            data_roots.append(legacy_root)

    font_destinations: list[str] = []
    font_sources = (
        ("user-fonts", home / ".local" / "share" / "fonts", (home,), uid),
        ("legacy-fonts", home / ".fonts", (home,), uid),
        (
            "local-fonts",
            Path("/usr/local/share/fonts"),
            (Path("/usr/local/share"),),
            None,
        ),
        (
            "system-fonts",
            Path("/usr/share/fonts"),
            (Path("/usr/share"),),
            None,
        ),
    )
    for label, source, allowed_roots, owner in font_sources:
        destination = f"{destination_root}/fonts/{label}"
        if add(
            label,
            source,
            destination,
            roots=allowed_roots,
            kinds=(stat.S_IFDIR,),
            owner=owner,
        ):
            font_destinations.append(destination)
    if font_destinations:
        font_config = temporary / "fonts.conf"
        directories = "".join(
            f"  <dir>{destination}</dir>\n"
            for destination in font_destinations
        )
        font_config.write_text(
            "<?xml version=\"1.0\"?>\n"
            "<!DOCTYPE fontconfig SYSTEM \"fonts.dtd\">\n"
            "<fontconfig>\n"
            f"{directories}"
            "  <include ignore_missing=\"yes\">"
            "/etc/fonts/fonts.conf</include>\n"
            "</fontconfig>\n",
            encoding="utf-8",
        )
        os.chmod(font_config, 0o644)
        add(
            "fontconfig",
            font_config,
            f"{destination_root}/fonts.conf",
            roots=(temporary,),
            kinds=(stat.S_IFREG,),
            owner=0,
            access_user=None,
        )
        environment["FONTCONFIG_FILE"] = f"{destination_root}/fonts.conf"

    if data_roots:
        environment["XDG_DATA_DIRS"] = ":".join(
            [*data_roots, "/usr/local/share", "/usr/share"]
        )
        cursor_paths = [
            f"{root}/icons"
            for root in data_roots
        ]
        environment["XCURSOR_PATH"] = ":".join(
            [*cursor_paths, "/usr/local/share/icons", "/usr/share/icons"]
        )

    environment["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
    environment["DBUS_SESSION_BUS_ADDRESS"] = (
        f"unix:path=/run/user/{uid}/bus"
    )
    return _SessionPlan(
        binds=tuple(binds),
        environment=environment,
        staging_root=staging_root,
        destination_root=destination_root,
    )


def _machine_root_command(space_name: str, command: list[str]) -> None:
    subprocess.run(
        [
            MACHINECTL,
            "--quiet",
            "--uid=root",
            "--",
            "shell",
            space_name,
            *command,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def _prepare_guest_session_directories(
    space_name: str,
    plan: _SessionPlan,
    user: pwd.struct_passwd,
) -> None:
    _machine_root_command(
        space_name,
        [
            "/usr/bin/install",
            "-d",
            "-m",
            "0700",
            "-o",
            "0",
            "-g",
            "0",
            plan.staging_root,
        ],
    )
    _machine_root_command(
        space_name,
        [
            "/usr/bin/install",
            "-d",
            "-m",
            "0700",
            "-o",
            str(user.pw_uid),
            "-g",
            str(user.pw_gid),
            plan.destination_root,
        ],
    )


def _bind_session_resource(space_name: str, binding: _SessionBind) -> None:
    descriptor = os.open(
        binding.source,
        os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            metadata.st_dev != binding.device
            or metadata.st_ino != binding.inode
        ):
            raise core.SpacesError(
                _("Session resource changed while it was being mounted.")
            )
        subprocess.run(
            [
                MACHINECTL,
                "--quiet",
                "--no-ask-password",
                "--mkdir",
                "--read-only",
                "bind",
                space_name,
                f"/proc/{os.getpid()}/fd/{descriptor}",
                binding.staging,
            ],
            check=True,
        )
    finally:
        os.close(descriptor)


def _unmount_session_resource(space_name: str, path: str) -> None:
    _machine_root_command(
        space_name,
        [
            "/usr/bin/umount",
            "--lazy",
            "--",
            path,
        ],
    )


def _remove_guest_session_paths(
    space_name: str,
    *paths: str,
) -> None:
    _machine_root_command(
        space_name,
        [
            "/bin/rm",
            "-rf",
            "--",
            *paths,
        ],
    )


def _space_directory(name: str) -> Path:
    core.validate_space_name(name)
    space = core.STATE_ROOT / name
    if space.is_symlink() or not space.is_dir():
        raise core.SpacesError(
            _("Space {name!r} does not exist.", name=name)
        )
    return space


def _guest_login_shell(space_name: str, user_name: str) -> str:
    passwd_path = _space_directory(space_name) / "rootfs" / "etc" / "passwd"
    if passwd_path.is_symlink() or not passwd_path.is_file():
        raise core.SpacesError(_("Unsafe guest passwd database."))
    for line in passwd_path.read_text(encoding="utf-8").splitlines():
        fields = line.split(":")
        if len(fields) == 7 and fields[0] == user_name:
            shell = fields[6]
            if shell.startswith("/") and "\0" not in shell:
                return shell
            break
    return "/bin/sh"


def _session_unit_command(
    space_name: str,
    user: pwd.struct_passwd,
    plan: _SessionPlan,
    unit_name: str,
    command: list[str],
) -> list[str]:
    actual_command = command or [
        _guest_login_shell(space_name, user.pw_name),
        "-l",
    ]
    result = [
        SYSTEMD_RUN,
        f"--machine={space_name}",
        f"--unit={unit_name}",
        "--quiet",
        "--wait",
        "--collect",
        "--service-type=exec",
        "--no-ask-password",
        "--expand-environment=no",
        f"--uid={user.pw_name}",
        f"--working-directory=/home/{user.pw_name}",
        "--pty",
        "--pipe",
        "--property=PrivateMounts=yes",
        "--property=ExitType=cgroup",
        "--property=KillMode=control-group",
    ]
    for binding in plan.binds:
        result.append(
            "--property=BindReadOnlyPaths="
            f"{binding.staging}:{binding.destination}"
        )
    for name, value in sorted(plan.environment.items()):
        result.append(f"--setenv={name}={value}")
    return [*result, "--", *actual_command]


def _wait_for_private_unit(
    process: subprocess.Popen[Any],
    space_name: str,
    unit_name: str,
) -> bool:
    deadline = time.monotonic() + 30
    while process.poll() is None and time.monotonic() < deadline:
        state = subprocess.run(
            [
                SYSTEMCTL,
                f"--machine={space_name}",
                "--no-ask-password",
                "show",
                "--property=ActiveState",
                "--value",
                unit_name,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if state.returncode == 0 and state.stdout.strip() == "active":
            return True
        time.sleep(0.05)
    return False


def _stop_session_unit(space_name: str, unit_name: str) -> None:
    subprocess.run(
        [
            SYSTEMCTL,
            f"--machine={space_name}",
            "--no-ask-password",
            "stop",
            unit_name,
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _ensure_guest_user_manager(
    space_name: str,
    user: pwd.struct_passwd,
) -> None:
    subprocess.run(
        [
            SYSTEMCTL,
            f"--machine={space_name}",
            "--no-ask-password",
            "start",
            f"user@{user.pw_uid}.service",
        ],
        check=True,
    )


def enter(
    user: pwd.struct_passwd,
    space_name: str,
    command: list[str],
    manifest: dict[str, Any],
) -> int:
    """Run one command with the validated host session attached."""

    token = secrets.token_hex(12)
    unit_name = f"spaces-enter-{token}.service"
    mounted: list[_SessionBind] = []
    signal_state = _SessionSignalState()
    try:
        with (
            _forward_session_signals(signal_state),
            tempfile.TemporaryDirectory(
                prefix="spaces-session-",
                dir="/run",
            ) as name,
        ):
            plan = _prepare_session_plan(
                manifest,
                user,
                token,
                Path(name),
            )
            try:
                _ensure_guest_user_manager(space_name, user)
                _prepare_guest_session_directories(space_name, plan, user)
                for binding in plan.binds:
                    mounted.append(binding)
                    _bind_session_resource(space_name, binding)
                previous_mask = signal.pthread_sigmask(
                    signal.SIG_BLOCK,
                    {signal.SIGINT, signal.SIGTERM, signal.SIGHUP},
                )
                try:
                    process = subprocess.Popen(
                        _session_unit_command(
                            space_name,
                            user,
                            plan,
                            unit_name,
                            command,
                        )
                    )
                    signal_state.process = process
                finally:
                    signal.pthread_sigmask(
                        signal.SIG_SETMASK,
                        previous_mask,
                    )
                if _wait_for_private_unit(
                    process,
                    space_name,
                    unit_name,
                ):
                    while mounted:
                        binding = mounted[-1]
                        _unmount_session_resource(
                            space_name,
                            binding.staging,
                        )
                        mounted.pop()
                    _remove_guest_session_paths(
                        space_name,
                        plan.staging_root,
                    )
                returncode = process.wait()
                if signal_state.signum is not None:
                    return 128 + signal_state.signum
                return returncode
            finally:
                process = signal_state.process
                if process is not None:
                    _stop_session_unit(space_name, unit_name)
                    if process.poll() is None:
                        process.wait()
                for binding in reversed(mounted):
                    try:
                        _unmount_session_resource(
                            space_name,
                            binding.staging,
                        )
                    except (OSError, subprocess.CalledProcessError):
                        pass
                try:
                    _remove_guest_session_paths(
                        space_name,
                        plan.staging_root,
                        plan.destination_root,
                    )
                except (OSError, subprocess.CalledProcessError):
                    pass
    except _SessionInterrupted as interruption:
        return 128 + interruption.signum
