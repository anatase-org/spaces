"""Textual prompts used by the unprivileged Spaces client."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    RadioButton,
    RadioSet,
    SelectionList,
    Static,
)
from textual.widgets.selection_list import Selection

from .core import (
    NETWORK_LEVELS,
    SpacesError,
    validate_space_name,
)


class NamePrompt(App[str | None]):
    """Collect a safe custom space name."""

    CSS = """
    Screen { align: center middle; }
    #dialog {
        width: 90%;
        max-width: 60;
        height: auto;
        border: round $accent;
        padding: 1 2;
    }
    #name-error { color: $error; height: auto; }
    Horizontal { height: auto; align-horizontal: right; margin-top: 1; }
    Button { margin-left: 1; }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Custom space name")
            yield Input(placeholder="my-space", id="space-name", max_length=63)
            yield Static("", id="name-error")
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("Continue", id="continue", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#space-name", Input).focus()

    def action_cancel(self) -> None:
        self.exit(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.exit(None)
            return
        name = self.query_one("#space-name", Input).value.strip()
        try:
            validate_space_name(name, allow_reserved=False)
        except SpacesError as error:
            self.query_one("#name-error", Static).update(str(error))
            return
        self.exit(name)


class PermissionWizard(App[dict[str, Any] | None]):
    """Collect generic permissions and optional distribution configuration."""

    CSS = """
    Screen { align: center middle; }
    #wizard {
        width: 90%;
        max-width: 78;
        height: 90%;
        max-height: 35;
        border: round $accent;
        padding: 1 2;
    }
    .step { height: 1fr; }
    .hidden { display: none; }
    .description { color: $text-muted; margin-bottom: 1; }
    RadioSet, SelectionList { height: 1fr; }
    #buttons { height: auto; align-horizontal: right; margin-top: 1; }
    Button { margin-left: 1; }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    NETWORK_LABELS = {
        "basic": "Basic — shared networking and unprivileged ports",
        "advanced": "Advanced — shared networking and privileged ports",
        "admin": "Admin — full network admin with CAP_NET_RAW and CAP_NET_ADMIN",
    }

    def __init__(
        self,
        *,
        home: Path,
        folders: list[str],
        network: str,
        selected_home: list[str],
        include_system: bool,
        distribution_title: str,
        distribution_description: str,
        distribution_options: list[tuple[str, str]],
        distribution_value: str | None,
        submit_label: str = "Create",
    ) -> None:
        super().__init__()
        self.home = home
        self.folders = folders
        self.initial_network = network
        self.initial_home = set(selected_home)
        self.include_system = include_system
        self.distribution_title = distribution_title
        self.distribution_description = distribution_description
        self.distribution_options = distribution_options
        self.distribution_value = distribution_value
        self.submit_label = submit_label
        self.steps = (
            (["system"] if include_system else [])
            + ["user"]
            + (["distribution"] if distribution_options else [])
        )
        self.step_index = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="wizard"):
            yield Header(show_clock=False)
            with Vertical(id="system-step", classes="step"):
                yield Label("System permissions")
                yield Static(
                    "Choose the networking privileges available inside this space.",
                    classes="description",
                )
                with RadioSet(id="network"):
                    for level in NETWORK_LEVELS:
                        yield RadioButton(
                            self.NETWORK_LABELS[level],
                            value=level == self.initial_network,
                            id=f"network-{level}",
                        )
            with Vertical(id="user-step", classes="step"):
                yield Label("User permissions")
                yield Static(
                    f"Choose folders from {self.home} to make available.",
                    classes="description",
                )
                yield SelectionList(
                    *[
                        Selection(
                            f"~/{folder}",
                            folder,
                            folder in self.initial_home,
                            id=f"home-{index}",
                        )
                        for index, folder in enumerate(self.folders)
                    ],
                    id="home-folders",
                )
            with Vertical(id="distribution-step", classes="step"):
                yield Label(self.distribution_title)
                yield Static(
                    self.distribution_description, classes="description"
                )
                with RadioSet(id="distribution-option"):
                    for label, value in self.distribution_options:
                        yield RadioButton(
                            label,
                            value=value == self.distribution_value,
                            id=f"option-{value}",
                        )
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Back", id="back")
                yield Button("Next", id="next", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self._show_step()

    def action_cancel(self) -> None:
        self.exit(None)

    def _show_step(self) -> None:
        current = self.steps[self.step_index]
        for step in ("system", "user", "distribution"):
            self.query_one(f"#{step}-step").set_class(step != current, "hidden")
        self.query_one("#back", Button).disabled = self.step_index == 0
        self.query_one("#next", Button).label = (
            self.submit_label if self.step_index == len(self.steps) - 1 else "Next"
        )

    @staticmethod
    def _radio_value(radio_set: RadioSet, prefix: str) -> str:
        pressed = radio_set.pressed_button
        if pressed is None or pressed.id is None:
            raise SpacesError("A selection is required.")
        return pressed.id.removeprefix(prefix)

    def _result(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "home": list(self.query_one("#home-folders", SelectionList).selected)
        }
        if self.include_system:
            result["network"] = self._radio_value(
                self.query_one("#network", RadioSet), "network-"
            )
        if self.distribution_options:
            result["distribution_option"] = self._radio_value(
                self.query_one("#distribution-option", RadioSet), "option-"
            )
        return result

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.exit(None)
        elif event.button.id == "back":
            self.step_index -= 1
            self._show_step()
        elif self.step_index < len(self.steps) - 1:
            self.step_index += 1
            self._show_step()
        else:
            self.exit(self._result())


def ask_custom_name() -> str | None:
    return NamePrompt().run()


def run_permission_wizard(**kwargs: Any) -> dict[str, Any] | None:
    return PermissionWizard(**kwargs).run()
