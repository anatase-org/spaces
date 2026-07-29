from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest import mock

from spaces import devices
from spaces import launch


def metadata(
    *,
    tags: tuple[str, ...] = (),
    properties: tuple[str, ...] = (),
    subsystems: tuple[str, ...] = (),
) -> devices.DeviceMetadata:
    return devices.DeviceMetadata(
        tags=frozenset(tags),
        properties=frozenset(properties),
        subsystems=frozenset(subsystems),
    )


def node(
    name: str,
    major: int,
    minor: int,
    *,
    kind: str = "c",
) -> devices.DeviceNode:
    return devices.DeviceNode(
        destination=PurePosixPath("/dev") / name,
        source=Path("/dev") / name,
        kind=kind,
        major=major,
        minor=minor,
    )


class DeviceDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.device_root = Path(self.temporary.name) / "dev"
        self.device_root.mkdir()
        self.original_lstat = Path.lstat

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _discover(
        self,
        level: str,
        definitions: dict[
            str,
            tuple[str, int, int, int, devices.DeviceMetadata],
        ],
        aliases: dict[str, str] | None = None,
    ) -> tuple[devices.DeviceNode, ...]:
        fake_stats: dict[Path, SimpleNamespace] = {}
        metadata_by_number: dict[tuple[str, int], devices.DeviceMetadata] = {}
        for relative, (
            kind,
            major,
            minor,
            gid,
            device_metadata,
        ) in definitions.items():
            path = self.device_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            mode = stat.S_IFCHR if kind == "c" else stat.S_IFBLK
            device_number = os.makedev(major, minor)
            fake_stats[path] = SimpleNamespace(
                st_mode=mode | 0o660,
                st_rdev=device_number,
                st_gid=gid,
            )
            metadata_by_number[(kind, device_number)] = device_metadata
        for relative, target in (aliases or {}).items():
            path = self.device_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(target)

        def lstat(path: Path) -> object:
            return fake_stats.get(path, self.original_lstat(path))

        def read_metadata(
            kind: str,
            device_number: int,
        ) -> devices.DeviceMetadata:
            return metadata_by_number[(kind, device_number)]

        with (
            mock.patch.object(Path, "lstat", lstat),
            mock.patch.object(
                devices.grp,
                "getgrnam",
                return_value=SimpleNamespace(gr_gid=44),
            ),
        ):
            return devices.discover(
                level,
                device_root=self.device_root,
                metadata_reader=read_metadata,
            )

    def test_basic_includes_user_video_and_controller_devices(self) -> None:
        definitions = {
            "snd/pcmC0D0p": ("c", 116, 16, 100, metadata(tags=("uaccess",))),
            "dri/renderD128": (
                "c",
                226,
                128,
                100,
                metadata(subsystems=("drm",)),
            ),
            "video0": (
                "c",
                81,
                0,
                100,
                metadata(subsystems=("video4linux",)),
            ),
            "media0": (
                "c",
                234,
                0,
                100,
                metadata(subsystems=("media",)),
            ),
            "video-group": ("c", 81, 1, 44, metadata()),
            "input/js0": (
                "c",
                13,
                0,
                100,
                metadata(
                    tags=("uaccess",),
                    properties=("ID_INPUT_JOYSTICK",),
                    subsystems=("input",),
                ),
            ),
            "input/event0": (
                "c",
                13,
                64,
                100,
                metadata(
                    tags=("uaccess",),
                    properties=("ID_INPUT_KEYBOARD",),
                    subsystems=("input",),
                ),
            ),
            "input/event1": (
                "c",
                13,
                65,
                100,
                metadata(tags=("uaccess",), subsystems=("input",)),
            ),
            "unknown": ("c", 240, 0, 100, metadata()),
        }

        found = {
            str(item.destination)
            for item in self._discover("basic", definitions)
        }

        self.assertEqual(
            found,
            {
                "/dev/snd/pcmC0D0p",
                "/dev/dri/renderD128",
                "/dev/video0",
                "/dev/media0",
                "/dev/video-group",
                "/dev/input/js0",
            },
        )

    def test_basic_and_admin_exclude_every_positive_security_match(self) -> None:
        definitions = {
            "tpm0": ("c", 10, 224, 100, metadata()),
            "vtpmx": ("c", 10, 232, 100, metadata()),
            "tee0": (
                "c",
                10,
                59,
                100,
                metadata(subsystems=("tee",)),
            ),
            "hidraw-token": (
                "c",
                238,
                0,
                100,
                metadata(
                    tags=("uaccess",),
                    properties=("ID_SECURITY_TOKEN",),
                ),
            ),
            "usb-smartcard": (
                "c",
                189,
                1,
                100,
                metadata(properties=("ID_SMARTCARD_READER",)),
            ),
            "wallet": (
                "c",
                238,
                2,
                100,
                metadata(properties=("ID_HARDWARE_WALLET",)),
            ),
            "tagged": (
                "c",
                240,
                1,
                100,
                metadata(tags=("security-device",)),
            ),
        }

        for level in ("basic", "admin"):
            with self.subTest(level=level):
                self.assertEqual(self._discover(level, definitions), ())

    def test_admin_includes_input_storage_and_unclassifiable_nodes(self) -> None:
        definitions = {
            "input/event0": (
                "c",
                13,
                64,
                100,
                metadata(properties=("ID_INPUT_KEYBOARD",)),
            ),
            "sda": ("b", 8, 0, 6, metadata(subsystems=("block",))),
            "unclassifiable": ("c", 240, 0, 100, metadata()),
            "tpmidi": ("c", 240, 1, 100, metadata()),
        }

        found = {
            str(item.destination)
            for item in self._discover("admin", definitions)
        }

        self.assertEqual(
            found,
            {
                "/dev/input/event0",
                "/dev/sda",
                "/dev/unclassifiable",
                "/dev/tpmidi",
            },
        )

    def test_full_includes_security_and_storage_but_leaves_api_dev_managed(
        self,
    ) -> None:
        definitions = {
            "console": ("c", 5, 1, 0, metadata()),
            "pts/7": ("c", 136, 7, 100, metadata()),
            "tpm0": (
                "c",
                10,
                224,
                100,
                metadata(subsystems=("tpm",)),
            ),
            "input/event0": (
                "c",
                13,
                64,
                100,
                metadata(properties=("ID_INPUT_KEYBOARD",)),
            ),
            "sda": ("b", 8, 0, 6, metadata(subsystems=("block",))),
            "dm-0": ("b", 253, 0, 6, metadata(subsystems=("block",))),
        }

        discovered = self._discover(
            "full",
            definitions,
            aliases={
                "mapper/cryptroot": "../dm-0",
                "outside": "/dev/null",
            },
        )
        found = {str(item.destination) for item in discovered}

        self.assertEqual(
            found,
            {
                "/dev/dm-0",
                "/dev/input/event0",
                "/dev/mapper/cryptroot",
                "/dev/sda",
                "/dev/tpm0",
            },
        )
        alias = next(
            item
            for item in discovered
            if str(item.destination) == "/dev/mapper/cryptroot"
        )
        self.assertEqual(alias.source, self.device_root / "dm-0")

    def test_discovery_does_not_follow_device_symlinks(self) -> None:
        target = self.device_root / "target"
        target.touch()
        (self.device_root / "alias").symlink_to(target)
        target_stat = SimpleNamespace(
            st_mode=stat.S_IFCHR | 0o660,
            st_rdev=os.makedev(240, 0),
            st_gid=100,
        )

        def lstat(path: Path) -> object:
            if path == target:
                return target_stat
            return self.original_lstat(path)

        with (
            mock.patch.object(Path, "lstat", lstat),
            mock.patch.object(
                devices.grp,
                "getgrnam",
                return_value=SimpleNamespace(gr_gid=44),
            ),
        ):
            found = devices.discover(
                "admin",
                device_root=self.device_root,
                metadata_reader=lambda _kind, _number: metadata(),
            )

        self.assertEqual(
            tuple(str(item.destination) for item in found),
            ("/dev/target",),
        )


class DevicePolicyTests(unittest.TestCase):
    def test_filtered_policy_is_closed_and_atomically_replaced(self) -> None:
        allowed = node("dri/renderD128", 226, 128)
        with mock.patch.object(launch.subprocess, "run") as run:
            launch._set_device_policy("work", "basic", (allowed,))

        run.assert_called_once_with(
            [
                "/usr/bin/busctl",
                "call",
                "org.freedesktop.systemd1",
                "/org/freedesktop/systemd1",
                "org.freedesktop.systemd1.Manager",
                "SetUnitProperties",
                "sba(sv)",
                "spaces@work.service",
                "true",
                "2",
                "DevicePolicy",
                "s",
                "closed",
                "DeviceAllow",
                "a(ss)",
                "4",
                "/dev/net/tun",
                "rwm",
                "char-pts",
                "rw",
                "/dev/fuse",
                "rwm",
                "/dev/char/226:128",
                "rw",
            ],
            check=True,
        )

    def test_full_policy_is_unrestricted_and_clears_allowances(self) -> None:
        with mock.patch.object(launch.subprocess, "run") as run:
            launch._set_device_policy("work", "full")

        arguments = run.call_args.args[0]
        self.assertIn("auto", arguments)
        self.assertEqual(arguments[-1], "0")

    def test_bind_arguments_cover_disabled_filtered_and_full(self) -> None:
        filtered = devices.DeviceNode(
            destination=PurePosixPath("/dev/snd/a:b"),
            source=Path("/dev/snd/a:b"),
            kind="c",
            major=116,
            minor=1,
        )
        self.assertEqual(launch._device_bind_arguments("disabled", ()), ())
        self.assertEqual(
            launch._device_bind_arguments("basic", (filtered,)),
            ("--bind=/dev/snd/a\\:b:/dev/snd/a\\:b",),
        )
        self.assertEqual(
            launch._device_bind_arguments("full", (filtered,)),
            ("--bind=/dev/snd/a\\:b:/dev/snd/a\\:b",),
        )


class DeviceWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.udev = mock.Mock()
        self.monitor = mock.Mock()
        self.existing = node("existing", 240, 0)
        self.added = node("added", 240, 1)
        self.worker = launch._DeviceWorker(
            "work",
            "admin",
            self.udev,
            self.monitor,
            (self.existing,),
        )

    def test_addition_extends_allowlist_before_binding(self) -> None:
        events: list[str] = []
        with (
            mock.patch.object(
                launch.devices,
                "discover",
                return_value=(self.existing, self.added),
            ),
            mock.patch.object(
                launch,
                "_set_device_policy",
                side_effect=lambda *_args: events.append("allow"),
            ) as policy,
            mock.patch.object(
                launch.subprocess,
                "run",
                side_effect=lambda *_args, **_kwargs: events.append("bind"),
            ) as run,
        ):
            self.worker._reconcile()

        self.assertEqual(events, ["allow", "bind"])
        self.assertEqual(
            set(policy.call_args.args[2]),
            {self.existing, self.added},
        )
        self.assertEqual(
            run.call_args.args[0][-3:],
            ["work", "/dev/added", "/dev/added"],
        )

    def test_failed_addition_rolls_back_allowance_and_is_skipped(self) -> None:
        with (
            mock.patch.object(
                launch.devices,
                "discover",
                return_value=(self.existing, self.added),
            ),
            mock.patch.object(launch, "_set_device_policy") as policy,
            mock.patch.object(
                launch.subprocess,
                "run",
                side_effect=subprocess.CalledProcessError(1, "machinectl"),
            ),
        ):
            self.worker._reconcile()

        self.assertEqual(policy.call_count, 2)
        self.assertEqual(
            set(policy.call_args_list[0].args[2]),
            {self.existing, self.added},
        )
        self.assertEqual(
            set(policy.call_args_list[1].args[2]),
            {self.existing},
        )
        self.assertEqual(self.worker._mounted, {self.existing})

    def test_removal_unmounts_before_shrinking_allowlist(self) -> None:
        events: list[str] = []
        with (
            mock.patch.object(
                launch.devices,
                "discover",
                return_value=(),
            ),
            mock.patch.object(
                launch,
                "_unmount_in_machine",
                side_effect=lambda *_args: events.append("unmount"),
            ),
            mock.patch.object(
                launch,
                "_set_device_policy",
                side_effect=lambda *_args: events.append("shrink"),
            ) as policy,
        ):
            self.worker._reconcile()

        self.assertEqual(events, ["unmount", "shrink"])
        self.assertEqual(tuple(policy.call_args.args[2]), ())
        self.assertEqual(self.worker._mounted, set())

    def test_monitor_failure_terminates_the_space(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        self.worker._process = process
        self.worker._attached.set()
        self.monitor.wait.side_effect = OSError("monitor failed")

        with (
            mock.patch.object(
                self.worker,
                "_wait_until_registered",
                return_value=True,
            ),
            mock.patch.object(self.worker, "_reconcile"),
        ):
            self.worker._run()

        process.send_signal.assert_called_once_with(launch.signal.SIGTERM)

    def test_revocation_failure_terminates_the_space(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        self.worker._process = process
        self.worker._attached.set()

        with (
            mock.patch.object(
                self.worker,
                "_wait_until_registered",
                return_value=True,
            ),
            mock.patch.object(
                self.worker,
                "_reconcile",
                side_effect=subprocess.CalledProcessError(1, "unmount"),
            ),
        ):
            self.worker._run()

        process.send_signal.assert_called_once_with(launch.signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
