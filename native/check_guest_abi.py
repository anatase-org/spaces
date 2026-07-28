#!/usr/bin/env python3
"""Fail the build if native authentication artifacts violate their ABI."""

from __future__ import annotations

import argparse
import re
import struct
import subprocess
import sys
from pathlib import Path


MACHINES = {
    "x86_64": 62,
    "aarch64": 183,
}
GLIBC_VERSION = re.compile(r"\bGLIBC_(\d+(?:\.\d+)+)\b")


def version(value: str) -> tuple[int, ...]:
    return tuple(int(component) for component in value.split("."))


def elf_machine(path: Path) -> int:
    try:
        header = path.read_bytes()[:64]
    except OSError as error:
        raise ValueError(f"cannot read ELF file: {error}") from error
    if len(header) < 20 or header[:4] != b"\x7fELF":
        raise ValueError("not an ELF file")
    if header[4] != 2:
        raise ValueError("not an ELF64 file")
    if header[5] != 1:
        raise ValueError("not a little-endian ELF file")
    if header[6] != 1:
        raise ValueError("unsupported ELF header version")
    return struct.unpack_from("<H", header, 18)[0]


def version_information(path: Path) -> str:
    process = subprocess.run(
        ["readelf", "--version-info", "--wide", path],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if process.returncode:
        detail = process.stderr.strip() or "readelf rejected the file"
        raise ValueError(detail)
    return process.stdout


def check_architecture(path: Path, expected: int) -> list[str]:
    try:
        actual = elf_machine(path)
    except ValueError as error:
        return [f"{path}: {error}"]
    if actual != expected:
        return [
            f"{path}: ELF machine {actual} does not match target machine "
            f"{expected}"
        ]
    return []


def check_glibc(path: Path, maximum: tuple[int, ...]) -> list[str]:
    try:
        information = version_information(path)
    except ValueError as error:
        return [f"{path}: cannot inspect symbol versions: {error}"]
    errors = []
    if "GLIBC_PRIVATE" in information:
        errors.append(f"{path}: references the unstable GLIBC_PRIVATE ABI")
    versions = {
        version(match.group(1))
        for match in GLIBC_VERSION.finditer(information)
    }
    if not versions:
        errors.append(f"{path}: has no verifiable glibc symbol requirements")
    elif max(versions) > maximum:
        newest = ".".join(str(component) for component in max(versions))
        allowed = ".".join(str(component) for component in maximum)
        errors.append(
            f"{path}: requires GLIBC_{newest} (maximum: GLIBC_{allowed})"
        )
    return errors


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=tuple(MACHINES), required=True)
    parser.add_argument("--glibc-max", required=True, type=version)
    parser.add_argument("--guest", nargs="+", type=Path, required=True)
    parser.add_argument("--host", nargs="+", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    expected = MACHINES[arguments.target]
    errors = []
    for path in (*arguments.guest, *arguments.host):
        errors.extend(check_architecture(path, expected))
    for path in arguments.guest:
        errors.extend(check_glibc(path, arguments.glibc_max))
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
