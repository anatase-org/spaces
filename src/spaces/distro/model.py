"""Class-based distribution driver contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .. import _


class DistributionError(ValueError):
    """Invalid distribution-specific configuration."""


@dataclass(frozen=True)
class Distribution:
    """Data and behavior needed to create one distribution."""

    id: str
    default_name: str | None
    configuration_title: str = ""
    configuration_description: str = ""
    option_key: str | None = None
    configuration_options: dict[str, str] = field(default_factory=dict)
    default_option: str | None = None

    def choices(self) -> list[tuple[str, str]]:
        return [
            (label, option) for option, label in self.configuration_options.items()
        ]

    def selected_option(self, metadata: Mapping[str, Any] | None) -> str | None:
        if metadata is not None and self.option_key is not None:
            selected = metadata.get(self.option_key)
            if selected in self.configuration_options:
                return str(selected)
        return self.default_option

    def metadata(self, option: str | None) -> dict[str, Any]:
        metadata: dict[str, Any] = {"id": self.id}
        if self.option_key is not None:
            if option not in self.configuration_options:
                raise DistributionError(
                    _(
                        "Unsupported {distribution} option: {option!r}.",
                        distribution=self.id,
                        option=option,
                    )
                )
            metadata[self.option_key] = option
        return metadata

    def validate(self, metadata: Mapping[str, Any]) -> None:
        if metadata.get("id") != self.id:
            raise DistributionError(
                _(
                    "{distribution} metadata has an invalid distribution ID.",
                    distribution=self.id,
                )
            )
        if self.option_key is not None:
            selected = metadata.get(self.option_key)
            if selected not in self.configuration_options:
                raise DistributionError(
                    _(
                        "Unsupported {distribution} option: {option!r}.",
                        distribution=self.id,
                        option=selected,
                    )
                )

    def bootstrap(self, metadata: Mapping[str, Any], rootfs: Path) -> None:
        self.validate(metadata)
