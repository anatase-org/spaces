"""Generic distribution driver loading."""

from __future__ import annotations

from importlib import import_module

from .model import Distribution, DistributionError

KNOWN_IDS = ("arch", "fedora", "ubuntu", "kali", "custom")
_DRIVERS = {
    "ubuntu": "spaces.distro.ubuntu",
    "custom": "spaces.distro.custom",
}


def get_driver(distribution_id: str) -> Distribution | None:
    module_name = _DRIVERS.get(distribution_id)
    if module_name is None:
        return None
    return import_module(module_name).DISTRIBUTION
