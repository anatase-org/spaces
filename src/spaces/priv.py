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
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import _
from . import core
from .distro import DistributionError, get_driver
from .launch import launch


SYSTEMCTL = "/usr/bin/systemctl"
MACHINECTL = "/usr/bin/machinectl"


def _root_owned_directory(path: Path) -> None:
    path.mkdir(mode=0o755, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise core.SpacesError(
            _("Expected a directory at {path}.", path=path)
        )
    os.chown(path, 0, 0)
    os.chmod(path, 0o755)


@contextmanager
def _state_lock() -> Iterator[None]:
    _root_owned_directory(core.STATE_ROOT)
    descriptor = os.open(core.STATE_ROOT, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
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


def _machine_shell(
    user_name: str,
    space_name: str,
    command: list[str],
) -> int:
    completed = subprocess.run(
        [
            MACHINECTL,
            "--quiet",
            f"--uid={user_name}",
            "--",
            "shell",
            space_name,
            *command,
        ],
        check=False,
    )
    return completed.returncode


def enter(target: str, command: list[str]) -> int:
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
    if str(caller_uid) not in info["permissions"]["users"]:
        raise core.SpacesError(
            _(
                "User {user!r} is not configured for space {space!r}.",
                user=user_name,
                space=space_name,
            )
        )

    returncode = _ensure_space_started(space_name)
    if returncode != 0:
        return returncode
    return _machine_shell(user.pw_name, space_name, command)


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
    _space_info(space_name)
    returncode = _ensure_space_started(space_name)
    if returncode != 0:
        return returncode
    return _machine_shell(user_name, space_name, command)


def create(info: dict[str, Any]) -> None:
    core.validate_creation_info(info)
    _assert_initiating_user(info)
    name = info["name"]
    distribution = info["distribution"]
    space = core.STATE_ROOT / name

    with _state_lock():
        subprocess.run(
            ["/usr/bin/systemctl", "stop", f"spaces@{name}.service"],
            check=True,
        )
        if space.is_symlink() or (space.exists() and not space.is_dir()):
            raise core.SpacesError(
                _("Unsafe space path: {space}.", space=space)
            )
        _root_owned_directory(space)

        home = space / "home"
        if home.is_symlink() or (home.exists() and not home.is_dir()):
            raise core.SpacesError(_("Unsafe home path: {home}.", home=home))
        _root_owned_directory(home)

        rootfs = space / "rootfs"
        _remove_rootfs(rootfs)
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
            driver.bootstrap(distribution, rootfs)
        except DistributionError as error:
            raise core.SpacesError(str(error)) from error


def configure(patch: dict[str, Any]) -> None:
    core.validate_configure_patch(patch)
    update = patch["permissions"]["user"]
    if update["uid"] != _caller_uid():
        raise core.SpacesError(
            _("Configure payload must target the initiating user's permissions.")
        )

    space = core.STATE_ROOT / patch["name"]
    with _state_lock():
        if space.is_symlink() or not space.is_dir():
            raise core.SpacesError(
                _("Space {name!r} does not exist.", name=patch["name"])
            )
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


def delete(request: dict[str, Any]) -> None:
    core.validate_delete_request(request)
    name = request["name"]
    space = core.STATE_ROOT / name

    with _state_lock():
        if space.is_symlink() or not space.is_dir():
            raise core.SpacesError(
                _("Space {name!r} does not exist.", name=name)
            )
        _assert_no_mounts(space)
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
        if arguments.command == "launch":
            return launch(arguments.space)
        if arguments.command == "enter":
            return enter(arguments.target, arguments.command_arguments)
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
        print(
            _(
                "Bootstrap failed; the partial rootfs and info.json were "
                "preserved for inspection or retry."
            ),
            file=sys.stderr,
        )
        return error.returncode or 1
    except (core.SpacesError, OSError) as error:
        print(_("spaces.priv: {error}", error=error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
