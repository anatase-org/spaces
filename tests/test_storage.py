from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spaces import core, storage


class PersistentCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.space = Path(self.temporary.name) / "work"
        self.rootfs = self.space / "rootfs"
        self.rootfs.mkdir(parents=True)
        self.cache_root = Path(self.temporary.name) / "cache"
        self.cache_root_patch = mock.patch.object(
            core,
            "CACHE_ROOT",
            self.cache_root,
        )
        self.cache_root_patch.start()

    def tearDown(self) -> None:
        self.cache_root_patch.stop()
        self.temporary.cleanup()

    def test_migrates_rootfs_cache_and_prepares_mountpoint(self) -> None:
        guest_cache = self.rootfs / "var" / "cache"
        guest_cache.mkdir(parents=True)
        (guest_cache / "package").write_text("cached", encoding="utf-8")

        cache = storage.prepare_persistent_cache(self.space)

        self.assertEqual(cache, self.cache_root / "work")
        self.assertEqual(
            (cache / "package").read_text(encoding="utf-8"),
            "cached",
        )
        self.assertTrue(guest_cache.is_dir())
        self.assertEqual(list(guest_cache.iterdir()), [])

    def test_creates_missing_cache_and_is_idempotent(self) -> None:
        cache = storage.prepare_persistent_cache(self.space)
        (cache / "keep").write_text("persistent", encoding="utf-8")
        guest_cache = self.rootfs / "var" / "cache"
        (guest_cache / "hidden").write_text("rootfs", encoding="utf-8")

        self.assertEqual(storage.prepare_persistent_cache(self.space), cache)
        self.assertEqual(
            (cache / "keep").read_text(encoding="utf-8"),
            "persistent",
        )
        self.assertEqual(
            (guest_cache / "hidden").read_text(encoding="utf-8"),
            "rootfs",
        )

    def test_rejects_unsafe_cache_paths(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        self.cache_root.mkdir()
        (self.cache_root / "work").symlink_to(
            outside,
            target_is_directory=True,
        )

        with self.assertRaises(core.SpacesError):
            storage.prepare_persistent_cache(self.space)

    def test_bootstrap_mount_is_always_unmounted(self) -> None:
        cache = storage.prepare_persistent_cache(self.space)

        with (
            mock.patch.object(storage.subprocess, "run") as run,
            self.assertRaisesRegex(RuntimeError, "bootstrap failed"),
        ):
            with storage.mounted_persistent_cache(self.rootfs, cache):
                raise RuntimeError("bootstrap failed")

        target = self.rootfs / "var" / "cache"
        self.assertEqual(
            run.call_args_list,
            [
                mock.call(
                    ["mount", "--bind", str(cache), str(target)],
                    check=True,
                ),
                mock.call(["umount", str(target)], check=True),
            ],
        )


if __name__ == "__main__":
    unittest.main()
