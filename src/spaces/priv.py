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
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

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


def _ensure_space_started(name: str) -> int:
    available = subprocess.run(
        [MACHINECTL, "--quiet", "show", name],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0
    if available:
        return 0
    return subprocess.run(
        [SYSTEMCTL, "start", f"spaces@{name}.service"],
        check=False,
    ).returncode


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
    launcher: bool = False,
    agent: str | None = None,
) -> int:
    actual_command = command
    if launcher:
        actual_command = ["/run/spaces-host/bin/spaces-session-launcher"]
        for name in sorted((environment or {})):
            option = (
                "--dbus-env-path"
                if name in MERGED_DBUS_PATH_ENVIRONMENT
                else "--dbus-env"
            )
            actual_command.extend([option, name])
        if agent is not None:
            actual_command.extend(["--agent", agent])
        actual_command.append("--")
        actual_command.extend(command)
    completed = subprocess.run(
        [
            MACHINECTL,
            "--quiet",
            f"--uid={user_name}",
            *(
                f"--setenv={name}={value}"
                for name, value in sorted((environment or {}).items())
            ),
            "--",
            "shell",
            space_name,
            *actual_command,
        ],
        check=False,
    )
    return completed.returncode


def enter(
    target: str,
    command: list[str],
) -> int:
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
    returncode = _ensure_space_started(space_name)
    if returncode != 0:
        return returncode
    if caller_uid == 0:
        return _machine_shell(user.pw_name, space_name, command)
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
        return _machine_shell(user.pw_name, space_name, command)
    graphical_environment = desktop and any(
        name != "SSH_AUTH_SOCK" for name in environment
    )
    if not graphical_environment:
        return _machine_shell(
            user.pw_name,
            space_name,
            command,
            environment=environment,
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
        if str(_caller_uid()) not in info["permissions"]["users"]:
            raise core.SpacesError(
                _("Initiating user is not configured for this space.")
            )
    returncode = _ensure_space_started(space_name)
    if returncode != 0:
        return returncode
    return _machine_shell(user_name, space_name, command)


def create(info: dict[str, Any]) -> None:
    from . import host_config
    from . import shortcuts
    from .distro import DistributionError

    core.validate_creation_info(info)
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
        if home.is_symlink() or (home.exists() and not home.is_dir()):
            raise core.SpacesError(_("Unsafe home path: {home}.", home=home))
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


def configure(patch: dict[str, Any]) -> None:
    from . import shortcuts

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


def delete(request: dict[str, Any]) -> None:
    from . import shortcuts

    core.validate_delete_request(request)
    name = request["name"]
    space = core.STATE_ROOT / name

    with _space_lock(space):
        subprocess.run(
            [SYSTEMCTL, "stop", f"spaces@{name}.service"],
            check=True,
        )
        _assert_no_mounts(space)
        shortcuts.remove(name)
        shutil.rmtree(space)


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
            return enter(
                arguments.target,
                arguments.command_arguments,
            )
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
