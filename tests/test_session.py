from __future__ import annotations

import json
import os
import pwd
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest import mock

from spaces import core, session


def graphical(
    session_id: str = "2",
    *,
    active: bool = True,
    remote: bool = False,
    session_type: str = "wayland",
    session_class: str = "user",
) -> session.LoginSession:
    return session.LoginSession(
        session_id,
        active,
        remote,
        session_type,
        session_class,
    )


def user(root: Path, *, desktop: bool = True, uid: int | None = None) -> object:
    actual_uid = os.getuid() if uid is None else uid
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        uid=actual_uid,
        gid=os.getgid(),
        name=pwd.getpwuid(os.getuid()).pw_name,
        host_home=home,
        space_home=root / "space-home",
        guest_home=PurePosixPath(f"/home/{pwd.getpwuid(os.getuid()).pw_name}"),
        desktop=desktop,
    )


class GraphicalSessionTests(unittest.TestCase):
    def test_selects_only_matching_active_local_graphical_user_session(self) -> None:
        records = (
            graphical("1", session_type="tty"),
            graphical("2"),
            graphical("3", remote=True),
            graphical("4", session_class="greeter"),
            graphical("5", active=False),
        )
        self.assertEqual(
            session.select_graphical_session(
                records, {"XDG_SESSION_ID": "2"}
            ),
            records[1],
        )
        for session_id in ("1", "3", "4", "5", "missing"):
            with self.subTest(session_id=session_id):
                self.assertIsNone(
                    session.select_graphical_session(
                        records, {"XDG_SESSION_ID": session_id}
                    )
                )
        self.assertIsNone(session.select_graphical_session(records, {}))

    def test_ambiguous_matching_records_are_rejected(self) -> None:
        duplicate = graphical("2")
        self.assertIsNone(
            session.select_graphical_session(
                (duplicate, duplicate), {"XDG_SESSION_ID": "2"}
            )
        )

    def test_host_manager_environment_is_sanitized(self) -> None:
        completed = subprocess.CompletedProcess(
            [],
            0,
            stdout=(
                "DISPLAY=:0\n"
                "WAYLAND_DISPLAY=wayland-0\n"
                "XDG_SESSION_ID=2\n"
                "XDG_RUNTIME_DIR=/run/user/1000\n"
                "DBUS_SESSION_BUS_ADDRESS=unix:path=/host/bus\n"
                "SSH_AUTH_SOCK=/run/user/1000/agent\n"
            ),
        )
        with mock.patch.object(
            session.subprocess, "run", return_value=completed
        ) as run:
            environment = session.host_manager_environment(
                SimpleNamespace(uid=1000, gid=100)
            )
        self.assertEqual(
            environment,
            {
                "DISPLAY": ":0",
                "WAYLAND_DISPLAY": "wayland-0",
                "XDG_SESSION_ID": "2",
                "XDG_RUNTIME_DIR": "/run/user/1000",
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/host/bus",
            },
        )
        self.assertEqual(
            run.call_args.args[0],
            [
                "/usr/bin/systemctl",
                "--user",
                "--no-ask-password",
                "show-environment",
            ],
        )
        self.assertEqual(
            run.call_args.kwargs["env"],
            {
                "DBUS_SESSION_BUS_ADDRESS": (
                    "unix:path=/run/user/1000/bus"
                ),
                "XDG_RUNTIME_DIR": "/run/user/1000",
            },
        )
        self.assertEqual(run.call_args.kwargs["user"], 1000)
        self.assertEqual(run.call_args.kwargs["group"], 100)


class DesktopPathTests(unittest.TestCase):
    def test_prepare_user_paths_are_owned_and_include_vscode_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            session.prepare_user_paths(
                home, os.getuid(), os.getgid(), "alice"
            )
            self.assertTrue((home / ".config").is_dir())
            self.assertTrue((home / ".config" / "gtk-3.0").is_dir())
            self.assertTrue((home / ".config" / "gtk-4.0").is_dir())
            self.assertTrue((home / ".config" / "fontconfig").is_dir())
            self.assertTrue((home / ".config" / "kdeglobals").is_file())
            self.assertEqual((home / ".config").stat().st_uid, os.getuid())

    def test_validated_source_rejects_symlink_escape_and_wrong_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "allowed"
            root.mkdir()
            regular = root / "file"
            regular.write_text("data", encoding="utf-8")
            outside = Path(temporary) / "outside"
            outside.write_text("data", encoding="utf-8")
            (root / "escape").symlink_to(outside)
            account = pwd.getpwuid(os.getuid())

            checked = session._validated_source(
                regular,
                roots=(root,),
                kinds=(stat.S_IFREG,),
                user=account,
                owner=os.getuid(),
            )
            self.assertEqual(checked[0], regular.resolve())
            with self.assertRaises(core.SpacesError):
                session._validated_source(
                    root / "escape",
                    roots=(root,),
                    kinds=(stat.S_IFREG,),
                    user=account,
                )
            with self.assertRaises(core.SpacesError):
                session._validated_source(
                    regular,
                    roots=(root,),
                    kinds=(stat.S_IFDIR,),
                    user=account,
                )

    def test_plan_uses_fixed_destinations_and_excludes_host_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            desktop_user = user(root)
            metadata = SimpleNamespace(
                st_mode=stat.S_IFSOCK | 0o700,
                st_uid=desktop_user.uid,
                st_gid=desktop_user.gid,
                st_dev=1,
                st_ino=2,
            )

            def validate(path: Path, **_kwargs: object) -> object:
                if path.name in {"wayland-0", "native", "pipewire-0"}:
                    return path, metadata
                return None

            environment = {
                "XDG_SESSION_ID": "2",
                "XDG_RUNTIME_DIR": f"/run/user/{desktop_user.uid}",
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/host/bus",
                "WAYLAND_DISPLAY": "wayland-0",
                "PULSE_SERVER": (
                    f"unix:/run/user/{desktop_user.uid}/pulse/native"
                ),
                "QT_SCALE_FACTOR": "1.5",
                "KDE_FULL_SESSION": "true",
                "KDE_SESSION_VERSION": "6",
                "XDG_CURRENT_DESKTOP": "KDE",
            }
            with (
                mock.patch.object(
                    session.pwd,
                    "getpwuid",
                    return_value=pwd.getpwuid(os.getuid()),
                ),
                mock.patch.object(
                    session, "_validated_source", side_effect=validate
                ),
            ):
                plan = session._plan(
                    desktop_user,
                    graphical(),
                    environment,
                    root / "generated",
                )

            destination_root = f"/run/spaces/desktop/{desktop_user.uid}"
            self.assertEqual(
                plan.environment["WAYLAND_DISPLAY"],
                f"{destination_root}/wayland/wayland-0",
            )
            self.assertEqual(
                plan.environment["PULSE_SERVER"],
                f"unix:{destination_root}/pulse/native",
            )
            self.assertEqual(plan.environment["QT_SCALE_FACTOR"], "1.5")
            self.assertNotIn("KDE_FULL_SESSION", plan.environment)
            self.assertEqual(
                plan.environment["KDE_SESSION_VERSION"], "6"
            )
            self.assertEqual(
                plan.environment["XDG_CURRENT_DESKTOP"], "Spaces:KDE"
            )
            self.assertEqual(
                plan.environment["XDG_CONFIG_DIRS"],
                "/run/spaces-host/config:/etc/xdg",
            )
            self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", plan.environment)
            self.assertNotIn("XDG_RUNTIME_DIR", plan.environment)
            self.assertNotIn("XDG_SESSION_ID", plan.environment)
            self.assertTrue(
                plan.environment["XDG_DATA_DIRS"].startswith(
                    f"{destination_root}/open-data:"
                )
            )
            self.assertTrue(
                all(
                    binding.destination.startswith(destination_root)
                    for binding in plan.binds
                )
            )

    def test_generated_open_defaults_cover_mime_types_and_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system_types = root / "rootfs/usr/share/mime/types"
            user_types = root / "home/.local/share/mime/types"
            system_types.parent.mkdir(parents=True)
            user_types.parent.mkdir(parents=True)
            system_types.write_text(
                "text/plain\napplication/pdf\n", encoding="utf-8"
            )
            user_types.write_text("image/x-test\n", encoding="utf-8")
            outside = root / "outside-types"
            outside.write_text("application/x-escaped\n", encoding="utf-8")
            escaped = root / "home/.local/share/mime/escaped"
            escaped.symlink_to(outside)
            data_root = root / "generated/open-data"

            self.assertTrue(
                session._write_open_data(
                    data_root,
                    (
                        (system_types, root),
                        (user_types, root),
                        (escaped, escaped.parent),
                    ),
                )
            )
            desktop = (
                data_root / "applications" / session.OPEN_DESKTOP_ID
            ).read_text(encoding="utf-8")
            defaults = (
                data_root / "applications" / "mimeapps.list"
            ).read_text(encoding="utf-8")
            self.assertIn("NoDisplay=true", desktop)
            self.assertIn(
                "Exec=/run/spaces-host/bin/spaces-open %u", desktop
            )
            self.assertNotIn("application/x-escaped", defaults)
            for mime_type in (
                "text/plain",
                "application/pdf",
                "image/x-test",
                "inode/directory",
                "x-scheme-handler/http",
                "x-scheme-handler/calendar",
            ):
                self.assertIn(
                    f"{mime_type}={session.OPEN_DESKTOP_ID};", defaults
                )

            self.assertFalse(
                session._write_open_data(
                    data_root,
                    ((system_types, root), (user_types, root)),
                )
            )
            system_types.write_text(
                "text/plain\napplication/pdf\napplication/x-new\n",
                encoding="utf-8",
            )
            self.assertTrue(
                session._write_open_data(
                    data_root,
                    ((system_types, root), (user_types, root)),
                )
            )
            self.assertIn(
                f"application/x-new={session.OPEN_DESKTOP_ID};",
                (data_root / "applications" / "mimeapps.list").read_text(
                    encoding="utf-8"
                ),
            )

    def test_open_mappings_are_pinned_and_reject_symlink_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rootfs = root / "rootfs"
            space_home = root / "home"
            forwarded = root / "forwarded"
            for directory in (
                rootfs,
                space_home / "root",
                forwarded,
            ):
                directory.mkdir(parents=True)
            mapped_file = root / "kdeglobals"
            mapped_file.write_text("[KDE]\n", encoding="utf-8")
            metadata = mapped_file.stat()
            plan = session.DesktopPlan(
                "2",
                (
                    session.DesktopBind(
                        "/home/alice/.config/kdeglobals",
                        mapped_file,
                        metadata.st_dev,
                        metadata.st_ino,
                    ),
                ),
                {},
                open_mappings=(
                    session.OpenPathMapping(
                        "/home/alice/Projects", forwarded
                    ),
                ),
            )

            descriptors, arguments = session._open_mapping_descriptors(
                plan, rootfs, space_home
            )
            try:
                destinations = {
                    arguments[index + 1]
                    for index, value in enumerate(arguments)
                    if value == "--map"
                }
                self.assertEqual(
                    destinations,
                    {
                        "/",
                        "/home",
                        "/root",
                        "/home/alice/Projects",
                        "/home/alice/.config/kdeglobals",
                    },
                )
                self.assertTrue(
                    all(
                        os.fstat(descriptor).st_ino
                        for descriptor in descriptors
                    )
                )
            finally:
                for descriptor in descriptors:
                    os.close(descriptor)

            escaped = root / "escaped"
            escaped.symlink_to(forwarded, target_is_directory=True)
            unsafe = session.DesktopPlan(
                "2",
                (),
                {},
                open_mappings=(
                    session.OpenPathMapping(
                        "/home/alice/Escaped", escaped
                    ),
                ),
            )
            with self.assertRaises(core.SpacesError):
                session._open_mapping_descriptors(
                    unsafe, rootfs, space_home
                )

    def test_remote_x11_display_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desktop_user = user(Path(temporary))
            with (
                mock.patch.object(
                    session.pwd,
                    "getpwuid",
                    return_value=pwd.getpwuid(os.getuid()),
                ),
                self.assertRaises(core.SpacesError),
            ):
                session._plan(
                    desktop_user,
                    graphical(session_type="x11"),
                    {"DISPLAY": "localhost:10.0"},
                    Path(temporary) / "generated",
                )


class StatusAndEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temporary.name)
        self.runtime_patch = mock.patch.object(
            session, "RUNTIME_ROOT", self.runtime
        )
        self.state_patch = mock.patch.object(
            core, "STATE_ROOT", self.runtime / "state"
        )
        self.runtime_patch.start()
        self.state_patch.start()
        (core.STATE_ROOT / "work").mkdir(parents=True)

    def tearDown(self) -> None:
        self.state_patch.stop()
        self.runtime_patch.stop()
        self.temporary.cleanup()

    def test_status_is_atomic_and_pending_barrier_returns_allowlist(self) -> None:
        session._write_record(
            "work", 1000, "active", "2", {"DISPLAY": ":0"}
        )
        path = core.STATE_ROOT / "work" / "env" / "1000.json"
        self.assertEqual(json.loads(path.read_text())["state"], "active")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            session.desktop_environment("work", 1000),
            {"DISPLAY": ":0"},
        )

    def test_inactive_status_clears_environment_in_same_record(self) -> None:
        session._write_record(
            "work", 1000, "active", "2", {"DISPLAY": ":0"}
        )
        session.set_status("work", 1000, "inactive")
        self.assertEqual(session.desktop_environment("work", 1000), {})
        self.assertEqual(
            session._read_record("work", 1000),
            ("inactive", None, {}),
        )

    def test_environment_file_rejects_host_bus_and_unsafe_permissions(self) -> None:
        with self.assertRaises(core.SpacesError):
            session._write_record(
                "work",
                1000,
                "active",
                "2",
                {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/host/bus"},
            )

        session._write_record(
            "work", 1000, "active", "2", {"DISPLAY": ":0"}
        )
        path = core.STATE_ROOT / "work" / "env" / "1000.json"
        path.chmod(0o644)
        with self.assertRaises(core.SpacesError):
            session.desktop_environment("work", 1000)

    def test_initialize_status_respects_permission_and_root(self) -> None:
        enabled = user(self.runtime / "one", uid=1000)
        disabled = user(self.runtime / "two", desktop=False, uid=1001)
        root = user(self.runtime / "three", uid=0)
        session._write_record(
            "work", 1000, "active", "old", {"DISPLAY": ":9"}
        )
        session.initialize_status("work", (enabled, disabled, root))
        self.assertEqual(
            session._read_record("work", 1000),
            ("pending", None, {}),
        )
        self.assertEqual(session._read_status("work", 1000), "pending")
        self.assertEqual(session._read_status("work", 1001), "inactive")
        self.assertEqual(session._read_status("work", 0), "inactive")


class DesktopControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.desktop_user = user(self.root)
        self.runtime_patch = mock.patch.object(
            session, "RUNTIME_ROOT", self.root / "runtime"
        )
        self.state_patch = mock.patch.object(
            core, "STATE_ROOT", self.root / "state"
        )
        self.runtime_patch.start()
        self.state_patch.start()
        (core.STATE_ROOT / "work").mkdir(parents=True)
        self.controller = session.DesktopController(
            "work", (self.desktop_user,)
        )

    def tearDown(self) -> None:
        self.state_patch.stop()
        self.runtime_patch.stop()
        self.temporary.cleanup()

    def test_mount_pins_identity_and_is_read_only(self) -> None:
        source = self.root / "socket"
        source.write_text("", encoding="utf-8")
        metadata = source.stat()
        binding = session.DesktopBind(
            "/run/spaces/desktop/1000/socket",
            source,
            metadata.st_dev,
            metadata.st_ino,
        )
        with mock.patch.object(session.subprocess, "run") as run:
            self.controller._mount(self.desktop_user.uid, binding)
        command = run.call_args.args[0]
        self.assertIn("--read-only", command)
        self.assertIn("--mkdir", command)
        self.assertEqual(command[-1], binding.destination)
        self.assertTrue(command[-2].startswith(f"/proc/{os.getpid()}/fd/"))

    def test_shared_destination_is_mounted_and_unmounted_once(self) -> None:
        binding = session.DesktopBind(
            "/tmp/.X11-unix/X0", Path("/source"), 1, 2
        )
        self.controller.destination_users[binding.destination] = {1000}
        self.controller.destination_sources[binding.destination] = (1, 2)
        with mock.patch.object(
            self.controller, "_machine_root"
        ) as machine_root:
            self.controller._mount(1001, binding)
            self.controller._unmount(1000, binding)
            machine_root.assert_not_called()
            self.controller._unmount(1001, binding)
        machine_root.assert_called_once_with(
            ["/usr/bin/umount", "--lazy", "--", binding.destination]
        )

    def test_setup_failure_is_nonfatal_and_marks_inactive(self) -> None:
        with (
            mock.patch.object(
                session,
                "host_manager_environment",
                return_value={"XDG_SESSION_ID": "2"},
            ),
            mock.patch.object(
                session,
                "_plan",
                side_effect=core.SpacesError("unsafe"),
            ),
            self.assertRaises(session.DesktopSetupError),
        ):
            self.controller.reconcile(
                self.desktop_user, (graphical(),)
            )
        self.assertEqual(
            session._read_status(
                self.controller.space_name, self.desktop_user.uid
            ),
            "inactive",
        )

    def test_record_publish_failure_unmounts_resources(self) -> None:
        binding = session.DesktopBind("/desktop/socket", Path("/host"), 1, 2)
        plan = session.DesktopPlan("2", (binding,), {"DISPLAY": ":0"})
        with (
            mock.patch.object(self.controller, "_prepare_guest_root"),
            mock.patch.object(self.controller, "_mount") as mount,
            mock.patch.object(
                session,
                "_write_record",
                side_effect=OSError("write failed"),
            ),
            mock.patch.object(self.controller, "_unmount") as unmount,
            self.assertRaises(OSError),
        ):
            self.controller._activate(self.desktop_user, plan)
        mount.assert_called_once_with(self.desktop_user.uid, binding)
        unmount.assert_called_once_with(self.desktop_user.uid, binding)

    def test_portal_failure_is_retried_without_replacing_desktop(self) -> None:
        self.controller.portals_enabled = True
        plan = session.DesktopPlan("2", (), {"DISPLAY": ":0"})
        current = session._ActiveDesktop(plan)
        self.controller.active[self.desktop_user.uid] = current
        proxy = mock.Mock()
        binding = session.DesktopBind(
            "/run/spaces/desktop/1000/portal/bus",
            self.root / "portal-bus",
            1,
            2,
        )
        with (
            mock.patch.object(
                session,
                "start_portal_proxy",
                side_effect=[
                    core.SpacesError("transient"),
                    (proxy, binding),
                ],
            ) as start,
            mock.patch.object(self.controller, "_mount") as mount,
            self.assertLogs(session.logger, "WARNING"),
        ):
            self.controller.reconcile_portals()
            self.assertIsNone(current.portal)
            self.assertIsNone(current.portal_binding)
            self.assertIs(self.controller.active[self.desktop_user.uid], current)
            self.controller.reconcile_portals()

        self.assertEqual(start.call_count, 2)
        mount.assert_called_once_with(self.desktop_user.uid, binding)
        self.assertIs(current.portal, proxy)
        self.assertEqual(current.portal_binding, binding)

    def test_forwarding_publishes_and_clears_combined_record(self) -> None:
        plan = session.DesktopPlan("2", (), {"DISPLAY": ":0"})
        with mock.patch.object(self.controller, "_prepare_guest_root"):
            active = self.controller._activate(self.desktop_user, plan)
            self.controller.active[self.desktop_user.uid] = active
            self.assertEqual(
                session._read_record("work", self.desktop_user.uid),
                ("active", "2", {"DISPLAY": ":0"}),
            )
            self.controller.deactivate(self.desktop_user)

        self.assertEqual(
            session._read_record("work", self.desktop_user.uid),
            ("inactive", None, {}),
        )

    def test_logout_lazily_revokes_active_generation(self) -> None:
        binding = session.DesktopBind("/desktop/socket", Path("/host"), 1, 2)
        plan = session.DesktopPlan("2", (binding,), {"DISPLAY": ":0"})
        self.controller.active[self.desktop_user.uid] = (
            session._ActiveDesktop(plan)
        )
        self.controller.destination_users[binding.destination] = {
            self.desktop_user.uid
        }
        self.controller.destination_sources[binding.destination] = (1, 2)
        with mock.patch.object(
            self.controller, "_machine_root"
        ) as machine_root:
            self.controller.deactivate(self.desktop_user)
        machine_root.assert_called_once_with(
            ["/usr/bin/umount", "--lazy", "--", binding.destination]
        )
        self.assertEqual(
            session._read_status("work", self.desktop_user.uid), "inactive"
        )

    def test_logout_revokes_portal_before_stopping_proxy(self) -> None:
        portal_binding = session.DesktopBind(
            "/run/spaces/desktop/1000/portal/bus",
            self.root / "portal-bus",
            1,
            2,
        )
        proxy = mock.Mock()
        proxy.socket_path = self.root / "proxy-bus"
        proxy.socket_path.write_text("", encoding="utf-8")
        active = session._ActiveDesktop(
            session.DesktopPlan("2", (), {"DISPLAY": ":0"}),
            portal=proxy,
            portal_binding=portal_binding,
        )

        with mock.patch.object(self.controller, "_unmount") as unmount:
            self.controller._deactivate(self.desktop_user, active)

        unmount.assert_called_once_with(
            self.desktop_user.uid, portal_binding
        )
        proxy.close.assert_called_once_with()
        self.assertIsNone(active.portal)
        self.assertIsNone(active.portal_binding)
        self.assertFalse(proxy.socket_path.exists())

    def test_failed_revocation_is_fatal(self) -> None:
        binding = session.DesktopBind("/desktop/socket", Path("/host"), 1, 2)
        plan = session.DesktopPlan("2", (binding,), {"DISPLAY": ":0"})
        active = session._ActiveDesktop(plan)
        with (
            mock.patch.object(
                self.controller,
                "_unmount",
                side_effect=OSError("mount unavailable"),
            ),
            self.assertRaises(session.DesktopRevocationError),
        ):
            self.controller._deactivate(self.desktop_user, active)


class PolkitAgentTests(unittest.TestCase):
    def test_recognizes_fedora_kf6_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rootfs = Path(temporary)
            agent = (
                rootfs
                / "usr/libexec/kf6"
                / "polkit-kde-authentication-agent-1"
            )
            agent.parent.mkdir(parents=True)
            agent.write_text("#!/bin/sh\n", encoding="utf-8")
            agent.chmod(0o755)
            self.assertEqual(
                session.polkit_agent(rootfs),
                "/usr/libexec/kf6/polkit-kde-authentication-agent-1",
            )

    def test_recognizes_ubuntu_multiarch_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rootfs = Path(temporary)
            agent = (
                rootfs
                / "usr/lib/x86_64-linux-gnu/libexec"
                / "polkit-kde-authentication-agent-1"
            )
            agent.parent.mkdir(parents=True)
            agent.write_text("#!/bin/sh\n", encoding="utf-8")
            agent.chmod(0o755)
            self.assertEqual(
                session.polkit_agent(rootfs),
                "/usr/lib/x86_64-linux-gnu/libexec/"
                "polkit-kde-authentication-agent-1",
            )

    def test_ignores_unrecognized_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rootfs = Path(temporary)
            (rootfs / "tmp").mkdir()
            agent = rootfs / "tmp/agent"
            agent.write_text("", encoding="utf-8")
            agent.chmod(0o755)
            self.assertIsNone(session.polkit_agent(rootfs))


class NativeLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.launcher = Path(cls.temporary.name) / "launcher"
        cls.dbus_update = Path(cls.temporary.name) / "dbus-update"
        cls.dbus_update.write_text(
            "#!/bin/sh\n"
            "if test -n \"$SPACES_DBUS_ENV_NAMES\"; then\n"
            "  printf '%s\\n' \"$@\" > \"$SPACES_DBUS_ENV_NAMES\"\n"
            "fi\n"
            "if test -n \"$SPACES_DBUS_DISPLAY\"; then\n"
            "  printf '%s' \"$DISPLAY\" > \"$SPACES_DBUS_DISPLAY\"\n"
            "fi\n"
            "if test \"$SPACES_DBUS_UPDATE_FAIL\" = 1; then\n"
            "  exit 23\n"
            "fi\n",
            encoding="utf-8",
        )
        cls.dbus_update.chmod(0o755)
        source = (
            Path(__file__).parents[1]
            / "native"
            / "spaces_session_launcher.c"
        )
        try:
            subprocess.run(
                [
                    "cc",
                    "-std=gnu11",
                    "-O2",
                    str(source),
                    (
                        "-DDBUS_UPDATE_ACTIVATION_ENVIRONMENT="
                        f'"{cls.dbus_update}"'
                    ),
                    "-Wl,--wrap=__libc_start_main",
                    "-o",
                    str(cls.launcher),
                ],
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise unittest.SkipTest(f"C compiler unavailable: {error}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    @staticmethod
    def wait_for_path(path: Path, timeout: float = 3) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return True
            time.sleep(0.01)
        return path.exists()

    def test_preserves_application_exit_status(self) -> None:
        completed = subprocess.run(
            [self.launcher, "--", "/bin/sh", "-c", "exit 37"],
            check=False,
        )
        self.assertEqual(completed.returncode, 37)

    def test_updates_guest_dbus_environment_before_application(self) -> None:
        names = Path(self.temporary.name) / "dbus-environment-names"
        display = Path(self.temporary.name) / "dbus-environment-display"
        environment = os.environ.copy()
        environment.update(
            {
                "DISPLAY": ":77",
                "SPACES_DBUS_DISPLAY": str(display),
                "SPACES_DBUS_ENV_NAMES": str(names),
                "WAYLAND_DISPLAY": "/run/spaces/wayland-0",
            }
        )
        completed = subprocess.run(
            [
                self.launcher,
                "--dbus-env",
                "DISPLAY",
                "--dbus-env",
                "WAYLAND_DISPLAY",
                "--",
                "/bin/sh",
                "-c",
                f"test -f {shlex.quote(str(names))}",
            ],
            check=False,
            env=environment,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(names.read_text(encoding="utf-8"), (
            "--systemd\nDISPLAY\nWAYLAND_DISPLAY\n"
        ))
        self.assertEqual(display.read_text(encoding="utf-8"), ":77")
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "")

    def test_dbus_environment_failure_does_not_block_application(self) -> None:
        environment = os.environ.copy()
        environment["SPACES_DBUS_UPDATE_FAIL"] = "1"
        completed = subprocess.run(
            [
                self.launcher,
                "--dbus-env",
                "DISPLAY",
                "--",
                "/bin/sh",
                "-c",
                "exit 29",
            ],
            check=False,
            env=environment,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 29)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(
            completed.stderr,
            (
                "spaces: warning: could not update guest D-Bus "
                "activation environment\n"
            ),
        )

    def test_hung_dbus_environment_update_is_bounded(self) -> None:
        original = self.dbus_update.read_text(encoding="utf-8")
        body = original.removeprefix("#!/bin/sh\n")
        self.dbus_update.write_text(
            "#!/bin/sh\n"
            "if test \"$SPACES_DBUS_UPDATE_HANG\" = 1; then\n"
            "  exec sleep 5\n"
            "fi\n"
            + body,
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment["SPACES_DBUS_UPDATE_HANG"] = "1"
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [
                    self.launcher,
                    "--dbus-env",
                    "DISPLAY",
                    "--",
                    "/bin/sh",
                    "-c",
                    "exit 31",
                ],
                check=False,
                env=environment,
                capture_output=True,
                text=True,
                timeout=3,
            )
        finally:
            self.dbus_update.write_text(original, encoding="utf-8")

        self.assertEqual(completed.returncode, 31)
        self.assertLess(time.monotonic() - started, 2)

    def test_agent_stops_after_daemonized_descendants_exit(self) -> None:
        agent_pid = Path(self.temporary.name) / "lifecycle-agent-pid"
        agent_stopped = Path(self.temporary.name) / "lifecycle-agent-stopped"
        application_stopped = (
            Path(self.temporary.name) / "lifecycle-application-stopped"
        )
        agent = Path(self.temporary.name) / "lifecycle-agent"
        agent.write_text(
            "#!/bin/sh\n"
            "printf '%s' \"$$\" > \"$SPACES_AGENT_MARKER\"\n"
            "printf 'Authentication agent result: true\\n' >&2\n"
            "trap 'printf stopped > \"$SPACES_AGENT_STOPPED\"; exit 0' TERM\n"
            "while :; do sleep 0.05; done\n",
            encoding="utf-8",
        )
        agent.chmod(0o755)
        environment = os.environ.copy()
        environment["SPACES_AGENT_MARKER"] = str(agent_pid)
        environment["SPACES_AGENT_STOPPED"] = str(agent_stopped)
        started = time.monotonic()
        completed = subprocess.run(
            [
                self.launcher,
                "--agent",
                agent,
                "--",
                "/bin/sh",
                "-c",
                (
                    f"(sleep 0.3; printf stopped > "
                    f"{shlex.quote(str(application_stopped))}) & exit 7"
                ),
            ],
            check=False,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.assertEqual(completed.returncode, 7)
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertTrue(self.wait_for_path(agent_pid))
            self.assertFalse(agent_stopped.exists())
            self.assertTrue(self.wait_for_path(application_stopped))
            self.assertTrue(self.wait_for_path(agent_stopped))
        finally:
            if agent_pid.exists() and not agent_stopped.exists():
                try:
                    os.killpg(
                        int(agent_pid.read_text(encoding="utf-8")),
                        signal.SIGTERM,
                    )
                except ProcessLookupError:
                    pass

    def test_waits_for_polkit_agent_registration(self) -> None:
        marker = Path(self.temporary.name) / "registering-agent-pid"
        ready = Path(self.temporary.name) / "registering-agent-ready"
        stopped = Path(self.temporary.name) / "registering-agent-stopped"
        agent = Path(self.temporary.name) / "registering-agent"
        agent.write_text(
            "#!/bin/sh\n"
            "printf '%s' \"$$\" > \"$SPACES_AGENT_MARKER\"\n"
            "sleep 0.2\n"
            "printf ready > \"$SPACES_AGENT_READY\"\n"
            "printf 'Authentication agent result: true\\n' >&2\n"
            "trap 'printf stopped > \"$SPACES_AGENT_STOPPED\"; exit 0' TERM\n"
            "while :; do sleep 0.05; done\n",
            encoding="utf-8",
        )
        agent.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            {
                "SPACES_AGENT_MARKER": str(marker),
                "SPACES_AGENT_READY": str(ready),
                "SPACES_AGENT_STOPPED": str(stopped),
            }
        )
        completed = subprocess.run(
            [
                self.launcher,
                "--agent",
                agent,
                "--",
                "/bin/sh",
                "-c",
                f"test -f {shlex.quote(str(ready))}",
            ],
            check=False,
            env=environment,
            capture_output=True,
            text=True,
            timeout=5,
        )

        try:
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stderr, "")
            self.assertTrue(self.wait_for_path(stopped))
        finally:
            if marker.exists() and not stopped.exists():
                try:
                    os.killpg(
                        int(marker.read_text(encoding="utf-8")),
                        signal.SIGTERM,
                    )
                except ProcessLookupError:
                    pass

    def test_hides_polkit_agent_stdio(self) -> None:
        marker = Path(self.temporary.name) / "agent-pid"
        stopped = Path(self.temporary.name) / "agent-stopped"
        agent = Path(self.temporary.name) / "agent"
        agent.write_text(
            "#!/bin/sh\n"
            "printf '%s' \"$$\" > \"$SPACES_AGENT_MARKER\"\n"
            "printf 'Authentication agent result: true\\n' >&2\n"
            "printf 'agent stdout spam\\n'\n"
            "printf 'agent stderr spam\\n' >&2\n"
            "trap 'printf stopped > \"$SPACES_AGENT_STOPPED\"; exit 0' TERM\n"
            "while :; do sleep 0.05; done\n",
            encoding="utf-8",
        )
        agent.chmod(0o755)
        environment = os.environ.copy()
        environment["SPACES_AGENT_MARKER"] = str(marker)
        environment["SPACES_AGENT_STOPPED"] = str(stopped)
        completed = subprocess.run(
            [
                self.launcher,
                "--agent",
                agent,
                "--",
                "/bin/sh",
                "-c",
                "sleep 0.1; printf 'application output\\n'",
            ],
            check=False,
            env=environment,
            capture_output=True,
            text=True,
        )
        try:
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stdout, "application output\n")
            self.assertEqual(completed.stderr, "")
            self.assertTrue(self.wait_for_path(stopped))
        finally:
            if marker.exists() and not stopped.exists():
                try:
                    os.killpg(
                        int(marker.read_text(encoding="utf-8")),
                        signal.SIGTERM,
                    )
                except ProcessLookupError:
                    pass

    def test_application_receives_signals_directly(self) -> None:
        process = subprocess.Popen(
            [
                self.launcher,
                "--",
                "/bin/sh",
                "-c",
                "trap 'exit 23' TERM; while :; do sleep 0.05; done",
            ]
        )
        time.sleep(0.1)
        process.terminate()
        self.assertEqual(process.wait(timeout=5), 23)

    @unittest.skipUnless(shutil.which("script"), "script is unavailable")
    def test_application_remains_in_terminal_foreground(self) -> None:
        application = [
            self.launcher,
            "--",
            "/bin/sh",
            "-c",
            'read line; test "$line" = ready',
        ]
        completed = subprocess.run(
            [
                "script",
                "--quiet",
                "--return",
                "--command",
                shlex.join(str(item) for item in application),
                "/dev/null",
            ],
            input="ready\n",
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    @unittest.skipUnless(shutil.which("script"), "script is unavailable")
    def test_terminal_ctrl_c_reaches_application(self) -> None:
        application = [
            self.launcher,
            "--",
            "/bin/sh",
            "-c",
            "trap 'exit 23' INT; while :; do sleep 0.05; done",
        ]
        process = subprocess.Popen(
            [
                "script",
                "--quiet",
                "--return",
                "--command",
                shlex.join(str(item) for item in application),
                "/dev/null",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assert process.stdin is not None
        try:
            time.sleep(0.1)
            process.stdin.write(b"\x03")
            process.stdin.flush()
            self.assertEqual(process.wait(timeout=5), 23)
        finally:
            process.stdin.close()
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
