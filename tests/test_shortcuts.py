from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from spaces import shortcuts


class ShortcutExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.rootfs = self.root / "rootfs"
        self.system = self.rootfs / "usr" / "share" / "applications"
        self.local = self.rootfs / "usr" / "local" / "share" / "applications"
        self.system.mkdir(parents=True)
        self.local.mkdir(parents=True)
        self.output = self.root / "host-applications"
        self.output.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def desktop(
        name: str = "Editor",
        *,
        command: str = "/usr/bin/editor %F",
        icon: str = "editor",
        extra: str = "",
    ) -> str:
        return (
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={name}\n"
            "Name[fr]=Éditeur\n"
            f"Exec={command}\n"
            f"Icon={icon}\n"
            "Terminal=false\n"
            "NoDisplay=true\n"
            "TryExec=/usr/bin/editor\n"
            "Path=/tmp\n"
            "DBusActivatable=true\n"
            "X-KDE-SubstituteUID=true\n"
            "Actions=new-window;\n"
            f"{extra}"
            "\n"
            "[Desktop Action new-window]\n"
            "Name=New Window\n"
            "Exec=/usr/bin/editor --new-window\n"
        )

    def add_icon(self, name: str = "editor") -> Path:
        path = (
            self.rootfs
            / "usr"
            / "share"
            / "icons"
            / "hicolor"
            / "128x128"
            / "apps"
            / f"{name}.png"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (128, 128), (20, 40, 220, 255)).save(path)
        return path

    def add_svg_icon(self, name: str) -> Path:
        path = (
            self.rootfs
            / "usr"
            / "share"
            / "icons"
            / "hicolor"
            / "scalable"
            / "apps"
            / f"{name}.svg"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" '
            'viewBox="0 0 128 128">'
            '<rect width="128" height="128" fill="#143cdc"/>'
            "</svg>",
            encoding="utf-8",
        )
        return path

    def test_recursive_export_rewrites_and_sanitizes_entries(self) -> None:
        nested = self.system / "editors"
        nested.mkdir()
        (nested / "editor.desktop").write_text(self.desktop(), encoding="utf-8")
        (self.local / "browser.desktop").write_text(
            self.desktop("Browser", command="/usr/bin/browser %U"),
            encoding="utf-8",
        )
        self.add_icon()

        generated = shortcuts.generate(
            "work",
            self.rootfs,
            "ubuntu",
            self.output,
        )
        self.assertEqual(
            [item.name for item in generated],
            [
                "spaces-work-v1-editors-editor.desktop",
                "spaces-work-v1-local-browser.desktop",
            ],
        )
        text = generated[0].desktop.decode("utf-8")
        self.assertIn("Name=Editor (work)\n", text)
        self.assertIn("Name[fr]=Éditeur (work)\n", text)
        self.assertIn(
            "Exec=/usr/bin/spaces enter --graphical work -- " "/usr/bin/editor %F\n",
            text,
        )
        self.assertIn(
            "Exec=/usr/bin/spaces enter --graphical work -- "
            "/usr/bin/editor --new-window\n",
            text,
        )
        self.assertIn("NoDisplay=true\n", text)
        self.assertIn("StartupWMClass=editor\n", text)
        self.assertIn("DBusActivatable=false\n", text)
        self.assertNotIn("TryExec=", text)
        self.assertNotIn("Path=", text)
        self.assertNotIn("X-KDE-SubstituteUID", text)
        self.assertNotIn("/usr/bin/env", text)

    def test_existing_startup_wm_class_is_preserved(self) -> None:
        (self.system / "editor.desktop").write_text(
            self.desktop(extra="StartupWMClass=EditorClass\n"),
            encoding="utf-8",
        )

        generated = shortcuts.generate(
            "work",
            self.rootfs,
            "custom",
            self.output,
        )

        text = generated[0].desktop.decode("utf-8")
        self.assertEqual(text.count("StartupWMClass="), 1)
        self.assertIn("StartupWMClass=EditorClass\n", text)

    def test_empty_startup_wm_class_uses_desktop_basename(self) -> None:
        (self.system / "com.example.Editor.desktop").write_text(
            self.desktop(extra="StartupWMClass=   \n"),
            encoding="utf-8",
        )

        generated = shortcuts.generate(
            "work",
            self.rootfs,
            "custom",
            self.output,
        )

        text = generated[0].desktop.decode("utf-8")
        self.assertEqual(text.count("StartupWMClass="), 1)
        self.assertIn("StartupWMClass=com.example.Editor\n", text)

    def test_blacklist_filters_known_variants_before_export(self) -> None:
        for name in (
            "system-settings",
            "systemsettings",
            "kdesystemsettings",
        ):
            (self.system / f"{name}.desktop").write_text(
                self.desktop(name), encoding="utf-8"
            )
        for name in ("htop", "vim", "keep"):
            (self.system / f"{name}.desktop").write_text(
                self.desktop(name), encoding="utf-8"
            )

        generated = shortcuts.generate("work", self.rootfs, "custom", self.output)
        self.assertEqual(
            [item.name for item in generated],
            [
                "spaces-work-v1-htop.desktop",
                "spaces-work-v1-keep.desktop",
                "spaces-work-v1-vim.desktop",
            ],
        )

    def test_terminal_applications_are_blacklisted(self) -> None:
        (self.system / "terminal.desktop").write_text(
            self.desktop("Terminal", extra="Terminal=TrUe\n"),
            encoding="utf-8",
        )
        (self.system / "editor.desktop").write_text(
            self.desktop("Editor"),
            encoding="utf-8",
        )

        generated = shortcuts.generate("work", self.rootfs, "custom", self.output)
        self.assertEqual(
            [item.name for item in generated],
            ["spaces-work-v1-editor.desktop"],
        )

    def test_non_applications_and_entries_without_exec_are_ignored(self) -> None:
        (self.system / "service.desktop").write_text(
            "[Desktop Entry]\nType=Service\nName=Service\nExec=service\n",
            encoding="utf-8",
        )
        (self.system / "missing.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=Missing\n",
            encoding="utf-8",
        )
        self.assertEqual(
            shortcuts.generate("work", self.rootfs, "custom", self.output),
            [],
        )

    def test_hidden_terminal_callback_is_exported(self) -> None:
        source = self.system / "nordvpn.desktop"
        source.write_text(
            self.desktop(
                "NordVPN",
                command="nordvpn click %u",
                extra="Terminal=true\nMimeType=x-scheme-handler/nordvpn;\n",
            ),
            encoding="utf-8",
        )
        shortcuts.reconcile("work", self.rootfs, "custom", applications_root=self.output)
        exported = self.output / "spaces-work-v1-nordvpn.desktop"
        text = exported.read_text()
        self.assertIn(
            "Exec=/usr/bin/spaces enter --graphical work -- nordvpn click %u\n",
            text,
        )
        self.assertIn("NoDisplay=true\n", text)
        self.assertIn("Terminal=true\n", text)
        self.assertIn("MimeType=x-scheme-handler/nordvpn;\n", text)

    def test_local_prefix_collision_gets_stable_suffix(self) -> None:
        (self.system / "local-foo.desktop").write_text(
            self.desktop("System"), encoding="utf-8"
        )
        (self.local / "foo.desktop").write_text(self.desktop("Local"), encoding="utf-8")
        names = [
            item.name
            for item in shortcuts.generate("work", self.rootfs, "custom", self.output)
        ]
        self.assertEqual(names[0], "spaces-work-v1-local-foo.desktop")
        self.assertRegex(
            names[1],
            r"^spaces-work-v1-local-foo-[0-9a-f]{10}\.desktop$",
        )

    def test_icon_is_copied_at_256_pixels_with_distro_overlay(self) -> None:
        (self.system / "editor.desktop").write_text(self.desktop(), encoding="utf-8")
        self.add_icon()
        shortcuts.reconcile(
            "work",
            self.rootfs,
            "ubuntu",
            applications_root=self.output,
        )

        icon = self.output / "spaces-icons" / "spaces-work-v1-editor.png"
        with Image.open(icon) as image:
            self.assertEqual(image.size, (256, 256))
            self.assertEqual(image.mode, "RGBA")
            self.assertNotEqual(
                image.getpixel((255, 255)),
                (20, 40, 220, 255),
            )
        desktop = (self.output / "spaces-work-v1-editor.desktop").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"Icon={icon}\n", desktop)

    def test_themed_svg_icon_name_is_rasterized(self) -> None:
        (self.system / "org.gnome.Software.desktop").write_text(
            self.desktop("Software", icon="org.gnome.Software"),
            encoding="utf-8",
        )
        self.add_svg_icon("org.gnome.Software")

        generated = shortcuts.generate(
            "ubuntu",
            self.rootfs,
            "custom",
            self.output,
        )

        self.assertEqual(len(generated), 1)
        shortcut = generated[0]
        self.assertEqual(
            shortcut.icon_name,
            "spaces-ubuntu-v1-org.gnome.Software.png",
        )
        self.assertIsNotNone(shortcut.icon)
        assert shortcut.icon is not None
        with Image.open(io.BytesIO(shortcut.icon)) as icon:
            self.assertEqual(icon.size, (256, 256))
            self.assertEqual(icon.getpixel((128, 128)), (20, 60, 220, 255))
        self.assertIn(
            (
                f"Icon={self.output}/spaces-icons/"
                "spaces-ubuntu-v1-org.gnome.Software.png\n"
            ),
            shortcut.desktop.decode("utf-8"),
        )

    def test_reconcile_removes_stale_entries_icons_and_versions(self) -> None:
        source = self.system / "editor.desktop"
        source.write_text(self.desktop(), encoding="utf-8")
        self.add_icon()
        old = self.output / "spaces-work-v0-old.desktop"
        old.write_text("old", encoding="utf-8")
        icons = self.output / "spaces-icons"
        icons.mkdir()
        old_icon = icons / "spaces-work-v0-old.png"
        old_icon.write_bytes(b"old")
        other = self.output / "spaces-other-v0-old.desktop"
        other.write_text("other", encoding="utf-8")

        shortcuts.reconcile(
            "work",
            self.rootfs,
            "custom",
            applications_root=self.output,
        )
        self.assertFalse(old.exists())
        self.assertFalse(old_icon.exists())
        current = self.output / "spaces-work-v1-editor.desktop"
        current_icon = icons / "spaces-work-v1-editor.png"
        self.assertTrue(current.is_file())
        self.assertTrue(current_icon.is_file())
        self.assertTrue(other.is_file())

        source.unlink()
        shortcuts.reconcile(
            "work",
            self.rootfs,
            "custom",
            applications_root=self.output,
        )
        self.assertFalse(current.exists())
        self.assertFalse(current_icon.exists())
        self.assertTrue(other.is_file())

        shortcuts.remove("work", applications_root=self.output)
        self.assertTrue(other.is_file())

    def test_remove_deletes_every_flat_version_for_only_one_space(self) -> None:
        icons = self.output / "spaces-icons"
        icons.mkdir()
        managed = [
            self.output / "spaces-work-v0-old.desktop",
            self.output / "spaces-work-v1-current.desktop",
            self.output / "spaces-work-v23-future.desktop",
            icons / "spaces-work-v0-old.png",
            icons / "spaces-work-v1-current.png",
            icons / "spaces-work-v23-future.png",
        ]
        retained = [
            self.output / "spaces-other-v0-old.desktop",
            self.output / "spaces-work-version-unmanaged.desktop",
            icons / "spaces-other-v0-old.png",
        ]
        for path in (*managed, *retained):
            path.write_bytes(b"entry")

        shortcuts.remove("work", applications_root=self.output)

        self.assertFalse(any(path.exists() for path in managed))
        self.assertTrue(all(path.exists() for path in retained))

    def test_source_directory_cannot_escape_rootfs(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "escape.desktop").write_text(
            self.desktop("Escape"), encoding="utf-8"
        )
        self.system.rmdir()
        self.system.symlink_to(outside, target_is_directory=True)
        self.assertEqual(
            shortcuts.generate("work", self.rootfs, "custom", self.output),
            [],
        )


class InotifyTests(unittest.TestCase):
    def test_missing_source_creation_is_observed_through_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rootfs = Path(temporary) / "rootfs"
            rootfs.mkdir()
            monitor = shortcuts.InotifyMonitor(rootfs)
            try:
                source = rootfs / "usr" / "local" / "share" / "applications"
                source.mkdir(parents=True)
                (source / "new.desktop").write_text(
                    "[Desktop Entry]\n" "Type=Application\n" "Name=New\n" "Exec=new\n",
                    encoding="utf-8",
                )
                self.assertTrue(monitor.wait(1000))
                monitor.refresh()
            finally:
                monitor.close()

    def test_worker_monitor_failure_is_nonfatal(self) -> None:
        monitor = mock.Mock()
        monitor.wait.side_effect = OSError("failed")
        worker = shortcuts.ShortcutWorker(
            "work",
            Path("/rootfs"),
            "custom",
            monitor,
            applications_root=Path("/applications"),
        )
        with mock.patch.object(shortcuts.logger, "error") as error:
            worker._run()
        error.assert_called_once()


if __name__ == "__main__":
    unittest.main()
