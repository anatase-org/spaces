import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import core
from .distro import DistributionError, get_driver
from .tui import ask_custom_name, run_permission_wizard


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spaces")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="create a space")
    create_parser.add_argument("type", choices=core.KNOWN_DISTRIBUTIONS)

    configure_parser = subparsers.add_parser(
        "configure", help="configure permissions for a space"
    )
    configure_parser.add_argument("name")
    configure_parser.add_argument(
        "--user",
        action="store_true",
        help="configure only the current user's permissions",
    )
    return parser


def _confirm_rebuild(path: Path) -> bool:
    try:
        answer = input(
            f"Space {path.name!r} already exists. Recreate rootfs and preserve "
            "home? [Y/n] "
        )
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().casefold() in {"", "y", "yes"}


def _helper_command(operation: str, payload: dict[str, Any]) -> list[str]:
    helper = shutil.which("spaces.priv")
    if helper:
        command = [helper, operation, json.dumps(payload, separators=(",", ":"))]
    else:
        command = [
            sys.executable,
            "-m",
            "spaces.priv",
            operation,
            json.dumps(payload, separators=(",", ":")),
        ]
    if os.geteuid() != 0:
        pkexec = shutil.which("pkexec")
        if pkexec is None:
            raise core.SpacesError("pkexec is required to create or configure spaces.")
        command.insert(0, pkexec)
    return command


def _invoke_helper(operation: str, payload: dict[str, Any]) -> int:
    try:
        completed = subprocess.run(_helper_command(operation, payload), check=False)
    except OSError as error:
        raise core.SpacesError(f"Could not execute spaces.priv: {error}") from error
    return completed.returncode


def _create(distro_id: str) -> int:
    driver = get_driver(distro_id)
    if driver is None:
        print(
            f"spaces: distribution {distro_id!r} is known but not implemented.",
            file=sys.stderr,
        )
        return 2

    identity = core.initiating_identity()
    if driver.default_name is None:
        name = ask_custom_name()
        if name is None:
            return 130
    else:
        name = driver.default_name

    target = core.STATE_ROOT / name
    existing = target.exists() or target.is_symlink()
    if existing and not _confirm_rebuild(target):
        print("Creation cancelled.")
        return 0

    existing_info = core.load_info(target / "info.json") if existing else None
    network, selected_home = core.defaults_from_info(existing_info, identity)
    existing_distribution = (
        existing_info.get("distribution") if existing_info else None
    )
    distribution_options = driver.choices()
    distribution_value = driver.selected_option(existing_distribution)
    folders = core.discover_home_folders(identity.home)
    result = run_permission_wizard(
        home=identity.home,
        folders=folders,
        network=network,
        selected_home=selected_home,
        include_system=True,
        distribution_title=driver.configuration_title,
        distribution_description=driver.configuration_description,
        distribution_options=distribution_options,
        distribution_value=distribution_value,
        submit_label="Create",
    )
    if result is None:
        return 130

    try:
        distribution = driver.metadata(result.get("distribution_option"))
    except DistributionError as error:
        raise core.SpacesError(str(error)) from error
    info = core.create_info(
        name=name,
        distribution=distribution,
        identity=identity,
        network=result["network"],
        home=result["home"],
    )
    return_code = _invoke_helper("create", info)
    if return_code == 0 and distro_id == "custom":
        print(
            f"Custom space {name!r} created. Populate "
            f"{core.STATE_ROOT / name / 'rootfs'} to finish setting it up."
        )
    return return_code


def _configure(name: str, *, user_only: bool) -> int:
    core.validate_space_name(name)
    identity = core.initiating_identity()
    target = core.STATE_ROOT / name
    info = core.load_info(target / "info.json")
    if info is None:
        raise core.SpacesError(
            f"Space {name!r} does not exist or has an invalid info.json."
        )
    network, selected_home = core.defaults_from_info(info, identity)
    result = run_permission_wizard(
        home=identity.home,
        folders=core.discover_home_folders(identity.home),
        network=network,
        selected_home=selected_home,
        include_system=not user_only,
        distribution_title="",
        distribution_description="",
        distribution_options=[],
        distribution_value=None,
        submit_label="Configure",
    )
    if result is None:
        return 130

    permissions: dict[str, Any] = {
        "user": {
            "uid": identity.uid,
            "gid": identity.gid,
            "permissions": {"home": sorted(result["home"], key=str.casefold)},
        }
    }
    if not user_only:
        permissions["system"] = {"network": result["network"]}
    patch = {
        "schema_version": core.SCHEMA_VERSION,
        "name": name,
        "permissions": permissions,
    }
    core.validate_configure_patch(patch)
    return _invoke_helper("configure", patch)


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "create":
            return _create(arguments.type)
        return _configure(arguments.name, user_only=arguments.user)
    except core.SpacesError as error:
        print(f"spaces: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
