"""Class-based distribution driver contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .. import _


class DistributionError(ValueError):
    """Invalid distribution-specific configuration."""


@dataclass(frozen=True)
class Distribution:
    """Data and behavior needed to create one distribution."""

    id: str
    default_name: str | None
    administrator_group: str = "wheel"
    configuration_title: str = ""
    configuration_description: str = ""
    option_key: str | None = None
    configuration_options: dict[str, str] = field(default_factory=dict)
    default_option: str | None = None
    multiple_options: bool = False
    default_options: tuple[str, ...] = ()

    def choices(self) -> list[tuple[str, str]]:
        return [
            (label, option) for option, label in self.configuration_options.items()
        ]

    def selected_option(self, metadata: Mapping[str, Any] | None) -> str | None:
        if self.multiple_options:
            raise DistributionError(
                _(
                    "Distribution {distribution} uses multiple options.",
                    distribution=self.id,
                )
            )
        if metadata is not None and self.option_key is not None:
            selected = metadata.get(self.option_key)
            if selected in self.configuration_options:
                return str(selected)
        return self.default_option

    def selected_options(self, metadata: Mapping[str, Any] | None) -> list[str]:
        if not self.multiple_options:
            raise DistributionError(
                _(
                    "Distribution {distribution} uses one option.",
                    distribution=self.id,
                )
            )
        if metadata is not None and self.option_key is not None:
            selected = metadata.get(self.option_key)
            if isinstance(selected, list) and self._valid_options(selected):
                return list(selected)
        return list(self.default_options)

    def _valid_options(self, options: Sequence[object]) -> bool:
        if not all(isinstance(option, str) for option in options):
            return False
        selected = [str(option) for option in options]
        return (
            len(selected) == len(set(selected))
            and all(option in self.configuration_options for option in selected)
        )

    def metadata(
        self,
        option: str | Sequence[str] | None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {"id": self.id}
        if self.option_key is not None:
            if self.multiple_options:
                if (
                    isinstance(option, str)
                    or option is None
                    or not self._valid_options(option)
                ):
                    raise DistributionError(
                        _(
                            "Unsupported {distribution} options: {options!r}.",
                            distribution=self.id,
                            options=option,
                        )
                    )
                metadata[self.option_key] = list(option)
            elif option not in self.configuration_options:
                raise DistributionError(
                    _(
                        "Unsupported {distribution} option: {option!r}.",
                        distribution=self.id,
                        option=option,
                    )
                )
            else:
                metadata[self.option_key] = option
        return metadata

    def describe(self, metadata: Mapping[str, Any]) -> str:
        self.validate(metadata)
        return self.id

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
            if self.multiple_options:
                if not isinstance(selected, list) or not self._valid_options(selected):
                    raise DistributionError(
                        _(
                            "Unsupported {distribution} options: {options!r}.",
                            distribution=self.id,
                            options=selected,
                        )
                    )
            elif selected not in self.configuration_options:
                raise DistributionError(
                    _(
                        "Unsupported {distribution} option: {option!r}.",
                        distribution=self.id,
                        option=selected,
                    )
                )

    def bootstrap(self, metadata: Mapping[str, Any], rootfs: Path) -> None:
        self.validate(metadata)

    def reconcile_host_authentication(
        self,
        rootfs: Path,
        enabled: bool,
    ) -> bool:
        """Reconcile distro PAM integration before the rootfs is started."""

        return not enabled
