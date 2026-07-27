"""Ubuntu distribution driver."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Mapping

from .. import _
from .model import Distribution


PACKAGES = ("ssh", "python3", "nano")
RELEASES = {
    "noble": _("Noble (24.04)"),
    "resolute": _("Resolute (26.04)"),
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
        print(_("Bootstrapping Ubuntu into {rootfs}...", rootfs=rootfs))
        subprocess.run(self.command(metadata, rootfs), check=True)


DISTRIBUTION = UbuntuDistribution(
    id="ubuntu",
    default_name="ubuntu",
    configuration_title=_("Ubuntu version"),
    configuration_description=_("Choose the Ubuntu release to bootstrap."),
    option_key="version",
    configuration_options=RELEASES,
    default_option="resolute",
)
