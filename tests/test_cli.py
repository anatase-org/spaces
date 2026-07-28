from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spaces import core
from spaces import __main__ as cli


class CliTests(unittest.TestCase):
    def test_helper_output_is_streamed_after_privilege_handoff(self) -> None:
        command = ["pkexec", "/usr/bin/spaces.priv", "create", "{}"]
        completed = subprocess.CompletedProcess(command, 42)

        with (
            mock.patch.object(cli, "_helper_command", return_value=command),
            mock.patch.object(cli, "configure_logging") as configure_logging,
            mock.patch.object(
                cli, "run_streamed", return_value=completed
            ) as run_streamed,
        ):
            returncode = cli._invoke_helper("create", {})

        configure_logging.assert_called_once_with(rich=True)
        run_streamed.assert_called_once_with(command, check=False)
        self.assertEqual(returncode, 42)

    def test_cp_helper_keeps_callers_working_directory(self) -> None:
        with (
            mock.patch.object(cli.os, "geteuid", return_value=1000),
            mock.patch.object(
                cli.shutil,
                "which",
                side_effect=lambda name: f"/usr/bin/{name}",
            ),
        ):
            command = cli._helper_command("cp", {"arguments": ["source", "dest"]})

        self.assertEqual(
            command[:3],
            ["/usr/bin/pkexec", "--keep-cwd", "/usr/bin/spaces.priv"],
        )

    def test_raw_helper_inherits_terminal_streams(self) -> None:
        command = ["pkexec", "/usr/bin/spaces.priv", "enter", "alice@work"]
        completed = subprocess.CompletedProcess(command, 42)
        with (
            mock.patch.object(
                cli, "_raw_helper_command", return_value=command
            ),
            mock.patch.object(
                cli.subprocess, "run", return_value=completed
            ) as run,
        ):
            self.assertEqual(
                cli._invoke_raw_helper("enter", ["alice@work"]),
                42,
            )

        run.assert_called_once_with(command, check=False)

    def test_known_unimplemented_distribution(self) -> None:
        self.assertEqual(cli.main(["create", "arch"]), 2)

    def test_keyboard_interrupt_returns_130(self) -> None:
        with (
            mock.patch.object(cli, "_create", side_effect=KeyboardInterrupt),
            mock.patch.object(cli, "print") as print_output,
        ):
            self.assertEqual(cli.main(["create", "ubuntu"]), 130)
        print_output.assert_called_once_with(
            "Exiting due to Ctrl+C", file=cli.sys.stderr
        )

    def test_create_ubuntu_builds_expected_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            (home / "Projects").mkdir()
            identity = core.Identity(1000, 1000, home)
            calls = mock.Mock()
            calls.invoke.return_value = 0
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
                mock.patch.object(cli, "configure_logging", calls.configure),
                mock.patch.object(cli, "log", calls.log),
                mock.patch.object(cli, "_invoke_helper", calls.invoke) as invoke,
            ):
                self.assertEqual(cli.main(["create", "ubuntu"]), 0)
        calls.assert_has_calls(
            [
                mock.call.configure(rich=True),
                mock.call.log(
                    "Creating Ubuntu Resolute (26.04) space 'ubuntu'..."
                ),
                mock.call.invoke("create", mock.ANY),
            ]
        )
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

    def test_delete_requires_enter_before_invoking_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            (state / "work").mkdir(parents=True)
            with (
                mock.patch.object(core, "STATE_ROOT", state),
                mock.patch("builtins.input", return_value="") as prompt,
                mock.patch.object(cli, "_invoke_helper", return_value=0) as invoke,
            ):
                self.assertEqual(cli.main(["delete", "work"]), 0)

        self.assertIn("Press Enter", prompt.call_args.args[0])
        invoke.assert_called_once_with("delete", {"name": "work"})

    def test_delete_is_cancelled_by_nonempty_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            (state / "work").mkdir(parents=True)
            with (
                mock.patch.object(core, "STATE_ROOT", state),
                mock.patch("builtins.input", return_value="no"),
                mock.patch.object(cli, "_invoke_helper") as invoke,
            ):
                self.assertEqual(cli.main(["delete", "work"]), 130)

        invoke.assert_not_called()

    def test_delete_noconfirm_skips_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            (state / "work").mkdir(parents=True)
            with (
                mock.patch.object(core, "STATE_ROOT", state),
                mock.patch("builtins.input") as prompt,
                mock.patch.object(cli, "_invoke_helper", return_value=0) as invoke,
            ):
                self.assertEqual(
                    cli.main(["delete", "work", "--noconfirm"]),
                    0,
                )

        prompt.assert_not_called()
        invoke.assert_called_once_with("delete", {"name": "work"})

    def test_delete_rejects_missing_space_without_prompting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch.object(core, "STATE_ROOT", Path(temporary)),
                mock.patch("builtins.input") as prompt,
                mock.patch.object(cli, "_invoke_helper") as invoke,
            ):
                self.assertEqual(cli.main(["delete", "missing"]), 1)

        prompt.assert_not_called()
        invoke.assert_not_called()

    def test_cp_supports_options_anywhere_and_fixes_space_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            (state / "work" / "rootfs").mkdir(parents=True)
            (state / "work" / "home").mkdir()
            with (
                mock.patch.object(core, "STATE_ROOT", state),
                mock.patch.object(cli, "_invoke_helper", return_value=0) as invoke,
            ):
                self.assertEqual(
                    cli.main(
                        [
                            "cp",
                            "-r",
                            "work:/etc/hosts",
                            "--preserve=mode",
                            "work:/home/alice/hosts",
                        ]
                    ),
                    0,
                )

        invoke.assert_called_once_with(
            "cp",
            {
                "arguments": [
                    "-r",
                    str(state / "work" / "rootfs" / "etc" / "hosts"),
                    "--preserve=mode",
                    str(state / "work" / "home" / "alice" / "hosts"),
                ]
            },
        )

    def test_cp_fixes_host_paths_and_preserves_trailing_arguments(self) -> None:
        with mock.patch.object(cli, "_invoke_helper", return_value=0) as invoke:
            self.assertEqual(
                cli.main(
                    [
                        "cp",
                        "../source",
                        "/tmp/destination",
                        "--suffix",
                        ".backup",
                    ]
                ),
                0,
            )

        invoke.assert_called_once_with(
            "cp",
            {
                "arguments": [
                    "../source",
                    "/tmp/destination",
                    "--suffix",
                    ".backup",
                ]
            },
        )

    def test_enter_delegates_startup_and_forwards_command(self) -> None:
        identity = core.Identity(1000, 1000, Path("/home/alice"))
        with (
            mock.patch.object(
                core, "initiating_identity", return_value=identity
            ),
            mock.patch.object(
                cli.pwd,
                "getpwuid",
                return_value=mock.Mock(pw_name="alice"),
            ),
            mock.patch.object(
                cli.subprocess, "run"
            ) as run,
            mock.patch.object(
                cli,
                "_invoke_raw_helper",
                return_value=0,
            ) as invoke,
        ):
            self.assertEqual(
                cli.main(
                    [
                        "enter",
                        "work",
                        "--",
                        "sh",
                        "-c",
                        "printf '%s' \"$HOME\"",
                    ]
                ),
                0,
            )

        run.assert_not_called()
        invoke.assert_called_once_with(
            "enter",
            [
                "alice@work",
                "--",
                "sh",
                "-c",
                "printf '%s' \"$HOME\"",
            ],
        )

    def test_enter_without_command_defaults_to_shell(self) -> None:
        identity = core.Identity(1000, 1000, Path("/home/alice"))
        with (
            mock.patch.object(
                core, "initiating_identity", return_value=identity
            ),
            mock.patch.object(
                cli.pwd,
                "getpwuid",
                return_value=mock.Mock(pw_name="alice"),
            ),
            mock.patch.object(
                cli, "_invoke_raw_helper", return_value=0
            ) as invoke,
        ):
            self.assertEqual(cli.main(["enter", "work"]), 0)

        invoke.assert_called_once_with("enter", ["alice@work"])

    def test_enter_root_and_user_root_use_privileged_helper(self) -> None:
        for arguments in (
            ["--root", "work"],
            ["--user=root", "work"],
            ["work", "--root"],
            ["work", "--user", "root"],
        ):
            with (
                self.subTest(arguments=arguments),
                mock.patch.object(
                    core, "initiating_identity"
                ) as initiating_identity,
                mock.patch.object(
                    cli, "_invoke_raw_helper", return_value=0
                ) as invoke,
            ):
                self.assertEqual(
                    cli.main(
                        [
                            "enter",
                            *arguments,
                            "--",
                            "id",
                            "-u",
                        ]
                    ),
                    0,
                )

            initiating_identity.assert_not_called()
            invoke.assert_called_once_with(
                "enter-as-user",
                ["root", "work", "--", "id", "-u"],
            )

    def test_enter_as_named_user_preserves_command_arguments(self) -> None:
        with (
            mock.patch.object(
                cli, "_invoke_raw_helper", return_value=0
            ) as invoke,
        ):
            self.assertEqual(
                cli.main(
                    [
                        "enter",
                        "--user",
                        "builder",
                        "work",
                        "--",
                        "printf",
                        "%s",
                        "$HOME",
                    ]
                ),
                0,
            )

        invoke.assert_called_once_with(
            "enter-as-user",
            [
                "builder",
                "work",
                "--",
                "printf",
                "%s",
                "$HOME",
            ],
        )

    def test_existing_space_prepends_override_step(self) -> None:
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
                mock.patch.object(
                    cli, "run_permission_wizard", return_value=None
                ) as wizard,
            ):
                self.assertEqual(cli.main(["create", "ubuntu"]), 130)
        self.assertTrue(wizard.call_args.kwargs["override"])


if __name__ == "__main__":
    unittest.main()
