"""Textual prompts used by the unprivileged Spaces client."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.segment import Segment
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.color import Color
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.style import Style
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
    DEVICE_LEVELS,
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

    def render(self) -> Content:
        content = super().render()
        if self.has_class("-selected"):
            # Textual retains its blue block-cursor background beneath later
            # component styles. Apply an opaque final background here so the
            # accent and hover states cannot blend with that cached blue.
            background = Color.parse(
                self.app.theme_variables[
                    "background" if self.is_mouse_over else "accent-muted"
                ]
            )
            content = content.stylize(
                Style(background=background),
                start=len(self._button),
            )
        return content


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
    Button.-primary {
        color: $accent;
    }
    Button:focus, Button:hover {
        text-style: reverse bold;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel", priority=True),
        Binding("escape", "cancel"),
        Binding("enter", "advance", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__(ansi_color=True)

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(_("Custom space name"))
            yield Input(placeholder=_("my-space"), id="space-name", max_length=63)
            yield Static("", id="name-error")
            with Horizontal():
                yield Button(
                    Content.from_text(_("Cancel") + " [ESC]", markup=False),
                    id="cancel",
                    compact=True,
                )
                yield Button(
                    Content.from_text(
                        _("Continue") + " [ENTER]",
                        markup=False,
                    ),
                    id="continue",
                    variant="primary",
                    compact=True,
                )

    def on_mount(self) -> None:
        self.query_one("#space-name", Input).focus()

    def action_cancel(self) -> None:
        self.exit(None)

    def action_advance(self) -> None:
        name = self.query_one("#space-name", Input).value.strip()
        try:
            validate_space_name(name, allow_reserved=False)
        except SpacesError as error:
            self.query_one("#name-error", Static).update(str(error))
            return
        self.exit(name)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.action_cancel()
        elif event.button.id == "continue":
            self.action_advance()


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
    #form RadioSet, #form SelectionList {
        scrollbar-color: $accent-muted;
        scrollbar-color-hover: $accent;
        scrollbar-color-active: $accent;
        scrollbar-background: ansi_default;
        scrollbar-background-hover: ansi_default;
        scrollbar-background-active: ansi_default;
        scrollbar-corner-color: ansi_default;
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
        background: $accent-muted;
    }
    #home-folders > .option-list--option-highlighted {
        color: $ansi-foreground;
        background: $accent-muted;
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
    #next {
        margin-right: 1;
    }
    Button:hover, Button:focus, Button.-active, Button.-primary {
        border: none;
        background: ansi_default;
    }
    Button.-primary {
        color: $accent;
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
        Binding("space", "select_current", priority=True),
        Binding("backspace", "back", priority=True),
        Binding("enter", "advance", priority=True),
    ]

    NETWORK_LABELS = {
        "basic": _("Basic — shared networking and unprivileged ports"),
        "advanced": _("Advanced — shared networking and privileged ports"),
        "admin": _("Admin — full network admin with CAP_NET_RAW and CAP_NET_ADMIN"),
    }
    DEVICE_LABELS = {
        "disabled": _("Disabled — no host devices"),
        "basic": _(
            "Basic — ordinary uaccess and video devices, excluding capture "
            "inputs and security devices"
        ),
        "admin": _(
            "Admin — all device nodes except positively identified "
            "security devices"
        ),
        "full": _(
            "Full — bind the host /dev directly, including input and "
            "security devices"
        ),
    }

    def __init__(
        self,
        *,
        home: Path,
        folders: list[str],
        network: str,
        selected_home: list[str],
        devices: str = "basic",
        host_authentication: bool = True,
        include_system: bool,
        distribution_title: str,
        distribution_description: str,
        distribution_options: list[tuple[str, str]],
        distribution_value: str | None,
        administrator: bool = True,
        desktop: bool = True,
        administrator_group: str = "wheel",
        submit_label: str = _("Create"),
        override: bool = False,
    ) -> None:
        super().__init__(ansi_color=True)
        self.home = home
        self.initial_network = network
        self.initial_devices = devices
        self.initial_host_authentication = host_authentication
        self.initial_home = set(selected_home)
        self.initial_administrator = administrator
        self.initial_desktop = desktop
        self.administrator_group = administrator_group
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
            (["override"] if override else [])
            + (
                ["system", "devices", "host-authentication"]
                if include_system
                else []
            )
            + ["user", "desktop", "administrator"]
            + (["distribution"] if distribution_options else [])
        )
        self.step_index = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="form"):
            yield Label("", id="step-title")
            if "override" in self.steps:
                with Vertical(id="override-step", classes="step"):
                    yield Static(
                        _(
                            "The existing space will be overwritten. "
                            "Its home data will be preserved."
                        ),
                        classes="description",
                    )
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
                with Vertical(
                    id="devices-step",
                    classes="step",
                ):
                    yield Static(
                        _(
                            "Choose which host devices are available inside "
                            "this space."
                        ),
                        classes="description",
                    )
                    with RadioSet(id="devices"):
                        for level in DEVICE_LEVELS:
                            yield CleanRadioButton(
                                self.DEVICE_LABELS[level],
                                value=level == self.initial_devices,
                                id=f"devices-{level}",
                            )
                with Vertical(
                    id="host-authentication-step",
                    classes="step",
                ):
                    yield Static(
                        _(
                            "Should authentication requests be handled by the system (use your system password for sudo)?"
                        ),
                        classes="description",
                    )
                    with RadioSet(id="host-authentication"):
                        yield CleanRadioButton(
                            _("Do not use host authentication"),
                            value=not self.initial_host_authentication,
                            id="host-authentication-false",
                        )
                        yield CleanRadioButton(
                            _("Use host authentication"),
                            value=self.initial_host_authentication,
                            id="host-authentication-true",
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
            with Vertical(id="desktop-step", classes="step"):
                yield Static(
                    _(
                        "Choose whether the host graphical login is available "
                        "inside the space."
                    ),
                    classes="description",
                )
                with RadioSet(id="desktop"):
                    yield CleanRadioButton(
                        _(
                            "Do not enable desktop (GUI) apps for {user} user",
                            user=self.home.name,
                        ),
                        value=not self.initial_desktop,
                        id="desktop-false",
                    )
                    yield CleanRadioButton(
                        _(
                            "Enable desktop (GUI) apps to work for {user} user",
                            user=self.home.name,
                        ),
                        value=self.initial_desktop,
                        id="desktop-true",
                    )
            with Vertical(id="administrator-step", classes="step"):
                yield Static(
                    _(
                        "Choose whether {name} is an administrator inside "
                        "the space (part of {group} group).",
                        name=self.home.name,
                        group=self.administrator_group,
                    ),
                    classes="description",
                )
                with RadioSet(id="administrator"):
                    yield CleanRadioButton(
                        _("User {name} is not administrator", name=self.home.name),
                        value=not self.initial_administrator,
                        id="administrator-false",
                    )
                    yield CleanRadioButton(
                        _("User {name} is administrator", name=self.home.name),
                        value=self.initial_administrator,
                        id="administrator-true",
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
                yield Button(
                    Content.from_text(_("Cancel") + " [ESC]", markup=False),
                    id="cancel",
                    compact=True,
                )
                yield Button(
                    Content.from_text(
                        _("Back") + " [BACKSPACE]",
                        markup=False,
                    ),
                    id="back",
                    compact=True,
                )
                yield Button(
                    Content.from_text(_("Select") + " [SPACE]", markup=False),
                    id="select",
                    compact=True,
                )
                yield Button(
                    Content.from_text(_("Next") + " [ENTER]", markup=False),
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
            "override": _("Space already exists"),
            "system": _("System permissions"),
            "devices": _("Device permissions"),
            "host-authentication": _("Host authentication permissions"),
            "user": _("User permissions"),
            "desktop": _("Desktop permissions"),
            "administrator": _("Administrator permissions"),
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
        next_label = (
            self.submit_label
            if self.step_index == len(self.steps) - 1
            else _("Next")
        )
        next_button = self.query_one("#next", Button)
        next_button.label = Content.from_text(
            next_label + " [ENTER]",
            markup=False,
        )
        next_button.refresh(layout=True)
        select_button = self.query_one("#select", Button)
        select_button.disabled = current == "override"
        focus_targets = {
            "system": "#network",
            "devices": "#devices",
            "host-authentication": "#host-authentication",
            "user": "#home-folders",
            "desktop": "#desktop",
            "administrator": "#administrator",
            "distribution": "#distribution-option",
        }
        if current == "override":
            next_button.focus()
            return
        focus_target = self.query_one(focus_targets[current])
        if isinstance(focus_target, RadioSet):
            pressed_index = focus_target.pressed_index
            if pressed_index >= 0:
                focus_target._selected = pressed_index
        focus_target.focus()

    def action_select_current(self) -> None:
        """Select or toggle the highlighted option on the current step."""

        current = self.steps[self.step_index]
        if current == "override":
            return
        if current == "user":
            self.query_one(
                "#home-folders", FolderSelectionList
            ).action_select()
        else:
            targets = {
                "system": "#network",
                "devices": "#devices",
                "host-authentication": "#host-authentication",
                "desktop": "#desktop",
                "administrator": "#administrator",
                "distribution": "#distribution-option",
            }
            target = targets[current]
            self.query_one(target, RadioSet).action_toggle_button()

    def action_advance(self) -> None:
        """Advance to the next step or submit the completed form."""

        if self.step_index < len(self.steps) - 1:
            self.step_index += 1
            self._show_step()
        else:
            self.exit(self._result())

    def action_back(self) -> None:
        """Return to the previous step."""

        if self.step_index > 0:
            self.step_index -= 1
            self._show_step()

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
            ),
            "administrator": self._radio_value(
                self.query_one("#administrator", RadioSet),
                "administrator-",
            )
            == "true",
            "desktop": self._radio_value(
                self.query_one("#desktop", RadioSet),
                "desktop-",
            )
            == "true",
        }
        if self.include_system:
            result["network"] = self._radio_value(
                self.query_one("#network", RadioSet), "network-"
            )
            result["devices"] = self._radio_value(
                self.query_one("#devices", RadioSet), "devices-"
            )
            result["host_authentication"] = self._radio_value(
                self.query_one("#host-authentication", RadioSet),
                "host-authentication-",
            ) == "true"
        if self.distribution_options:
            result["distribution_option"] = self._radio_value(
                self.query_one("#distribution-option", RadioSet), "option-"
            )
        return result

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.exit(None)
        elif event.button.id == "back":
            self.action_back()
        elif event.button.id == "select":
            self.action_select_current()
        elif event.button.id == "next":
            self.action_advance()


def ask_custom_name() -> str | None:
    return NamePrompt().run(inline=True)


def run_permission_wizard(**kwargs: Any) -> dict[str, Any] | None:
    return PermissionForm(**kwargs).run(inline=True)
