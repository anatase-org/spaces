"""Ubuntu distribution driver."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Mapping

from .. import _
from .model import Distribution


PACKAGES = ("openssh-client", "python3", "nano")
RELEASES = {
    "noble": _("Noble (24.04)"),
    "resolute": _("Resolute (26.04)"),
}


class UbuntuDistribution(Distribution):
    def describe(self, metadata: Mapping[str, Any]) -> str:
        self.validate(metadata)
        version = str(metadata["version"])
        return _(
            "Ubuntu {version}",
            version=RELEASES.get(version, version),
        )

    def command(self, metadata: Mapping[str, Any], rootfs: Path) -> list[str]:
        self.validate(metadata)
        release = str(metadata["version"])
        return [
            "debootstrap",
            release,
            str(rootfs),
        ]

    def bootstrap(self, metadata: Mapping[str, Any], rootfs: Path) -> None:
        version = str(metadata["version"])
        print(_("Bootstrapping Ubuntu {version}...", version=RELEASES.get(version, version)), flush=True)
        subprocess.run(self.command(metadata, rootfs), check=True)
        print(
            _(
                "Adding additional packages:\n{packages}",
                packages=", ".join(PACKAGES),
            ),
            flush=True,
        )
        subprocess.run(
            [
                "chroot",
                str(rootfs),
                "/usr/bin/env",
                "DEBIAN_FRONTEND=noninteractive",
                "apt-get",
                "install",
                "--yes",
                "--no-install-recommends",
                *PACKAGES,
            ],
            check=True,
        )


DISTRIBUTION = UbuntuDistribution(
    id="ubuntu",
    default_name="ubuntu",
    configuration_title=_("Ubuntu version"),
    configuration_description=_("Choose the Ubuntu release to bootstrap."),
    option_key="version",
    configuration_options=RELEASES,
    default_option="resolute",
)
