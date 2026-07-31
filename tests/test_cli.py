from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spaces import core
from spaces import __main__ as cli


class CliTests(unittest.TestCase):
    def test_entry_modules_defer_command_specific_dependencies(self) -> None:
        source_root = Path(cli.__file__).resolve().parents[1]
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(source_root)!r})\n"
            "import spaces.__main__\n"
            "assert 'spaces.tui' not in sys.modules\n"
            "assert 'textual' not in sys.modules\n"
            "import spaces.priv\n"
            "assert 'spaces.shortcuts' not in sys.modules\n"
            "assert 'spaces.launch' not in sys.modules\n"
            "assert 'PIL' not in sys.modules\n"
        )
        subprocess.run(
            [sys.executable, "-I", "-c", script],
            check=True,
        )

    def test_helper_output_is_streamed_after_privilege_handoff(self) -> None:
        command = ["pkexec", "/usr/bin/spaces.priv", "create", "{}"]
        completed = subprocess.CompletedProcess(command, 42)

        with (
            mock.patch.object(cli, "_helper_command", return_value=command),
            mock.patch.object(
                cli, "_has_controlling_terminal", return_value=False
            ),
            mock.patch.object(cli, "configure_logging") as configure_logging,
            mock.patch.object(
                cli, "run_streamed", return_value=completed
            ) as run_streamed,
        ):
            returncode = cli._invoke_helper("create", {})

        configure_logging.assert_called_once_with(rich=True)
        run_streamed.assert_called_once_with(command, check=False)
        self.assertEqual(returncode, 42)

    def test_helper_prefers_tty_polkit_authentication(self) -> None:
        command = ["pkexec", "/usr/bin/spaces.priv", "create", "{}"]
        completed = subprocess.CompletedProcess(command, 0)
        agent = mock.Mock()
        agent.poll.return_value = None

        with (
            mock.patch.object(cli.os, "geteuid", return_value=1000),
            mock.patch.object(
                cli, "_has_controlling_terminal", return_value=True
            ),
            mock.patch.object(
                cli.shutil,
                "which",
                return_value="/usr/bin/pkttyagent",
            ),
            mock.patch.object(cli.os, "pipe", return_value=(10, 11)),
            mock.patch.object(cli.os, "read", return_value=b"") as read,
            mock.patch.object(cli.os, "close") as close,
            mock.patch.object(
                cli.subprocess, "Popen", return_value=agent
            ) as popen,
            mock.patch.object(cli, "_helper_command", return_value=command),
            mock.patch.object(cli, "configure_logging"),
            mock.patch.object(
                cli, "run_streamed", return_value=completed
            ),
            mock.patch.object(cli.os, "getpid", return_value=1234),
        ):
            self.assertEqual(cli._invoke_helper("create", {}), 0)

        popen.assert_called_once_with(
            [
                "/usr/bin/pkttyagent",
                "--process",
                "1234",
                "--notify-fd",
                "11",
            ],
            pass_fds=(11,),
        )
        read.assert_called_once_with(10, 1)
        self.assertEqual(close.call_args_list, [mock.call(11), mock.call(10)])
        agent.terminate.assert_called_once_with()
        agent.wait.assert_called_once_with()

    def test_helper_uses_graphical_auth_without_a_tty_agent(self) -> None:
        command = ["pkexec", "/usr/bin/spaces.priv", "create", "{}"]
        completed = subprocess.CompletedProcess(command, 0)

        with (
            mock.patch.object(cli.os, "geteuid", return_value=1000),
            mock.patch.object(
                cli, "_has_controlling_terminal", return_value=True
            ),
            mock.patch.object(cli.shutil, "which", return_value=None),
            mock.patch.object(cli.subprocess, "Popen") as popen,
            mock.patch.object(cli, "_helper_command", return_value=command),
            mock.patch.object(cli, "configure_logging"),
            mock.patch.object(
                cli, "run_streamed", return_value=completed
            ),
        ):
            self.assertEqual(cli._invoke_helper("create", {}), 0)

        popen.assert_not_called()

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
                cli, "_has_controlling_terminal", return_value=False
            ),
            mock.patch.object(
                cli.subprocess, "run", return_value=completed
            ) as run,
            mock.patch.object(cli, "_tty_polkit_agent") as agent,
        ):
            self.assertEqual(
                cli._invoke_raw_helper("enter", ["alice@work"]),
                42,
            )

        agent.assert_not_called()
        run.assert_called_once_with(command, check=False)

    def test_privileged_enter_keeps_tty_polkit_agent(self) -> None:
        command = [
            "pkexec",
            "/usr/bin/spaces.priv",
            "enter-as-user",
            "root",
            "work",
        ]
        completed = subprocess.CompletedProcess(command, 0)
        with (
            mock.patch.object(
                cli, "_raw_helper_command", return_value=command
            ),
            mock.patch.object(
                cli.subprocess, "run", return_value=completed
            ),
            mock.patch.object(cli, "_tty_polkit_agent") as agent,
        ):
            self.assertEqual(
                cli._invoke_raw_helper(
                    "enter-as-user",
                    ["root", "work"],
                ),
                0,
            )

        agent.assert_called_once_with()

    def test_all_parser_distributions_have_drivers(self) -> None:
        for distribution_id in ("arch", "fedora", "ubuntu", "kali", "custom"):
            self.assertIsNotNone(cli.get_driver(distribution_id))

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
                        "kernel_capabilities": "development",
                        "home": ["Projects"],
                        "distribution_option": "resolute",
                    },
                ) as wizard,
                mock.patch.object(cli, "configure_logging", calls.configure),
                mock.patch.object(cli, "log", calls.log),
                mock.patch.object(cli, "_invoke_helper", calls.invoke) as invoke,
            ):
                self.assertEqual(
                    cli.main(["create", "ubuntu", "--purge"]),
                    0,
                )
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
        self.assertTrue(payload["purge"])
        self.assertEqual(payload["distribution"]["version"], "resolute")
        self.assertEqual(set(payload["permissions"]["users"]), {"1000"})
        self.assertEqual(
            wizard.call_args.kwargs["administrator_group"],
            "sudo",
        )
        self.assertEqual(wizard.call_args.kwargs["preset"], "basic")
        self.assertTrue(wizard.call_args.kwargs["purge"])
        self.assertFalse(wizard.call_args.kwargs["missing"])
        self.assertTrue(
            payload["permissions"]["users"]["1000"]["permissions"][
                "administrator"
            ]
        )
        self.assertTrue(
            payload["permissions"]["users"]["1000"]["permissions"][
                "credential_agents"
            ]
        )
        self.assertTrue(
            payload["permissions"]["users"]["1000"]["permissions"][
                "mounted_drives"
            ]
        )
        self.assertTrue(
            payload["permissions"]["system"]["host_authentication"]
        )
        self.assertTrue(payload["permissions"]["system"]["shortcuts"])
        self.assertEqual(
            payload["permissions"]["system"]["kernel_capabilities"],
            "development",
        )
        self.assertEqual(
            payload["permissions"]["system"]["devices"],
            "basic",
        )

    def test_create_from_enter_marks_space_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            identity = core.Identity(1000, 1000, home)
            with (
                mock.patch.object(core, "STATE_ROOT", Path(temporary) / "state"),
                mock.patch.object(
                    core, "initiating_identity", return_value=identity
                ),
                mock.patch.object(
                    cli, "run_permission_wizard", return_value=None
                ) as wizard,
                mock.patch.object(cli, "_invoke_helper") as invoke,
            ):
                self.assertEqual(cli._create("ubuntu", missing=True), 130)

                self.assertTrue(wizard.call_args.kwargs["missing"])
                self.assertFalse(wizard.call_args.kwargs["override"])
                self.assertEqual(wizard.call_args.kwargs["space_name"], "ubuntu")
                invoke.assert_not_called()

    def test_create_persists_basic_preset_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            identity = core.Identity(1000, 1000, home)
            result = {
                "preset": "basic",
                **core.PERMISSION_PRESETS["basic"]["system"],
                **core.PERMISSION_PRESETS["basic"]["user"],
                "distribution_option": "resolute",
            }
            with (
                mock.patch.object(
                    core, "STATE_ROOT", Path(temporary) / "state"
                ),
                mock.patch.object(
                    core, "initiating_identity", return_value=identity
                ),
                mock.patch.object(
                    cli, "run_permission_wizard", return_value=result
                ),
                mock.patch.object(cli, "configure_logging"),
                mock.patch.object(cli, "log"),
                mock.patch.object(
                    cli, "_invoke_helper", return_value=0
                ) as invoke,
            ):
                self.assertEqual(cli.main(["create", "ubuntu"]), 0)

        payload = invoke.call_args.args[1]
        self.assertEqual(
            payload["permissions"]["system"],
            {
                "preset": "basic",
                **core.PERMISSION_PRESETS["basic"]["system"],
            },
        )
        self.assertEqual(
            payload["permissions"]["users"]["1000"]["permissions"],
            {
                "preset": "basic",
                **core.PERMISSION_PRESETS["basic"]["user"],
            },
        )
    def test_create_from_enter_replaces_partial_rootfs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            (state / "kali" / "rootfs").mkdir(parents=True)
            home = Path(temporary) / "home"
            home.mkdir()
            identity = core.Identity(1000, 1000, home)
            with (
                mock.patch.object(core, "STATE_ROOT", state),
                mock.patch.object(
                    core, "initiating_identity", return_value=identity
                ),
                mock.patch.object(
                    cli, "run_permission_wizard", return_value=None
                ) as wizard,
                mock.patch.object(cli, "_invoke_helper") as invoke,
            ):
                self.assertEqual(cli._create("kali", missing=True), 130)

        self.assertTrue(wizard.call_args.kwargs["override"])
        self.assertFalse(wizard.call_args.kwargs["missing"])
        self.assertEqual(wizard.call_args.kwargs["space_name"], "kali")
        invoke.assert_not_called()

    def test_create_arch_builds_options_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            identity = core.Identity(1000, 1000, home)
            with (
                mock.patch.object(core, "STATE_ROOT", Path(temporary) / "state"),
                mock.patch.object(core, "initiating_identity", return_value=identity),
                mock.patch.object(
                    cli,
                    "run_permission_wizard",
                    return_value={
                        "network": "basic",
                        "home": [],
                        "distribution_options": [],
                    },
                ) as wizard,
                mock.patch.object(cli, "_invoke_helper", return_value=0) as invoke,
            ):
                self.assertEqual(cli.main(["create", "arch"]), 0)

        self.assertTrue(wizard.call_args.kwargs["distribution_multiple"])
        self.assertEqual(wizard.call_args.kwargs["distribution_values"], ["yay"])
        self.assertEqual(
            invoke.call_args.args[1]["distribution"],
            {"id": "arch", "options": []},
        )

    def test_rebuilding_arch_persists_selected_options(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            home = Path(temporary) / "home"
            home.mkdir()
            identity = core.Identity(1000, 1000, home)
            info = core.create_info(
                "arch",
                {"id": "arch", "options": []},
                identity,
                "basic",
                [],
            )
            info_path = state / "arch" / "info.json"
            info_path.parent.mkdir(parents=True)
            info_path.write_text(json.dumps(info), encoding="utf-8")
            with (
                mock.patch.object(core, "STATE_ROOT", state),
                mock.patch.object(core, "initiating_identity", return_value=identity),
                mock.patch.object(
                    cli,
                    "run_permission_wizard",
                    return_value={
                        "network": "basic",
                        "home": [],
                        "distribution_options": [],
                    },
                ) as wizard,
                mock.patch.object(cli, "_invoke_helper", return_value=0),
            ):
                self.assertEqual(cli.main(["create", "arch"]), 0)

        self.assertEqual(wizard.call_args.kwargs["distribution_values"], [])

    def test_create_kali_has_no_distribution_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            identity = core.Identity(1000, 1000, home)
            with (
                mock.patch.object(core, "STATE_ROOT", Path(temporary) / "state"),
                mock.patch.object(core, "initiating_identity", return_value=identity),
                mock.patch.object(
                    cli,
                    "run_permission_wizard",
                    return_value={"network": "basic", "home": []},
                ) as wizard,
                mock.patch.object(cli, "_invoke_helper", return_value=0) as invoke,
            ):
                self.assertEqual(cli.main(["create", "kali"]), 0)

        self.assertEqual(wizard.call_args.kwargs["distribution_options"], [])
        self.assertFalse(wizard.call_args.kwargs["distribution_multiple"])
        self.assertEqual(
            invoke.call_args.args[1]["distribution"],
            {"id": "kali"},
        )

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
        self.assertEqual(wizard.call_args.kwargs["submit_label"], "Confirm")
        patch = invoke.call_args.args[1]
        self.assertNotIn("system", patch["permissions"])
        self.assertTrue(
            patch["permissions"]["user"]["permissions"]["administrator"]
        )

    def test_configure_user_before_space_targets_current_user(self) -> None:
        with mock.patch.object(cli, "_configure", return_value=0) as configure:
            self.assertEqual(
                cli.main(["configure", "--user", "fedora"]),
                0,
            )

        configure.assert_called_once_with("fedora", user="")

    def test_full_configure_includes_host_authentication(self) -> None:
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
                [],
            )
            info_path = state / "ubuntu" / "info.json"
            info_path.parent.mkdir(parents=True)
            import json

            info_path.write_text(json.dumps(info), encoding="utf-8")
            with (
                mock.patch.object(core, "STATE_ROOT", state),
                mock.patch.object(
                    core, "initiating_identity", return_value=identity
                ),
                mock.patch.object(
                    cli,
                    "run_permission_wizard",
                    return_value={
                        "network": "advanced",
                        "kernel_capabilities": "development",
                        "devices": "admin",
                        "host_authentication": False,
                        "home": [],
                        "administrator": True,
                    },
                ),
                mock.patch.object(
                    cli, "_invoke_helper", return_value=0
                ) as invoke,
            ):
                self.assertEqual(cli.main(["configure", "ubuntu"]), 0)

        self.assertEqual(
            invoke.call_args.args[1]["permissions"]["system"],
            {
                "preset": "custom",
                "network": "advanced",
                "kernel_capabilities": "development",
                "devices": "admin",
                "host_authentication": False,
                "shortcuts": True,
            },
        )

    def test_graphical_enter_is_internal_cli_context(self) -> None:
        with mock.patch.object(cli, "_enter", return_value=42) as enter:
            self.assertEqual(
                cli.main(
                    [
                        "enter",
                        "--graphical",
                        "work",
                        "--",
                        "/usr/bin/code",
                    ]
                ),
                42,
            )

        enter.assert_called_once_with(
            "work",
            ["/usr/bin/code"],
            enter_user=None,
            graphical=True,
        )

    def test_configure_named_user_targets_host_account(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            initiating_home = Path(temporary) / "home" / "dev"
            initiating_home.mkdir(parents=True)
            target_home = Path(temporary) / "home" / "alice"
            target_home.mkdir()
            (target_home / "Documents").mkdir()
            identity = core.Identity(1000, 1000, initiating_home)
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
            account = mock.Mock(
                pw_uid=1001,
                pw_gid=1002,
                pw_dir=str(target_home),
            )
            with (
                mock.patch.object(core, "STATE_ROOT", state),
                mock.patch.object(core, "initiating_identity", return_value=identity),
                mock.patch.object(cli.pwd, "getpwnam", return_value=account) as lookup,
                mock.patch.object(
                    cli,
                    "run_permission_wizard",
                    return_value={
                        "home": ["Documents"],
                        "administrator": False,
                        "credential_agents": False,
                    },
                ) as wizard,
                mock.patch.object(cli, "_invoke_helper", return_value=0) as invoke,
            ):
                self.assertEqual(
                    cli.main(
                        ["configure", "ubuntu", "--user", "alice"]
                    ),
                    0,
                )

        lookup.assert_called_once_with("alice")
        self.assertEqual(wizard.call_args.kwargs["home"], target_home)
        self.assertFalse(wizard.call_args.kwargs["include_system"])
        self.assertEqual(wizard.call_args.kwargs["preset"], "basic")
        patch = invoke.call_args.args[1]
        self.assertNotIn("system", patch["permissions"])
        self.assertEqual(
            patch["permissions"]["user"],
            {
                "uid": 1001,
                "gid": 1002,
                "permissions": {
                    "preset": "custom",
                    "home": ["Documents"],
                    "administrator": False,
                    "desktop": True,
                    "credential_agents": False,
                    "mounted_drives": True,
                },
            },
        )

    def test_configure_rejects_unknown_named_user(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            home = Path(temporary) / "home"
            home.mkdir()
            identity = core.Identity(1000, 1000, home)
            info = core.create_info(
                "work",
                {"id": "custom"},
                identity,
                "basic",
                [],
            )
            info_path = state / "work" / "info.json"
            info_path.parent.mkdir(parents=True)
            import json

            info_path.write_text(json.dumps(info), encoding="utf-8")
            with (
                mock.patch.object(core, "STATE_ROOT", state),
                mock.patch.object(core, "initiating_identity", return_value=identity),
                mock.patch.object(cli.pwd, "getpwnam", side_effect=KeyError),
                mock.patch.object(cli, "_invoke_helper") as invoke,
            ):
                self.assertEqual(
                    cli.main(
                        ["configure", "work", "--user", "missing"]
                    ),
                    1,
                )

        invoke.assert_not_called()

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
        self.assertIn("preserving its home data", prompt.call_args.args[0])
        invoke.assert_called_once_with(
            "delete", {"name": "work", "purge": False}
        )

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
        invoke.assert_called_once_with(
            "delete", {"name": "work", "purge": False}
        )

    def test_delete_purge_deletes_home_without_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            (state / "work").mkdir(parents=True)
            with (
                mock.patch.object(core, "STATE_ROOT", state),
                mock.patch("builtins.input") as prompt,
                mock.patch.object(
                    cli, "_invoke_helper", return_value=0
                ) as invoke,
            ):
                self.assertEqual(
                    cli.main(
                        ["delete", "work", "--purge", "--noconfirm"]
                    ),
                    0,
                )

        prompt.assert_not_called()
        invoke.assert_called_once_with(
            "delete", {"name": "work", "purge": True}
        )

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

        invoke.assert_called_once_with(
            "enter",
            ["alice@work"],
        )

    def test_enter_creates_missing_fixed_distribution_then_enters(self) -> None:
        identity = core.Identity(1000, 1000, Path("/home/alice"))
        for distribution in ("arch", "fedora", "ubuntu", "kali"):
            with (
                self.subTest(distribution=distribution),
                tempfile.TemporaryDirectory() as temporary,
                mock.patch.object(core, "STATE_ROOT", Path(temporary)),
                mock.patch.object(
                    cli, "_has_controlling_terminal", return_value=True
                ),
                mock.patch.object(cli, "_create", return_value=0) as create,
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
                self.assertEqual(cli.main(["enter", distribution]), 0)

            create.assert_called_once_with(distribution, missing=True)
            invoke.assert_called_once_with(
                "enter",
                [f"alice@{distribution}"],
            )

    def test_enter_after_creation_preserves_options_and_command(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(core, "STATE_ROOT", Path(temporary)),
            mock.patch.object(
                cli, "_has_controlling_terminal", return_value=True
            ),
            mock.patch.object(cli, "_create", return_value=0) as create,
            mock.patch.object(
                cli, "_invoke_raw_helper", return_value=0
            ) as invoke,
        ):
            self.assertEqual(
                cli.main(
                    [
                        "enter",
                        "--root",
                        "--graphical",
                        "arch",
                        "--",
                        "printf",
                        "%s",
                        "$HOME",
                    ]
                ),
                0,
            )

        create.assert_called_once_with("arch", missing=True)
        invoke.assert_called_once_with(
            "enter-as-user",
            [
                "root",
                "arch",
                "--",
                "printf",
                "%s",
                "$HOME",
            ],
        )

    def test_enter_creates_partial_space_with_missing_info(self) -> None:
        identity = core.Identity(1000, 1000, Path("/home/alice"))
        for rootfs_present in (False, True):
            with (
                self.subTest(rootfs_present=rootfs_present),
                tempfile.TemporaryDirectory() as temporary,
            ):
                state = Path(temporary)
                target = state / "ubuntu"
                target.mkdir()
                if rootfs_present:
                    (target / "rootfs").mkdir()
                with (
                    mock.patch.object(core, "STATE_ROOT", state),
                    mock.patch.object(
                        cli,
                        "_has_controlling_terminal",
                        return_value=True,
                    ),
                    mock.patch.object(
                        cli, "_create", return_value=0
                    ) as create,
                    mock.patch.object(
                        core,
                        "initiating_identity",
                        return_value=identity,
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
                    self.assertEqual(cli.main(["enter", "ubuntu"]), 0)

            create.assert_called_once_with("ubuntu", missing=True)
            invoke.assert_called_once_with(
                "enter",
                ["alice@ubuntu"],
            )

    def test_cancelled_missing_space_creation_does_not_enter(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(core, "STATE_ROOT", Path(temporary)),
            mock.patch.object(
                cli, "_has_controlling_terminal", return_value=True
            ),
            mock.patch.object(cli, "_create", return_value=130) as create,
            mock.patch.object(
                cli, "_invoke_raw_helper"
            ) as invoke,
        ):
            self.assertEqual(cli.main(["enter", "ubuntu"]), 130)

        create.assert_called_once_with("ubuntu", missing=True)
        invoke.assert_not_called()

    def test_missing_space_auto_create_requires_tty_and_fixed_name(
        self,
    ) -> None:
        identity = core.Identity(1000, 1000, Path("/home/alice"))
        cases = (
            ("arch", False, None),
            ("work", True, None),
            ("custom", True, None),
            ("arch", True, "metadata"),
            ("arch", True, "space-symlink"),
            ("arch", True, "info-symlink"),
        )
        for space, terminal, target_kind in cases:
            with (
                self.subTest(
                    space=space,
                    terminal=terminal,
                    target_kind=target_kind,
                ),
                tempfile.TemporaryDirectory() as temporary,
            ):
                state = Path(temporary)
                target = state / space
                if target_kind == "metadata":
                    target.mkdir()
                    (target / "info.json").write_text(
                        "{}",
                        encoding="utf-8",
                    )
                elif target_kind == "space-symlink":
                    target.symlink_to(state / "missing-target")
                elif target_kind == "info-symlink":
                    target.mkdir()
                    (target / "info.json").symlink_to(
                        state / "missing-info"
                    )
                with (
                    mock.patch.object(core, "STATE_ROOT", state),
                    mock.patch.object(
                        cli,
                        "_has_controlling_terminal",
                        return_value=terminal,
                    ),
                    mock.patch.object(cli, "_create") as create,
                    mock.patch.object(
                        core,
                        "initiating_identity",
                        return_value=identity,
                    ),
                    mock.patch.object(
                        cli.pwd,
                        "getpwuid",
                        return_value=mock.Mock(pw_name="alice"),
                    ),
                    mock.patch.object(
                        cli, "_invoke_raw_helper", return_value=1
                    ) as invoke,
                ):
                    self.assertEqual(cli.main(["enter", space]), 1)

            create.assert_not_called()
            invoke.assert_called_once_with(
                "enter",
                [f"alice@{space}"],
            )

    def test_enter_never_sends_terminal_session_data_to_helper(self) -> None:
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
                cli,
                "_invoke_raw_helper",
                return_value=0,
            ) as invoke,
        ):
            self.assertEqual(
                cli.main(
                    [
                        "enter",
                        "--graphical",
                        "work",
                        "--",
                        "/usr/bin/kate",
                    ]
                ),
                0,
            )

        arguments = invoke.call_args.args[1]
        self.assertEqual(
            arguments,
            ["alice@work", "--", "/usr/bin/kate"],
        )

    def test_disabled_enter_uses_the_same_direct_entry_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            home = Path(temporary) / "home"
            home.mkdir()
            identity = core.Identity(1000, 1000, home)
            info = core.create_info(
                "work",
                {"id": "custom"},
                identity,
                "basic",
                [],
                host_authentication=False,
            )
            info_path = state / "work" / "info.json"
            info_path.parent.mkdir(parents=True)
            info_path.write_text(json.dumps(info), encoding="utf-8")
            with (
                mock.patch.object(core, "STATE_ROOT", state),
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
                [
                    "root",
                    "work",
                    "--",
                    "id",
                    "-u",
                ],
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
        self.assertEqual(
            wizard.call_args.kwargs["kernel_capabilities"],
            "basic",
        )
        self.assertEqual(wizard.call_args.kwargs["devices"], "basic")

    def test_recreating_space_preserves_system_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            target = state / "ubuntu"
            target.mkdir(parents=True)
            identity = core.Identity(1000, 1000, Path(temporary))
            info = core.create_info(
                "ubuntu",
                {"id": "ubuntu", "version": "resolute"},
                identity,
                "basic",
                [],
                devices="admin",
                kernel_capabilities="development",
            )
            (target / "info.json").write_text(
                json.dumps(info),
                encoding="utf-8",
            )
            with (
                mock.patch.object(core, "STATE_ROOT", state),
                mock.patch.object(
                    core,
                    "initiating_identity",
                    return_value=identity,
                ),
                mock.patch.object(
                    cli,
                    "run_permission_wizard",
                    return_value=None,
                ) as wizard,
            ):
                self.assertEqual(cli.main(["create", "ubuntu"]), 130)

        self.assertEqual(
            wizard.call_args.kwargs["kernel_capabilities"],
            "development",
        )
        self.assertEqual(wizard.call_args.kwargs["devices"], "admin")


if __name__ == "__main__":
    unittest.main()
