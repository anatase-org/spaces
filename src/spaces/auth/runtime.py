"""Ephemeral guest authentication socket and native-module binds."""

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
)
ELF_MACHINES = {
    "x86_64": 62,
    "aarch64": 183,
}


@dataclass(frozen=True)
class AuthenticationRuntime:
    directory: Path
    socket_path: Path

    @property
    def bind_arguments(self) -> tuple[str, ...]:
        return (
            f"--bind-ro={self.socket_path}:{GUEST_SOCKET}",
            f"--bind-ro={NATIVE_ROOT}:{GUEST_NATIVE}",
        )


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
    parent = RUNTIME_ROOT / space_name
    directory = parent / "authentication"
    if parent.is_symlink() or directory.is_symlink():
        raise core.SpacesError(_("Unsafe authentication runtime directory."))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in (parent, directory):
        os.chown(path, 0, 0)
        os.chmod(path, 0o700)

    return AuthenticationRuntime(
        directory=directory,
        socket_path=directory / "auth.sock",
    )
