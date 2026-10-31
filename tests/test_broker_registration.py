"""Exercise the production broker's login race on an isolated session bus."""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP_ID = "org.anatase.Spaces.fedora"
BROKER = "org.anatase.Spaces.Integration.registrationtest"
PORTAL = "org.freedesktop.portal.Desktop"


class BrokerRegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import gi
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib
        except (ImportError, AttributeError, ValueError):
            raise unittest.SkipTest("PyGObject is required")
        cls.Gio, cls.GLib = Gio, GLib
        if not all(shutil.which(tool) for tool in ("cc", "pkg-config", "dbus-daemon")):
            raise unittest.SkipTest("C compiler, GLib headers and dbus-daemon are required")
        flags = subprocess.run(["pkg-config", "--cflags", "--libs", "gio-unix-2.0"],
            capture_output=True, text=True)
        if flags.returncode:
            raise unittest.SkipTest("GLib development files are required")
        cls.build = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.build.cleanup)
        cls.executable = Path(cls.build.name) / "spaces-broker"
        source = Path(os.environ.get("SPACES_BROKER_SOURCE", ROOT / "native/spaces_broker.c"))
        subprocess.run(["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-o",
            str(cls.executable), str(source),
            *shlex.split(flags.stdout)], check=True)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.marker = self.root / "activated"
        services = self.root / "services"
        services.mkdir()
        (services / f"{PORTAL}.service").write_text(
            f"[D-BUS Service]\nName={PORTAL}\nExec=/usr/bin/touch {self.marker}\n")
        config = self.root / "bus.conf"
        config.write_text(f"""<busconfig><type>session</type>
<listen>unix:tmpdir={self.root}</listen><auth>EXTERNAL</auth>
<servicedir>{services}</servicedir><policy context="default">
<allow send_destination="*"/><allow receive_sender="*"/>
<allow own="*"/></policy></busconfig>""")
        self.daemon = self.child(["dbus-daemon", "--nofork", "--print-address",
            f"--config-file={config}"], stdout=subprocess.PIPE)
        self.address = self.daemon.stdout.readline().strip()
        self.bus = self.Gio.DBusConnection.new_for_address_sync(self.address,
            self.Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | self.Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
        self.addCleanup(self.bus.close_sync, None)
        self.events_path = self.root / "events"
        self.events_path.touch()

    def child(self, args, **kwargs):
        process = subprocess.Popen(args, text=True, stderr=subprocess.PIPE, **kwargs)
        def cleanup():
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        self.addCleanup(cleanup)
        return process

    def events(self):
        return [json.loads(line) for line in self.events_path.read_text().splitlines()]

    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail(f"Timed out; portal events: {self.events()}")

    def start_portal(self, failures=0, delay=0):
        count = sum(e["method"] == "Ready" for e in self.events())
        process = self.child([sys.executable, str(ROOT / "tests/broker_portal_fixture.py"),
            self.address, str(self.events_path), str(failures), str(delay)])
        self.wait_for(lambda: sum(e["method"] == "Ready" for e in self.events()) > count)
        return process

    def start_broker(self):
        read_fd, write_fd = os.pipe()
        mapping = os.open(self.root, os.O_PATH)
        try:
            self.broker = self.child([str(self.executable), "--name", BROKER,
                "--space", "fedora", "--app-id", APP_ID, "--ready-fd", str(write_fd),
                "--map", "/guest", str(mapping)],
                pass_fds=(write_fd, mapping), env={**os.environ,
                    "DBUS_SESSION_BUS_ADDRESS": self.address,
                    "DBUS_SYSTEM_BUS_ADDRESS": self.address})
        finally:
            os.close(write_fd)
            os.close(mapping)
        try:
            self.assertTrue(select.select([read_fd], [], [], 8)[0], "broker did not start")
            self.assertEqual(os.read(read_fd, 1), b"1")
        finally:
            os.close(read_fd)

    def secret(self):
        fd = os.memfd_create("test-secret")
        try:
            descriptors = self.Gio.UnixFDList.new()
            handle = descriptors.append(fd)
            parameters = self.GLib.Variant("(sssv)", (
                "org.freedesktop.portal.Secret", "RetrieveSecret", "test.app",
                self.GLib.Variant("(ha{sv})", (handle, {
                    "handle_token": self.GLib.Variant("s", "secret")
                }))))
            reply, _ = self.bus.call_with_unix_fd_list_sync(BROKER,
                "/org/anatase/Spaces/Integration", "org.anatase.Spaces.Integration1",
                "PortalRequest", parameters, None, self.Gio.DBusCallFlags.NONE,
                3000, descriptors, None)
            self.assertEqual(reply.unpack()[0], 0)
            return os.pread(fd, 64, 0)
        finally:
            os.close(fd)

    def registered(self, count=1):
        self.wait_for(lambda: sum(e["method"] == "Register" and e["success"]
            for e in self.events()) >= count)

    def test_broker_before_portal_never_uses_unregistered_identity(self):
        self.start_broker()
        with self.assertRaises(self.GLib.Error):
            self.secret()
        self.assertFalse(self.marker.exists(), "broker activated the host portal")
        self.start_portal()
        # A client may race the owner notification. It must fail or use the
        # registered identity; it must never obtain an unregistered secret.
        for _ in range(100):
            try:
                secret = self.secret()
                break
            except self.GLib.Error:
                time.sleep(0.02)
        else:
            self.fail("broker did not recover when the portal appeared")
        self.assertEqual(len(secret), 64)
        self.assertEqual(self.events()[-1], {"method": "RetrieveSecret", "app_id": APP_ID})
        self.assertEqual(secret, self.secret())

    def test_transient_registration_failure_retries_and_blocks_secrets(self):
        self.start_portal(failures=2)
        self.start_broker()
        with self.assertRaisesRegex(self.GLib.Error, "registration|registered|identity"):
            self.secret()
        self.assertFalse(any(e["method"] == "RetrieveSecret" for e in self.events()))
        self.registered()
        self.assertEqual(len(self.secret()), 64)
        self.assertEqual(sum(e["method"] == "Register" for e in self.events()), 3)
        self.broker.terminate()
        _, logs = self.broker.communicate(timeout=3)
        self.assertIn("Portal is still starting", logs)

    def test_slow_registration_rejects_requests_without_blocking(self):
        self.start_portal(delay=2)
        self.start_broker()
        self.wait_for(lambda: any(e["method"] == "RegisterStarted" for e in self.events()))
        started = time.monotonic()
        with self.assertRaisesRegex(self.GLib.Error, "registration|registered|identity"):
            self.secret()
        self.assertLess(time.monotonic() - started, 1.5)
        self.registered()
        self.assertEqual(len(self.secret()), 64)

    def test_portal_owner_replacement_registers_new_owner(self):
        self.start_portal()
        self.start_broker()
        self.registered()
        original = self.secret()
        # Replace the owner while the old service connection is still alive.
        self.start_portal(failures=1)
        with self.assertRaises(self.GLib.Error):
            self.secret()
        self.registered(count=2)
        self.assertEqual(original, self.secret())
        self.assertTrue(all(e["app_id"] == APP_ID for e in self.events()
            if e["method"] == "RetrieveSecret"))

    def test_ambiguous_registration_timeout_requires_fresh_connection(self):
        self.start_portal(delay=6)
        self.start_broker()
        with self.assertRaises(self.GLib.Error):
            self.secret()
        self.assertEqual(self.broker.wait(timeout=8), 1)
        self.assertFalse(any(e["method"] == "RetrieveSecret" for e in self.events()))
        _, logs = self.broker.communicate(timeout=1)
        self.assertIn("registration outcome unknown", logs)
        self.assertIn("fresh D-Bus connection", logs)

    def test_late_registration_reply_cannot_certify_replacement_owner(self):
        self.start_portal(delay=1)
        self.start_broker()
        self.wait_for(lambda: any(e["method"] == "RegisterStarted" for e in self.events()))
        self.start_portal(delay=2)
        # The old owner's delayed success arrives while the replacement's
        # registration is pending. It must not make the replacement usable.
        self.registered()
        with self.assertRaises(self.GLib.Error):
            self.secret()
        self.registered(count=2)
        self.assertEqual(len(self.secret()), 64)
        self.assertTrue(all(e["app_id"] == APP_ID for e in self.events()
            if e["method"] == "RetrieveSecret"))

    def test_portal_disappears_and_returns_without_activating_it(self):
        portal = self.start_portal()
        self.start_broker()
        self.registered()
        original = self.secret()
        portal.terminate()
        portal.wait(timeout=3)
        with self.assertRaises(self.GLib.Error):
            self.secret()
        self.assertFalse(self.marker.exists())
        self.start_portal()
        self.registered(count=2)
        self.assertEqual(original, self.secret())
        self.assertIsNone(self.broker.poll())


if __name__ == "__main__":
    unittest.main()
