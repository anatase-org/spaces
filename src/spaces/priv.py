"""Root-only Spaces management operations.

This module intentionally imports only Python standard-library modules and
``spaces.core``, which is also standard-library-only.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pwd
import select
import secrets
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import _
from . import core
from . import session


SYSTEMCTL = "/usr/bin/systemctl"
MACHINECTL = "/usr/bin/machinectl"
# These are search paths, not scalar session coordinates.  Keep Spaces'
# entries first, then retain guest distribution and administrator additions.
MERGED_DBUS_PATH_ENVIRONMENT = frozenset(
    {"XCURSOR_PATH", "XDG_CONFIG_DIRS", "XDG_DATA_DIRS"}
)
PROC_ROOT = Path("/proc")


def get_driver(distribution_id: str) -> Any:
    """Load distribution management code only when it is needed."""

    from .distro import get_driver as load_driver

    return load_driver(distribution_id)


def launch(space_name: str) -> int:
    """Load launch orchestration only for the launch operation."""

    from .launch import launch as launch_space

    return launch_space(space_name)


def _root_owned_directory(path: Path) -> None:
    path.mkdir(mode=0o755, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise core.SpacesError(
            _("Expected a directory at {path}.", path=path)
        )
    os.chown(path, 0, 0)
    os.chmod(path, 0o755)


@contextmanager
def _space_lock(space: Path, *, create: bool = False) -> Iterator[None]:
    """Serialize mutations to one space without blocking other spaces."""

    _root_owned_directory(core.STATE_ROOT)
    if space.parent != core.STATE_ROOT:
        raise core.SpacesError(
            _("Space lock path is outside the Spaces state directory.")
        )
    if space.is_symlink() or (space.exists() and not space.is_dir()):
        raise core.SpacesError(
            _("Unsafe space path: {space}.", space=space)
        )
    if not space.exists() and not create:
        raise core.SpacesError(
            _("Space {name!r} does not exist.", name=space.name)
        )
    if create:
        _root_owned_directory(space)
    try:
        descriptor = os.open(
            space,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as error:
        raise core.SpacesError(
            _("Could not lock space {name!r}.", name=space.name)
        ) from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        opened = os.fstat(descriptor)
        try:
            current = space.stat(follow_symlinks=False)
        except OSError as error:
            raise core.SpacesError(
                _(
                    "Space {name!r} changed while waiting for its lock.",
                    name=space.name,
                )
            ) from error
        if (
            not stat.S_ISDIR(current.st_mode)
            or opened.st_dev != current.st_dev
            or opened.st_ino != current.st_ino
        ):
            raise core.SpacesError(
                _(
                    "Space {name!r} changed while waiting for its lock.",
                    name=space.name,
                )
            )
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _write_info(space: Path, info: dict[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".info.", dir=space)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(info, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), 0o644)
            os.fchown(output.fileno(), 0, 0)
        os.replace(temporary, space / "info.json")
    finally:
        temporary.unlink(missing_ok=True)


def _remove_rootfs(path: Path) -> None:
    _assert_no_mounts(path)
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _preserve_failed_rootfs(rootfs: Path, failed_rootfs: Path) -> None:
    _assert_no_mounts(rootfs)
    if rootfs.is_symlink() or not rootfs.is_dir():
        raise core.SpacesError(
            _("Cannot preserve unsafe failed rootfs: {rootfs}.", rootfs=rootfs)
        )
    if failed_rootfs.is_symlink() or failed_rootfs.exists():
        raise core.SpacesError(
            _(
                "Failed rootfs destination already exists: {rootfs}.",
                rootfs=failed_rootfs,
            )
        )
    os.replace(rootfs, failed_rootfs)


def _unescape_mount_path(value: str) -> str:
    for escaped, plain in (
        ("\\040", " "),
        ("\\011", "\t"),
        ("\\012", "\n"),
        ("\\134", "\\"),
    ):
        value = value.replace(escaped, plain)
    return value


def _assert_no_mounts(path: Path) -> None:
    if not path.exists() or path.is_symlink():
        return
    target = path.resolve()
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError as error:
        raise core.SpacesError(
            _("Could not inspect active mounts: {error}", error=error)
        ) from error
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        mountpoint = Path(_unescape_mount_path(fields[4]))
        if mountpoint == target or target in mountpoint.parents:
            raise core.SpacesError(
                _(
                    "Cannot remove {target} while {mountpoint} is mounted "
                    "at or beneath it.",
                    target=target,
                    mountpoint=mountpoint,
                )
            )


def _caller_uid() -> int:
    for variable in ("PKEXEC_UID", "SUDO_UID"):
        if variable in os.environ:
            try:
                uid = int(os.environ[variable])
            except ValueError as error:
                raise core.SpacesError(
                    _("{variable} must be a numeric UID.", variable=variable)
                ) from error
            if uid < 0:
                raise core.SpacesError(
                    _("{variable} must not be negative.", variable=variable)
                )
            return uid
    return 0


def _assert_initiating_user(info: dict[str, Any]) -> None:
    users = info["permissions"]["users"]
    if set(users) != {str(_caller_uid())}:
        raise core.SpacesError(
            _(
                "Creation payload must contain only the initiating user's "
                "permissions."
            )
        )


def _requested_enable(request: dict[str, Any]) -> bool:
    enable = request.pop("enable", False)
    if not isinstance(enable, bool):
        raise core.SpacesError(_("Enable option must be a boolean."))
    return enable


def _enable_user_service(name: str, uid: int, gid: int) -> None:
    """Enable a space through the target account's user manager."""

    try:
        account = pwd.getpwuid(uid)
    except KeyError as error:
        raise core.SpacesError(
            _("No passwd entry exists for UID {uid}.", uid=uid)
        ) from error
    home = Path(account.pw_dir)
    if not home.is_absolute() or account.pw_gid != gid:
        raise core.SpacesError(
            _("Host account details changed for UID {uid}.", uid=uid)
        )
    # Reenable also removes symlinks left under the former default.target.
    try:
        subprocess.run(
            [
                SYSTEMCTL,
                f"--machine={account.pw_name}@.host",
                "--user",
                "--no-reload",
                "reenable",
                f"spaces@{name}.service",
            ],
            check=True,
        )
    except subprocess.CalledProcessError as error:
        raise core.SpacesError(
            _(
                "Space {name!r} was saved, but its user service could not "
                "be enabled for UID {uid}.",
                name=name,
                uid=uid,
            )
        ) from error


def _space_directory(name: str) -> Path:
    core.validate_space_name(name)
    space = core.STATE_ROOT / name
    if space.is_symlink() or not space.is_dir():
        raise core.SpacesError(
            _("Space {name!r} does not exist.", name=name)
        )
    return space


def _space_info(name: str) -> dict[str, Any]:
    space = _space_directory(name)
    info_path = space / "info.json"
    if info_path.is_symlink() or not info_path.is_file():
        raise core.SpacesError(
            _("Unsafe space information path: {path}.", path=info_path)
        )
    info = core.load_info(info_path)
    if info is None:
        raise core.SpacesError(
            _("Space {name!r} has an invalid info.json.", name=name)
        )
    if info["name"] != name:
        raise core.SpacesError(_("Space name does not match its info.json."))
    return info


def _ensure_space_started(name: str, *, entering: bool = False) -> int:
    available = subprocess.run(
        [MACHINECTL, "--quiet", "show", name],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0
    if available:
        return 0

    visible = entering and bool(
        getattr(sys.stdout, "isatty", lambda: False)()
    )
    if visible:
        message = _(
            "Starting space {space} and entering it...",
            space=name,
        )
        sys.stdout.write(message + "\n")
        sys.stdout.flush()

    try:
        return subprocess.run(
            [SYSTEMCTL, "start", f"spaces@{name}.service"],
            check=False,
        ).returncode
    finally:
        if visible:
            sys.stdout.write("\033[F\033[2K")
            sys.stdout.flush()


def start(name: str) -> int:
    """Start a space for a configured initiating user."""

    info = _space_info(name)
    caller_uid = _caller_uid()
    if str(caller_uid) not in info["permissions"]["users"]:
        raise core.SpacesError(
            _(
                "User ID {uid} is not configured for space {space!r}.",
                uid=caller_uid,
                space=name,
            )
        )
    return _ensure_space_started(name)


def _machine_shell(
    user_name: str,
    space_name: str,
    command: list[str],
    *,
    environment: dict[str, str] | None = None,
    launch_environment: dict[str, str] | None = None,
    steam_app_id: int | None = None,
    caller_pidfd: int | None = None,
    launcher: bool = False,
    agent: str | None = None,
) -> int:
    actual_command = command
    launch_id = secrets.token_hex(16) if caller_pidfd is not None else None
    if launcher:
        actual_command = ["/run/spaces-host/bin/spaces"]
        if launch_id is not None:
            actual_command.extend(["--launch-id", launch_id])
        # Only the stable desktop environment is published to D-Bus. The
        # one-shot launch environment remains local to this command.
        for name in sorted((environment or {})):
            option = (
                "--dbus-env-path"
                if name in MERGED_DBUS_PATH_ENVIRONMENT
                else "--dbus-env"
            )
            actual_command.extend([option, name])
        if agent is not None:
            actual_command.extend(["--agent", agent])
        if steam_app_id is not None:
            actual_command.extend(
                ["SteamLaunch", f"AppId={steam_app_id}"]
            )
        actual_command.append("--")
        actual_command.extend(command)
    command_environment = {
        **(environment or {}),
        **(launch_environment or {}),
    }
    machine_command = [
        MACHINECTL,
        "--quiet",
        f"--uid={user_name}",
        *(
            f"--setenv={name}={value}"
            for name, value in sorted(command_environment.items())
        ),
        "--",
        "shell",
        space_name,
        *actual_command,
    ]
    if caller_pidfd is None:
        return subprocess.run(machine_command, check=False).returncode

    poller = select.poll()
    poller.register(caller_pidfd, select.POLLIN)
    if poller.poll(0):
        return 128 + signal.SIGTERM

    process = subprocess.Popen(machine_command)
    process_pidfd = os.pidfd_open(process.pid)
    try:
        poller.register(process_pidfd, select.POLLIN)
        while True:
            ready = {descriptor for descriptor, _events in poller.poll()}
            returncode = process.poll()
            if returncode is not None:
                return returncode
            if caller_pidfd in ready:
                if launch_id is not None:
                    user = pwd.getpwnam(user_name)
                    _terminate_launch(launch_id, user.pw_uid)
                if process.poll() is None:
                    process.terminate()
                process.wait()
                return 128 + signal.SIGTERM
    finally:
        os.close(process_pidfd)


def enter(
    target: str,
    command: list[str],
    *,
    launch_environment: dict[str, str] | None = None,
    steam_app_id: int | str | None = None,
    caller_pidfd: int | None = None,
) -> int:
    launch_environment = _validate_launch_environment(launch_environment)
    validated_steam_app_id = _validate_steam_app_id(steam_app_id)
    launch_options: dict[str, Any] = {}
    if launch_environment:
        launch_options["launch_environment"] = launch_environment
    if validated_steam_app_id is not None:
        launch_options["steam_app_id"] = validated_steam_app_id
    if caller_pidfd is not None:
        launch_options["caller_pidfd"] = caller_pidfd
    user_name, separator, space_name = target.rpartition("@")
    if not separator or not user_name or not space_name:
        raise core.SpacesError(
            _("Enter target must have the form USER@SPACE.")
        )
    core.validate_space_name(space_name)

    try:
        user = pwd.getpwnam(user_name)
    except KeyError as error:
        raise core.SpacesError(
            _("Host user {user!r} does not exist.", user=user_name)
        ) from error
    caller_uid = _caller_uid()
    if user.pw_uid != caller_uid:
        raise core.SpacesError(
            _(
                "Enter target user must match the initiating user."
            )
        )
    info = _space_info(space_name)
    record = info["permissions"]["users"].get(str(caller_uid))
    if record is None:
        raise core.SpacesError(
            _(
                "User {user!r} is not configured for space {space!r}.",
                user=user_name,
                space=space_name,
            )
        )

    user_permissions = core.effective_user_permissions(record)
    returncode = _ensure_space_started(space_name, entering=True)
    if returncode != 0:
        return returncode
    if caller_uid == 0:
        return _machine_shell(
            user.pw_name,
            space_name,
            command,
            **launch_options,
        )
    desktop = user_permissions.get("desktop", True) and caller_uid != 0
    credential_agents = (
        user_permissions.get("credential_agents", True)
        and caller_uid != 0
    )
    environment = (
        session.desktop_environment(
            space_name,
            user.pw_uid,
        )
        if desktop or credential_agents
        else {}
    )
    if not environment:
        return _machine_shell(
            user.pw_name,
            space_name,
            command,
            **launch_options,
        )
    graphical_environment = desktop and any(
        name != "SSH_AUTH_SOCK" for name in environment
    )
    if not graphical_environment:
        return _machine_shell(
            user.pw_name,
            space_name,
            command,
            environment=environment,
            **launch_options,
        )
    agent: str | None = None
    agent = session.polkit_agent(
        _space_directory(space_name) / "rootfs"
    )
    if agent is None:
        print(
            _(
                "spaces: warning: no supported graphical polkit "
                "authentication agent is installed in the space."
            ),
            file=sys.stderr,
        )
    return _machine_shell(
        user.pw_name,
        space_name,
        command,
        environment=environment,
        launcher=True,
        agent=agent,
        **launch_options,
    )


def enter_as_user(
    user_name: str,
    space_name: str,
    command: list[str],
) -> int:
    if (
        not user_name
        or user_name in {".", ".."}
        or any(character in user_name for character in "/:\0\n\r")
    ):
        raise core.SpacesError(
            _("Invalid target user name: {user!r}.", user=user_name)
        )
    info = _space_info(space_name)
    if user_name != "root":
        try:
            target_user = pwd.getpwnam(user_name)
        except KeyError as error:
            raise core.SpacesError(
                _("Host user {user!r} does not exist.", user=user_name)
            ) from error
        if str(target_user.pw_uid) not in info["permissions"]["users"]:
            raise core.SpacesError(
                _(
                    "User {user!r} is not configured for space {space!r}.",
                    user=user_name,
                    space=space_name,
                )
            )
    returncode = _ensure_space_started(space_name, entering=True)
    if returncode != 0:
        return returncode
    return _machine_shell(user_name, space_name, command)


def _validate_launch_environment(
    environment: object | None,
) -> dict[str, str]:
    if environment is None:
        return {}
    if not isinstance(environment, dict) or any(
        name not in core.GRAPHICAL_LAUNCH_ENVIRONMENT
        or not isinstance(value, str)
        for name, value in environment.items()
    ):
        raise core.SpacesError(_("Invalid graphical launch environment."))
    return dict(environment)


def _validate_steam_app_id(value: object | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise core.SpacesError(_("Invalid Steam application ID."))
    text = str(value)
    if not text.isascii() or not text.isdecimal() or len(text) > 10:
        raise core.SpacesError(_("Invalid Steam application ID."))
    app_id = int(text)
    if app_id == 0 or app_id > 0xFFFFFFFF:
        raise core.SpacesError(_("Invalid Steam application ID."))
    return app_id


def _open_caller_pidfd(value: str) -> int:
    if not value.isascii() or not value.isdecimal():
        raise core.SpacesError(_("Invalid caller process ID."))
    caller_pid = int(value)
    if caller_pid <= 1 or caller_pid != os.getppid():
        raise core.SpacesError(_("The initiating process is unavailable."))
    return os.pidfd_open(caller_pid)


def _terminate_launch(launch_id: str, uid: int) -> None:
    marker = f"--launch-id\0{launch_id}\0".encode()
    for entry in PROC_ROOT.iterdir():
        if not entry.name.isdecimal():
            continue
        descriptor = None
        try:
            descriptor = os.pidfd_open(int(entry.name))
            command_line = (entry / "cmdline").read_bytes()
            status = (entry / "status").read_text(encoding="utf-8")
            process_uid = next(
                int(line.split()[1])
                for line in status.splitlines()
                if line.startswith("Uid:")
            )
            if (
                process_uid == uid
                and command_line.startswith(
                    b"/run/spaces-host/bin/spaces\0"
                )
                and marker in command_line
            ):
                signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)


def create(request: dict[str, Any]) -> None:
    from . import host_config
    from . import shortcuts
    from .distro import DistributionError

    request = dict(request)
    enable = _requested_enable(request)
    info, purge = core.validate_create_request(request)
    _assert_initiating_user(info)
    name = info["name"]
    distribution = info["distribution"]
    space = core.STATE_ROOT / name

    with _space_lock(space, create=True):
        subprocess.run(
            ["/usr/bin/systemctl", "stop", f"spaces@{name}.service"],
            check=True,
        )
        shortcuts.remove(name)
        home = space / "home"
        if purge:
            _remove_rootfs(home)
        elif home.is_symlink() or (home.exists() and not home.is_dir()):
            raise core.SpacesError(
                _("Unsafe home path: {home}.", home=home)
            )
        _root_owned_directory(home)

        rootfs = space / "rootfs"
        failed_rootfs = space / "rootfs.fail"
        _assert_no_mounts(rootfs)
        _assert_no_mounts(failed_rootfs)
        _remove_rootfs(rootfs)
        _remove_rootfs(failed_rootfs)
        _root_owned_directory(rootfs)
        _write_info(space, info)

        driver = get_driver(distribution["id"])
        if driver is None:
            raise core.SpacesError(
                _(
                    "Distribution {distro_id!r} is not implemented.",
                    distro_id=distribution["id"],
                )
            )
        try:
            configuration = host_config.load()
            additional_packages = configuration.packages_for(
                distribution["id"]
            )
            if additional_packages:
                driver.bootstrap(
                    distribution,
                    rootfs,
                    additional_packages=additional_packages,
                )
            else:
                driver.bootstrap(distribution, rootfs)
        except (Exception, KeyboardInterrupt) as error:
            _preserve_failed_rootfs(rootfs, failed_rootfs)
            if isinstance(error, DistributionError):
                raise core.SpacesError(str(error)) from error
            raise
        if enable:
            uid_key, record = next(iter(info["permissions"]["users"].items()))
            _enable_user_service(name, int(uid_key), record["gid"])


def configure(patch: dict[str, Any]) -> None:
    from . import shortcuts

    patch = dict(patch)
    enable = _requested_enable(patch)
    core.validate_configure_patch(patch)
    update = patch["permissions"]["user"]

    space = core.STATE_ROOT / patch["name"]
    with _space_lock(space):
        info_path = space / "info.json"
        if info_path.is_symlink() or not info_path.is_file():
            raise core.SpacesError(
                _(
                    "Unsafe space information path: {path}.",
                    path=info_path,
                )
            )
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise core.SpacesError(
                _("Could not read {path}: {error}", path=info_path, error=error)
            ) from error
        core.validate_info(info)
        if info["name"] != patch["name"]:
            raise core.SpacesError(
                _("Space name does not match its info.json.")
            )

        permissions = info["permissions"]
        if "system" in patch["permissions"]:
            permissions["system"].update(patch["permissions"]["system"])
        uid_key = str(update["uid"])
        existing_user = permissions["users"].get(uid_key, {})
        existing_user_permissions = existing_user.get("permissions", {})
        merged_user_permissions = dict(existing_user_permissions)
        merged_user_permissions.update(update["permissions"])
        permissions["users"][uid_key] = {
            "gid": update["gid"],
            "permissions": merged_user_permissions,
        }
        core.validate_info(info)
        _write_info(space, info)
        subprocess.run(
            [
                SYSTEMCTL,
                "try-restart",
                f"spaces@{patch['name']}.service",
            ],
            check=True,
        )
        system_permissions = core.effective_system_permissions(
            permissions["system"]
        )
        if system_permissions.get("shortcuts", True):
            shortcuts.reconcile(
                patch["name"],
                space / "rootfs",
                info["distribution"]["id"],
            )
        else:
            shortcuts.remove(patch["name"])
        if enable:
            _enable_user_service(
                patch["name"],
                update["uid"],
                update["gid"],
            )


def delete(request: dict[str, Any]) -> None:
    from . import shortcuts

    core.validate_delete_request(request)
    name = request["name"]
    purge = request.get("purge", False)
    space = core.STATE_ROOT / name

    with _space_lock(space):
        subprocess.run(
            [SYSTEMCTL, "stop", f"spaces@{name}.service"],
            check=True,
        )
        _assert_no_mounts(space)
        shortcuts.remove(name)
        if purge:
            shutil.rmtree(space)
            return

        home = space / "home"
        if home.is_symlink() or (home.exists() and not home.is_dir()):
            raise core.SpacesError(_("Unsafe home path: {home}.", home=home))
        for entry in space.iterdir():
            if entry == home:
                continue
            _remove_rootfs(entry)
        if not home.exists():
            space.rmdir()


def copy(request: dict[str, Any]) -> int:
    core.validate_cp_request(request)
    completed = subprocess.run(
        ["/usr/bin/cp", *request["arguments"]],
        check=False,
    )
    return completed.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spaces.priv")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("create", "configure", "delete", "cp"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("payload")
    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("space")
    launch_parser = subparsers.add_parser("launch")
    launch_parser.add_argument("space")
    enter_parser = subparsers.add_parser("enter")
    enter_parser.add_argument("--caller-pid")
    enter_parser.add_argument("--launch-environment")
    enter_parser.add_argument("--steam-app-id")
    enter_parser.add_argument("target")
    enter_parser.add_argument(
        "command_arguments",
        nargs=argparse.REMAINDER,
    )
    enter_as_user_parser = subparsers.add_parser("enter-as-user")
    enter_as_user_parser.add_argument("user")
    enter_as_user_parser.add_argument("space")
    enter_as_user_parser.add_argument(
        "command_arguments",
        nargs=argparse.REMAINDER,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        if os.geteuid() != 0:
            print(_("spaces.priv must run as root."), file=sys.stderr)
            return 1
        arguments = build_parser().parse_args(argv)
        if arguments.command == "start":
            return start(arguments.space)
        if arguments.command == "launch":
            return launch(arguments.space)
        if arguments.command == "enter":
            launch_options = {}
            if arguments.launch_environment is not None:
                launch_options["launch_environment"] = json.loads(
                    arguments.launch_environment
                )
            if arguments.steam_app_id is not None:
                launch_options["steam_app_id"] = arguments.steam_app_id
            caller_pidfd = None
            if arguments.caller_pid is not None:
                caller_pidfd = _open_caller_pidfd(arguments.caller_pid)
                launch_options["caller_pidfd"] = caller_pidfd
            try:
                return enter(
                    arguments.target,
                    arguments.command_arguments,
                    **launch_options,
                )
            finally:
                if caller_pidfd is not None:
                    os.close(caller_pidfd)
        if arguments.command == "enter-as-user":
            return enter_as_user(
                arguments.user,
                arguments.space,
                arguments.command_arguments,
            )

        payload = json.loads(arguments.payload)
        if arguments.command == "create":
            create(payload)
        elif arguments.command == "configure":
            configure(payload)
        elif arguments.command == "delete":
            delete(payload)
        elif arguments.command == "cp":
            return copy(payload)
        else:
            raise core.SpacesError(
                _("Unknown privileged command: {command!r}.", command=arguments.command)
            )
    except KeyboardInterrupt:
        print(_("Exiting due to Ctrl+C"), file=sys.stderr)
        return 130
    except json.JSONDecodeError as error:
        print(_("Invalid JSON payload: {error}", error=error), file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        if arguments.command == "configure":
            message = _(
                "Configuration saved but restarting the space failed."
            )
        else:
            message = _(
                "Creation failed; any partial bootstrap was moved to "
                "rootfs.fail and info.json was preserved."
            )
        print(message, file=sys.stderr)
        return error.returncode or 1
    except (core.SpacesError, OSError) as error:
        print(_("spaces.priv: {error}", error=error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
