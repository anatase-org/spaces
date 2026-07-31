import argparse
import contextlib
import json
import os
import pwd
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import _
from . import core


def get_driver(distribution_id: str) -> Any:
    """Load distribution code only when a command needs it."""

    from .distro import get_driver as load_driver

    return load_driver(distribution_id)


def configure_logging(*, rich: bool = False) -> None:
    """Load console logging only for operations that produce progress."""

    from .logging import configure_logging as configure

    configure(rich=rich)


def log(message: str) -> None:
    """Load console logging only for operations that produce progress."""

    from .logging import log as write_log

    write_log(message)


def run_streamed(
    command: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Load streamed logging only for management operations."""

    from .logging import run_streamed as run

    return run(command, check=check)


def ask_custom_name() -> str | None:
    """Load the interactive TUI only for commands that need it."""

    from .tui import ask_custom_name as tui_ask_custom_name

    return tui_ask_custom_name()


def run_permission_wizard(**arguments: Any) -> dict[str, Any] | None:
    """Load the interactive TUI only for commands that need it."""

    from .tui import run_permission_wizard as tui_run_permission_wizard

    return tui_run_permission_wizard(**arguments)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spaces")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help=_("create a space"))
    create_parser.add_argument("type", choices=core.KNOWN_DISTRIBUTIONS)
    create_parser.add_argument(
        "--purge",
        action="store_true",
        help=_("delete existing space home data before creating"),
    )

    configure_parser = subparsers.add_parser(
        "configure", help=_("configure permissions for a space")
    )
    configure_parser.add_argument("name")
    configure_parser.add_argument(
        "--user",
        nargs="?",
        const="",
        metavar=_("USER"),
        help=_(
            "configure only USER's permissions "
            "(default: current user)"
        ),
    )

    delete_parser = subparsers.add_parser(
        "delete", help=_("permanently delete a space")
    )
    delete_parser.add_argument("name")
    delete_parser.add_argument(
        "--noconfirm",
        action="store_true",
        help=_("delete without prompting for confirmation"),
    )
    delete_parser.add_argument(
        "--purge",
        action="store_true",
        help=_("delete the space home data as well"),
    )

    cp_parser = subparsers.add_parser(
        "cp",
        help=_("copy files to, from, or between spaces"),
        usage=_("spaces cp SOURCE... DESTINATION [CP_ARGUMENT ...]"),
        description=_(
            "Prefix a path with SPACE: to address a space filesystem."
        ),
    )
    cp_parser.add_argument(
        "arguments",
        nargs=argparse.REMAINDER,
        metavar=_("ARGUMENT"),
    )

    enter_parser = subparsers.add_parser(
        "enter",
        help=_("enter a space or run a command in it"),
        usage=_(
            "spaces enter [--root | --user USER] SPACE [--] [COMMAND ...]"
        ),
    )
    enter_user = enter_parser.add_mutually_exclusive_group()
    enter_user.add_argument(
        "--root",
        dest="enter_user",
        action="store_const",
        const="root",
        help=_("enter as root (requires administrator authentication)"),
    )
    enter_user.add_argument(
        "--user",
        dest="enter_user",
        metavar=_("USER"),
        help=_("enter as USER (requires administrator authentication)"),
    )
    enter_parser.add_argument(
        "--graphical",
        action="store_true",
        help=_("record that the command was launched from a desktop shortcut"),
    )
    enter_parser.add_argument("space")
    enter_parser.add_argument(
        "command_arguments",
        nargs=argparse.REMAINDER,
        metavar=_("COMMAND"),
    )
    return parser


def _raw_helper_command(
    operation: str,
    arguments: list[str],
    *,
    keep_cwd: bool = False,
) -> list[str]:
    helper = shutil.which("spaces.priv")
    if helper:
        command = [helper, operation, *arguments]
    else:
        command = [
            sys.executable,
            "-m",
            "spaces.priv",
            operation,
            *arguments,
        ]
    if os.geteuid() != 0:
        pkexec = shutil.which("pkexec")
        if pkexec is None:
            raise core.SpacesError(
                _("pkexec is required to modify spaces.")
            )
        if keep_cwd:
            command[0:0] = [pkexec, "--keep-cwd"]
        else:
            command.insert(0, pkexec)
    return command


def _helper_command(operation: str, payload: dict[str, Any]) -> list[str]:
    return _raw_helper_command(
        operation,
        [json.dumps(payload, separators=(",", ":"))],
        keep_cwd=operation == "cp",
    )


def _has_controlling_terminal() -> bool:
    try:
        descriptor = os.open(
            "/dev/tty",
            os.O_RDWR | os.O_CLOEXEC,
        )
    except OSError:
        return False
    os.close(descriptor)
    return True


@contextlib.contextmanager
def _tty_polkit_agent() -> Iterator[None]:
    if os.geteuid() == 0 or not _has_controlling_terminal():
        yield
        return

    executable = shutil.which("pkttyagent")
    if executable is None:
        yield
        return

    try:
        ready_descriptor, notify_descriptor = os.pipe()
    except OSError:
        yield
        return
    ready_descriptor_open = True
    process: subprocess.Popen[bytes] | None = None
    try:
        try:
            process = subprocess.Popen(
                [
                    executable,
                    "--process",
                    str(os.getpid()),
                    "--notify-fd",
                    str(notify_descriptor),
                ],
                pass_fds=(notify_descriptor,),
            )
        except OSError:
            yield
            return
        finally:
            os.close(notify_descriptor)

        try:
            os.read(ready_descriptor, 1)
        finally:
            os.close(ready_descriptor)
            ready_descriptor_open = False

        yield
    finally:
        if ready_descriptor_open:
            os.close(ready_descriptor)
        if process is not None:
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            process.wait()


def _invoke_helper(operation: str, payload: dict[str, Any]) -> int:
    try:
        configure_logging(rich=True)
        with _tty_polkit_agent():
            completed = run_streamed(
                _helper_command(operation, payload),
                check=False,
            )
    except OSError as error:
        raise core.SpacesError(
            _("Could not execute spaces.priv: {error}", error=error)
        ) from error
    return completed.returncode


def _invoke_raw_helper(operation: str, arguments: list[str]) -> int:
    try:
        if operation == "enter":
            completed = subprocess.run(
                _raw_helper_command(operation, arguments),
                check=False,
            )
            return completed.returncode
        with _tty_polkit_agent():
            completed = subprocess.run(
                _raw_helper_command(operation, arguments),
                check=False,
            )
    except OSError as error:
        raise core.SpacesError(
            _("Could not execute spaces.priv: {error}", error=error)
        ) from error
    return completed.returncode


def _create(
    distro_id: str,
    *,
    missing: bool = False,
    purge: bool = False,
) -> int:
    from .distro import DistributionError

    driver = get_driver(distro_id)
    if driver is None:
        print(
            _(
                "spaces: distribution {distro_id!r} is known but not "
                "implemented.",
                distro_id=distro_id,
            ),
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
    target_present = target.exists() or target.is_symlink()
    rootfs = target / "rootfs"
    rootfs_present = rootfs.exists() or rootfs.is_symlink()
    override = target_present and (not missing or rootfs_present)

    existing_info = (
        core.load_info(target / "info.json") if target_present else None
    )
    (
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
    ) = core.defaults_from_info(
        existing_info, identity
    )
    preset = core.selected_preset(existing_info, identity)
    existing_distribution = (
        existing_info.get("distribution") if existing_info else None
    )
    distribution_options = driver.choices()
    distribution_value = (
        None
        if driver.multiple_options
        else driver.selected_option(existing_distribution)
    )
    distribution_values = (
        driver.selected_options(existing_distribution)
        if driver.multiple_options
        else []
    )
    folders = core.discover_home_folders(identity.home)
    result = run_permission_wizard(
        home=identity.home,
        folders=folders,
        network=network,
        kernel_capabilities=kernel_capabilities,
        devices=devices,
        host_authentication=host_authentication,
        shortcuts=shortcuts,
        selected_home=selected_home,
        administrator=administrator,
        desktop=desktop,
        credential_agents=credential_agents,
        mounted_drives=mounted_drives,
        administrator_group=driver.administrator_group,
        include_system=True,
        distribution_title=driver.configuration_title,
        distribution_description=driver.configuration_description,
        distribution_options=distribution_options,
        distribution_value=distribution_value,
        distribution_multiple=driver.multiple_options,
        distribution_values=distribution_values,
        submit_label=_("Create"),
        override=override,
        purge=purge,
        missing=missing and not override,
        space_name=name,
        preset=preset,
    )
    if result is None:
        return 130

    try:
        selection = (
            result.get("distribution_options", [])
            if driver.multiple_options
            else result.get("distribution_option")
        )
        distribution = driver.metadata(selection)
    except DistributionError as error:
        raise core.SpacesError(str(error)) from error
    info = core.create_info(
        name=name,
        distribution=distribution,
        identity=identity,
        network=result["network"],
        home=result["home"],
        kernel_capabilities=result.get("kernel_capabilities", "basic"),
        devices=result.get("devices", "basic"),
        administrator=result.get("administrator", True),
        host_authentication=result.get("host_authentication", True),
        shortcuts=result.get("shortcuts", True),
        desktop=result.get("desktop", True),
        credential_agents=result.get("credential_agents", True),
        mounted_drives=result.get("mounted_drives", True),
        preset=result.get("preset", "custom"),
    )
    info["purge"] = purge
    configure_logging(rich=True)
    log(
        _(
            "Creating {distribution} space {name!r}...",
            distribution=driver.describe(distribution),
            name=name,
        )
    )
    return_code = _invoke_helper("create", info)
    if return_code == 0 and distro_id == "custom":
        print(
            _(
                "Custom space {name!r} created. Populate {rootfs} to finish "
                "setting it up.",
                name=name,
                rootfs=core.STATE_ROOT / name / "rootfs",
            )
        )
    return return_code


def _configure(name: str, *, user: str | None) -> int:
    core.validate_space_name(name)
    identity = core.initiating_identity()
    user_only = user is not None
    if user:
        try:
            account = pwd.getpwnam(user)
        except KeyError as error:
            raise core.SpacesError(
                _("Host user {user!r} does not exist.", user=user)
            ) from error
        home = Path(account.pw_dir)
        if not home.is_absolute():
            raise core.SpacesError(
                _("Host user {user!r} has a non-absolute home path.", user=user)
            )
        identity = core.Identity(
            uid=account.pw_uid,
            gid=account.pw_gid,
            home=home,
        )
    target = core.STATE_ROOT / name
    info = core.load_info(target / "info.json")
    if info is None:
        raise core.SpacesError(
            _(
                "Space {name!r} does not exist or has an invalid info.json.",
                name=name,
            )
        )
    (
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
    ) = core.defaults_from_info(info, identity)
    preset = core.selected_preset(info, identity)
    driver = get_driver(info["distribution"]["id"])
    administrator_group = (
        driver.administrator_group if driver is not None else "wheel"
    )
    result = run_permission_wizard(
        home=identity.home,
        folders=core.discover_home_folders(identity.home),
        network=network,
        kernel_capabilities=kernel_capabilities,
        devices=devices,
        host_authentication=host_authentication,
        shortcuts=shortcuts,
        selected_home=selected_home,
        administrator=administrator,
        desktop=desktop,
        credential_agents=credential_agents,
        mounted_drives=mounted_drives,
        administrator_group=administrator_group,
        include_system=not user_only,
        distribution_title="",
        distribution_description="",
        distribution_options=[],
        distribution_value=None,
        submit_label=_("Confirm"),
        preset=preset,
    )
    if result is None:
        return 130

    permissions: dict[str, Any] = {
        "user": {
            "uid": identity.uid,
            "gid": identity.gid,
            "permissions": {
                "preset": result.get("preset", "custom"),
                "home": sorted(result["home"], key=str.casefold),
                "administrator": result.get("administrator", True),
                "desktop": result.get("desktop", True),
                "credential_agents": result.get("credential_agents", True),
                "mounted_drives": result.get("mounted_drives", True),
            },
        }
    }
    if not user_only:
        permissions["system"] = {
            "preset": result.get("preset", "custom"),
            "network": result["network"],
            "kernel_capabilities": result.get(
                "kernel_capabilities", "basic"
            ),
            "devices": result["devices"],
            "host_authentication": result.get("host_authentication", True),
            "shortcuts": result.get("shortcuts", True),
        }
    patch = {
        "schema_version": core.SCHEMA_VERSION,
        "name": name,
        "permissions": permissions,
    }
    core.validate_configure_patch(patch)
    return _invoke_helper("configure", patch)


def _delete(name: str, *, noconfirm: bool, purge: bool = False) -> int:
    core.validate_space_name(name)
    target = core.STATE_ROOT / name
    if target.is_symlink() or not target.is_dir():
        raise core.SpacesError(
            _("Space {name!r} does not exist.", name=name)
        )

    if not noconfirm:
        deletion = (
            _("including its home data")
            if purge
            else _("while preserving its home data")
        )
        response = input(
            _(
                "Press Enter to permanently delete space {name!r} at {target} "
                "{deletion}, or type anything to cancel: ",
                name=name,
                target=target,
                deletion=deletion,
            )
        )
        if response:
            print(_("Deletion cancelled."))
            return 130
    return _invoke_helper("delete", {"name": name, "purge": purge})


def _cp(arguments: list[str]) -> int:
    if not arguments:
        raise core.SpacesError(_("cp requires arguments."))
    fixed_arguments = [
        core.resolve_space_location(argument) for argument in arguments
    ]
    return _invoke_helper("cp", {"arguments": fixed_arguments})


def _enter(
    space: str,
    command: list[str],
    *,
    enter_user: str | None = None,
    graphical: bool = False,
) -> int:
    core.validate_space_name(space)
    target = core.STATE_ROOT / space
    info_path = target / "info.json"
    target_missing = not target.exists() and not target.is_symlink()
    info_missing = (
        not target.is_symlink()
        and target.is_dir()
        and not info_path.exists()
        and not info_path.is_symlink()
    )
    if (target_missing or info_missing) and _has_controlling_terminal():
        driver = get_driver(space)
        if driver is not None and driver.default_name == space:
            return_code = _create(space, missing=True)
            if return_code != 0:
                return return_code

    user_name: str | None = None
    if enter_user is None:
        identity = core.initiating_identity()
        try:
            user_name = pwd.getpwuid(identity.uid).pw_name
        except KeyError as error:
            raise core.SpacesError(
                _("No passwd entry exists for UID {uid}.", uid=identity.uid)
            ) from error

    if enter_user is None:
        assert user_name is not None
        operation = "enter"
        enter_arguments = [f"{user_name}@{space}"]
    else:
        operation = "enter-as-user"
        enter_arguments = [enter_user, space]
    if command:
        enter_arguments.extend(["--", *command])
    return _invoke_raw_helper(operation, enter_arguments)


def _normalize_enter_options(arguments: list[str]) -> list[str]:
    if (
        len(arguments) < 3
        or arguments[0] != "enter"
        or arguments[1].startswith("-")
    ):
        return arguments

    space = arguments[1]
    option = arguments[2]
    if option == "--root" or option.startswith("--user="):
        return ["enter", option, space, *arguments[3:]]
    if option == "--user" and len(arguments) >= 4:
        return [
            "enter",
            option,
            arguments[3],
            space,
            *arguments[4:],
        ]
    return arguments


def _normalize_configure_options(arguments: list[str]) -> list[str]:
    """Disambiguate ``configure --user SPACE`` as the current user."""

    if (
        len(arguments) == 3
        and arguments[:2] == ["configure", "--user"]
        and not arguments[2].startswith("-")
    ):
        return ["configure", arguments[2], "--user"]
    return arguments


def main(argv: list[str] | None = None) -> int:
    try:
        raw_arguments = list(sys.argv[1:] if argv is None else argv)
        raw_arguments = _normalize_configure_options(raw_arguments)
        raw_arguments = _normalize_enter_options(raw_arguments)
        if raw_arguments[:1] == ["cp"]:
            if raw_arguments[1:] in (["-h"], ["--help"]):
                build_parser().parse_args(raw_arguments)
            return _cp(raw_arguments[1:])

        arguments = build_parser().parse_args(raw_arguments)
        if arguments.command == "create":
            return _create(arguments.type, purge=arguments.purge)
        elif arguments.command == "configure":
            return _configure(arguments.name, user=arguments.user)
        elif arguments.command == "delete":
            return _delete(
                arguments.name,
                noconfirm=arguments.noconfirm,
                purge=arguments.purge,
            )
        elif arguments.command == "enter":
            return _enter(
                arguments.space,
                arguments.command_arguments,
                enter_user=arguments.enter_user,
                graphical=arguments.graphical,
            )
        raise core.SpacesError(
            _("Unknown command: {command!r}.", command=arguments.command)
        )
    except KeyboardInterrupt:
        print(_("Exiting due to Ctrl+C"), file=sys.stderr)
        return 130
    except core.SpacesError as error:
        print(_("spaces: {error}", error=error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
