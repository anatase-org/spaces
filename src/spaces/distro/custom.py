"""Custom, user-populated distribution driver."""

from __future__ import annotations

from .model import Distribution


DISTRIBUTION = Distribution(id="custom", default_name=None)
