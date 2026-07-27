from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spaces import core
from spaces import __main__ as cli


class CliTests(unittest.TestCase):
    def test_known_unimplemented_distribution(self) -> None:
        self.assertEqual(cli.main(["create", "arch"]), 2)

    def test_create_ubuntu_builds_expected_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            (home / "Projects").mkdir()
            identity = core.Identity(1000, 1000, home)
            with (
                mock.patch.object(core, "STATE_ROOT", Path(temporary) / "state"),
                mock.patch.object(core, "initiating_identity", return_value=identity),
                mock.patch.object(
                    cli,
                    "run_permission_wizard",
                    return_value={
                        "network": "basic",
                        "home": ["Projects"],
                        "distribution_option": "resolute",
                    },
                ),
                mock.patch.object(cli, "_invoke_helper", return_value=0) as invoke,
            ):
                self.assertEqual(cli.main(["create", "ubuntu"]), 0)
        operation, payload = invoke.call_args.args
        self.assertEqual(operation, "create")
        self.assertEqual(payload["distribution"]["version"], "resolute")
        self.assertEqual(set(payload["permissions"]["users"]), {"1000"})

    def test_create_custom_uses_prompted_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            identity = core.Identity(1000, 1000, home)
            with (
                mock.patch.object(core, "STATE_ROOT", Path(temporary) / "state"),
                mock.patch.object(core, "initiating_identity", return_value=identity),
                mock.patch.object(cli, "ask_custom_name", return_value="work"),
                mock.patch.object(
                    cli,
                    "run_permission_wizard",
                    return_value={"network": "basic", "home": ["Projects"]},
                ),
                mock.patch.object(cli, "_invoke_helper", return_value=0) as invoke,
            ):
                self.assertEqual(cli.main(["create", "custom"]), 0)
        self.assertEqual(invoke.call_args.args[1]["name"], "work")
        self.assertEqual(invoke.call_args.args[1]["distribution"], {"id": "custom"})

    def test_configure_user_omits_system_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            home = Path(temporary) / "home"
            home.mkdir()
            identity = core.Identity(1000, 1000, home)
            info = core.create_info(
                "ubuntu",
                {"id": "ubuntu", "version": "resolute"},
                identity,
                "basic",
                ["Projects"],
            )
            info_path = state / "ubuntu" / "info.json"
            info_path.parent.mkdir(parents=True)
            import json

            info_path.write_text(json.dumps(info), encoding="utf-8")
            with (
                mock.patch.object(core, "STATE_ROOT", state),
                mock.patch.object(core, "initiating_identity", return_value=identity),
                mock.patch.object(
                    cli,
                    "run_permission_wizard",
                    return_value={"home": ["Projects"]},
                ) as wizard,
                mock.patch.object(cli, "_invoke_helper", return_value=0) as invoke,
            ):
                self.assertEqual(cli.main(["configure", "ubuntu", "--user"]), 0)
        self.assertFalse(wizard.call_args.kwargs["include_system"])
        patch = invoke.call_args.args[1]
        self.assertNotIn("system", patch["permissions"])

    def test_existing_space_can_cancel_before_tui(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            (state / "ubuntu").mkdir(parents=True)
            with (
                mock.patch.object(core, "STATE_ROOT", state),
                mock.patch.object(
                    core,
                    "initiating_identity",
                    return_value=core.Identity(1000, 1000, Path(temporary)),
                ),
                mock.patch.object(cli, "_confirm_rebuild", return_value=False),
                mock.patch.object(cli, "run_permission_wizard") as wizard,
            ):
                self.assertEqual(cli.main(["create", "ubuntu"]), 0)
        wizard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
