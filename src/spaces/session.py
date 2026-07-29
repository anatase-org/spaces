"""Login-scoped host desktop forwarding for running spaces.

Only this module knows which host-session resources may cross into a space.
The launch monitor supplies trusted logind records; terminal environments and
callers never supply paths, mount destinations, or systemd properties.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pwd
import re
import select
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from . import _
from . import core


MAX_ENVIRONMENT_VALUE = 4096
MACHINECTL = "/usr/bin/machinectl"
SYSTEMCTL = "/usr/bin/systemctl"
SYSTEMD_RUN = "/usr/bin/systemd-run"
XDG_DBUS_PROXY = "/usr/bin/xdg-dbus-proxy"
RUNTIME_ROOT = Path("/run/spaces")
DESKTOP_ROOT = PurePosixPath("/run/spaces/desktop")
ENVIRONMENT_DIRECTORY = "env"
PORTAL_SOCKET_NAME = "bus"
PORTAL_READY_TIMEOUT = 5.0
LOCAL_DISPLAY_PATTERN = re.compile(
    r"^(?:(?:unix)/)?:(?P<number>[0-9]+)(?:\.[0-9]+)?$"
)
SOCKET_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
STATUS_STATES = frozenset({"active", "inactive", "pending"})

# Do not add DBUS_SESSION_BUS_ADDRESS, XDG_RUNTIME_DIR, or XDG_SESSION_ID here.
# Those identify the host login and must remain guest-native values established
# by pam_systemd. Every entry in this set is safe to copy from a selected host
# graphical session into a PAM-backed command environment.
DESKTOP_ENVIRONMENT = frozenset(
    {
        "COLORTERM",
        "DESKTOP_SESSION",
        "DISPLAY",
        "FONTCONFIG_FILE",
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
        "PIPEWIRE_RUNTIME_DIR",
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
        "SPACES_NAME",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "XCURSOR_PATH",
        "XCURSOR_SIZE",
        "XCURSOR_THEME",
        "XDG_CURRENT_DESKTOP",
        "XDG_CONFIG_DIRS",
        "XDG_DATA_DIRS",
        "XDG_MENU_PREFIX",
        "XDG_SESSION_CLASS",
        "XDG_SESSION_DESKTOP",
        "XDG_SESSION_TYPE",
        "XMODIFIERS",
    }
)
CONFIG_DIRECTORIES = ("gtk-3.0", "gtk-4.0", "fontconfig")
CONFIG_FILES = ("kdeglobals",)
POLKIT_AGENTS = (
    "/usr/libexec/polkit-kde-authentication-agent-1",
    "/usr/libexec/kf6/polkit-kde-authentication-agent-1",
    "/usr/lib/polkit-kde-authentication-agent-1",
)
POLKIT_AGENT_GLOB = "usr/lib/*/libexec/polkit-kde-authentication-agent-1"
HOST_PORTAL_INTERFACES = (
    "Account",
    "Clipboard",
    "Email",
    "GlobalShortcuts",
    "Inhibit",
    "InputCapture",
    "Notification",
    "Print",
    "RemoteDesktop",
    "ScreenCast",
    "Screenshot",
    "Secret",
    "Settings",
    "Wallpaper",
)
PORTAL_DATA_ROOT = Path("/usr/share/spaces/portal")
PORTAL_DATA_BINDS = (
    (
        PORTAL_DATA_ROOT / "dbus-1" / "services",
        "/usr/local/share/dbus-1/services",
    ),
    (
        PORTAL_DATA_ROOT / "xdg-desktop-portal",
        "/usr/local/share/xdg-desktop-portal",
    ),
    (
        PORTAL_DATA_ROOT / "systemd" / "user",
        "/usr/local/share/systemd/user",
    ),
    (
        PORTAL_DATA_ROOT / "config",
        "/run/spaces-host/config",
    ),
)
GUEST_PORTAL_FRONTENDS = (
    "usr/libexec/xdg-desktop-portal",
    "usr/lib/xdg-desktop-portal",
)
GUEST_KDE_PORTAL = "usr/share/xdg-desktop-portal/portals/kde.portal"
GUEST_KWALLET_PROVIDERS = (
    "usr/bin/ksecretd",
    "usr/bin/kwalletd5",
)
GUEST_PIPEWIRE_CONFIGS = (
    "usr/share/pipewire/client.conf",
    "etc/pipewire/client.conf",
)


logger = logging.getLogger(__name__)


class DesktopUser(Protocol):
    uid: int
    gid: int
    name: str
    host_home: Path
    guest_home: PurePosixPath
    desktop: bool


@dataclass(frozen=True)
class LoginSession:
    """The logind properties relevant to graphical-session selection."""

    session_id: str
    active: bool
    remote: bool
    session_type: str
    session_class: str


@dataclass(frozen=True, order=True)
class DesktopBind:
    """A validated and identity-pinned host resource."""

    destination: str
    source: Path
    device: int
    inode: int


@dataclass(frozen=True)
class DesktopPlan:
    session_id: str
    binds: tuple[DesktopBind, ...]
    environment: dict[str, str]
    generated_root: Path | None = None


class DesktopSetupError(Exception):
    """A forwarding setup failed but the space may continue safely."""


class DesktopRevocationError(Exception):
    """A stale host resource could not be removed safely."""


@dataclass
class _ActiveDesktop:
    plan: DesktopPlan
    portal: PortalProxy | None = None
    portal_binding: DesktopBind | None = None


@dataclass
class PortalProxy:
    """One login-scoped filtered connection to the host session bus."""

    process: subprocess.Popen[bytes]
    control_fd: int
    socket_path: Path

    def close(self) -> None:
        if self.control_fd >= 0:
            os.close(self.control_fd)
            self.control_fd = -1
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


def prepare_user_paths(
    home: Path,
    uid: int,
    gid: int,
    user_name: str,
) -> None:
    """Precreate guest-owned configuration targets used by read-only binds.

    Keep this here with the forwarding constants: adding another conventional
    home destination without preparing it first makes ``machinectl bind
    --mkdir`` create a root-owned parent and breaks unrelated application data.
    """

    directory_flags = (
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
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
            ".config", directory_flags, dir_fd=home_descriptor
        )
        os.close(home_descriptor)
        home_descriptor = None
        os.fchown(config_descriptor, uid, gid)
        for name in CONFIG_DIRECTORIES:
            try:
                os.mkdir(name, mode=0o700, dir_fd=config_descriptor)
            except FileExistsError:
                pass
            descriptor = os.open(
                name, directory_flags, dir_fd=config_descriptor
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
                        _("Unsafe user session path for {name!r}.", name=user_name)
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
        and 0 < len(value) <= MAX_ENVIRONMENT_VALUE
        and "\0" not in value
        and "\n" not in value
        and "\r" not in value
    )


def _parse_environment(output: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in output.splitlines():
        name, separator, value = line.partition("=")
        if separator and name in DESKTOP_ENVIRONMENT | {
            "DBUS_SESSION_BUS_ADDRESS",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_RUNTIME_DIR",
            "XDG_SESSION_ID",
        } and _safe_value(value):
            result[name] = value
    return result


def host_manager_environment(user: DesktopUser) -> dict[str, str]:
    """Read the host user manager without creating another login session."""

    # Do not use ``--machine=<user>@.host`` here. That transport starts a
    # systemd-stdio-bridge PAM session; logind then wakes this monitor again,
    # turning one environment read into an unbounded reconciliation loop.
    completed = subprocess.run(
        [
            SYSTEMCTL,
            "--user",
            "--no-ask-password",
            "show-environment",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            "DBUS_SESSION_BUS_ADDRESS": (
                f"unix:path=/run/user/{user.uid}/bus"
            ),
            "XDG_RUNTIME_DIR": f"/run/user/{user.uid}",
        },
        user=user.uid,
        group=user.gid,
    )
    if completed.returncode != 0:
        return {}
    return _parse_environment(completed.stdout)


def portal_bind_arguments(rootfs: Path) -> tuple[str, ...]:
    """Return immutable portal assets when the guest portal stack is present.

    This is deliberately a read-only preflight. Existing spaces can install
    the missing packages and retry without a rootfs migration.
    """

    frontend = any(
        (rootfs / candidate).is_file()
        for candidate in GUEST_PORTAL_FRONTENDS
    )
    kde = (rootfs / GUEST_KDE_PORTAL).is_file()
    kwallet = any(
        (rootfs / candidate).is_file()
        for candidate in GUEST_KWALLET_PROVIDERS
    )
    pipewire = any(
        (rootfs / candidate).is_file()
        for candidate in GUEST_PIPEWIRE_CONFIGS
    )
    assets = all(source.is_dir() for source, _destination in PORTAL_DATA_BINDS)
    if not frontend or not kde or not kwallet or not pipewire:
        missing = []
        if not frontend:
            missing.append("xdg-desktop-portal")
        if not kde:
            missing.append("xdg-desktop-portal-kde")
        if not kwallet:
            missing.append("kwallet Secret Service provider")
        if not pipewire:
            missing.append("pipewire")
        logger.warning(
            _(
                "Host portal integration is unavailable; install {packages} "
                "inside the space to retry.",
                packages=", ".join(missing),
            )
        )
        return ()
    if not assets:
        logger.warning(
            _("Host portal integration assets are missing; continuing without them.")
        )
        return ()
    return tuple(
        f"--bind-ro={source}:{destination}"
        for source, destination in PORTAL_DATA_BINDS
    )


def _portal_policy_arguments() -> list[str]:
    name = "org.freedesktop.portal.Desktop"
    desktop = "/org/freedesktop/portal/desktop"
    arguments = [
        "--filter",
        (
            f"--call={name}=org.freedesktop.DBus.Introspectable."
            f"Introspect@{desktop}"
        ),
        f"--call={name}=org.freedesktop.DBus.Properties.*@{desktop}",
    ]
    for interface in HOST_PORTAL_INTERFACES:
        arguments.append(
            f"--call={name}=org.freedesktop.portal.{interface}.*@{desktop}"
        )
        arguments.append(
            f"--broadcast={name}=org.freedesktop.portal.{interface}.*@{desktop}"
        )
    for interface, subtree in (
        ("Request", f"{desktop}/request/*"),
        ("Session", f"{desktop}/session/*"),
    ):
        arguments.append(
            f"--call={name}=org.freedesktop.portal.{interface}.*@{subtree}"
        )
        arguments.append(
            f"--broadcast={name}=org.freedesktop.portal.{interface}.*@{subtree}"
        )
    return arguments


def _prepare_portal_directory(space_name: str, user: DesktopUser) -> Path:
    core.validate_space_name(space_name)
    space = RUNTIME_ROOT / space_name
    desktop = space / "desktop"
    parent = desktop / str(user.uid)
    directory = parent / "portal"
    expected_owner = 0 if os.geteuid() == 0 else os.getuid()
    for path in (space, desktop, parent):
        if path.is_symlink():
            raise core.SpacesError(_("Unsafe portal runtime path: {path}.", path=path))
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != expected_owner:
            raise core.SpacesError(_("Unsafe portal runtime path: {path}.", path=path))
        os.chmod(path, 0o711)
    if directory.is_symlink():
        raise core.SpacesError(_("Unsafe portal runtime path: {path}.", path=directory))
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chown(directory, user.uid, user.gid)
    os.chmod(directory, 0o700)
    socket_path = directory / PORTAL_SOCKET_NAME
    socket_path.unlink(missing_ok=True)
    return socket_path


def start_portal_proxy(
    space_name: str,
    user: DesktopUser,
    session_id: str,
) -> tuple[PortalProxy, DesktopBind]:
    """Start a filtered host bus in an app-scoped user systemd unit."""

    socket_path = _prepare_portal_directory(space_name, user)
    generation = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    unit = (
        "app-spaces-org.anatase.spaces."
        f"{space_name}-{generation}.scope"
    )
    control_read, control_write = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
    address = f"unix:path=/run/user/{user.uid}/bus"
    command = [
        SYSTEMD_RUN,
        "--user",
        "--scope",
        "--quiet",
        f"--unit={unit}",
        "--",
        XDG_DBUS_PROXY,
        address,
        str(socket_path),
        f"--fd={control_write}",
        *_portal_policy_arguments(),
    ]
    try:
        process = subprocess.Popen(
            command,
            env={
                "DBUS_SESSION_BUS_ADDRESS": address,
                "LANG": "C.UTF-8",
                "PATH": "/usr/bin",
                "XDG_RUNTIME_DIR": f"/run/user/{user.uid}",
            },
            user=user.uid,
            group=user.gid,
            pass_fds=(control_write,),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        os.close(control_read)
        os.close(control_write)
        raise
    os.close(control_write)
    proxy = PortalProxy(process, control_read, socket_path)
    poller = select.poll()
    poller.register(control_read, select.POLLIN | select.POLLHUP | select.POLLERR)
    try:
        events = poller.poll(round(PORTAL_READY_TIMEOUT * 1000))
        if not events or process.poll() is not None:
            raise core.SpacesError(_("Timed out starting the host portal proxy."))
        try:
            ready = os.read(control_read, 1)
        except BlockingIOError:
            ready = b""
        if not ready:
            raise core.SpacesError(_("The host portal proxy exited before readiness."))
        metadata = socket_path.lstat()
        if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != user.uid:
            raise core.SpacesError(_("Unsafe host portal proxy socket."))
        binding = DesktopBind(
            destination=str(
                DESKTOP_ROOT
                / str(user.uid)
                / "portal"
                / PORTAL_SOCKET_NAME
            ),
            source=socket_path,
            device=metadata.st_dev,
            inode=metadata.st_ino,
        )
        return proxy, binding
    except Exception:
        proxy.close()
        socket_path.unlink(missing_ok=True)
        raise


def select_graphical_session(
    sessions: tuple[LoginSession, ...],
    environment: dict[str, str],
) -> LoginSession | None:
    """Select one unambiguous active local graphical user session."""

    manager_session = environment.get("XDG_SESSION_ID")
    if not manager_session:
        return None
    candidates = [
        item
        for item in sessions
        if item.session_id == manager_session
        and item.active
        and not item.remote
        and item.session_class == "user"
        and item.session_type in {"wayland", "x11"}
    ]
    return candidates[0] if len(candidates) == 1 else None


def _permissions(metadata: os.stat_result, user: pwd.struct_passwd) -> int:
    groups = set(os.getgrouplist(user.pw_name, user.pw_gid))
    if metadata.st_uid == user.pw_uid:
        return (metadata.st_mode >> 6) & 0o7
    if metadata.st_gid in groups:
        return (metadata.st_mode >> 3) & 0o7
    return metadata.st_mode & 0o7


def _validated_source(
    path: Path,
    *,
    roots: tuple[Path, ...],
    kinds: tuple[int, ...],
    user: pwd.struct_passwd,
    owner: int | None = None,
    require_access: bool = True,
) -> tuple[Path, os.stat_result] | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise core.SpacesError(
            _("Could not inspect desktop resource {path}: {error}", path=path, error=error)
        ) from error
    try:
        resolved = path.resolve(strict=True)
        resolved_roots = tuple(root.resolve(strict=True) for root in roots)
    except (OSError, RuntimeError) as error:
        raise core.SpacesError(
            _("Could not resolve desktop resource {path}.", path=path)
        ) from error
    if not any(
        resolved == root or resolved.is_relative_to(root)
        for root in resolved_roots
    ):
        raise core.SpacesError(
            _("Desktop resource escapes its allowed directory: {path}.", path=path)
        )
    metadata = resolved.stat()
    if stat.S_IFMT(metadata.st_mode) not in kinds:
        raise core.SpacesError(
            _("Desktop resource has an unexpected file type: {path}.", path=path)
        )
    if owner is not None and metadata.st_uid != owner:
        raise core.SpacesError(
            _("Desktop resource has the wrong owner: {path}.", path=path)
        )
    if require_access:
        required = (
            0o2
            if stat.S_ISSOCK(metadata.st_mode)
            else 0o5
            if stat.S_ISDIR(metadata.st_mode)
            else 0o4
        )
        if _permissions(metadata, user) & required != required:
            raise core.SpacesError(
                _("Desktop resource is inaccessible to its user: {path}.", path=path)
            )
        for parent in resolved.parents:
            if _permissions(parent.stat(), user) & 0o1 == 0:
                raise core.SpacesError(
                    _(
                        "Desktop resource has an inaccessible parent: {path}.",
                        path=path,
                    )
                )
            if parent in resolved_roots:
                break
    return resolved, metadata


def _plan(
    user: DesktopUser,
    selected: LoginSession,
    source_environment: dict[str, str],
    generated_root: Path,
) -> DesktopPlan:
    host_user = pwd.getpwuid(user.uid)
    runtime = Path(f"/run/user/{user.uid}")
    home = user.host_home
    root = DESKTOP_ROOT / str(user.uid)
    environment = {
        name: value
        for name, value in source_environment.items()
        if name in DESKTOP_ENVIRONMENT and _safe_value(value)
    }
    for forbidden in (
        "DBUS_SESSION_BUS_ADDRESS",
        "FONTCONFIG_FILE",
        "PIPEWIRE_RUNTIME_DIR",
        "XCURSOR_PATH",
        "XDG_DATA_DIRS",
        "XDG_CONFIG_DIRS",
        "XDG_RUNTIME_DIR",
        "XDG_SESSION_ID",
    ):
        environment.pop(forbidden, None)
    binds: list[DesktopBind] = []

    def add(
        source: Path,
        destination: PurePosixPath | str,
        *,
        roots: tuple[Path, ...],
        kinds: tuple[int, ...],
        owner: int | None = None,
        require_access: bool = True,
    ) -> bool:
        checked = _validated_source(
            source,
            roots=roots,
            kinds=kinds,
            user=host_user,
            owner=owner,
            require_access=require_access,
        )
        if checked is None:
            return False
        resolved, metadata = checked
        binds.append(
            DesktopBind(
                destination=str(destination),
                source=resolved,
                device=metadata.st_dev,
                inode=metadata.st_ino,
            )
        )
        return True

    wayland = environment.pop("WAYLAND_DISPLAY", None)
    if wayland is not None:
        source = Path(wayland)
        if not source.is_absolute():
            if not SOCKET_NAME_PATTERN.fullmatch(wayland):
                raise core.SpacesError(_("Invalid Wayland display name."))
            source = runtime / wayland
        destination = root / "wayland" / source.name
        if add(
            source,
            destination,
            roots=(runtime,),
            kinds=(stat.S_IFSOCK,),
            owner=user.uid,
        ):
            environment["WAYLAND_DISPLAY"] = str(destination)

    display = environment.pop("DISPLAY", None)
    if display is not None:
        match = LOCAL_DISPLAY_PATTERN.fullmatch(display)
        if match is None:
            raise core.SpacesError(_("Only local X11 displays may be forwarded."))
        source = Path(f"/tmp/.X11-unix/X{match.group('number')}")
        if add(
            source,
            source,
            roots=(Path("/tmp/.X11-unix"),),
            kinds=(stat.S_IFSOCK,),
        ):
            environment["DISPLAY"] = display

    xauthority_value = source_environment.get("XAUTHORITY")
    if "DISPLAY" in environment:
        xauthority = (
            Path(xauthority_value)
            if xauthority_value
            else home / ".Xauthority"
        )
        if not xauthority.is_absolute():
            raise core.SpacesError(_("XAUTHORITY must be an absolute path."))
        destination = root / "xauthority"
        if add(
            xauthority,
            destination,
            roots=(home, runtime),
            kinds=(stat.S_IFREG,),
            owner=user.uid,
        ):
            environment["XAUTHORITY"] = str(destination)

    pulse_value = source_environment.get("PULSE_SERVER")
    pulse_source = runtime / "pulse" / "native"
    if pulse_value and pulse_value.startswith("unix:"):
        pulse_source = Path(pulse_value.removeprefix("unix:"))
    pulse_destination = root / "pulse" / "native"
    if add(
        pulse_source,
        pulse_destination,
        roots=(runtime,),
        kinds=(stat.S_IFSOCK,),
        owner=user.uid,
    ):
        environment["PULSE_SERVER"] = f"unix:{pulse_destination}"
    else:
        environment.pop("PULSE_SERVER", None)

    pipewire_remote = source_environment.get("PIPEWIRE_REMOTE", "pipewire-0")
    if not SOCKET_NAME_PATTERN.fullmatch(pipewire_remote):
        raise core.SpacesError(_("Invalid PipeWire remote name."))
    pipewire_root = root / "pipewire"
    if add(
        runtime / pipewire_remote,
        pipewire_root / pipewire_remote,
        roots=(runtime,),
        kinds=(stat.S_IFSOCK,),
        owner=user.uid,
    ):
        environment["PIPEWIRE_RUNTIME_DIR"] = str(pipewire_root)
        environment["PIPEWIRE_REMOTE"] = pipewire_remote
        add(
            runtime / f"{pipewire_remote}-manager",
            pipewire_root / f"{pipewire_remote}-manager",
            roots=(runtime,),
            kinds=(stat.S_IFSOCK,),
            owner=user.uid,
        )
    else:
        environment.pop("PIPEWIRE_REMOTE", None)
        environment.pop("PIPEWIRE_RUNTIME_DIR", None)

    config_value = source_environment.get("XDG_CONFIG_HOME")
    config = Path(config_value) if config_value else home / ".config"
    if not config.is_absolute():
        raise core.SpacesError(_("XDG_CONFIG_HOME must be an absolute path."))
    add(
        config / "kdeglobals",
        user.guest_home / ".config" / "kdeglobals",
        roots=(home,),
        kinds=(stat.S_IFREG,),
        owner=user.uid,
    )
    for name in CONFIG_DIRECTORIES:
        add(
            config / name,
            user.guest_home / ".config" / name,
            roots=(home,),
            kinds=(stat.S_IFDIR,),
            owner=user.uid,
        )

    data_home_value = source_environment.get("XDG_DATA_HOME")
    data_home = Path(data_home_value) if data_home_value else home / ".local/share"
    if not data_home.is_absolute():
        raise core.SpacesError(_("XDG_DATA_HOME must be an absolute path."))
    data_roots: list[str] = []
    appearance_sources = (
        ("user", data_home, (home,), user.uid),
        ("local", Path("/usr/local/share"), (Path("/usr/local/share"),), None),
        ("system", Path("/usr/share"), (Path("/usr/share"),), None),
    )
    for label, source_root, roots, owner in appearance_sources:
        destination_root = root / "data" / label
        found = False
        for name in ("icons", "themes", "color-schemes"):
            found = (
                add(
                    source_root / name,
                    destination_root / name,
                    roots=roots,
                    kinds=(stat.S_IFDIR,),
                    owner=owner,
                )
                or found
            )
        if found:
            data_roots.append(str(destination_root))
    for label, source, kind in (
        ("legacy-icons", home / ".icons", "icons"),
        ("legacy-themes", home / ".themes", "themes"),
    ):
        destination_root = root / "data" / label
        if add(
            source,
            destination_root / kind,
            roots=(home,),
            kinds=(stat.S_IFDIR,),
            owner=user.uid,
        ):
            data_roots.append(str(destination_root))

    font_destinations: list[str] = []
    for label, source, roots, owner in (
        ("user", home / ".local/share/fonts", (home,), user.uid),
        ("legacy", home / ".fonts", (home,), user.uid),
        ("local", Path("/usr/local/share/fonts"), (Path("/usr/local/share"),), None),
        ("system", Path("/usr/share/fonts"), (Path("/usr/share"),), None),
    ):
        destination = root / "fonts" / label
        if add(
            source,
            destination,
            roots=roots,
            kinds=(stat.S_IFDIR,),
            owner=owner,
        ):
            font_destinations.append(str(destination))
    if font_destinations:
        generated_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        font_config = generated_root / "fonts.conf"
        directories = "".join(
            f"  <dir>{destination}</dir>\n"
            for destination in font_destinations
        )
        font_config.write_text(
            "<?xml version=\"1.0\"?>\n"
            "<!DOCTYPE fontconfig SYSTEM \"fonts.dtd\">\n"
            "<fontconfig>\n"
            f"{directories}"
            "  <include ignore_missing=\"yes\">/etc/fonts/fonts.conf</include>\n"
            "</fontconfig>\n",
            encoding="utf-8",
        )
        os.chmod(font_config, 0o644)
        if add(
            font_config,
            root / "fonts.conf",
            roots=(generated_root,),
            kinds=(stat.S_IFREG,),
            require_access=False,
        ):
            environment["FONTCONFIG_FILE"] = str(root / "fonts.conf")

    if data_roots:
        environment["XDG_DATA_DIRS"] = ":".join(
            [*data_roots, "/usr/local/share", "/usr/share"]
        )
        environment["XCURSOR_PATH"] = ":".join(
            [
                *(f"{item}/icons" for item in data_roots),
                "/usr/local/share/icons",
                "/usr/share/icons",
            ]
        )
    current_desktop = environment.get("XDG_CURRENT_DESKTOP", "")
    desktops = [
        item
        for item in current_desktop.split(":")
        if item and item.casefold() != "spaces"
    ]
    environment["XDG_CURRENT_DESKTOP"] = ":".join(["Spaces", *desktops])
    environment["XDG_CONFIG_DIRS"] = "/run/spaces-host/config:/etc/xdg"
    environment["XDG_SESSION_TYPE"] = selected.session_type
    environment["XDG_SESSION_CLASS"] = "user"
    return DesktopPlan(
        session_id=selected.session_id,
        binds=tuple(sorted(binds)),
        environment=environment,
        generated_root=generated_root if generated_root.exists() else None,
    )


def _environment_path(space_name: str, uid: int) -> Path:
    core.validate_space_name(space_name)
    if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
        raise core.SpacesError(_("Invalid desktop environment user ID."))
    return (
        core.STATE_ROOT
        / space_name
        / ENVIRONMENT_DIRECTORY
        / f"{uid}.json"
    )


def _environment_directory(space_name: str) -> Path:
    path = _environment_path(space_name, 0).parent
    path.mkdir(mode=0o700, exist_ok=True)
    metadata = path.lstat()
    expected_owner = 0 if os.geteuid() == 0 else os.getuid()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != expected_owner
        or metadata.st_mode & 0o077
    ):
        raise core.SpacesError(
            _("Unsafe desktop environment directory: {path}.", path=path)
        )
    return path


def _validate_desktop_environment(
    environment: object,
) -> dict[str, str]:
    if not isinstance(environment, dict):
        raise core.SpacesError(_("Invalid desktop environment data."))
    if any(
        not isinstance(name, str)
        or name not in DESKTOP_ENVIRONMENT
        or not _safe_value(value)
        for name, value in environment.items()
    ):
        raise core.SpacesError(_("Invalid desktop environment data."))
    return {
        name: value
        for name, value in sorted(environment.items())
        if isinstance(value, str)
    }


def _write_record(
    space_name: str,
    uid: int,
    state: str,
    session_id: str | None,
    environment: dict[str, str],
) -> None:
    if state not in STATUS_STATES:
        raise ValueError(state)
    if session_id is not None and not isinstance(session_id, str):
        raise core.SpacesError(_("Invalid desktop session ID."))
    validated = _validate_desktop_environment(environment)
    path = _environment_path(space_name, uid)
    directory = _environment_directory(space_name)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{uid}.",
        dir=directory,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "environment": validated,
                    "session_id": session_id,
                    "state": state,
                },
                stream,
                separators=(",", ":"),
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o600)
            if os.geteuid() == 0:
                os.fchown(stream.fileno(), 0, 0)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_record(
    space_name: str,
    uid: int,
    *,
    missing_ok: bool = False,
) -> tuple[str, str | None, dict[str, str]]:
    path = _environment_path(space_name, uid)
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except FileNotFoundError as error:
        if missing_ok:
            return "inactive", None, {}
        raise core.SpacesError(
            _("Desktop environment data is unavailable.")
        ) from error
    except OSError as error:
        raise core.SpacesError(
            _("Could not open desktop environment data: {error}", error=error)
        ) from error
    try:
        metadata = os.fstat(descriptor)
        expected_owner = 0 if os.geteuid() == 0 else os.getuid()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != expected_owner
            or metadata.st_mode & 0o077
        ):
            raise core.SpacesError(
                _("Unsafe desktop environment file: {path}.", path=path)
            )
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            value = json.load(stream)
        if not isinstance(value, dict) or set(value) != {
            "environment",
            "session_id",
            "state",
        }:
            raise core.SpacesError(_("Invalid desktop environment data."))
        state = value["state"]
        session_id = value["session_id"]
        if state not in STATUS_STATES or (
            session_id is not None and not isinstance(session_id, str)
        ):
            raise core.SpacesError(_("Invalid desktop environment data."))
        environment = _validate_desktop_environment(value["environment"])
        return state, session_id, environment
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise core.SpacesError(
            _("Could not read desktop environment data: {error}", error=error)
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def set_status(
    space_name: str,
    uid: int,
    state: str,
    *,
    session_id: str | None = None,
) -> None:
    _previous_state, previous_session, environment = _read_record(
        space_name,
        uid,
        missing_ok=True,
    )
    if state == "inactive":
        environment = {}
        previous_session = None
    _write_record(
        space_name,
        uid,
        state,
        session_id if session_id is not None else previous_session,
        environment,
    )


def initialize_status(space_name: str, users: tuple[DesktopUser, ...]) -> None:
    for user in users:
        # Replace any record left by a service crash before this generation
        # becomes visible to ``spaces enter``.
        _write_record(
            space_name,
            user.uid,
            "pending" if user.desktop and user.uid != 0 else "inactive",
            None,
            {},
        )


def _status_record(space_name: str, uid: int) -> tuple[str, str | None]:
    state, session_id, _environment = _read_record(
        space_name,
        uid,
        missing_ok=True,
    )
    return state, session_id


def _read_status(space_name: str, uid: int) -> str:
    return _status_record(space_name, uid)[0]


def desktop_environment(
    space_name: str,
    uid: int,
    *,
    timeout: float = 30,
) -> dict[str, str]:
    """Wait through reconciliation and read its root-only GUI environment."""

    deadline = time.monotonic() + timeout
    while True:
        state, _session_id, environment = _read_record(
            space_name,
            uid,
            missing_ok=True,
        )
        if state == "inactive":
            return {}
        if state == "active":
            return environment
        if time.monotonic() >= deadline:
            raise core.SpacesError(
                _("Timed out waiting for desktop forwarding to settle.")
            )
        time.sleep(0.05)


def polkit_agent(rootfs: Path) -> str | None:
    """Find a recognized executable guest polkit agent."""

    candidates = [rootfs / item.removeprefix("/") for item in POLKIT_AGENTS]
    candidates.extend(sorted(rootfs.glob(POLKIT_AGENT_GLOB)))
    resolved_root = rootfs.resolve(strict=True)
    expected_owner = 0 if os.geteuid() == 0 else os.getuid()
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            metadata = resolved.stat()
        except (OSError, RuntimeError):
            continue
        if (
            resolved.is_relative_to(resolved_root)
            and stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == expected_owner
            and metadata.st_mode & 0o111
        ):
            return "/" + str(resolved.relative_to(resolved_root))
    return None


class DesktopController:
    """Reconcile login-scoped desktop resources for one running space."""

    def __init__(
        self,
        space_name: str,
        users: tuple[DesktopUser, ...],
        *,
        portals_enabled: bool = False,
    ) -> None:
        self.space_name = space_name
        self.users = {user.uid: user for user in users}
        self.portals_enabled = portals_enabled
        self.active: dict[int, _ActiveDesktop] = {}
        self.destination_users: dict[str, set[int]] = {}
        self.destination_sources: dict[str, tuple[int, int]] = {}

    def reconcile(
        self,
        user: DesktopUser,
        sessions: tuple[LoginSession, ...],
    ) -> None:
        if not user.desktop or user.uid == 0:
            if user.uid in self.active:
                self.deactivate(user)
            return
        environment = host_manager_environment(user)
        selected = select_graphical_session(sessions, environment)
        current = self.active.get(user.uid)
        if selected is None:
            self.deactivate(user)
            return
        if current is not None and current.plan.session_id == selected.session_id:
            self._repair_portal(user, current)
            return

        set_status(self.space_name, user.uid, "pending")
        generation = hashlib.sha256(
            selected.session_id.encode("utf-8")
        ).hexdigest()[:16]
        generated = (
            RUNTIME_ROOT
            / self.space_name
            / "desktop"
            / str(user.uid)
            / "generated"
            / generation
        )
        previous = current
        try:
            plan = _plan(user, selected, environment, generated)
            plan.environment["SPACES_NAME"] = self.space_name
        except Exception as error:
            self._remove_generated(
                DesktopPlan(selected.session_id, (), {}, generated)
            )
            set_status(
                self.space_name,
                user.uid,
                "active" if previous is not None else "inactive",
                session_id=(
                    previous.plan.session_id if previous is not None else None
                ),
            )
            raise DesktopSetupError(str(error)) from error
        if previous is not None:
            self._deactivate(user, previous, remove_generated=False)
        try:
            activated = self._activate(user, plan)
        except DesktopRevocationError:
            set_status(self.space_name, user.uid, "inactive")
            raise
        except Exception as error:
            self._remove_generated(plan)
            if previous is not None:
                try:
                    self.active[user.uid] = self._activate(user, previous.plan)
                except Exception:
                    set_status(self.space_name, user.uid, "inactive")
                    raise
            else:
                set_status(self.space_name, user.uid, "inactive")
            raise DesktopSetupError(str(error)) from error
        self.active[user.uid] = activated
        if previous is not None:
            self._remove_generated(previous.plan)

    def deactivate(self, user: DesktopUser) -> None:
        current = self.active.get(user.uid)
        if current is None:
            set_status(self.space_name, user.uid, "inactive")
            return
        set_status(self.space_name, user.uid, "pending")
        self._deactivate(user, current)
        set_status(self.space_name, user.uid, "inactive")

    def close(self) -> None:
        for user in self.users.values():
            if user.uid in self.active:
                self.deactivate(user)
            elif user.desktop and user.uid != 0:
                set_status(self.space_name, user.uid, "inactive")

    def reconcile_portals(self) -> None:
        """Retry portal-only failures without rebuilding desktop forwarding."""

        if not self.portals_enabled:
            return
        for uid, current in tuple(self.active.items()):
            user = self.users.get(uid)
            if user is not None:
                self._repair_portal(user, current)

    def abandon(self) -> None:
        """Forget state after the machine has already removed its namespaces."""

        for current in self.active.values():
            if current.portal is not None:
                current.portal.close()
                current.portal.socket_path.unlink(missing_ok=True)
        self.active.clear()
        self.destination_users.clear()
        self.destination_sources.clear()
        for user in self.users.values():
            if user.desktop and user.uid != 0:
                set_status(self.space_name, user.uid, "inactive")

    def _activate(
        self, user: DesktopUser, plan: DesktopPlan
    ) -> _ActiveDesktop:
        self._prepare_guest_root(user)
        mounted: list[DesktopBind] = []
        portal: PortalProxy | None = None
        portal_binding: DesktopBind | None = None
        try:
            for binding in plan.binds:
                self._mount(user.uid, binding)
                mounted.append(binding)
            if self.portals_enabled:
                try:
                    portal, portal_binding = start_portal_proxy(
                        self.space_name,
                        user,
                        plan.session_id,
                    )
                    self._mount(user.uid, portal_binding)
                    mounted.append(portal_binding)
                except Exception as error:
                    if portal is not None:
                        portal.close()
                        portal.socket_path.unlink(missing_ok=True)
                    portal = None
                    portal_binding = None
                    logger.warning(
                        _(
                            "Could not enable host portals for {user}; "
                            "desktop forwarding will continue: {error}",
                            user=user.name,
                            error=error,
                        )
                    )
            _write_record(
                self.space_name,
                user.uid,
                "active",
                plan.session_id,
                plan.environment,
            )
        except Exception:
            if portal is not None:
                portal.close()
                portal.socket_path.unlink(missing_ok=True)
            cleanup_errors: list[Exception] = []
            for binding in reversed(mounted):
                try:
                    self._unmount(user.uid, binding)
                except Exception as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                cleanup_error = cleanup_errors[0]
                raise DesktopRevocationError(
                    _(
                        "Could not revoke a partially configured desktop "
                        "generation: {error}",
                        error=cleanup_error,
                    )
                ) from cleanup_error
            raise
        return _ActiveDesktop(
            plan=plan,
            portal=portal,
            portal_binding=portal_binding,
        )

    def _repair_portal(
        self,
        user: DesktopUser,
        current: _ActiveDesktop,
    ) -> None:
        if not self.portals_enabled:
            return
        if (
            current.portal is not None
            and current.portal_binding is not None
            and current.portal.process.poll() is None
        ):
            try:
                metadata = current.portal.socket_path.lstat()
            except OSError:
                metadata = None
            if (
                metadata is not None
                and stat.S_ISSOCK(metadata.st_mode)
                and metadata.st_uid == user.uid
                and (metadata.st_dev, metadata.st_ino)
                == (
                    current.portal_binding.device,
                    current.portal_binding.inode,
                )
            ):
                return

        if current.portal_binding is not None:
            try:
                self._unmount(user.uid, current.portal_binding)
                current.portal_binding = None
            except Exception as error:
                logger.warning(
                    _(
                        "Could not revoke the failed host portal bridge for "
                        "{user}; retrying later: {error}",
                        user=user.name,
                        error=error,
                    )
                )
                return
        if current.portal is not None:
            current.portal.close()
            current.portal.socket_path.unlink(missing_ok=True)
        current.portal = None
        current.portal_binding = None

        portal: PortalProxy | None = None
        try:
            portal, binding = start_portal_proxy(
                self.space_name,
                user,
                current.plan.session_id,
            )
            self._mount(user.uid, binding)
        except Exception as error:
            if portal is not None:
                portal.close()
                portal.socket_path.unlink(missing_ok=True)
            logger.warning(
                _(
                    "Could not enable host portals for {user}; retrying "
                    "without affecting desktop forwarding: {error}",
                    user=user.name,
                    error=error,
                )
            )
            return
        current.portal = portal
        current.portal_binding = binding

    def _deactivate(
        self,
        user: DesktopUser,
        current: _ActiveDesktop,
        *,
        remove_generated: bool = True,
    ) -> None:
        try:
            if current.portal_binding is not None:
                self._unmount(user.uid, current.portal_binding)
                current.portal_binding = None
            for binding in reversed(current.plan.binds):
                self._unmount(user.uid, binding)
        except Exception as error:
            raise DesktopRevocationError(
                _(
                    "Could not safely revoke desktop forwarding for {user}: "
                    "{error}",
                    user=user.name,
                    error=error,
                )
            ) from error
        finally:
            if current.portal is not None:
                current.portal.close()
                current.portal.socket_path.unlink(missing_ok=True)
                current.portal = None
        self.active.pop(user.uid, None)
        if remove_generated:
            self._remove_generated(current.plan)

    @staticmethod
    def _remove_generated(plan: DesktopPlan) -> None:
        root = plan.generated_root
        if root is None:
            return
        try:
            (root / "fonts.conf").unlink(missing_ok=True)
            root.rmdir()
            root.parent.rmdir()
        except OSError:
            pass

    def _prepare_guest_root(self, user: DesktopUser) -> None:
        self._machine_root(
            [
                "/usr/bin/install",
                "-d",
                "-m",
                "0700",
                "-o",
                str(user.uid),
                "-g",
                str(user.gid),
                str(DESKTOP_ROOT / str(user.uid)),
            ]
        )

    def _mount(self, uid: int, binding: DesktopBind) -> None:
        identity = (binding.device, binding.inode)
        users = self.destination_users.get(binding.destination)
        if users is not None:
            if self.destination_sources[binding.destination] != identity:
                raise core.SpacesError(
                    _(
                        "Desktop destination {path} is already bound to a "
                        "different source.",
                        path=binding.destination,
                    )
                )
            users.add(uid)
            return
        descriptor = os.open(
            binding.source, os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW
        )
        try:
            metadata = os.fstat(descriptor)
            if (metadata.st_dev, metadata.st_ino) != identity:
                raise core.SpacesError(
                    _("Desktop resource changed while it was being mounted.")
                )
            subprocess.run(
                [
                    MACHINECTL,
                    "--quiet",
                    "--no-ask-password",
                    "--mkdir",
                    "--read-only",
                    "bind",
                    self.space_name,
                    f"/proc/{os.getpid()}/fd/{descriptor}",
                    binding.destination,
                ],
                check=True,
            )
        finally:
            os.close(descriptor)
        self.destination_users[binding.destination] = {uid}
        self.destination_sources[binding.destination] = identity

    def _unmount(self, uid: int, binding: DesktopBind) -> None:
        users = self.destination_users.get(binding.destination)
        if users is None or uid not in users:
            return
        if len(users) > 1:
            users.remove(uid)
            return
        # Lazy detachment is intentional: GUI clients frequently keep socket
        # descriptors open while the login disappears. Existing users may
        # finish, but no process can open the revoked host path afterward.
        self._machine_root(
            ["/usr/bin/umount", "--lazy", "--", binding.destination]
        )
        self.destination_users.pop(binding.destination, None)
        self.destination_sources.pop(binding.destination, None)

    def _machine_root(self, command: list[str]) -> None:
        subprocess.run(
            [
                MACHINECTL,
                "--quiet",
                "--uid=root",
                "--",
                "shell",
                self.space_name,
                *command,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
