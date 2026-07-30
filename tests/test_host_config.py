from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path, PurePosixPath

from spaces import host_config


class HostConfigTests(unittest.TestCase):
    def test_absent_configuration_is_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            configuration = host_config.load(
                Path(temporary) / "missing.json"
            )
        self.assertEqual(configuration, host_config.HostConfig())

    def test_malformed_and_unsupported_configuration_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            for content in ('{"version":', '{"version": 2}'):
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    with self.assertLogs(host_config.logger, "WARNING"):
                        configuration = host_config.load(path)
                    self.assertEqual(configuration, host_config.HostConfig())

    def test_valid_configuration_is_typed_and_distro_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source_directory = directory / "zsh"
            source_directory.mkdir()
            source_file = directory / "zshrc"
            source_file.write_text("test\n", encoding="utf-8")
            path = directory / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "distros": {
                            "arch": {
                                "packages": ["screen", "tmux", "zsh"],
                                "mounts": [
                                    {
                                        "source": str(source_directory),
                                        "destination": "/usr/share/vendor/zsh",
                                    },
                                    {
                                        "source": str(source_file),
                                        "destination": "/etc/zshrc",
                                    },
                                ],
                                "overlays": [
                                    {
                                        "source": str(source_directory),
                                        "destination": "/usr/lib64",
                                    }
                                ],
                            },
                            "fedora": {
                                "mounts": [
                                    {
                                        "source": str(source_file),
                                        "destination": "/etc/fedora-only",
                                    }
                                ]
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            configuration = host_config.load(path)

        self.assertEqual(configuration.version, 1)
        self.assertEqual(
            configuration.packages_for("arch"),
            ("screen", "tmux", "zsh"),
        )
        self.assertEqual(
            configuration.overlays_for("arch"),
            (
                host_config.Overlay(
                    source_directory,
                    PurePosixPath("/usr/lib64"),
                ),
            ),
        )
        self.assertEqual(configuration.packages_for("fedora"), ())
        self.assertEqual(
            tuple(
                mount.destination
                for mount in configuration.mounts_for("arch")
            ),
            (
                PurePosixPath("/usr/share/vendor/zsh"),
                PurePosixPath("/etc/zshrc"),
            ),
        )
        self.assertEqual(
            tuple(
                mount.destination
                for mount in configuration.mounts_for("fedora")
            ),
            (PurePosixPath("/etc/fedora-only"),),
        )
        self.assertEqual(configuration.mounts_for("ubuntu"), ())

    def test_invalid_entries_warn_without_discarding_valid_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "source"
            source.mkdir()
            missing = directory / "missing"
            path = directory / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "distros": {
                            "fedora": {
                                "packages": [
                                    "screen",
                                    "screen",
                                    "--assumeyes",
                                    "",
                                    1,
                                ],
                                "overlays": [
                                    {
                                        "source": str(source),
                                        "destination": "/usr/lib64",
                                    },
                                    {
                                        "source": str(source),
                                        "destination": "/usr/lib64",
                                    },
                                    {
                                        "source": str(missing),
                                        "destination": "/usr/lib",
                                    },
                                ],
                                "mounts": [
                                    {
                                        "source": str(source),
                                        "destination": "/usr/share/vendor",
                                    },
                                    {
                                        "source": str(source),
                                        "destination": "/usr/share/vendor",
                                    },
                                    {
                                        "source": str(missing),
                                        "destination": "/etc/missing",
                                    },
                                    {
                                        "source": str(source),
                                        "destination": "/proc/vendor",
                                    },
                                ],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertLogs(host_config.logger, "WARNING") as warnings:
                configuration = host_config.load(path)

        self.assertEqual(configuration.packages_for("fedora"), ("screen",))
        self.assertEqual(len(configuration.overlays_for("fedora")), 1)
        self.assertEqual(len(configuration.mounts_for("fedora")), 1)
        messages = "\n".join(warnings.output)
        self.assertIn("duplicate package", messages)
        self.assertIn("invalid package name", messages)
        self.assertIn("duplicate overlay destination", messages)
        self.assertIn("duplicate mount destination", messages)
        self.assertIn("unsafe mount destination", messages)

    def test_missing_sources_are_silently_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            missing = directory / "missing"
            path = directory / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "distros": {
                            "arch": {
                                "mounts": [
                                    {
                                        "source": str(missing),
                                        "destination": "/etc/missing",
                                    }
                                ],
                                "overlays": [
                                    {
                                        "source": str(missing),
                                        "destination": "/usr/lib",
                                    }
                                ],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertNoLogs(host_config.logger, "WARNING"):
                configuration = host_config.load(path)

        self.assertEqual(configuration.mounts_for("arch"), ())
        self.assertEqual(configuration.overlays_for("arch"), ())


if __name__ == "__main__":
    unittest.main()
