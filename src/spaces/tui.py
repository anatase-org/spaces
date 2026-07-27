"""Textual prompts used by the unprivileged Spaces client."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.segment import Segment
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.strip import Strip
from textual.widgets import (
    Button,
    Input,
    Label,
    RadioButton,
    RadioSet,
    SelectionList,
    Static,
)
from textual.widgets.selection_list import Selection

from . import _
from .core import (
    DEFAULT_HOME_FOLDERS,
    NETWORK_LEVELS,
    SpacesError,
    validate_space_name,
)


class FolderSelectionList(SelectionList[str]):
    """A folder list with conventional checkbox symbols."""

    def render_line(self, y: int) -> Strip:
        line = super().render_line(y)
        segments = list(line)
        if len(segments) < 4:
            return line

        _, scroll_y = self.scroll_offset
        option_index = scroll_y + y
        if not 0 <= option_index < self.option_count:
            return line
        option = self.get_option_at_index(option_index)

        marker = "■" if option.value in self.selected else "□"
        return Strip(
            [
                Segment(marker, style=segments[1].style),
                Segment(" ", style=segments[3].style),
                *segments[4:],
            ]
        )


class CleanRadioButton(RadioButton):
    """A radio button without Textual's decorative side bars."""

    BUTTON_LEFT = ""
    BUTTON_RIGHT = " "


class NamePrompt(App[str | None], inherit_bindings=False):
    """Collect a safe custom space name."""

    ENABLE_COMMAND_PALETTE = False
    INLINE_PADDING = 0

    CSS = """
    Screen {
        height: auto;
        border: none;
        background: ansi_default;
        color: ansi_default;
    }
    #dialog {
        width: 100%;
        max-width: 60;
        height: auto;
        padding: 0 1;
    }
    #name-error { color: $error; height: auto; }
    Horizontal { height: auto; align-horizontal: right; margin-top: 1; }
    Button {
        min-width: 0;
        height: 1;
        padding: 0 1;
        margin-left: 1;
        border: none;
        background: ansi_default;
    }
    Button:hover, Button:focus, Button.-active, Button.-primary {
        border: none;
        background: ansi_default;
    }
    Button:focus, Button:hover {
        text-style: reverse bold;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel", priority=True),
        Binding("escape", "cancel"),
    ]

    def __init__(self) -> None:
        super().__init__(ansi_color=True)

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(_("Custom space name"))
            yield Input(placeholder=_("my-space"), id="space-name", max_length=63)
            yield Static("", id="name-error")
            with Horizontal():
                yield Button(_("Cancel"), id="cancel", compact=True)
                yield Button(
                    _("Continue"),
                    id="continue",
                    variant="primary",
                    compact=True,
                )

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


class PermissionForm(
    App[dict[str, Any] | None],
    inherit_bindings=False,
):
    """Collect permissions and distribution configuration in an inline form."""

    ENABLE_COMMAND_PALETTE = False
    INLINE_PADDING = 0

    CSS = """
    Screen {
        height: auto;
        max-height: 90vh;
        border: none;
        background: ansi_default;
        color: ansi_default;
    }
    #form {
        width: 100%;
        max-width: 78;
        height: auto;
        padding: 0 1;
    }
    #step-title {
        text-style: bold;
    }
    .step {
        height: auto;
        margin-bottom: 1;
    }
    .hidden {
        display: none;
    }
    .description {
        color: $text-muted;
        height: auto;
    }
    RadioSet, SelectionList {
        height: auto;
        max-height: 12;
        border: none;
        background: transparent;
    }
    #home-folders > .selection-list--button,
    #home-folders > .selection-list--button-highlighted {
        color: $ansi-foreground;
        background: ansi_default;
        text-style: dim;
    }
    #home-folders > .selection-list--button-selected,
    #home-folders > .selection-list--button-selected-highlighted {
        color: $accent;
        background: ansi_default;
        text-style: bold not dim;
    }
    #home-folders > .selection-list--button-highlighted,
    #home-folders > .selection-list--button-selected-highlighted {
        background: $block-cursor-background;
    }
    #buttons {
        height: auto;
        align-horizontal: right;
    }
    Button {
        min-width: 0;
        height: 1;
        padding: 0 1;
        margin-left: 1;
        border: none;
        background: ansi_default;
    }
    Button:hover, Button:focus, Button.-active, Button.-primary {
        border: none;
        background: ansi_default;
    }
    Button:focus, Button:hover {
        text-style: reverse bold;
    }
    Button:disabled {
        border: none;
        background: ansi_default;
        text-style: dim;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel", priority=True),
        Binding("escape", "cancel"),
    ]

    NETWORK_LABELS = {
        "basic": _("Basic — shared networking and unprivileged ports"),
        "advanced": _("Advanced — shared networking and privileged ports"),
        "admin": _("Admin — full network admin with CAP_NET_RAW and CAP_NET_ADMIN"),
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
        submit_label: str = _("Create"),
    ) -> None:
        super().__init__(ansi_color=True)
        self.home = home
        self.initial_network = network
        self.initial_home = set(selected_home)
        selected_folders = [
            folder
            for folder in (*DEFAULT_HOME_FOLDERS, *selected_home)
            if folder in self.initial_home and folder in folders
        ]
        selected_folders = list(dict.fromkeys(selected_folders))
        self.folders = [
            *selected_folders,
            *[folder for folder in folders if folder not in self.initial_home],
        ]
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
        with Vertical(id="form"):
            yield Label("", id="step-title")
            if self.include_system:
                with Vertical(id="system-step", classes="step"):
                    yield Static(
                        _(
                            "Choose the networking privileges available inside this "
                            "space."
                        ),
                        classes="description",
                    )
                    with RadioSet(id="network"):
                        for level in NETWORK_LEVELS:
                            yield CleanRadioButton(
                                self.NETWORK_LABELS[level],
                                value=level == self.initial_network,
                                id=f"network-{level}",
                            )
            with Vertical(id="user-step", classes="step"):
                yield Static(
                    _(
                        "Choose folders from {home} to make available.",
                        home=self.home,
                    ),
                    classes="description",
                )
                yield FolderSelectionList(
                    *[
                        Selection(
                            _("~/{folder}", folder=folder),
                            folder,
                            folder in self.initial_home,
                            id=f"home-{index}",
                        )
                        for index, folder in enumerate(self.folders)
                    ],
                    id="home-folders",
                )
            if self.distribution_options:
                with Vertical(id="distribution-step", classes="step"):
                    yield Label(self.distribution_title)
                    yield Static(
                        self.distribution_description, classes="description"
                    )
                    with RadioSet(id="distribution-option"):
                        for label, value in self.distribution_options:
                            yield CleanRadioButton(
                                label,
                                value=value == self.distribution_value,
                                id=f"option-{value}",
                            )
            with Horizontal(id="buttons"):
                yield Button(_("Cancel"), id="cancel", compact=True)
                yield Button(_("Back"), id="back", compact=True)
                yield Button(
                    _("Next"),
                    id="next",
                    variant="primary",
                    compact=True,
                )

    def on_mount(self) -> None:
        self._show_step()

    def action_cancel(self) -> None:
        self.exit(None)

    def _show_step(self) -> None:
        current = self.steps[self.step_index]
        for step in self.steps:
            self.query_one(f"#{step}-step").set_class(step != current, "hidden")

        titles = {
            "system": _("System permissions"),
            "user": _("User permissions"),
            "distribution": _("Distribution settings"),
        }
        self.query_one("#step-title", Label).update(
            _(
                "{title} ({current}/{total})",
                title=titles[current],
                current=self.step_index + 1,
                total=len(self.steps),
            )
        )
        self.query_one("#back", Button).disabled = self.step_index == 0
        self.query_one("#next", Button).label = (
            self.submit_label
            if self.step_index == len(self.steps) - 1
            else _("Next")
        )
        focus_targets = {
            "system": "#network",
            "user": "#home-folders",
            "distribution": "#distribution-option",
        }
        self.query_one(focus_targets[current]).focus()

    @staticmethod
    def _radio_value(radio_set: RadioSet, prefix: str) -> str:
        pressed = radio_set.pressed_button
        if pressed is None or pressed.id is None:
            raise SpacesError(_("A selection is required."))
        return pressed.id.removeprefix(prefix)

    def _result(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "home": list(
                self.query_one("#home-folders", FolderSelectionList).selected
            )
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
        elif event.button.id == "next":
            if self.step_index < len(self.steps) - 1:
                self.step_index += 1
                self._show_step()
            else:
                self.exit(self._result())


def ask_custom_name() -> str | None:
    return NamePrompt().run(inline=True)


def run_permission_wizard(**kwargs: Any) -> dict[str, Any] | None:
    return PermissionForm(**kwargs).run(inline=True)
