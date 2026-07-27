"""Root-only Spaces filesystem operations.

This module intentionally imports only Python standard-library modules and
``spaces.core``, which is also standard-library-only.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import core
from .distro import DistributionError, get_driver


def _root_owned_directory(path: Path) -> None:
    path.mkdir(mode=0o755, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise core.SpacesError(f"Expected a directory at {path}.")
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
    rootfs = path.resolve()
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError as error:
        raise core.SpacesError(f"Could not inspect active mounts: {error}") from error
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        mountpoint = Path(_unescape_mount_path(fields[4]))
        if mountpoint == rootfs or rootfs in mountpoint.parents:
            raise core.SpacesError(
                f"Cannot recreate {rootfs} while {mountpoint} is mounted beneath it."
            )


def _caller_uid() -> int:
    for variable in ("PKEXEC_UID", "SUDO_UID"):
        if variable in os.environ:
            try:
                uid = int(os.environ[variable])
            except ValueError as error:
                raise core.SpacesError(f"{variable} must be a numeric UID.") from error
            if uid < 0:
                raise core.SpacesError(f"{variable} must not be negative.")
            return uid
    return 0


def _assert_initiating_user(info: dict[str, Any]) -> None:
    users = info["permissions"]["users"]
    if set(users) != {str(_caller_uid())}:
        raise core.SpacesError(
            "Creation payload must contain only the initiating user's permissions."
        )


def create(info: dict[str, Any]) -> None:
    core.validate_creation_info(info)
    _assert_initiating_user(info)
    name = info["name"]
    distribution = info["distribution"]
    space = core.STATE_ROOT / name

    with _state_lock():
        if space.is_symlink() or (space.exists() and not space.is_dir()):
            raise core.SpacesError(f"Unsafe space path: {space}.")
        _root_owned_directory(space)

        home = space / "home"
        if home.is_symlink() or (home.exists() and not home.is_dir()):
            raise core.SpacesError(f"Unsafe home path: {home}.")
        _root_owned_directory(home)

        rootfs = space / "rootfs"
        _remove_rootfs(rootfs)
        _root_owned_directory(rootfs)
        _write_info(space, info)

        driver = get_driver(distribution["id"])
        if driver is None:
            raise core.SpacesError(
                f"Distribution {distribution['id']!r} is not implemented."
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
            "Configure payload must target the initiating user's permissions."
        )

    space = core.STATE_ROOT / patch["name"]
    with _state_lock():
        if space.is_symlink() or not space.is_dir():
            raise core.SpacesError(f"Space {patch['name']!r} does not exist.")
        info_path = space / "info.json"
        if info_path.is_symlink() or not info_path.is_file():
            raise core.SpacesError(f"Unsafe space information path: {info_path}.")
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise core.SpacesError(f"Could not read {info_path}: {error}") from error
        core.validate_info(info)
        if info["name"] != patch["name"]:
            raise core.SpacesError("Space name does not match its info.json.")

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spaces.priv")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("create", "configure"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("payload")
    return parser


def main(argv: list[str] | None = None) -> int:
    if os.geteuid() != 0:
        print("spaces.priv must run as root.", file=sys.stderr)
        return 1
    arguments = build_parser().parse_args(argv)
    try:
        payload = json.loads(arguments.payload)
        if arguments.command == "create":
            create(payload)
        else:
            configure(payload)
    except json.JSONDecodeError as error:
        print(f"Invalid JSON payload: {error}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        print(
            "Bootstrap failed; the partial rootfs and info.json were preserved "
            "for inspection or retry.",
            file=sys.stderr,
        )
        return error.returncode or 1
    except (core.SpacesError, OSError) as error:
        print(f"spaces.priv: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
