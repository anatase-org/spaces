"""Permission and lifecycle checks for the isolated KWin host endpoint."""
import os
from dataclasses import replace
from pathlib import Path, PurePosixPath
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from spaces import session


class ComputerLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.user = SimpleNamespace(uid=os.getuid() or 1000, name="test", accessibility=True, desktop=True)
        self.controller = session.DesktopController("fedora", (self.user,))
        self.current = session._ActiveDesktop(session.DesktopPlan("2", (), {}, accessibility=True))
        self.path = self.root / "computer"
        self.listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(self.listener.close)
        self.listener.bind(str(self.path))
        self.path.chmod(0o600)
        metadata = self.path.stat()
        self.binding = session.DesktopBind("/run/spaces/desktop/1000/computer/bus",
            self.path, metadata.st_dev, metadata.st_ino)
        self.service = mock.Mock(socket_path=self.path)
        self.service.executable_identity = None
        self.service.process.poll.return_value = None
        self.start = mock.patch.object(session, "start_computer_service", return_value=(self.service, self.binding)).start()
        self.addCleanup(mock.patch.stopall)
        self.mount = mock.patch.object(self.controller, "_mount").start()
        self.unmount = mock.patch.object(self.controller, "_unmount").start()

    def test_basic_disabled_and_root_never_start_service(self):
        for desktop, accessibility, uid in ((True, False, 1000), (False, True, 1000), (True, True, 0)):
            self.user.desktop, self.user.accessibility, self.user.uid = desktop, accessibility, uid
            self.controller._repair_computer(self.user, self.current)
        self.start.assert_not_called()

    def test_grant_is_independent_of_atspi_and_portals_and_reuses_endpoint(self):
        self.assertIsNone(self.current.accessibility)
        self.assertIsNone(self.current.portal)
        self.controller._repair_computer(self.user, self.current)
        self.controller._repair_computer(self.user, self.current)
        self.start.assert_called_once_with("fedora", self.user)
        self.mount.assert_called_once_with(self.user.uid, self.binding)

    def test_downgrade_closes_connections_before_removing_mount(self):
        self.controller._repair_computer(self.user, self.current)
        order = []
        self.service.close.side_effect = lambda: order.append("close")
        self.unmount.side_effect = lambda *_: order.append("unmount")
        self.user.accessibility = False
        self.controller._repair_computer(self.user, self.current)
        self.assertEqual(order, ["close", "unmount"])
        self.assertIsNone(self.current.computer)
        self.assertIsNone(self.current.computer_binding)

    def test_dead_service_is_retired_before_recovery(self):
        self.controller._repair_computer(self.user, self.current)
        self.service.process.poll.return_value = 1
        self.controller._repair_computer(self.user, self.current)
        self.service.close.assert_called_once()
        self.assertEqual(self.start.call_count, 2)

    def test_replaced_executable_restarts_the_authorized_host_process(self):
        self.controller._repair_computer(self.user, self.current)
        self.service.executable_identity = (1, 2)
        with mock.patch.object(Path, "stat", return_value=SimpleNamespace(st_dev=1, st_ino=3)):
            self.controller._repair_computer(self.user, self.current)
        self.service.close.assert_called_once()
        self.assertEqual(self.start.call_count, 2)

    def test_failed_mount_does_not_leave_an_authorized_service_running(self):
        self.mount.side_effect = RuntimeError("mount failed")
        with self.assertLogs(session.logger, level="WARNING"):
            self.controller._repair_computer(self.user, self.current)
        self.service.close.assert_called_once()
        self.assertIsNone(self.current.computer)

    def test_revocation_happens_even_if_unmount_fails(self):
        self.controller._repair_computer(self.user, self.current)
        self.unmount.side_effect = RuntimeError("busy")
        with self.assertRaises(session.DesktopRevocationError):
            self.controller._revoke_computer(self.user, self.current)
        self.service.close.assert_called_once()
        self.assertIsNone(self.current.computer)

    def prepare_settings_update(self):
        """Keep live adapters while replacing the file KDE saves atomically."""
        self.user.guest_home = PurePosixPath("/home/test")
        source = self.root / "kdeglobals"
        source.write_text("old settings")
        metadata = source.stat()
        self.old_settings = session.DesktopBind(
            "/home/test/.config/kdeglobals", source, metadata.st_dev, metadata.st_ino
        )
        self.unchanged = session.DesktopBind(
            f"/run/spaces/desktop/{self.user.uid}/wayland/wayland-0",
            self.root / "wayland-0", 1, 2,
        )
        self.current.plan = session.DesktopPlan(
            "2", tuple(sorted((self.old_settings, self.unchanged))),
            {"SPACES_NAME": "fedora", "SPACES_INTEGRATION_BROKER":
             session._broker_name("fedora", self.user.uid, "2")},
            accessibility=True,
        )
        self.controller.active[self.user.uid] = self.current
        self.controller._repair_computer(self.user, self.current)
        self.mount.reset_mock()
        self.start.reset_mock()

        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        path = self.root / "a11y"
        listener.bind(str(path))
        metadata = path.stat()
        self.a11y_binding = session.DesktopBind(
            f"/run/spaces/desktop/{self.user.uid}/a11y/bus",
            path, metadata.st_dev, metadata.st_ino,
        )
        self.a11y_proxy = mock.Mock(socket_path=path)
        self.a11y_proxy.process.poll.return_value = None
        self.current.accessibility = self.a11y_proxy
        self.current.accessibility_binding = self.a11y_binding
        self.current.accessibility_source = self.a11y_binding
        self.current.graphical_session = True

        replacement = self.root / "replacement"
        replacement.write_text("new settings")
        replacement.replace(source)
        metadata = source.stat()
        self.new_settings = replace(
            self.old_settings, device=metadata.st_dev, inode=metadata.st_ino
        )
        self.assertNotEqual(self.old_settings.inode, self.new_settings.inode)
        self.updated_plan = replace(
            self.current.plan, binds=tuple(sorted((self.new_settings, self.unchanged)))
        )
        self.enterContext(mock.patch.object(
            session, "host_manager_environment", return_value={"XDG_SESSION_ID": "2"}
        ))
        self.build_plan = self.enterContext(mock.patch.object(
            session, "_plan", return_value=self.updated_plan
        ))
        self.enterContext(mock.patch.object(
            session, "discover_accessibility_bus", return_value=self.a11y_binding
        ))
        self.publish = self.enterContext(mock.patch.object(session, "_write_record"))
        self.command = self.enterContext(mock.patch.object(self.controller, "_machine_user"))

    def reconcile_settings(self):
        login = session.LoginSession("2", True, False, "wayland", "user")
        self.controller.reconcile(self.user, (login,))

    def assert_adapters_preserved(self):
        self.service.close.assert_not_called()
        self.a11y_proxy.close.assert_not_called()
        self.start.assert_not_called()
        self.assertIs(self.current.computer, self.service)
        self.assertIs(self.current.accessibility, self.a11y_proxy)
        self.assertIs(self.controller.active[self.user.uid], self.current)
        self.assertTrue(self.current.graphical_session)
        self.command.assert_not_called()
        self.publish.assert_not_called()

    def test_settings_replacement_preserves_input_and_accessibility(self):
        self.prepare_settings_update()
        self.reconcile_settings()
        self.assert_adapters_preserved()
        self.unmount.assert_called_once_with(self.user.uid, self.old_settings)
        self.mount.assert_called_once_with(self.user.uid, self.new_settings)
        self.assertEqual(self.current.plan, self.updated_plan)
        # An unchanged subsequent reconciliation must leave every mount alone.
        self.reconcile_settings()
        self.assertEqual(self.mount.call_count, 1)
        self.assertEqual(self.unmount.call_count, 1)
        self.assert_adapters_preserved()

    def test_settings_refresh_rebuilds_only_portal_file_mappings(self):
        self.prepare_settings_update()
        portal_path = self.root / "portal"
        portal_path.touch()
        portal = mock.Mock(socket_path=portal_path)
        portal_binding = session.DesktopBind("/portal/bus", portal_path, 1, 2)
        self.current.portal = portal
        self.current.portal_binding = portal_binding
        # Inspect the plan passed to portal repair without starting a host UI.
        with mock.patch.object(self.controller, "_repair_portal") as repair:
            self.reconcile_settings()
        portal.close.assert_called_once()
        self.assertFalse(portal_path.exists())
        self.assertIsNone(self.current.portal)
        self.assertIsNone(self.current.portal_binding)
        repair.assert_called_once_with(self.user, self.current)
        self.assertEqual(self.current.plan, self.updated_plan)
        self.assertEqual(self.unmount.call_args_list, [
            mock.call(self.user.uid, portal_binding),
            mock.call(self.user.uid, self.old_settings),
        ])
        self.assert_adapters_preserved()

    def test_settings_mount_failure_retries_without_revoking_adapters(self):
        self.prepare_settings_update()
        self.mount.side_effect = [RuntimeError("source replaced again"), None]
        with self.assertRaisesRegex(session.DesktopSetupError, "source replaced again"):
            self.reconcile_settings()
        self.assertNotIn(self.old_settings, self.current.plan.binds)
        self.assertNotIn(self.new_settings, self.current.plan.binds)
        self.assertIn(self.unchanged, self.current.plan.binds)
        self.assert_adapters_preserved()
        self.reconcile_settings()
        self.unmount.assert_called_once_with(self.user.uid, self.old_settings)
        self.assertEqual(self.mount.call_count, 2)
        self.assertEqual(self.current.plan, self.updated_plan)
        self.assert_adapters_preserved()

    def test_settings_refresh_retains_mount_identity_checks(self):
        self.prepare_settings_update()
        # Use production mounting (only the machinectl subprocess is mocked).
        # Replacing the file after planning must not mount an unchecked inode.
        replacement = self.root / "newer"
        replacement.write_text("newer settings")
        replacement.replace(self.new_settings.source)
        self.mount.side_effect = lambda uid, binding: session.DesktopController._mount(
            self.controller, uid, binding
        )
        with mock.patch.object(session.subprocess, "run") as run:
            with self.assertRaisesRegex(session.DesktopSetupError, "changed before"):
                self.reconcile_settings()
        run.assert_not_called()
        self.assert_adapters_preserved()
        # A new plan pins the current file and completes the pending mount.
        metadata = self.new_settings.source.stat()
        latest = replace(self.new_settings, device=metadata.st_dev, inode=metadata.st_ino)
        self.build_plan.return_value = replace(
            self.updated_plan, binds=tuple(sorted((latest, self.unchanged)))
        )
        with mock.patch.object(session.subprocess, "run") as run:
            self.reconcile_settings()
        self.assertIn("--read-only", run.call_args.args[0])
        self.assertEqual(self.controller.destination_sources[latest.destination],
                         (latest.device, latest.inode))
        self.assert_adapters_preserved()

    def test_settings_unmount_failure_retries_without_losing_mount_state(self):
        self.prepare_settings_update()
        previous = self.current.plan
        self.unmount.side_effect = [RuntimeError("busy"), None]
        with self.assertRaisesRegex(session.DesktopSetupError, "busy"):
            self.reconcile_settings()
        self.assertEqual(self.current.plan, previous)
        self.mount.assert_not_called()
        self.assert_adapters_preserved()
        self.reconcile_settings()
        self.assertEqual(self.unmount.call_count, 2)
        self.mount.assert_called_once_with(self.user.uid, self.new_settings)
        self.assert_adapters_preserved()

    def test_backend_or_authority_changes_cannot_use_settings_refresh(self):
        self.prepare_settings_update()
        for plan in (
            replace(self.updated_plan, session_id="3"),
            replace(self.updated_plan, accessibility=False),
            replace(self.updated_plan, desktop=False),
            replace(self.updated_plan, environment={"WAYLAND_DISPLAY": "wayland-1"}),
            replace(self.updated_plan, binds=(replace(self.unchanged, inode=3),)),
            replace(self.updated_plan, open_mappings=(
                session.OpenPathMapping("/home/test/shared", self.root),
            )),
        ):
            with self.subTest(plan=plan):
                self.assertFalse(self.controller._refresh_settings(self.user, self.current, plan))
        self.unmount.assert_not_called()
        self.mount.assert_not_called()
        self.assert_adapters_preserved()

    def test_permission_downgrade_still_revokes_before_settings_update(self):
        self.prepare_settings_update()
        self.user.accessibility = False
        self.build_plan.side_effect = RuntimeError("planning failed")
        with self.assertRaisesRegex(session.DesktopSetupError, "planning failed"):
            self.reconcile_settings()
        self.service.close.assert_called_once()
        self.a11y_proxy.close.assert_called_once()
        self.assertIsNone(self.current.computer)
        self.assertIsNone(self.current.accessibility)

    def test_session_replacement_still_revokes_before_settings_update(self):
        self.prepare_settings_update()
        self.build_plan.side_effect = RuntimeError("planning failed")
        with mock.patch.object(session, "select_graphical_session", return_value=
                session.LoginSession("3", True, False, "wayland", "user")):
            with self.assertRaisesRegex(session.DesktopSetupError, "planning failed"):
                self.reconcile_settings()
        self.service.close.assert_called_once()
        self.a11y_proxy.close.assert_called_once()
        self.assertIsNone(self.current.computer)
        self.assertIsNone(self.current.accessibility)

    def test_logout_still_revokes_with_pending_settings_update(self):
        self.prepare_settings_update()
        self.controller.reconcile(self.user, (), session_active=False)
        self.service.close.assert_called_once()
        self.a11y_proxy.close.assert_called_once()
        self.assertNotIn(self.user.uid, self.controller.active)
