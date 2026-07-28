from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from textual.color import Color
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
    async def test_initial_values_and_step_progress(self) -> None:
        app = PermissionForm(
            home=Path("/home/user"),
            folders=["Documents", "Downloads", "Projects"],
            network="advanced",
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
                app.query_one("#home-folders", SelectionList).selected,
                ["Projects", "Downloads"],
            )
            self.assertEqual(
                app.steps,
                [
                    "system",
                    "host-authentication",
                    "user",
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
            self.assertTrue(app.native_ansi_color)
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
                "System permissions (1/6)",
            )
            self.assertEqual(
                sum(not step.has_class("hidden") for step in app.query(".step")),
                1,
            )
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                str(app.query_one("#step-title").render()),
                "Host authentication permissions (2/6)",
            )
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                str(app.query_one("#step-title").render()),
                "User permissions (3/6)",
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
                "Desktop permissions (4/6)",
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
                "Administrator permissions (5/6)",
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
                "Distribution settings (6/6)",
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
                "Administrator permissions (5/6)",
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
                },
            )
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                str(app.query_one("#step-title").render()),
                "Desktop permissions (2/3)",
            )
            await pilot.press("enter")
            await pilot.pause()
            await pilot.click("#administrator-true")
            await pilot.pause()
            self.assertTrue(app._result()["administrator"])
            self.assertEqual(len(app.query("#network")), 0)
            self.assertEqual(
                str(app.query_one("#step-title").render()),
                "Administrator permissions (3/3)",
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
                    "host-authentication",
                    "user",
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
                "Space already exists (1/6)",
            )
            self.assertTrue(app.query_one("#select", Button).disabled)
            self.assertTrue(app.query_one("#next", Button).has_focus)
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                str(app.query_one("#step-title").render()),
                "System permissions (2/6)",
            )
            self.assertFalse(app.query_one("#select", Button).disabled)

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
