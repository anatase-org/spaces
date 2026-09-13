"""Generic distribution driver loading."""

from __future__ import annotations

import platform
from importlib import import_module

from .model import Distribution, DistributionError

_DRIVERS = {
    "arch": "spaces.distro.arch",
    "fedora": "spaces.distro.fedora",
    "ubuntu": "spaces.distro.ubuntu",
    "kali": "spaces.distro.kali",
    "custom": "spaces.distro.custom",
}
_SUPPORTED_BY_ARCHITECTURE = {
    "aarch64": frozenset({"fedora", "ubuntu", "kali", "custom"}),
}


def supported_ids(machine: str | None = None) -> tuple[str, ...]:
    """Return distributions supported by the host architecture."""

    host_architecture = platform.machine() if machine is None else machine
    supported = _SUPPORTED_BY_ARCHITECTURE.get(
        host_architecture,
        _DRIVERS.keys(),
    )
    return tuple(
        distribution_id
        for distribution_id in _DRIVERS
        if distribution_id in supported
    )


KNOWN_IDS = supported_ids()


def get_driver(
    distribution_id: str,
    *,
    machine: str | None = None,
) -> Distribution | None:
    if distribution_id not in supported_ids(machine):
        return None
    module_name = _DRIVERS.get(distribution_id)
    if module_name is None:
        return None
    return import_module(module_name).DISTRIBUTION
