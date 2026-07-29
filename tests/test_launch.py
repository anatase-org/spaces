from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from spaces import core
from spaces import launch as launch_module
from spaces import shortcuts


class LaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state_root = Path(self.temporary.name) / "spaces"
        self.space = self.state_root / "work"
        self.rootfs = self.space / "rootfs"
        self.rootfs.mkdir(parents=True)
        self.home = self.space / "home"
        self.home.mkdir()
        self.root_home = self.home / "root"
        self.host_home = Path(self.temporary.name) / "host-home"
        self.host_home.mkdir()
        etc = self.rootfs / "etc"
        etc.mkdir()
        (etc / "passwd").write_text(
            "root:x:0:0:root:/root:/bin/sh\n",
            encoding="utf-8",
        )
        (etc / "group").write_text("root:x:0:\n", encoding="utf-8")
        (etc / "shadow").write_text(
            "root:!:20000:0:99999:7:::\n",
            encoding="utf-8",
        )
        (etc / "gshadow").write_text("root:!::\n", encoding="utf-8")
        (etc / "skel").mkdir()
        self.identity = core.Identity(1000, 1000, Path("/home/user"))
        self.state_root_patch = mock.patch.object(
            core, "STATE_ROOT", self.state_root
        )
        self.state_root_patch.start()
        self.shortcuts_root_patch = mock.patch.object(
            shortcuts,
            "APPLICATIONS_ROOT",
            Path(self.temporary.name) / "applications",
        )
        self.shortcuts_root_patch.start()
        self.session_runtime_patch = mock.patch.object(
            launch_module.session,
            "RUNTIME_ROOT",
            Path(self.temporary.name) / "runtime",
        )
        self.session_runtime_patch.start()
        host_user = pwd.struct_passwd(
            (
                "user",
                "x",
                1000,
                1000,
                "Test User",
                str(self.host_home),
                "/bin/sh",
            )
        )
        self.passwd_patch = mock.patch.object(
            launch_module.pwd,
            "getpwuid",
            return_value=host_user,
        )
        self.passwd_patch.start()
        self.monitor = mock.Mock()
        self.monitor.state.return_value = "offline"
        self.monitor_patch = mock.patch.object(
            launch_module,
            "_LoginMonitor",
            return_value=self.monitor,
        )
        self.monitor_patch.start()
        self.worker = mock.Mock()
        self.worker_patch = mock.patch.object(
            launch_module,
            "_MountWorker",
            return_value=self.worker,
        )
        self.worker_class = self.worker_patch.start()
        self.device_policy_patch = mock.patch.object(
            launch_module,
            "_set_device_policy",
        )
        self.device_policy = self.device_policy_patch.start()

    def tearDown(self) -> None:
        self.device_policy_patch.stop()
        self.worker_patch.stop()
        self.session_runtime_patch.stop()
        self.monitor_patch.stop()
        self.passwd_patch.stop()
        self.state_root_patch.stop()
        self.shortcuts_root_patch.stop()
        self.temporary.cleanup()

    def _write_info(
        self,
        network: str = "basic",
        name: str = "work",
        home: list[str] | None = None,
        host_authentication: bool = False,
        desktop: bool = False,
        devices: str = "disabled",
        shortcuts_enabled: bool = True,
        kernel_capabilities: str = "basic",
    ) -> None:
        info = core.create_info(
            name,
            {"id": "custom"},
            self.identity,
            network,
            home or [],
            devices=devices,
            host_authentication=host_authentication,
            shortcuts=shortcuts_enabled,
            desktop=desktop,
            kernel_capabilities=kernel_capabilities,
        )
        (self.space / "info.json").write_text(
            json.dumps(info),
            encoding="utf-8",
        )

    def test_enabled_shortcuts_are_reconciled_and_monitored(self) -> None:
        self._write_info()
        process = mock.Mock()
        process.wait.return_value = 0
        shortcut_monitor = mock.Mock()
        shortcut_worker = mock.Mock()
        with (
            mock.patch.object(
                launch_module.shortcuts,
                "reconcile",
            ) as reconcile,
            mock.patch.object(
                launch_module.shortcuts,
                "InotifyMonitor",
                return_value=shortcut_monitor,
            ),
            mock.patch.object(
                launch_module.shortcuts,
                "ShortcutWorker",
                return_value=shortcut_worker,
            ) as worker_class,
            mock.patch.object(
                launch_module.subprocess,
                "Popen",
                return_value=process,
            ),
            mock.patch.object(launch_module.signal, "signal"),
        ):
            self.assertEqual(launch_module.launch("work"), 0)

        reconcile.assert_called_once_with("work", self.rootfs, "custom")
        worker_class.assert_called_once_with(
            "work",
            self.rootfs,
            "custom",
            shortcut_monitor,
        )
        shortcut_worker.start.assert_called_once_with()
        shortcut_worker.stop.assert_called_once_with()
        shortcut_worker.join.assert_called_once_with()
        shortcut_monitor.close.assert_called_once_with()

    def test_disabled_shortcuts_remove_exports_without_monitoring(self) -> None:
        self._write_info(shortcuts_enabled=False)
        process = mock.Mock()
        process.wait.return_value = 0
        with (
            mock.patch.object(
                launch_module.shortcuts,
                "remove",
            ) as remove,
            mock.patch.object(
                launch_module.shortcuts,
                "reconcile",
            ) as reconcile,
            mock.patch.object(
                launch_module.shortcuts,
                "InotifyMonitor",
            ) as monitor,
            mock.patch.object(
                launch_module.subprocess,
                "Popen",
                return_value=process,
            ),
            mock.patch.object(launch_module.signal, "signal"),
        ):
            self.assertEqual(launch_module.launch("work"), 0)

        remove.assert_called_once_with("work")
        reconcile.assert_not_called()
        monitor.assert_not_called()

    def test_network_permissions_build_expected_launch(self) -> None:
        kept_caps = [
            "CAP_CHOWN",
            "CAP_DAC_OVERRIDE",
            "CAP_DAC_READ_SEARCH",
            "CAP_FOWNER",
            "CAP_FSETID",
            "CAP_IPC_OWNER",
            "CAP_KILL",
            "CAP_LEASE",
            "CAP_LINUX_IMMUTABLE",
            "CAP_MKNOD",
            "CAP_SETFCAP",
            "CAP_SETGID",
            "CAP_SETPCAP",
            "CAP_SETUID",
            "CAP_SYS_ADMIN",
            "CAP_SYS_BOOT",
            "CAP_SYS_CHROOT",
            "CAP_SYS_NICE",
            "CAP_SYS_RESOURCE",
        ]
        dropped_caps = [
            "CAP_AUDIT_CONTROL",
            "CAP_AUDIT_WRITE",
            "CAP_NET_BIND_SERVICE",
            "CAP_NET_BROADCAST",
            "CAP_NET_RAW",
            "CAP_SYS_PTRACE",
            "CAP_SYS_TTY_CONFIG",
        ]
        network_caps = {
            "basic": [],
            "advanced": ["CAP_NET_BIND_SERVICE"],
            "admin": [
                "CAP_NET_BIND_SERVICE",
                "CAP_NET_RAW",
                "CAP_NET_ADMIN",
            ],
        }
        common = [
            "/usr/bin/systemd-nspawn",
            "--quiet",
            f"--directory={self.rootfs}",
            "--machine=work",
            f"--bind={self.home}:/home",
            f"--bind={self.root_home}:/root",
            *launch_module._unit_mask_bind_arguments(),
            "--boot",
            "--setenv=SYSTEMD_GETTY_AUTO=no",
            "--console=read-only",
            "--private-users=no",
            "--keep-unit",
            "--settings=no",
            "--notify-ready=yes",
            "--resolv-conf=bind-host",
        ]

        for network, added_caps in network_caps.items():
            with self.subTest(network=network):
                self._write_info(network)
                if network == "basic":
                    legacy_info = json.loads(
                        (self.space / "info.json").read_text(encoding="utf-8")
                    )
                    del legacy_info["permissions"]["system"][
                        "kernel_capabilities"
                    ]
                    (self.space / "info.json").write_text(
                        json.dumps(legacy_info),
                        encoding="utf-8",
                    )
                caller_thread = threading.current_thread()
                called_thread: threading.Thread | None = None

                process = mock.Mock()
                process.wait.return_value = 42

                def run(*args: object, **kwargs: object) -> mock.Mock:
                    nonlocal called_thread
                    called_thread = threading.current_thread()
                    var_home = self.rootfs / "var" / "home"
                    self.assertTrue(var_home.is_symlink())
                    self.assertEqual(os.readlink(var_home), "/home")
                    return process

                with (
                    mock.patch.object(
                        launch_module,
                        "configure_logging",
                    ) as configure_logging,
                    mock.patch.dict(
                        os.environ,
                        {
                            "PRESERVED": "yes",
                            launch_module.API_VFS_WRITABLE: "yes",
                        },
                        clear=True,
                    ),
                    mock.patch.object(
                        launch_module.subprocess,
                        "Popen",
                        side_effect=run,
                    ) as run_mock,
                    mock.patch.object(launch_module.signal, "signal"),
                ):
                    self.assertEqual(launch_module.launch("work"), 42)

                self.assertIs(called_thread, caller_thread)
                configure_logging.assert_called_once_with(rich=False)
                self.assertTrue(self.root_home.is_dir())
                self.assertEqual(self.root_home.stat().st_mode & 0o777, 0o700)
                run_mock.assert_called_once()
                arguments, = run_mock.call_args.args
                environment = run_mock.call_args.kwargs["env"]
                expected_kept = [*kept_caps, *added_caps]
                expected_dropped = [
                    capability
                    for capability in dropped_caps
                    if capability not in expected_kept
                ]
                self.assertEqual(
                    arguments,
                    [
                        *common,
                        f"--drop-capability={','.join(expected_dropped)}",
                        f"--capability={','.join(expected_kept)}",
                    ],
                )
                self.assertEqual(environment["PRESERVED"], "yes")
                if network == "admin":
                    self.assertEqual(
                        environment[launch_module.API_VFS_WRITABLE],
                        "network",
                    )
                else:
                    self.assertNotIn(
                        launch_module.API_VFS_WRITABLE,
                        environment,
                    )

    def test_development_kernel_capabilities_extend_launch(self) -> None:
        for level in ("development", "admin"):
            with self.subTest(level=level):
                arguments = launch_module._command(
                    "work",
                    self.rootfs,
                    self.home,
                    "basic",
                    level,
                )

                self.assertIn("--system-call-filter=perf_event_open", arguments)
                capability_argument = next(
                    argument
                    for argument in arguments
                    if argument.startswith("--capability=")
                )
                self.assertTrue(
                    capability_argument.endswith(
                        "CAP_AUDIT_CONTROL,CAP_AUDIT_WRITE,"
                        "CAP_PERFMON,CAP_BPF"
                    ),
                    capability_argument,
                )
                dropped_capability_argument = next(
                    argument
                    for argument in arguments
                    if argument.startswith("--drop-capability=")
                )
                self.assertNotIn(
                    "CAP_AUDIT_CONTROL",
                    dropped_capability_argument,
                )
                self.assertNotIn(
                    "CAP_AUDIT_WRITE",
                    dropped_capability_argument,
                )

        basic_arguments = launch_module._command(
            "work",
            self.rootfs,
            self.home,
            "basic",
            "basic",
        )
        self.assertNotIn("--system-call-filter=perf_event_open", basic_arguments)
        basic_capability_argument = next(
            argument
            for argument in basic_arguments
            if argument.startswith("--capability=")
        )
        self.assertNotIn("CAP_PERFMON", basic_capability_argument)
        self.assertNotIn("CAP_BPF", basic_capability_argument)
        basic_dropped_capability_argument = next(
            argument
            for argument in basic_arguments
            if argument.startswith("--drop-capability=")
        )
        self.assertIn(
            "CAP_AUDIT_CONTROL",
            basic_dropped_capability_argument,
        )
        self.assertIn(
            "CAP_AUDIT_WRITE",
            basic_dropped_capability_argument,
        )

    def test_system_administrator_makes_api_vfs_writeable(self) -> None:
        self._write_info(kernel_capabilities="admin")
        process = mock.Mock()
        process.wait.return_value = 0

        with (
            mock.patch.object(
                launch_module.subprocess,
                "Popen",
                return_value=process,
            ) as run,
            mock.patch.object(launch_module.signal, "signal"),
        ):
            self.assertEqual(launch_module.launch("work"), 0)

        self.assertEqual(
            run.call_args.kwargs["env"][launch_module.API_VFS_WRITABLE],
            "yes",
        )

    def test_enabled_authentication_starts_service_and_adds_exact_binds(
        self,
    ) -> None:
        info = core.create_info(
            "work",
            {"id": "ubuntu", "version": "resolute"},
            self.identity,
            "basic",
            [],
            devices="disabled",
            host_authentication=True,
            desktop=False,
        )
        del info["permissions"]["system"]["host_authentication"]
        (self.space / "info.json").write_text(
            json.dumps(info), encoding="utf-8"
        )
        runtime = mock.Mock()
        runtime.bind_arguments = (
            "--bind-ro=/run/spaces/work/authentication/auth.sock:"
            "/run/spaces-host/auth.sock",
        )
        driver = mock.Mock(administrator_group="sudo")
        driver.reconcile_host_authentication.return_value = True
        authentication = mock.Mock()
        process = mock.Mock()
        process.wait.return_value = 0

        with (
            mock.patch.object(
                launch_module.auth,
                "prepare_runtime",
                return_value=runtime,
            ) as prepare,
            mock.patch.object(launch_module.auth, "validate_native_runtime"),
            mock.patch.object(
                launch_module,
                "get_driver",
                return_value=driver,
            ),
            mock.patch.object(
                launch_module.auth,
                "AuthenticationService",
                return_value=authentication,
            ) as service,
            mock.patch.object(
                launch_module.subprocess, "Popen", return_value=process
            ) as popen,
            mock.patch.object(launch_module.signal, "signal"),
        ):
            self.assertEqual(launch_module.launch("work"), 0)

        driver.reconcile_host_authentication.assert_called_once_with(
            self.rootfs,
            True,
        )
        prepare.assert_called_once_with("work", self.rootfs)
        service.assert_called_once_with("work", runtime, {1000: True})
        authentication.start.assert_called_once_with()
        authentication.stop.assert_called_once_with()
        command = popen.call_args.args[0]
        self.assertIn(
            "--bind-ro=/usr/lib/spaces/guest:/run/spaces-host/bin",
            command,
        )
        for bind in runtime.bind_arguments:
            self.assertIn(bind, command)

    def test_disabled_authentication_has_no_runtime_binds(self) -> None:
        self._write_info(host_authentication=False)
        process = mock.Mock()
        process.wait.return_value = 0
        with (
            mock.patch.object(
                launch_module.subprocess, "Popen", return_value=process
            ) as popen,
            mock.patch.object(
                launch_module.auth, "prepare_runtime"
            ) as prepare,
            mock.patch.object(launch_module.signal, "signal"),
        ):
            self.assertEqual(launch_module.launch("work"), 0)

        prepare.assert_not_called()
        self.assertFalse(
            any(
                "spaces-host" in argument
                for argument in popen.call_args.args[0]
            )
        )

    def test_desktop_permission_mounts_guest_native_launcher(self) -> None:
        self._write_info(host_authentication=False, desktop=True)
        process = mock.Mock()
        process.wait.return_value = 0
        with (
            mock.patch.object(
                launch_module.auth, "validate_native_runtime"
            ) as validate,
            mock.patch.object(
                launch_module.subprocess, "Popen", return_value=process
            ) as popen,
            mock.patch.object(launch_module.signal, "signal"),
        ):
            self.assertEqual(launch_module.launch("work"), 0)
        validate.assert_called_once_with(self.rootfs)
        self.assertIn(
            "--bind-ro=/usr/lib/spaces/guest:/run/spaces-host/bin",
            popen.call_args.args[0],
        )
        self.assertFalse(
            any("/dev/dri" in argument for argument in popen.call_args.args[0])
        )

    def test_disabled_devices_add_no_host_device_binds(self) -> None:
        self._write_info(devices="disabled", desktop=True)
        process = mock.Mock()
        process.wait.return_value = 0
        self.device_policy.reset_mock()

        with (
            mock.patch.object(
                launch_module.auth,
                "validate_native_runtime",
            ),
            mock.patch.object(
                launch_module.subprocess,
                "Popen",
                return_value=process,
            ) as popen,
            mock.patch.object(launch_module.signal, "signal"),
        ):
            self.assertEqual(launch_module.launch("work"), 0)

        command = popen.call_args.args[0]
        self.assertNotIn("--bind=/dev", command)
        self.assertFalse(any("/dev/dri" in argument for argument in command))
        self.assertEqual(
            self.device_policy.call_args_list,
            [
                mock.call("work", "disabled", ()),
                mock.call("work", "disabled"),
            ],
        )

    def test_full_devices_bind_all_nodes_and_use_unrestricted_policy(
        self,
    ) -> None:
        self._write_info(devices="full")
        process = mock.Mock()
        process.wait.return_value = 0
        device_udev = mock.Mock()
        device_monitor = mock.Mock()
        device_udev.monitor.return_value = device_monitor
        device_worker = mock.Mock()
        discovered = launch_module.devices.DeviceNode(
            destination=launch_module.PurePosixPath("/dev/nvme0n1"),
            source=Path("/dev/nvme0n1"),
            kind="b",
            major=259,
            minor=0,
        )
        self.device_policy.reset_mock()

        with (
            mock.patch.object(
                launch_module.devices,
                "Udev",
                return_value=device_udev,
            ),
            mock.patch.object(
                launch_module.devices,
                "discover",
                return_value=(discovered,),
            ),
            mock.patch.object(
                launch_module,
                "_DeviceWorker",
                return_value=device_worker,
            ),
            mock.patch.object(
                launch_module.subprocess,
                "Popen",
                return_value=process,
            ) as popen,
            mock.patch.object(launch_module.signal, "signal"),
        ):
            self.assertEqual(launch_module.launch("work"), 0)

        self.assertNotIn("--bind=/dev", popen.call_args.args[0])
        self.assertIn(
            "--bind=/dev/nvme0n1:/dev/nvme0n1",
            popen.call_args.args[0],
        )
        self.assertEqual(
            self.device_policy.call_args_list,
            [
                mock.call("work", "full", (discovered,)),
                mock.call("work", "disabled"),
            ],
        )
        device_worker.start.assert_called_once_with()
        device_worker.attach.assert_called_once_with(process)
        device_worker.stop.assert_called_once_with()
        device_worker.join.assert_called_once_with()

    def test_missing_devices_defaults_to_basic_and_starts_monitor_first(
        self,
    ) -> None:
        self._write_info(devices="basic")
        info_path = self.space / "info.json"
        info = json.loads(info_path.read_text(encoding="utf-8"))
        del info["permissions"]["system"]["devices"]
        info_path.write_text(json.dumps(info), encoding="utf-8")
        process = mock.Mock()
        process.wait.return_value = 0
        device_udev = mock.Mock()
        device_monitor = mock.Mock()
        device_udev.monitor.return_value = device_monitor
        device_worker = mock.Mock()
        discovered = launch_module.devices.DeviceNode(
            destination=launch_module.PurePosixPath("/dev/snd/pcm0"),
            source=Path("/dev/snd/pcm0"),
            kind="c",
            major=116,
            minor=1,
        )
        events: list[str] = []
        device_udev.monitor.side_effect = lambda: (
            events.append("monitor") or device_monitor
        )
        self.device_policy.reset_mock()

        with (
            mock.patch.object(
                launch_module.devices,
                "Udev",
                return_value=device_udev,
            ),
            mock.patch.object(
                launch_module.devices,
                "discover",
                side_effect=lambda *_args, **_kwargs: (
                    events.append("discover") or (discovered,)
                ),
            ),
            mock.patch.object(
                launch_module,
                "_DeviceWorker",
                return_value=device_worker,
            ),
            mock.patch.object(
                launch_module.subprocess,
                "Popen",
                return_value=process,
            ) as popen,
            mock.patch.object(launch_module.signal, "signal"),
        ):
            self.assertEqual(launch_module.launch("work"), 0)

        self.assertEqual(events, ["monitor", "discover"])
        self.assertIn(
            "--bind=/dev/snd/pcm0:/dev/snd/pcm0",
            popen.call_args.args[0],
        )
        self.assertEqual(
            self.device_policy.call_args_list,
            [
                mock.call("work", "basic", (discovered,)),
                mock.call("work", "disabled"),
            ],
        )
        device_worker.start.assert_called_once_with()
        device_worker.attach.assert_called_once_with(process)
        device_worker.stop.assert_called_once_with()
        device_worker.join.assert_called_once_with()

    def test_enabled_unknown_policy_is_rejected(self) -> None:
        self._write_info(host_authentication=True)

        with self.assertRaises(core.SpacesError):
            launch_module.launch("work")

    def test_launch_forwards_sigterm_and_waits(self) -> None:
        self._write_info()
        process = mock.Mock()
        process.wait.return_value = 42

        with (
            mock.patch.object(
                launch_module.subprocess,
                "Popen",
                return_value=process,
            ),
            mock.patch.object(launch_module.signal, "signal") as set_handler,
        ):
            self.assertEqual(launch_module.launch("work"), 42)

        set_handler.assert_called_once()
        signum, handler = set_handler.call_args.args
        self.assertEqual(signum, launch_module.signal.SIGTERM)
        self.assertTrue(callable(handler))
        handler(launch_module.signal.SIGTERM, None)
        handler(launch_module.signal.SIGTERM, None)
        self.assertEqual(
            process.send_signal.call_args_list,
            [
                mock.call(launch_module.signal.SIGTERM),
                mock.call(launch_module.signal.SIGTERM),
            ],
        )
        process.wait.assert_called_once_with()

    def test_initial_user_mounts_are_given_to_nspawn_and_worker(self) -> None:
        (self.host_home / "Projects").mkdir()
        self._write_info(home=["Projects"])
        self.monitor.state.return_value = "lingering"
        process = mock.Mock()
        process.wait.return_value = 0

        with (
            mock.patch.object(
                launch_module.subprocess,
                "Popen",
                return_value=process,
            ) as run,
            mock.patch.object(launch_module.signal, "signal"),
        ):
            self.assertEqual(launch_module.launch("work"), 0)

        arguments = run.call_args.args[0]
        bind = next(
            argument
            for argument in arguments
            if argument.endswith(":/home/user/Projects")
        )
        self.assertEqual(
            bind,
            f"--bind={self.host_home}/Projects:/home/user/Projects",
        )
        mounts = self.worker_class.call_args.args[4]
        self.assertEqual(len(mounts), 1)
        self.assertEqual(mounts[0].destination, "/home/user/Projects")
        self.assertEqual(
            self.worker_class.call_args.args[5],
            frozenset({1000}),
        )
        self.worker.start.assert_called()
        self.worker.attach.assert_called_once_with(process)
        self.worker.stop.assert_called()
        self.worker.join.assert_called()
        self.monitor.close.assert_called()

    def test_unit_masks_are_launch_time_binds(self) -> None:
        arguments = launch_module._unit_mask_bind_arguments()

        self.assertEqual(
            set(arguments),
            {
                f"--bind-ro=/dev/null:{destination}"
                for destination in launch_module.MASKED_UNIT_DESTINATIONS
            },
        )

    def test_network_manager_units_are_masked(self) -> None:
        units = {
            "NetworkManager-config-initrd.service",
            "NetworkManager-dispatcher.service",
            "NetworkManager-initrd.service",
            "NetworkManager-ovs.service",
            "NetworkManager-wait-online-initrd.service",
            "NetworkManager-wait-online.service",
            "NetworkManager.service",
            "nm-cloud-setup.service",
            "nm-cloud-setup.timer",
            "nm-priv-helper.service",
        }
        arguments = set(launch_module._unit_mask_bind_arguments())
        for unit in units:
            with self.subTest(unit=unit):
                self.assertIn(
                    f"--bind-ro=/dev/null:/etc/systemd/system/{unit}",
                    arguments,
                )

    def test_bluetooth_units_are_masked(self) -> None:
        expected = {
            "--bind-ro=/dev/null:/etc/systemd/system/bluetooth-mesh.service",
            "--bind-ro=/dev/null:/etc/systemd/system/bluetooth.service",
            "--bind-ro=/dev/null:/etc/systemd/system/bluetooth.target",
            "--bind-ro=/dev/null:/etc/systemd/system/dbus-org.bluez.service",
            "--bind-ro=/dev/null:/etc/systemd/user/"
            "dbus-org.bluez.obex.service",
            "--bind-ro=/dev/null:/etc/systemd/user/obex.service",
        }
        self.assertLessEqual(
            expected,
            set(launch_module._unit_mask_bind_arguments()),
        )

    def test_rootfs_fixups_only_create_var_home_symlink(self) -> None:
        launch_module._apply_rootfs_fixups(self.rootfs)

        var_home = self.rootfs / "var" / "home"
        self.assertTrue(var_home.is_symlink())
        self.assertEqual(os.readlink(var_home), "/home")
        for destination in launch_module.MASKED_UNIT_DESTINATIONS:
            with self.subTest(destination=destination):
                self.assertFalse(
                    (self.rootfs / destination.removeprefix("/")).exists()
                )

    def test_rootfs_fixups_drop_ping_capability_when_sockets_are_enabled(
        self,
    ) -> None:
        ping_group_range = Path(self.temporary.name) / "ping_group_range"
        ping_group_range.write_text("0 2147483647\n", encoding="utf-8")
        ping = self.rootfs / "usr" / "bin" / "ping"
        ping.parent.mkdir(parents=True)
        ping.write_bytes(b"ping")

        def remove_capability(
            descriptor: int,
            attribute: str,
        ) -> None:
            self.assertEqual(
                Path(f"/proc/self/fd/{descriptor}").resolve(),
                ping,
            )
            self.assertEqual(attribute, "security.capability")

        with (
            mock.patch.object(
                launch_module,
                "PING_GROUP_RANGE",
                ping_group_range,
            ),
            mock.patch.object(
                launch_module.os,
                "removexattr",
                side_effect=remove_capability,
            ) as removexattr,
        ):
            launch_module._apply_rootfs_fixups(self.rootfs)

        removexattr.assert_called_once()

    def test_rootfs_fixups_keep_ping_capability_for_restricted_sockets(
        self,
    ) -> None:
        ping_group_range = Path(self.temporary.name) / "ping_group_range"
        ping_group_range.write_text("1 0\n", encoding="utf-8")
        ping = self.rootfs / "usr" / "bin" / "ping"
        ping.parent.mkdir(parents=True)
        ping.write_bytes(b"ping")

        with (
            mock.patch.object(
                launch_module,
                "PING_GROUP_RANGE",
                ping_group_range,
            ),
            mock.patch.object(
                launch_module.os,
                "removexattr",
            ) as removexattr,
        ):
            launch_module._apply_rootfs_fixups(self.rootfs)

        removexattr.assert_not_called()

    def test_rootfs_fixups_keep_ping_capability_for_missing_sysctl(
        self,
    ) -> None:
        ping = self.rootfs / "usr" / "bin" / "ping"
        ping.parent.mkdir(parents=True)
        ping.write_bytes(b"ping")

        with (
            mock.patch.object(
                launch_module,
                "PING_GROUP_RANGE",
                Path(self.temporary.name) / "missing",
            ),
            mock.patch.object(
                launch_module.os,
                "removexattr",
            ) as removexattr,
        ):
            launch_module._apply_rootfs_fixups(self.rootfs)

        removexattr.assert_not_called()

    def test_rootfs_fixups_reject_ping_outside_rootfs(self) -> None:
        ping_group_range = Path(self.temporary.name) / "ping_group_range"
        ping_group_range.write_text("0 2147483647\n", encoding="utf-8")
        outside = Path(self.temporary.name) / "outside-ping"
        outside.write_bytes(b"ping")
        ping = self.rootfs / "usr" / "bin" / "ping"
        ping.parent.mkdir(parents=True)
        ping.symlink_to(outside)

        with (
            mock.patch.object(
                launch_module,
                "PING_GROUP_RANGE",
                ping_group_range,
            ),
            mock.patch.object(
                launch_module.os,
                "removexattr",
            ) as removexattr,
            self.assertLogs(launch_module.logger, level="ERROR") as logs,
        ):
            launch_module._apply_rootfs_fixups(self.rootfs)

        removexattr.assert_not_called()
        self.assertIn("outside the rootfs", logs.output[0])

    def test_invalid_name_is_rejected_before_state_access(self) -> None:
        with (
            mock.patch.object(launch_module.subprocess, "Popen") as run,
            self.assertRaises(core.SpacesError),
        ):
            launch_module.launch("../work")
        run.assert_not_called()

    def test_missing_space_is_rejected(self) -> None:
        self.space.rename(self.state_root / "other")
        with (
            mock.patch.object(launch_module.subprocess, "Popen") as run,
            self.assertRaises(core.SpacesError),
        ):
            launch_module.launch("work")
        run.assert_not_called()

    def test_symlinked_space_is_rejected(self) -> None:
        target = self.state_root / "target"
        self.space.rename(target)
        self.space.symlink_to(target, target_is_directory=True)
        with self.assertRaises(core.SpacesError):
            launch_module.launch("work")

    def test_missing_or_symlinked_rootfs_is_rejected(self) -> None:
        self._write_info()
        shutil.rmtree(self.rootfs)
        with self.assertRaises(core.SpacesError):
            launch_module.launch("work")

        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        self.rootfs.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(core.SpacesError):
            launch_module.launch("work")

    def test_missing_or_symlinked_home_is_rejected(self) -> None:
        self.home.rmdir()
        with self.assertRaises(core.SpacesError):
            launch_module.launch("work")

        outside = Path(self.temporary.name) / "home"
        outside.mkdir()
        self.home.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(core.SpacesError):
            launch_module.launch("work")

    def test_symlinked_root_home_is_rejected(self) -> None:
        outside = Path(self.temporary.name) / "root"
        outside.mkdir()
        self.root_home.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(core.SpacesError):
            launch_module.launch("work")

    def test_missing_invalid_or_symlinked_info_is_rejected(self) -> None:
        with self.assertRaises(core.SpacesError):
            launch_module.launch("work")

        info_path = self.space / "info.json"
        info_path.write_text("{", encoding="utf-8")
        with self.assertRaises(core.SpacesError):
            launch_module.launch("work")

        info_path.unlink()
        outside = Path(self.temporary.name) / "info.json"
        outside.write_text("{}", encoding="utf-8")
        info_path.symlink_to(outside)
        with self.assertRaises(core.SpacesError):
            launch_module.launch("work")

    def test_metadata_name_must_match_space(self) -> None:
        self._write_info(name="other")
        with (
            mock.patch.object(launch_module.subprocess, "Popen") as run,
            self.assertRaises(core.SpacesError),
        ):
            launch_module.launch("work")
        run.assert_not_called()


class UserFixupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.rootfs = self.root / "rootfs"
        self.etc = self.rootfs / "etc"
        self.etc.mkdir(parents=True)
        self.space_home = self.root / "home"
        self.space_home.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _user(
        self,
        *,
        uid: int | None = None,
        gid: int | None = None,
        name: str = "alice",
        permitted: tuple[str, ...] = (),
        administrator: bool = True,
    ) -> launch_module.SpaceUser:
        fixed_uid = os.getuid() if uid is None else uid
        fixed_gid = os.getgid() if gid is None else gid
        return launch_module.SpaceUser(
            uid=fixed_uid,
            gid=fixed_gid,
            name=name,
            host_home=self.root / "host" / name,
            space_home=self.space_home / name,
            guest_home=launch_module.PurePosixPath(f"/home/{name}"),
            permitted_home=permitted,
            administrator=administrator,
            desktop=False,
        )

    def _write_accounts(
        self,
        *,
        passwd_text: str,
        group_text: str,
        shadow_text: str = "",
        gshadow_text: str = "",
    ) -> None:
        (self.etc / "passwd").write_text(passwd_text, encoding="utf-8")
        (self.etc / "group").write_text(group_text, encoding="utf-8")
        if shadow_text:
            (self.etc / "shadow").write_text(shadow_text, encoding="utf-8")
        if gshadow_text:
            (self.etc / "gshadow").write_text(gshadow_text, encoding="utf-8")

    def test_reconcile_renames_and_locks_existing_uid(self) -> None:
        uid = os.getuid()
        gid = os.getgid()
        self._write_accounts(
            passwd_text=f"root:x:0:0::/root:/bin/sh\nold:x:{uid}:55::/old:/bin/zsh\n",
            group_text=f"root:x:0:\nstaff:x:{gid}:old\n",
            shadow_text="root:!:::::::\nold:$6$hash:::::::\n",
            gshadow_text="root:!::\nstaff:!::old\n",
        )
        user = self._user(name="alice")

        launch_module._reconcile_accounts(self.rootfs, (user,))

        passwd_records = launch_module._read_database(self.etc / "passwd", 7)
        account = next(record for record in passwd_records if record[0] == "alice")
        self.assertEqual(
            account,
            [
                "alice",
                "x",
                str(uid),
                str(gid),
                "",
                "/home/alice",
                "/bin/sh",
            ],
        )
        shadow = launch_module._read_database(self.etc / "shadow", 9)
        shadow_account = next(
            record for record in shadow if record[0] == "alice"
        )
        self.assertEqual(shadow_account[1], "!")
        group = launch_module._read_database(self.etc / "group", 4)
        group_account = next(record for record in group if record[0] == "staff")
        self.assertEqual(group_account[3], "alice")
        gshadow = launch_module._read_database(self.etc / "gshadow", 4)
        gshadow_account = next(
            record for record in gshadow if record[0] == "staff"
        )
        self.assertEqual(gshadow_account[3], "alice")

    def test_reconcile_creates_account_and_primary_group(self) -> None:
        self._write_accounts(
            passwd_text="root:!:0:0::/root:/bin/sh\n",
            group_text="root:x:0:\n",
        )
        user = self._user(uid=12345, gid=12346)

        launch_module._reconcile_accounts(self.rootfs, (user,))

        passwd_records = launch_module._read_database(self.etc / "passwd", 7)
        self.assertIn(
            ["alice", "!", "12345", "12346", "", "/home/alice", "/bin/sh"],
            passwd_records,
        )
        self.assertIn(
            ["alice", "x", "12346", ""],
            launch_module._read_database(self.etc / "group", 4),
        )

    def test_reconcile_adds_administrator_to_wheel_group(self) -> None:
        self._write_accounts(
            passwd_text=(
                "root:x:0:0::/root:/bin/sh\n"
                "alice:x:12345:12346::/home/alice:/bin/sh\n"
            ),
            group_text="root:x:0:\nwheel:x:10:bob\nalice:x:12346:\n",
            shadow_text="root:!:::::::\nalice:!:::::::\n",
            gshadow_text="root:!::\nwheel:!::bob\nalice:!::\n",
        )

        launch_module._reconcile_accounts(
            self.rootfs,
            (self._user(uid=12345, gid=12346, administrator=True),),
        )

        group = launch_module._read_database(self.etc / "group", 4)
        wheel = next(record for record in group if record[0] == "wheel")
        self.assertEqual(wheel[3], "bob,alice")
        gshadow = launch_module._read_database(self.etc / "gshadow", 4)
        wheel_shadow = next(record for record in gshadow if record[0] == "wheel")
        self.assertEqual(wheel_shadow[3], "bob,alice")

    def test_reconcile_uses_distribution_administrator_group(self) -> None:
        self._write_accounts(
            passwd_text=(
                "root:x:0:0::/root:/bin/sh\n"
                "alice:x:12345:12346::/home/alice:/bin/sh\n"
            ),
            group_text="root:x:0:\nsudo:x:27:\nalice:x:12346:\n",
            shadow_text="root:!:::::::\nalice:!:::::::\n",
            gshadow_text="root:!::\nsudo:!::\nalice:!::\n",
        )

        launch_module._reconcile_accounts(
            self.rootfs,
            (self._user(uid=12345, gid=12346, administrator=True),),
            "sudo",
        )

        group = launch_module._read_database(self.etc / "group", 4)
        sudo = next(record for record in group if record[0] == "sudo")
        self.assertEqual(sudo[3], "alice")
        self.assertNotIn("wheel", {record[0] for record in group})
        gshadow = launch_module._read_database(self.etc / "gshadow", 4)
        sudo_shadow = next(record for record in gshadow if record[0] == "sudo")
        self.assertEqual(sudo_shadow[3], "alice")

    def test_reconcile_creates_missing_administrator_group(self) -> None:
        self._write_accounts(
            passwd_text="root:x:0:0::/root:/bin/sh\n",
            group_text="root:x:0:\nreserved:x:999:\n",
            shadow_text="root:!:::::::\n",
            gshadow_text="root:!::\nreserved:!::\n",
        )

        launch_module._reconcile_accounts(
            self.rootfs,
            (self._user(uid=12345, gid=998, administrator=True),),
            "sudo",
        )

        group = launch_module._read_database(self.etc / "group", 4)
        sudo = next(record for record in group if record[0] == "sudo")
        self.assertEqual(sudo, ["sudo", "x", "997", "alice"])
        gshadow = launch_module._read_database(self.etc / "gshadow", 4)
        sudo_shadow = next(record for record in gshadow if record[0] == "sudo")
        self.assertEqual(sudo_shadow, ["sudo", "!", "", "alice"])

    def test_reconcile_removes_non_administrator_from_wheel_group(self) -> None:
        self._write_accounts(
            passwd_text=(
                "root:x:0:0::/root:/bin/sh\n"
                "alice:x:12345:12346::/home/alice:/bin/sh\n"
            ),
            group_text="root:x:0:\nwheel:x:10:bob,alice\nalice:x:12346:\n",
            shadow_text="root:!:::::::\nalice:!:::::::\n",
            gshadow_text="root:!::\nwheel:!::bob,alice\nalice:!::\n",
        )

        launch_module._reconcile_accounts(
            self.rootfs,
            (self._user(uid=12345, gid=12346, administrator=False),),
        )

        group = launch_module._read_database(self.etc / "group", 4)
        wheel = next(record for record in group if record[0] == "wheel")
        self.assertEqual(wheel[3], "bob")
        gshadow = launch_module._read_database(self.etc / "gshadow", 4)
        wheel_shadow = next(record for record in gshadow if record[0] == "wheel")
        self.assertEqual(wheel_shadow[3], "bob")

    def test_reconcile_uses_bash_when_available(self) -> None:
        binary = self.rootfs / "bin" / "bash"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o755)
        self._write_accounts(
            passwd_text=(
                "root:x:0:0::/root:/bin/sh\n"
                "alice:x:12345:12346::/old:/bin/sh\n"
            ),
            group_text="root:x:0:\nalice:x:12346:\n",
        )
        user = self._user(uid=12345, gid=12346)

        launch_module._reconcile_accounts(self.rootfs, (user,))

        passwd_records = launch_module._read_database(
            self.etc / "passwd",
            7,
        )
        account = next(record for record in passwd_records if record[0] == "alice")
        self.assertEqual(account[6], "/bin/bash")

    def test_reconcile_rejects_name_and_uid_on_different_accounts(self) -> None:
        self._write_accounts(
            passwd_text=(
                "root:x:0:0::/root:/bin/sh\n"
                "alice:x:2001:2001::/home/alice:/bin/sh\n"
                "other:x:2000:2000::/home/other:/bin/sh\n"
            ),
            group_text="root:x:0:\nalice:x:2001:\nother:x:2000:\n",
        )
        user = self._user(uid=2000, gid=2000)

        with self.assertRaises(core.SpacesError):
            launch_module._reconcile_accounts(self.rootfs, (user,))

    def test_reconcile_rejects_symlinked_account_database(self) -> None:
        outside = self.root / "passwd"
        outside.write_text("root:x:0:0::/root:/bin/sh\n", encoding="utf-8")
        (self.etc / "passwd").symlink_to(outside)
        (self.etc / "group").write_text("root:x:0:\n", encoding="utf-8")
        with self.assertRaises(core.SpacesError):
            launch_module._reconcile_accounts(self.rootfs, (self._user(),))

    def test_root_user_does_not_require_account_database_changes(self) -> None:
        root = launch_module.SpaceUser(
            uid=0,
            gid=0,
            name="root",
            host_home=Path("/root"),
            space_home=self.space_home / "root",
            guest_home=launch_module.PurePosixPath("/root"),
            permitted_home=(),
            desktop=False,
        )
        launch_module._reconcile_accounts(self.rootfs, (root,))

    def test_new_home_copies_skeleton_once(self) -> None:
        skeleton = self.etc / "skel"
        nested = skeleton / ".config"
        nested.mkdir(parents=True)
        profile = skeleton / ".profile"
        profile.write_text("profile\n", encoding="utf-8")
        profile.chmod(0o640)
        (nested / "settings").write_text("first\n", encoding="utf-8")
        (skeleton / "config-link").symlink_to(".config")
        user = self._user()

        launch_module._ensure_user_homes(self.rootfs, (user,))

        self.assertEqual((user.space_home / ".profile").read_text(), "profile\n")
        self.assertEqual((user.space_home / ".profile").stat().st_mode & 0o777, 0o640)
        self.assertTrue((user.space_home / "config-link").is_symlink())
        self.assertEqual(os.readlink(user.space_home / "config-link"), ".config")
        self.assertEqual(user.space_home.stat().st_mode & 0o777, 0o700)

        (nested / "settings").write_text("second\n", encoding="utf-8")
        launch_module._ensure_user_homes(self.rootfs, (user,))
        self.assertEqual(
            (user.space_home / ".config" / "settings").read_text(),
            "first\n",
        )

    def test_missing_skeleton_creates_session_paths(self) -> None:
        user = self._user()
        launch_module._ensure_user_homes(self.rootfs, (user,))
        self.assertEqual(
            {path.name for path in user.space_home.iterdir()},
            {".config"},
        )

    def test_symlinked_skeleton_is_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (self.etc / "skel").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(core.SpacesError):
            launch_module._ensure_user_homes(self.rootfs, (self._user(),))

    def test_failed_skeleton_copy_leaves_no_home(self) -> None:
        skeleton = self.etc / "skel"
        skeleton.mkdir()
        (skeleton / "file").write_text("data", encoding="utf-8")
        user = self._user()
        with (
            mock.patch.object(
                launch_module.shutil,
                "copy2",
                side_effect=OSError("copy failed"),
            ),
            self.assertRaises(core.SpacesError),
        ):
            launch_module._ensure_user_homes(self.rootfs, (user,))
        self.assertFalse(user.space_home.exists())
        self.assertEqual(list(self.space_home.iterdir()), [])

    def test_mount_plan_and_nspawn_escaping(self) -> None:
        user = self._user(permitted=("Project:One", "linked"))
        user.host_home.mkdir(parents=True)
        (user.host_home / "Project:One").mkdir()
        (user.host_home / "linked").symlink_to(
            user.host_home / "Project:One",
            target_is_directory=True,
        )
        user.space_home.mkdir()

        with self.assertLogs(launch_module.logger, level="WARNING"):
            available = launch_module._prepare_mounts((user,))
        mounts = launch_module._plan_mounts(
            available,
            frozenset({user.uid}),
        )
        self.assertEqual(len(mounts), 1)
        self.assertEqual(mounts[0].destination, "/home/alice/Project:One")
        self.assertEqual(
            mounts[0].source,
            (user.host_home / "Project:One").resolve(),
        )
        self.assertEqual(
            launch_module._bind_argument(mounts[0]),
            (
                f"--bind={user.host_home}/Project\\:One:"
                "/home/alice/Project\\:One"
            ),
        )
        self.assertTrue((user.space_home / "Project:One").is_dir())
        self.assertEqual(
            launch_module._plan_mounts(available, frozenset()),
            (),
        )

    def test_mount_destination_preparation_failure_is_skipped(self) -> None:
        user = self._user(permitted=("Projects",))
        user.host_home.mkdir(parents=True)
        (user.host_home / "Projects").mkdir()
        user.space_home.mkdir()
        original_close = os.close

        with (
            mock.patch.object(
                launch_module.os,
                "chown",
                side_effect=PermissionError("not permitted"),
            ),
            mock.patch.object(
                launch_module.os,
                "close",
                wraps=original_close,
            ) as close,
            self.assertLogs(launch_module.logger, level="WARNING"),
        ):
            available = launch_module._prepare_mounts((user,))

        self.assertEqual(available, ())
        close.assert_called_once()
        self.assertFalse((user.space_home / "Projects").exists())


class LoginAndMountWorkerTests(unittest.TestCase):
    def _user(
        self, uid: int, *, desktop: bool = False
    ) -> launch_module.SpaceUser:
        return launch_module.SpaceUser(
            uid=uid,
            gid=uid,
            name=f"user{uid}",
            host_home=Path(f"/home/user{uid}"),
            space_home=Path(f"/space/home/user{uid}"),
            guest_home=launch_module.PurePosixPath(f"/home/user{uid}"),
            permitted_home=(),
            desktop=desktop,
        )

    def test_eligible_states_include_lingering(self) -> None:
        users = tuple(self._user(uid) for uid in range(1000, 1005))
        monitor = mock.Mock()
        monitor.state.side_effect = [
            "active",
            "online",
            "lingering",
            "closing",
            "offline",
        ]
        self.assertEqual(
            launch_module._eligible_uids(monitor, users),
            frozenset({1000, 1001, 1002}),
        )

    def test_unknown_login_state_is_rejected(self) -> None:
        monitor = mock.Mock()
        monitor.state.return_value = "future-state"
        with self.assertRaises(core.SpacesError):
            launch_module._eligible_uids(monitor, (self._user(1000),))

    def test_login_monitor_flushes_expired_timeout(self) -> None:
        monitor = object.__new__(launch_module._LoginMonitor)
        monitor._monitor = mock.Mock()
        monitor._read_fd = 8
        monitor._library = mock.Mock()
        monitor._library.sd_login_monitor_get_fd.return_value = 9
        monitor._poll = mock.Mock()
        monitor._poll.poll.return_value = []

        with (
            mock.patch.object(
                launch_module._LoginMonitor,
                "_timeout_ms",
                return_value=0,
            ),
            mock.patch.object(
                launch_module._LoginMonitor,
                "_flush",
            ) as flush,
        ):
            self.assertTrue(monitor.wait())

        flush.assert_called_once_with()

    def test_registration_waits_for_guest_system_bus(self) -> None:
        worker = launch_module._MountWorker(
            "work",
            (),
            mock.Mock(),
            (),
            (),
            frozenset(),
        )
        worker._process = mock.Mock()
        worker._process.poll.return_value = None
        machine_unavailable = SimpleNamespace(returncode=1)
        machine_available = SimpleNamespace(returncode=0)
        shell_unavailable = SimpleNamespace(returncode=1)
        shell_available = SimpleNamespace(returncode=0)

        with (
            mock.patch.object(
                launch_module.subprocess,
                "run",
                side_effect=[
                    machine_unavailable,
                    machine_available,
                    shell_unavailable,
                    machine_available,
                    shell_available,
                ],
            ) as run,
            mock.patch.object(worker._stopping, "wait") as wait,
        ):
            self.assertTrue(worker._wait_until_registered())

        self.assertEqual(
            run.call_args_list,
            [
                mock.call(
                    [
                        launch_module.MACHINECTL,
                        "--quiet",
                        "--no-ask-password",
                        "show",
                        "--property=Leader",
                        "--value",
                        "work",
                    ],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ),
                mock.call(
                    [
                        launch_module.MACHINECTL,
                        "--quiet",
                        "--no-ask-password",
                        "show",
                        "--property=Leader",
                        "--value",
                        "work",
                    ],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ),
                mock.call(
                    [
                        launch_module.MACHINECTL,
                        "--quiet",
                        "--no-ask-password",
                        "--uid=root",
                        "--",
                        "shell",
                        "work",
                        "/usr/bin/true",
                    ],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ),
                mock.call(
                    [
                        launch_module.MACHINECTL,
                        "--quiet",
                        "--no-ask-password",
                        "show",
                        "--property=Leader",
                        "--value",
                        "work",
                    ],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ),
                mock.call(
                    [
                        launch_module.MACHINECTL,
                        "--quiet",
                        "--no-ask-password",
                        "--uid=root",
                        "--",
                        "shell",
                        "work",
                        "/usr/bin/true",
                    ],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ),
            ],
        )
        self.assertEqual(wait.call_count, 2)

    def test_unchanged_graphical_snapshot_skips_desktop_reconciliation(
        self,
    ) -> None:
        user = self._user(1000, desktop=True)
        graphical = launch_module.session.LoginSession(
            "2", True, False, "wayland", "user"
        )
        monitor = mock.Mock()
        monitor.state.return_value = "active"
        monitor.sessions.side_effect = [
            (
                graphical,
                launch_module.session.LoginSession(
                    "100", True, False, "tty", "user"
                ),
            ),
            (
                graphical,
                launch_module.session.LoginSession(
                    "101", True, False, "tty", "user"
                ),
            ),
        ]
        worker = launch_module._MountWorker(
            "work",
            (user,),
            monitor,
            (),
            (),
            frozenset({user.uid}),
        )
        worker._registered = True
        worker._process = mock.Mock()
        worker._desktop.reconcile = mock.Mock()

        worker._reconcile()
        worker._reconcile()

        worker._desktop.reconcile.assert_called_once_with(
            user, (graphical,), ()
        )

    def test_worker_rate_limits_monitor_reconciliation(self) -> None:
        user = self._user(1000)
        monitor = mock.Mock()
        monitor.state.return_value = "active"
        monitor.wait.side_effect = [True, False]
        worker = launch_module._MountWorker(
            "work",
            (user,),
            monitor,
            (),
            (),
            frozenset({user.uid}),
        )
        worker._registered = True
        process = mock.Mock()
        process.poll.return_value = None
        worker.attach(process)
        worker._desktop.reconcile = mock.Mock()

        with mock.patch.object(
            worker._stopping, "wait", return_value=False
        ) as rate_wait:
            worker._run()

        self.assertGreaterEqual(
            rate_wait.call_args.args[0],
            launch_module.LOGIN_RECONCILE_INTERVAL_SECONDS - 0.01,
        )
        worker._desktop.reconcile.assert_called_once_with(user, (), ())

    def test_lingering_keeps_mount_until_user_is_closing(self) -> None:
        user = self._user(1000)
        mount = launch_module.HomeMount(
            destination="/home/user1000/Projects",
            source=Path("/host/Projects"),
            uid=1000,
        )
        monitor = mock.Mock()
        monitor.state.return_value = "lingering"
        worker = launch_module._MountWorker(
            "work",
            (user,),
            monitor,
            (mount,),
            (mount,),
            frozenset({user.uid}),
        )
        worker._registered = True
        worker._process = mock.Mock()

        with (
            mock.patch.object(worker, "_remove") as remove,
            self.assertLogs(launch_module.logger, level="INFO"),
        ):
            worker._reconcile()
            remove.assert_not_called()
            monitor.state.return_value = "closing"
            worker._reconcile()
            remove.assert_called_once_with(mount)

    def test_login_and_logout_log_user_and_mounts(self) -> None:
        user = self._user(1000)
        mount = launch_module.HomeMount(
            destination="/home/user1000/Projects",
            source=Path("/host/Projects"),
            uid=1000,
        )
        monitor = mock.Mock()
        monitor.state.return_value = "offline"
        worker = launch_module._MountWorker(
            "work",
            (user,),
            monitor,
            (mount,),
            (),
            frozenset(),
        )
        worker._registered = True
        worker._process = mock.Mock()

        with (
            mock.patch.object(worker, "_add"),
            mock.patch.object(worker, "_remove"),
            self.assertLogs(
                launch_module.logger,
                level="INFO",
            ) as logs,
        ):
            monitor.state.return_value = "active"
            worker._reconcile()
            monitor.state.return_value = "offline"
            worker._reconcile()

        self.assertEqual(
            logs.output,
            [
                (
                    "INFO:spaces.launch:User user1000 (1000:1000) logged in; "
                    "mounted: /host/Projects -> /home/user1000/Projects."
                ),
                (
                    "INFO:spaces.launch:User user1000 (1000:1000) logged out; "
                    "unmounted: /host/Projects -> /home/user1000/Projects."
                ),
            ],
        )

    def test_initial_user_mounts_are_logged_after_registration(self) -> None:
        user = self._user(1000)
        mount = launch_module.HomeMount(
            destination="/home/user1000/Projects",
            source=Path("/host/Projects"),
            uid=1000,
        )
        monitor = mock.Mock()
        monitor.state.return_value = "active"
        monitor.wait.return_value = False
        worker = launch_module._MountWorker(
            "work",
            (user,),
            monitor,
            (mount,),
            (mount,),
            frozenset({user.uid}),
        )
        worker._registered = True
        process = mock.Mock()
        process.poll.return_value = None
        worker.attach(process)

        with self.assertLogs(
            launch_module.logger,
            level="INFO",
        ) as logs:
            worker._run()

        self.assertEqual(
            logs.output,
            [
                (
                    "INFO:spaces.launch:User user1000 (1000:1000) mounted at "
                    "space launch: /host/Projects -> "
                    "/home/user1000/Projects."
                )
            ],
        )

    def test_failed_runtime_mount_is_skipped(self) -> None:
        user = self._user(1000)
        failed_mount = launch_module.HomeMount(
            destination="/home/user1000/Documents",
            source=Path("/host/Documents"),
            uid=1000,
        )
        mounted = launch_module.HomeMount(
            destination="/home/user1000/Projects",
            source=Path("/host/Projects"),
            uid=1000,
        )
        monitor = mock.Mock()
        monitor.state.return_value = "active"
        worker = launch_module._MountWorker(
            "work",
            (user,),
            monitor,
            (failed_mount, mounted),
            (),
            frozenset(),
        )
        worker._registered = True
        worker._process = mock.Mock()

        with (
            mock.patch.object(
                worker,
                "_add",
                side_effect=[
                    launch_module.subprocess.CalledProcessError(
                        1,
                        ["machinectl", "bind"],
                    ),
                    None,
                ],
            ) as add,
            self.assertLogs(launch_module.logger, level="WARNING"),
        ):
            worker._reconcile()

        self.assertEqual(
            add.call_args_list,
            [mock.call(failed_mount), mock.call(mounted)],
        )
        self.assertEqual(worker._mounted, {mounted})

    def test_remove_uses_systemd_lazy_unmount(self) -> None:
        monitor = mock.Mock()
        worker = launch_module._MountWorker(
            "work",
            (),
            monitor,
            (),
            (),
            frozenset(),
        )
        mount = launch_module.HomeMount(
            destination="/home/alice/Projects",
            source=Path("/host/Projects"),
            uid=1000,
        )
        completed = SimpleNamespace(stdout="home-alice-Projects.mount\n")
        with mock.patch.object(
            launch_module.subprocess,
            "run",
            side_effect=[completed, mock.DEFAULT, mock.DEFAULT],
        ) as run:
            worker._remove(mount)

        self.assertEqual(
            run.call_args_list,
            [
                mock.call(
                    [
                        launch_module.SYSTEMD_ESCAPE,
                        "--path",
                        "--suffix=mount",
                        "/home/alice/Projects",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ),
                mock.call(
                    [
                        launch_module.SYSTEMCTL,
                        "--machine=work",
                        "--no-ask-password",
                        "set-property",
                        "--runtime",
                        "home-alice-Projects.mount",
                        "LazyUnmount=yes",
                    ],
                    check=True,
                ),
                mock.call(
                    [
                        launch_module.SYSTEMD_UMOUNT,
                        "--machine=work",
                        "--no-ask-password",
                        "--quiet",
                        "/home/alice/Projects",
                    ],
                    check=True,
                ),
            ],
        )

    def test_add_uses_machinectl_bind(self) -> None:
        worker = launch_module._MountWorker(
            "work",
            (),
            mock.Mock(),
            (),
            (),
            frozenset(),
        )
        mount = launch_module.HomeMount(
            destination="/home/alice/Projects",
            source=Path("/host/Projects"),
            uid=1000,
        )
        with mock.patch.object(launch_module.subprocess, "run") as run:
            worker._add(mount)
        run.assert_called_once_with(
            [
                launch_module.MACHINECTL,
                "--quiet",
                "--no-ask-password",
                "--mkdir",
                "bind",
                "work",
                "/host/Projects",
                "/home/alice/Projects",
            ],
            check=True,
        )


if __name__ == "__main__":
    unittest.main()
