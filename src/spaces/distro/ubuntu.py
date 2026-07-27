"""Ubuntu distribution driver."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Mapping

from .model import Distribution


PACKAGES = ("ssh", "python3", "nano")
RELEASES = {
    "noble": "Noble (24.04)",
    "resolute": "Resolute (26.04)",
}


class UbuntuDistribution(Distribution):
    def command(self, metadata: Mapping[str, Any], rootfs: Path) -> list[str]:
        self.validate(metadata)
        release = str(metadata["version"])
        return [
            "debootstrap",
            f"--include={','.join(PACKAGES)}",
            release,
            str(rootfs),
        ]

    def bootstrap(self, metadata: Mapping[str, Any], rootfs: Path) -> None:
        print(f"Bootstrapping Ubuntu into {rootfs}...")
        subprocess.run(self.command(metadata, rootfs), check=True)


DISTRIBUTION = UbuntuDistribution(
    id="ubuntu",
    default_name="ubuntu",
    configuration_title="Ubuntu version",
    configuration_description="Choose the Ubuntu release to bootstrap.",
    option_key="version",
    configuration_options=RELEASES,
    default_option="resolute",
)
