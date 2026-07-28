"""Ephemeral guest authentication policies and runtime binds."""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path

from .. import _
from .. import core


RUNTIME_ROOT = Path("/run/spaces")
NATIVE_ROOT = Path("/usr/lib/spaces/guest")
GUEST_RUNTIME = "/run/spaces-host"
GUEST_SOCKET = f"{GUEST_RUNTIME}/auth.sock"
GUEST_NATIVE = f"{GUEST_RUNTIME}/bin"
GUEST_BINARIES = (
    "pam_spaces.so",
    "spaces-polkit-agent",
)
ELF_MACHINES = {
    "x86_64": 62,
    "aarch64": 183,
}


@dataclass(frozen=True)
class AuthenticationRuntime:
    directory: Path
    socket_path: Path
    policy_binds: tuple[tuple[Path, str], ...]

    @property
    def bind_arguments(self) -> tuple[str, ...]:
        return (
            f"--bind-ro={self.socket_path}:{GUEST_SOCKET}",
            f"--bind-ro={NATIVE_ROOT}:{GUEST_NATIVE}",
            *(
                f"--bind-ro={source}:{destination}"
                for source, destination in self.policy_binds
            ),
        )


def _safe_policy(rootfs: Path, policy_path: str) -> Path:
    relative = Path(policy_path.removeprefix("/"))
    path = rootfs / relative
    if path.is_symlink() or not path.is_file():
        raise core.SpacesError(
            _("Unsafe or missing PAM policy path: {path}.", path=path)
        )
    resolved_root = rootfs.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(resolved_root):
        raise core.SpacesError(
            _("PAM policy escapes the space rootfs: {path}.", path=path)
        )
    return path


def _write_policy(destination: Path, lines: list[str]) -> None:
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    destination.chmod(0o400)


def generate_pam_policy(
    source: Path,
    destination: Path,
    *,
    session_hook: bool = False,
) -> None:
    """Replace only the auth records in a PAM policy copy."""

    result: list[str] = []
    inserted = False
    for line in source.read_text(encoding="utf-8").splitlines():
        fields = line.lstrip().split()
        if fields and fields[0].removeprefix("-") == "auth":
            if not inserted:
                result.append(f"auth required {GUEST_NATIVE}/pam_spaces.so")
                inserted = True
            continue
        result.append(line)
    if not inserted:
        result.append(f"auth required {GUEST_NATIVE}/pam_spaces.so")
    if session_hook:
        result.append(f"session optional {GUEST_NATIVE}/pam_spaces.so")
    _write_policy(destination, result)


def generate_pam_session_policy(source: Path, destination: Path) -> None:
    """Append the runtime-only session notification after distro modules."""

    lines = source.read_text(encoding="utf-8").splitlines()
    lines.append(f"session optional {GUEST_NATIVE}/pam_spaces.so")
    _write_policy(destination, lines)


def _elf_header(path: Path) -> bytes:
    with path.open("rb") as stream:
        return stream.read(20)


def _compatible_elf(header: bytes, expected_machine: int) -> bool:
    return (
        len(header) >= 20
        and header[:6] == b"\x7fELF\x02\x01"
        and int.from_bytes(header[18:20], "little") == expected_machine
    )


def validate_native_bundle(machine: str) -> None:
    expected_machine = ELF_MACHINES[machine]
    for name in GUEST_BINARIES:
        path = NATIVE_ROOT / name
        try:
            header = _elf_header(path)
        except OSError as error:
            raise core.SpacesError(
                _("Host authentication binary is missing: {path}.", path=path)
            ) from error
        if not _compatible_elf(header, expected_machine):
            raise core.SpacesError(
                _(
                    "Host authentication binary has an incompatible "
                    "architecture: {path}.",
                    path=path,
                )
            )


def validate_guest_architecture(rootfs: Path, machine: str) -> None:
    expected_machine = ELF_MACHINES[machine]
    resolved_root = rootfs.resolve(strict=True)
    for relative in ("usr/bin/env", "bin/sh", "usr/bin/sh"):
        candidate = rootfs / relative
        try:
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(resolved_root):
                continue
            header = _elf_header(resolved)
        except OSError:
            continue
        if header[:4] != b"\x7fELF":
            continue
        if not _compatible_elf(header, expected_machine):
            raise core.SpacesError(
                _(
                    "The space architecture is incompatible with the host "
                    "authentication bundle."
                )
            )
        return
    raise core.SpacesError(
        _("Could not determine the space architecture for host authentication.")
    )


def prepare_runtime(
    space_name: str,
    rootfs: Path,
    policy_path: str,
    session_policy_path: str,
) -> AuthenticationRuntime:
    machine = platform.machine()
    if machine not in ELF_MACHINES:
        raise core.SpacesError(
            _("Host authentication is unsupported on {machine}.", machine=machine)
        )
    if not NATIVE_ROOT.is_dir():
        raise core.SpacesError(
            _("Host authentication bundle is missing at {path}.", path=NATIVE_ROOT)
        )
    validate_native_bundle(machine)
    validate_guest_architecture(rootfs, machine)
    source = _safe_policy(rootfs, policy_path)
    session_source = _safe_policy(rootfs, session_policy_path)
    parent = RUNTIME_ROOT / space_name
    directory = parent / "authentication"
    if parent.is_symlink() or directory.is_symlink():
        raise core.SpacesError(_("Unsafe authentication runtime directory."))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in (parent, directory):
        os.chown(path, 0, 0)
        os.chmod(path, 0o700)

    policy_source = directory / f"authentication-{source.name}"
    if policy_source.is_symlink():
        raise core.SpacesError(_("Unsafe generated PAM policy path."))
    policy_binds: list[tuple[Path, str]] = [
        (policy_source, policy_path),
    ]
    if policy_path == session_policy_path:
        generate_pam_policy(source, policy_source, session_hook=True)
    else:
        generate_pam_policy(source, policy_source)
        session_policy_source = directory / f"session-{session_source.name}"
        if session_policy_source.is_symlink():
            raise core.SpacesError(_("Unsafe generated PAM policy path."))
        generate_pam_session_policy(session_source, session_policy_source)
        policy_binds.append((session_policy_source, session_policy_path))
    return AuthenticationRuntime(
        directory=directory,
        socket_path=directory / "auth.sock",
        policy_binds=tuple(policy_binds),
    )
