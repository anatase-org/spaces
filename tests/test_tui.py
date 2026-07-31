from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from textual.color import Color
from textual.geometry import Offset
from textual.widgets import Button, Footer, Header, RadioSet, SelectionList

from spaces.tui import (
    CleanRadioButton,
    FolderSelectionList,
    NamePrompt,
    PermissionForm,
    ask_custom_name,
    run_permission_wizard,
)


class TuiTests(unittest.IsolatedAsyncioTestCase):
    def test_permission_form_uses_textual_4_theme_variables(self) -> None:
        self.assertNotIn("$ansi-foreground", PermissionForm.CSS)
        self.assertIn("color: $foreground;", PermissionForm.CSS)

    def test_selected_home_entries_are_listed_first_by_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "notes.txt").touch()
            (home / ".bashrc").touch()
            app = PermissionForm(
                home=home,
                folders=["notes.txt", ".bashrc", "Documents", "Projects"],
                network="basic",
                selected_home=[".bashrc", "Projects"],
                include_system=False,
                distribution_title="",
                distribution_description="",
                distribution_options=[],
                distribution_value=None,
            )
            self.assertEqual(
                app.folders,
                ["Projects", ".bashrc", "Documents", "notes.txt"],
            )

    async def test_initial_values_and_step_progress(self) -> None:
        app = PermissionForm(
            home=Path("/home/user"),
            folders=["Documents", "Downloads", "Projects"],
            network="advanced",
            kernel_capabilities="development",
            devices="admin",
            selected_home=["Downloads", "Projects"],
            administrator=True,
            administrator_group="sudo",
            include_system=True,
            distribution_title="Ubuntu version",
            distribution_description="Choose the Ubuntu release to bootstrap.",
            distribution_options=[
                ("Noble (24.04)", "noble"),
                ("Resolute (26.04)", "resolute"),
            ],
            distribution_value="resolute",
            submit_label="Create",
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(
                app.query_one("#network", RadioSet).pressed_button.id,
                "network-advanced",
            )
            for radio in app.query(CleanRadioButton):
                rendered = radio.render().plain
                self.assertNotIn("▐", rendered)
                self.assertNotIn("▌", rendered)
            self.assertEqual(
                app.query_one("#distribution-option", RadioSet).pressed_button.id,
                "option-resolute",
            )
            network = app.query_one("#network", RadioSet)
            self.assertEqual(network._selected, network.pressed_index)
            self.assertEqual(
                [
                    button.label.plain
                    for button in network.query(CleanRadioButton)
                ],
                [
                    "Basic — shared networking and unprivileged ports",
                    "Advanced — shared networking and privileged ports",
                    "Admin — required for Docker; full network admin with "
                    "CAP_NET_RAW and CAP_NET_ADMIN",
                ],
            )
            self.assertEqual(
                app.query_one("#home-folders", SelectionList).selected,
                ["Projects", "Downloads"],
            )
            self.assertEqual(
                app.steps,
                [
                    "system",
                    "kernel-capabilities",
                    "devices",
                    "host-authentication",
                    "shortcuts",
                    "user",
                    "credential-agents",
                    "desktop",
                    "administrator",
                    "distribution",
                ],
            )
            self.assertEqual(
                app.query_one(
                    "#host-authentication", RadioSet
                ).pressed_button.id,
                "host-authentication-true",
            )
            self.assertTrue(app.ansi_color)
            variables = app.get_css_variables()
            selected_label_background = next(
                segment.style.bgcolor
                for segment in network.pressed_button.render_line(0)
                if "Advanced" in segment.text
            )
            self.assertEqual(
                selected_label_background,
                Color.parse(variables["accent-muted"]).rich_color,
            )
            self.assertTrue(
                all(button.region.height == 1 for button in app.query(Button))
            )
            self.assertTrue(
                all(button.region.width <= 20 for button in app.query(Button))
            )
            self.assertEqual(
                app.query_one("#cancel", Button).label.plain,
                "Cancel [ESC]",
            )
            self.assertEqual(
                app.query_one("#select", Button).label.plain,
                "Select [SPACE]",
            )
            self.assertEqual(
                app.query_one("#back", Button).label.plain,
                "Back [BACKSPACE]",
            )
            self.assertEqual(
                app.query_one("#next", Button).label.plain,
                "Next [ENTER]",
            )
            self.assertEqual(
                str(app.query_one("#step-title").render()),
                "System permissions (1/10)",
            )
            self.assertEqual(
                sum(not step.has_class("hidden") for step in app.query(".step")),
                1,
            )
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                str(app.query_one("#step-title").render()),
                "Kernel capabilities (2/10)",
            )
            kernel_capabilities = app.query_one(
                "#kernel-capabilities",
                RadioSet,
            )
            self.assertEqual(
                kernel_capabilities.pressed_button.id,
                "kernel-capabilities-development",
            )
            self.assertEqual(
                app._result()["kernel_capabilities"],
                "development",
            )
            self.assertEqual(
                [
                    button.label.plain
                    for button in kernel_capabilities.query(
                        CleanRadioButton
                    )
                ],
                [
                    "Basic — Essentials for daily work",
                    "Development — required for Docker; adds "
                    "CAP_AUDIT_CONTROL, CAP_AUDIT_WRITE, CAP_SYS_PTRACE, "
                    "CAP_PERFMON, CAP_BPF, and perf_event_open",
                    "System Administrator — Development plus /sys is "
                    "writeable",
                ],
            )
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                str(app.query_one("#step-title").render()),
                "Device permissions (3/10)",
            )
            devices = app.query_one("#devices", RadioSet)
            self.assertEqual(devices.pressed_button.id, "devices-admin")
            self.assertEqual(app._result()["devices"], "admin")
            self.assertEqual(
                [
                    button.label.plain
                    for button in devices.query(CleanRadioButton)
                ],
                [
                    "Disabled — no host devices",
                    "Basic — ordinary uaccess and video devices, excluding "
                    "capture inputs and security devices",
                    "Admin — all device nodes except positively identified "
                    "security devices",
                    "Full — bind the host /dev directly, including input "
                    "and security devices",
                ],
            )
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                str(app.query_one("#step-title").render()),
                "Host authentication permissions (4/10)",
            )
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                str(app.query_one("#step-title").render()),
                "Application shortcuts permissions (5/10)",
            )
            shortcuts = app.query_one("#shortcuts", RadioSet)
            self.assertEqual(shortcuts.pressed_button.id, "shortcuts-true")
            self.assertTrue(app._result()["shortcuts"])
            self.assertEqual(
                [
                    button.label.plain
                    for button in shortcuts.query(CleanRadioButton)
                ],
                [
                    "Do not create application shortcuts",
                    "Create desktop application shortcuts",
                ],
            )
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                str(app.query_one("#step-title").render()),
                "User permissions (6/10)",
            )
            folders = app.query_one("#home-folders", FolderSelectionList)
            self.assertEqual(
                [
                    folders.get_option_at_index(index).value
                    for index in range(folders.option_count)
                ],
                ["Projects", "Downloads", "Documents"],
            )
            markers = [
                "".join(segment.text for segment in folders.render_line(index))[0]
                for index in range(folders.option_count)
            ]
            self.assertEqual(markers, ["■", "■", "□"])
            selected_radio = app.query_one(
                "#network", RadioSet
            ).pressed_button.get_component_rich_style("toggle--button")
            selected_folder = folders.get_component_rich_style(
                "selection-list--button-selected"
            )
            self.assertEqual(selected_folder.color, selected_radio.color)
            await pilot.hover("#select")
            await pilot.click("#select")
            await pilot.pause()
            self.assertEqual(folders.selected, ["Downloads"])
            await pilot.press("space")
            await pilot.pause()
            self.assertEqual(set(folders.selected), {"Projects", "Downloads"})
            self.assertEqual(
                sum(not step.has_class("hidden") for step in app.query(".step")),
                1,
            )
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                str(app.query_one("#step-title").render()),
                "Forward Credentials Agents (7/10)",
            )
            credential_agents = app.query_one(
                "#credential-agents", RadioSet
            )
            self.assertEqual(
                credential_agents.pressed_button.id,
                "credential-agents-true",
            )
            self.assertEqual(
                [
                    button.label.plain
                    for button in credential_agents.query(CleanRadioButton)
                ],
                [
                    "No",
                    "Yes — Share my GPG, SSH, and compatible credential agents",
                ],
            )
            self.assertTrue(app._result()["credential_agents"])
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                str(app.query_one("#step-title").render()),
                "Desktop permissions (8/10)",
            )
            desktop = app.query_one("#desktop", RadioSet)
            self.assertEqual(desktop.pressed_button.id, "desktop-true")
            self.assertEqual(
                [
                    button.label.plain
                    for button in desktop.query(CleanRadioButton)
                ],
                [
                    "Do not enable desktop (GUI) apps for user user",
                    "Enable desktop (GUI) apps to work for user user",
                ],
            )
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                str(app.query_one("#step-title").render()),
                "Administrator permissions (9/10)",
            )
            administrator = app.query_one("#administrator", RadioSet)
            self.assertEqual(
                administrator.pressed_button.id,
                "administrator-true",
            )
            self.assertEqual(
                [
                    button.label.plain
                    for button in administrator.query(CleanRadioButton)
                ],
                ["User user is not administrator", "User user is administrator"],
            )
            self.assertEqual(
                str(app.query_one("#administrator-step Static").render()),
                "Choose whether user is an administrator inside the space "
                "(part of sudo group).",
            )
            self.assertEqual(administrator._selected, administrator.pressed_index)
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                str(app.query_one("#step-title").render()),
                "Distribution settings (10/10)",
            )
            distribution = app.query_one("#distribution-option", RadioSet)
            self.assertEqual(distribution._selected, distribution.pressed_index)
            self.assertEqual(
                app.query_one("#next", Button).label.plain,
                "Create [ENTER]",
            )
            self.assertIn(
                "[ENTER]",
                "".join(
                    segment.text
                    for segment in app.query_one("#next", Button).render_line(0)
                ),
            )
            await pilot.press("backspace")
            await pilot.pause()
            self.assertEqual(
                str(app.query_one("#step-title").render()),
                "Administrator permissions (9/10)",
            )
            self.assertEqual(len(app.query(Header)), 0)
            self.assertEqual(len(app.query(Footer)), 0)
            self.assertFalse(app.ENABLE_COMMAND_PALETTE)
            self.assertNotIn("ctrl+q", app._bindings.key_to_bindings)
            self.assertEqual(
                app._bindings.key_to_bindings["space"][0].action,
                "select_current",
            )
            self.assertEqual(
                app._bindings.key_to_bindings["enter"][0].action,
                "advance",
            )
            self.assertEqual(
                app._bindings.key_to_bindings["backspace"][0].action,
                "back",
            )

    async def test_selected_radio_renders_with_mouse_outside_screen(self) -> None:
        app = PermissionForm(
            home=Path("/home/user"),
            folders=[],
            network="advanced",
            selected_home=[],
            include_system=True,
            distribution_title="",
            distribution_description="",
            distribution_options=[],
            distribution_value=None,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            selected = app.query_one("#network", RadioSet).pressed_button
            app.mouse_position = Offset(0, -1000)
            selected.render()

    async def test_radio_markers_use_terminal_background(self) -> None:
        app = PermissionForm(
            home=Path("/home/user"),
            folders=[],
            network="advanced",
            selected_home=[],
            include_system=True,
            distribution_title="",
            distribution_description="",
            distribution_options=[],
            distribution_value=None,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            for radio in app.query(CleanRadioButton):
                marker_style = radio.get_visual_style("toggle--button")
                self.assertEqual(marker_style.background.ansi, -1)

    async def test_outer_scrollbar_uses_terminal_palette(self) -> None:
        app = PermissionForm(
            home=Path("/home/user"),
            folders=[],
            network="advanced",
            selected_home=[],
            include_system=True,
            distribution_title="",
            distribution_description="",
            distribution_options=[],
            distribution_value=None,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(
                app.screen.styles.scrollbar_color,
                Color.parse(app.theme_variables["accent-muted"]),
            )
            self.assertEqual(
                app.screen.styles.scrollbar_background.ansi,
                -1,
            )

    async def test_arch_distribution_options_are_checkboxes(self) -> None:
        app = PermissionForm(
            home=Path("/home/user"),
            folders=["Projects"],
            network="basic",
            selected_home=[],
            administrator=True,
            include_system=False,
            distribution_title="Arch options",
            distribution_description="Choose optional software.",
            distribution_options=[
                ("yay — AUR helper (built from community source)", "yay"),
                ("Shelly — graphical package manager", "shelly"),
            ],
            distribution_value=None,
            distribution_multiple=True,
            distribution_values=["yay"],
            submit_label="Create",
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(4):
                await pilot.press("enter")
                await pilot.pause()
            options = app.query_one(
                "#distribution-options",
                FolderSelectionList,
            )
            self.assertEqual(options.selected, ["yay"])
            self.assertEqual(app._result()["distribution_options"], ["yay"])
            self.assertEqual(len(app.query("#distribution-option")), 0)
            await pilot.press("space")
            await pilot.pause()
            self.assertEqual(options.selected, [])
            self.assertEqual(app._result()["distribution_options"], [])

    async def test_user_only_omits_system_controls(self) -> None:
        app = PermissionForm(
            home=Path("/home/user"),
            folders=["Projects"],
            network="basic",
            selected_home=["Projects"],
            administrator=False,
            include_system=False,
            distribution_title="",
            distribution_description="",
            distribution_options=[],
            distribution_value=None,
            submit_label="Configure",
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(
                app._result(),
                {
                    "home": ["Projects"],
                    "administrator": False,
                    "desktop": True,
                    "credential_agents": True,
                },
            )
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                str(app.query_one("#step-title").render()),
                "Forward Credentials Agents (2/4)",
            )
            await pilot.click("#credential-agents-false")
            await pilot.pause()
            self.assertFalse(app._result()["credential_agents"])
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                str(app.query_one("#step-title").render()),
                "Desktop permissions (3/4)",
            )
            await pilot.press("enter")
            await pilot.pause()
            await pilot.click("#administrator-true")
            await pilot.pause()
            self.assertTrue(app._result()["administrator"])
            self.assertEqual(len(app.query("#network")), 0)
            self.assertEqual(len(app.query("#kernel-capabilities")), 0)
            self.assertEqual(
                str(app.query_one("#step-title").render()),
                "Administrator permissions (4/4)",
            )
            self.assertEqual(
                app.query_one("#next", Button).label.plain,
                "Configure [ENTER]",
            )
            self.assertLess(
                app.query_one("#next", Button).region.right,
                app.screen.region.right,
            )

    async def test_ctrl_c_cancels(self) -> None:
        app = PermissionForm(
            home=Path("/home/user"),
            folders=["Projects"],
            network="basic",
            selected_home=[],
            administrator=True,
            include_system=False,
            distribution_title="",
            distribution_description="",
            distribution_options=[],
            distribution_value=None,
        )
        async with app.run_test() as pilot:
            await pilot.press("ctrl+c")
        self.assertIsNone(app.return_value)

    async def test_name_prompt_continue_uses_enter(self) -> None:
        app = NamePrompt()
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(
                app.query_one("#continue", Button).label.plain,
                "Continue [ENTER]",
            )
            await pilot.click("#space-name")
            await pilot.press("w", "o", "r", "k", "enter")
        self.assertEqual(app.return_value, "work")

    async def test_override_step_is_prepended(self) -> None:
        app = PermissionForm(
            home=Path("/home/user"),
            folders=["Projects"],
            network="basic",
            selected_home=[],
            administrator=True,
            include_system=True,
            distribution_title="",
            distribution_description="",
            distribution_options=[],
            distribution_value=None,
            override=True,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(
                app.steps,
                [
                    "override",
                    "system",
                    "kernel-capabilities",
                    "devices",
                    "host-authentication",
                    "shortcuts",
                    "user",
                    "credential-agents",
                    "desktop",
                    "administrator",
                ],
            )
            self.assertEqual(
                str(app.query_one("#override-step Static").render()),
                "The existing space will be overwritten. "
                "Its home data will be preserved.",
            )
            self.assertEqual(
                str(app.query_one("#step-title").render()),
                "Space already exists (1/10)",
            )
            self.assertTrue(app.query_one("#select", Button).disabled)
            self.assertTrue(app.query_one("#next", Button).has_focus)
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                str(app.query_one("#step-title").render()),
                "System permissions (2/10)",
            )
            self.assertFalse(app.query_one("#select", Button).disabled)

    async def test_missing_step_is_prepended(self) -> None:
        app = PermissionForm(
            home=Path("/home/user"),
            folders=["Projects"],
            network="basic",
            selected_home=[],
            administrator=True,
            include_system=True,
            distribution_title="",
            distribution_description="",
            distribution_options=[],
            distribution_value=None,
            missing=True,
            space_name="ubuntu",
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(
                app.steps,
                [
                    "missing",
                    "system",
                    "kernel-capabilities",
                    "devices",
                    "host-authentication",
                    "shortcuts",
                    "user",
                    "credential-agents",
                    "desktop",
                    "administrator",
                ],
            )
            self.assertEqual(
                str(app.query_one("#missing-step Static").render()),
                "Space ubuntu does not exist. It must be created "
                "before you can enter it.",
            )
            self.assertEqual(
                str(app.query_one("#step-title").render()),
                "Space does not exist (1/10)",
            )
            self.assertTrue(app.query_one("#select", Button).disabled)
            self.assertTrue(app.query_one("#next", Button).has_focus)
            await pilot.press("space")
            self.assertEqual(app.step_index, 0)
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                str(app.query_one("#step-title").render()),
                "System permissions (2/10)",
            )
            self.assertFalse(app.query_one("#select", Button).disabled)

    async def test_override_warning_takes_precedence_over_missing(self) -> None:
        app = PermissionForm(
            home=Path("/home/user"),
            folders=[],
            network="basic",
            selected_home=[],
            include_system=False,
            distribution_title="",
            distribution_description="",
            distribution_options=[],
            distribution_value=None,
            override=True,
            missing=True,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(app.steps[0], "override")
            self.assertNotIn("missing", app.steps)

    def test_name_prompt_runs_inline(self) -> None:
        with mock.patch.object(NamePrompt, "run", return_value="work") as run:
            self.assertEqual(ask_custom_name(), "work")
        run.assert_called_once_with(inline=True)

    def test_permission_form_runs_inline(self) -> None:
        with mock.patch("spaces.tui.PermissionForm") as form:
            form.return_value.run.return_value = None
            self.assertIsNone(run_permission_wizard())
        form.return_value.run.assert_called_once_with(inline=True)


if __name__ == "__main__":
    unittest.main()
