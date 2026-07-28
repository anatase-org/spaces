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
from . import auth
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
    lease_token: str | None = None,
) -> int:
    environment: list[str] = []
    if lease_token is not None:
        environment = [f"--setenv={auth.SESSION_ENV}={lease_token}"]
    completed = subprocess.run(
        [
            MACHINECTL,
            "--quiet",
            f"--uid={user_name}",
            *environment,
            "--",
            "shell",
            space_name,
            *command,
        ],
        check=False,
    )
    return completed.returncode


def _verified_subject(
    pid: int | None,
    start_time: int | None,
    session_id: str | None,
) -> auth.LeaseSubject | None:
    if pid is None and start_time is None and session_id is None:
        return None
    if pid is None or start_time is None or not session_id:
        raise core.SpacesError(_("Incomplete host authentication subject."))
    caller_uid = _caller_uid()
    try:
        metadata = Path(f"/proc/{pid}").stat()
    except OSError as error:
        raise core.SpacesError(
            _("Could not inspect initiating process {pid}.", pid=pid)
        ) from error
    if metadata.st_uid != caller_uid:
        raise core.SpacesError(_("Initiating process UID does not match caller."))
    if auth.process_start_time(pid) != start_time:
        raise core.SpacesError(_("Initiating process start time changed."))
    ancestor = os.getpid()
    seen: set[int] = set()
    while ancestor > 1 and ancestor not in seen and ancestor != pid:
        seen.add(ancestor)
        ancestor = auth.process_parent(ancestor)
    if ancestor != pid:
        raise core.SpacesError(_("Initiating process is not a helper ancestor."))
    if not auth.process_session_matches(pid, session_id, caller_uid):
        raise core.SpacesError(_("Initiating process session does not match."))
    try:
        account = pwd.getpwuid(caller_uid)
    except KeyError as error:
        raise core.SpacesError(_("Initiating host user no longer exists.")) from error
    return auth.LeaseSubject(
        pid=pid,
        start_time=start_time,
        uid=caller_uid,
        gid=account.pw_gid,
        session_id=session_id,
    )


def _authentication_lease(
    info: dict[str, Any],
    space_name: str,
    subject: auth.LeaseSubject | None,
) -> auth.LeaseConnection | None:
    enabled = info["permissions"]["system"].get("host_authentication", True)
    if not enabled:
        return None
    if subject is None:
        raise core.SpacesError(
            _("Host authentication requires an initiating login session.")
        )
    socket_path = (
        auth.RUNTIME_ROOT
        / space_name
        / "authentication"
        / "auth.sock"
    )
    try:
        return auth.LeaseConnection(socket_path, subject)
    except OSError as error:
        raise core.SpacesError(
            _(
                "Could not connect to the space authentication service: "
                "{error}",
                error=error,
            )
        ) from error


def enter(
    target: str,
    command: list[str],
    *,
    subject_pid: int | None = None,
    subject_start_time: int | None = None,
    subject_session: str | None = None,
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
    if str(caller_uid) not in info["permissions"]["users"]:
        raise core.SpacesError(
            _(
                "User {user!r} is not configured for space {space!r}.",
                user=user_name,
                space=space_name,
            )
        )

    subject = None
    if info["permissions"]["system"].get("host_authentication", True):
        subject = _verified_subject(
            subject_pid,
            subject_start_time,
            subject_session,
        )
    returncode = _ensure_space_started(space_name)
    if returncode != 0:
        return returncode
    lease = _authentication_lease(info, space_name, subject)
    try:
        return _machine_shell(
            user.pw_name,
            space_name,
            command,
            lease.token if lease is not None else None,
        )
    finally:
        if lease is not None:
            lease.close()


def enter_as_user(
    user_name: str,
    space_name: str,
    command: list[str],
    *,
    subject_pid: int | None = None,
    subject_start_time: int | None = None,
    subject_session: str | None = None,
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
    subject = None
    if (
        user_name != "root"
        and info["permissions"]["system"].get("host_authentication", True)
    ):
        if str(_caller_uid()) not in info["permissions"]["users"]:
            raise core.SpacesError(
                _("Initiating user is not configured for this space.")
            )
        subject = _verified_subject(
            subject_pid,
            subject_start_time,
            subject_session,
        )
    returncode = _ensure_space_started(space_name)
    if returncode != 0:
        return returncode
    lease = (
        None
        if user_name == "root"
        else _authentication_lease(info, space_name, subject)
    )
    try:
        return _machine_shell(
            user_name,
            space_name,
            command,
            lease.token if lease is not None else None,
        )
    finally:
        if lease is not None:
            lease.close()


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
    enter_parser.add_argument("--subject-pid", type=int)
    enter_parser.add_argument("--subject-start-time", type=int)
    enter_parser.add_argument("--subject-session")
    enter_parser.add_argument("target")
    enter_parser.add_argument(
        "command_arguments",
        nargs=argparse.REMAINDER,
    )
    enter_as_user_parser = subparsers.add_parser("enter-as-user")
    enter_as_user_parser.add_argument("--subject-pid", type=int)
    enter_as_user_parser.add_argument("--subject-start-time", type=int)
    enter_as_user_parser.add_argument("--subject-session")
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
            subject_arguments = {}
            if arguments.subject_pid is not None:
                subject_arguments = {
                    "subject_pid": arguments.subject_pid,
                    "subject_start_time": arguments.subject_start_time,
                    "subject_session": arguments.subject_session,
                }
            return enter(
                arguments.target,
                arguments.command_arguments,
                **subject_arguments,
            )
        if arguments.command == "enter-as-user":
            subject_arguments = {}
            if arguments.subject_pid is not None:
                subject_arguments = {
                    "subject_pid": arguments.subject_pid,
                    "subject_start_time": arguments.subject_start_time,
                    "subject_session": arguments.subject_session,
                }
            return enter_as_user(
                arguments.user,
                arguments.space,
                arguments.command_arguments,
                **subject_arguments,
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
