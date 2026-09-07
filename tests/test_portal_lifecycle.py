"""Native portal lifecycle checks on private buses, without a desktop login."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PortalLifecycleTests(unittest.TestCase):
    def test_guest_startup_and_requests_do_not_activate_host_portal(self) -> None:
        if not shutil.which("cc") or not shutil.which("dbus-daemon"):
            self.skipTest("C compiler and dbus-daemon are required")
        flags = subprocess.run(
            ["pkg-config", "--cflags", "--libs", "gio-unix-2.0"],
            capture_output=True, text=True, check=True,
        )
        probes = {
            "spaces_portal.c": '''
                Portal portal = {.host = bus, .app_id = "org.anatase.Spaces.test"};
                g_assert_false(register_host_identity(&portal, &error));
                g_assert_nonnull(error);
                g_clear_error(&error);
                g_assert_false(register_interfaces(&portal, &error));
                g_assert_nonnull(error);
                g_clear_error(&error);
            ''',
            "spaces_broker.c": '''
                Broker broker = {.bus = bus};
                guint response = 0;
                GVariant *results = NULL;
                GVariant *parameters = g_variant_ref_sink(g_variant_new(
                    "(s@a{sv})", "", g_variant_new_array(
                        G_VARIANT_TYPE("{sv}"), NULL, 0)));
                g_assert_false(wait_portal_request(&broker,
                    "org.freedesktop.portal.Background", "RequestBackground",
                    parameters, &response, &results, NULL, &error));
                g_assert_nonnull(error);
                g_clear_error(&error);
                g_variant_unref(parameters);
            ''',
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            services = root / "services"
            services.mkdir()
            marker = root / "activated"
            (services / "org.freedesktop.portal.Desktop.service").write_text(
                "[D-BUS Service]\nName=org.freedesktop.portal.Desktop\n"
                f"Exec=/usr/bin/touch {marker}\n"
            )
            for source, probe in probes.items():
                with self.subTest(source=source):
                    marker.unlink(missing_ok=True)
                    harness = root / "probe.c"
                    harness.write_text(f'''
#define main spaces_program_main
#include "{ROOT / 'native' / source}"
#undef main
int main(int argc, char **argv) {{
    (void)argc;
    GError *error = NULL;
    GTestDBus *test_bus = g_test_dbus_new(G_TEST_DBUS_NONE);
    g_test_dbus_add_service_dir(test_bus, argv[1]);
    g_test_dbus_up(test_bus);
    GDBusConnection *bus = g_dbus_connection_new_for_address_sync(
        g_test_dbus_get_bus_address(test_bus),
        G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT |
        G_DBUS_CONNECTION_FLAGS_MESSAGE_BUS_CONNECTION, NULL, NULL, &error);
    g_assert_no_error(error);
    {probe}
    g_assert_false(g_file_test(argv[2], G_FILE_TEST_EXISTS));
    /* Prove this bus really can activate the fixture. */
    GVariant *reply = g_dbus_connection_call_sync(bus,
        "org.freedesktop.DBus", "/org/freedesktop/DBus",
        "org.freedesktop.DBus", "StartServiceByName",
        g_variant_new("(su)", PORTAL_NAME, 0), NULL,
        G_DBUS_CALL_FLAGS_NONE, 1000, NULL, &error);
    g_clear_pointer(&reply, g_variant_unref);
    g_clear_error(&error);
    g_assert_true(g_file_test(argv[2], G_FILE_TEST_EXISTS));
    g_dbus_connection_close_sync(bus, NULL, NULL);
    g_object_unref(bus);
    g_test_dbus_down(test_bus);
    g_object_unref(test_bus);
    return 0;
}}
''')
                    executable = root / "probe"
                    subprocess.run(
                        [os.environ.get("CC", "cc"), "-std=gnu11", "-o",
                         str(executable), str(harness), *shlex.split(flags.stdout)],
                        check=True, capture_output=True, text=True,
                    )
                    subprocess.run(
                        [str(executable), str(services), str(marker)],
                        check=True, capture_output=True, text=True, timeout=15,
                    )
