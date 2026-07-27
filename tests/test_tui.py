from __future__ import annotations

import unittest
from pathlib import Path

from textual.widgets import RadioSet, SelectionList

from spaces.tui import PermissionWizard


class TuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_values_and_steps(self) -> None:
        app = PermissionWizard(
            home=Path("/home/user"),
            folders=["Documents", "Projects"],
            network="advanced",
            selected_home=["Projects"],
            include_system=True,
            distribution_title="Ubuntu version",
            distribution_description="Choose the Ubuntu release to bootstrap.",
            distribution_options=[
                ("Noble (24.04)", "noble"),
                ("Resolute (26.04)", "resolute"),
            ],
            distribution_value="noble",
            submit_label="Create",
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(app.steps, ["system", "user", "distribution"])
            self.assertEqual(
                app.query_one("#network", RadioSet).pressed_button.id,
                "network-advanced",
            )
            self.assertEqual(
                app.query_one("#distribution-option", RadioSet).pressed_button.id,
                "option-noble",
            )
            self.assertEqual(
                app.query_one("#home-folders", SelectionList).selected,
                ["Projects"],
            )

    async def test_user_only_has_one_step(self) -> None:
        app = PermissionWizard(
            home=Path("/home/user"),
            folders=["Projects"],
            network="basic",
            selected_home=["Projects"],
            include_system=False,
            distribution_title="",
            distribution_description="",
            distribution_options=[],
            distribution_value=None,
            submit_label="Configure",
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(app.steps, ["user"])
            self.assertEqual(app._result(), {"home": ["Projects"]})
            self.assertEqual(str(app.query_one("#next").label), "Configure")


if __name__ == "__main__":
    unittest.main()
