"""Permission and lifecycle checks for the isolated KWin host endpoint."""
import os
from pathlib import Path
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
