import re
import unittest
from pathlib import Path

from spaces import _


class TranslationTests(unittest.TestCase):
    def test_translation_marker_returns_message(self) -> None:
        self.assertEqual(_("Hello"), "Hello")
        self.assertEqual(_("Hello, {}!", "world"), "Hello, world!")
        self.assertEqual(_("Hello, {name}!", name="world"), "Hello, world!")

    def test_translation_calls_do_not_use_fstrings(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src" / "spaces"
        for path in source_root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"_\(\s*f[\"']", source), path)


if __name__ == "__main__":
    unittest.main()
