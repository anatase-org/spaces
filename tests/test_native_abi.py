from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_guest_abi", ROOT / "native" / "check_guest_abi.py"
)
assert SPEC is not None and SPEC.loader is not None
check_guest_abi = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_guest_abi)


class GuestAbiBuildCheckTests(unittest.TestCase):
    def test_rejects_wrong_elf_machine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "binary"
            header = bytearray(64)
            header[:7] = b"\x7fELF\x02\x01\x01"
            header[18:20] = (183).to_bytes(2, "little")
            binary.write_bytes(header)

            self.assertTrue(
                check_guest_abi.check_architecture(binary, 62)
            )

    def test_rejects_new_glibc_symbol_version(self) -> None:
        output = (
            "Version needs section '.gnu.version_r'\n"
            "Name: GLIBC_2.17\n"
            "Name: GLIBC_2.38\n"
        )
        process = subprocess.CompletedProcess(
            ["readelf"], 0, stdout=output, stderr=""
        )
        with mock.patch.object(
            check_guest_abi.subprocess, "run", return_value=process
        ):
            errors = check_guest_abi.check_glibc(
                Path("guest-binary"), (2, 17)
            )

        self.assertEqual(
            errors,
            [
                "guest-binary: requires GLIBC_2.38 "
                "(maximum: GLIBC_2.17)"
            ],
        )

    def test_rejects_private_glibc_abi(self) -> None:
        process = subprocess.CompletedProcess(
            ["readelf"],
            0,
            stdout="Name: GLIBC_2.17\nName: GLIBC_PRIVATE\n",
            stderr="",
        )
        with mock.patch.object(
            check_guest_abi.subprocess, "run", return_value=process
        ):
            errors = check_guest_abi.check_glibc(
                Path("guest-binary"), (2, 17)
            )

        self.assertEqual(
            errors,
            ["guest-binary: references the unstable GLIBC_PRIVATE ABI"],
        )


if __name__ == "__main__":
    unittest.main()
