"""Check native tree hunks against GNU diff/patch and the character budget."""
import ctypes
from pathlib import Path
import random
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HUNK = re.compile(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@\n')


def apply_tree_diff(previous, diff):
    """Apply only standard zero-context hunks, validating both coordinate sets."""
    if diff == 'No Changes.\n':
        return previous
    old = previous.splitlines(keepends=True)
    result, cursor = [], 0
    lines = iter(diff.splitlines(keepends=True))
    line = next(lines, None)
    while line is not None:
        header = HUNK.fullmatch(line)
        if header is None:
            raise AssertionError(f'Invalid hunk header: {line!r}')
        start, deleted, target, inserted = header.groups()
        deleted = 1 if deleted is None else int(deleted)
        inserted = 1 if inserted is None else int(inserted)
        start = int(start) - bool(deleted)
        target = int(target) - bool(inserted)
        assert start >= cursor
        result.extend(old[cursor:start])
        assert len(result) == target
        removed, added = [], []
        line = next(lines, None)
        while line is not None and not line.startswith('@@ '):
            if line.startswith('-'):
                removed.append(line[1:])
            elif line.startswith('+'):
                added.append(line[1:])
            else:
                raise AssertionError(f'Unexpected context or content: {line!r}')
            line = next(lines, None)
        assert len(removed) == deleted and len(added) == inserted
        assert old[start:start + deleted] == removed
        result.extend(added)
        cursor = start + deleted
    result.extend(old[cursor:])
    return ''.join(result)


class TreeDiffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('cc') or not shutil.which('pkg-config'):
            raise unittest.SkipTest('native diff tests require a compiler and GLib')
        flags = subprocess.run(['pkg-config', '--cflags', '--libs', 'glib-2.0'], capture_output=True, text=True)
        if flags.returncode:
            raise unittest.SkipTest('GLib development headers unavailable')
        cls.temporary = tempfile.TemporaryDirectory(prefix='scu-diff-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        binary = cls.directory / 'diff.so'
        subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        str(ROOT / 'tests/scu_diff_fixture.c'), *shlex.split(flags.stdout), '-o', str(binary)], check=True)
        cls.library = ctypes.CDLL(str(binary))
        cls.library.scu_test_diff.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        cls.library.scu_test_diff.restype = ctypes.c_void_p
        cls.library.scu_test_free.argtypes = [ctypes.c_void_p]

    def diff(self, previous, current):
        pointer = self.library.scu_test_diff(previous.encode(), current.encode())
        if not pointer:
            return None
        try:
            return ctypes.string_at(pointer).decode()
        finally:
            self.library.scu_test_free(pointer)

    def test_identical_and_empty_trees(self):
        for tree in ('', '[0] frame "Example"\n', 'text "Héllo 世界"\n'):
            self.assertEqual(self.diff(tree, tree), 'No Changes.\n')
        self.assertIsNone(self.diff('text "removed"\n', ''))
        self.assertIsNone(self.diff('', 'text "added"\n'))

    def test_hunks_match_gnu_and_reconstruct_trees(self):
        previous = ''.join(f'  [{i}] button "Button {i}"\n' for i in range(80))
        original = previous.splitlines(keepends=True)
        scenarios = [
            ['text "Beginning"\n', *original],
            [*original, 'text "End"\n'],
            original[:12] + ['  [99] button "Inserted"\n'] + original[12:],
            original[:12] + original[14:],
            original[1:],
            original[:-1],
            original[:12] + ['  [12] button "Renamed" disabled\n'] + original[13:],
            original[:12] + ['  text "Duplicate"\n', '  text "Duplicate"\n'] + original[12:],
            original[:12] + original[13:16] + [original[12]] + original[16:],
            original[:12] + ['  text "Héllo 世界 🌍"\n'] + original[13:60] + ['  text "More"\n'] + original[61:],
        ]
        for rows in scenarios:
            current = ''.join(rows)
            with self.subTest(current=current[:80]):
                diff = self.diff(previous, current)
                self.assertIsNotNone(diff)
                self.assertEqual(apply_tree_diff(previous, diff), current)
                if shutil.which('diff') and shutil.which('patch'):
                    old_path, new_path = self.directory / 'previous', self.directory / 'current'
                    old_path.write_text(previous)
                    new_path.write_text(current)
                    expected = subprocess.run(['diff', '-U0', str(old_path), str(new_path)], capture_output=True, text=True)
                    self.assertEqual(diff, ''.join(expected.stdout.splitlines(keepends=True)[2:]))
                    patched = subprocess.run(['patch', '--silent', str(old_path)],
                        input='--- previous\n+++ current\n' + diff, capture_output=True, text=True)
                    self.assertEqual(patched.returncode, 0, patched.stderr)
                    self.assertEqual(old_path.read_text(), current)

    def test_random_edits_and_duplicate_prose_reconstruct_exactly(self):
        rng = random.Random(73)
        padding = [f'  [{i}] button "Keep {i}"\n' for i in range(100)]
        for _ in range(100):
            old = [f'  text "Prose {rng.randrange(5)}"\n' for _ in range(15)] + padding
            new = old.copy()
            for _ in range(4):
                at = rng.randrange(16)
                new[at:at + rng.randrange(3)] = [f'  text "Edit {rng.randrange(5)}"\n' for _ in range(rng.randrange(3))]
            previous, current = ''.join(old), ''.join(new)
            diff = self.diff(previous, current)
            self.assertIsNotNone(diff)
            self.assertEqual(apply_tree_diff(previous, diff), current)

    def test_sixty_percent_uses_complete_diff_unicode_characters(self):
        # The complete hunk has 30 characters; a 50-character current tree
        # puts it exactly on the 60% boundary. Padding contains multibyte text.
        word, new = 'oldest!', 'OLDEST!'
        hunk = f'@@ -1 +1 @@\n-{word}\n+{new}\n'
        self.assertEqual(len(hunk), 30)
        full_length = len(hunk) * 5 // 3
        for delta in (-1, 0, 1):
            padding = '界' * (full_length + delta - len(new) - 2) + '\n'
            previous, current = word + '\n' + padding, new + '\n' + padding
            self.assertEqual(len(current), full_length + delta)
            diff = self.diff(previous, current)
            self.assertEqual(diff, None if delta < 0 else hunk)
            if delta < 0:
                # Byte counting would incorrectly allow this diff.
                self.assertLessEqual(len(hunk.encode()) * 5, len(current.encode()) * 3)

    def test_size_is_not_fraction_of_rows_changed(self):
        # One long changed row among 100 short rows still needs a full tree.
        padding = 'x\n' * 100
        self.assertIsNone(self.diff('a' * 1000 + '\n' + padding, 'b' * 1000 + '\n' + padding))
        # Most rows changed, but a long unchanged prose row makes hunks cheap.
        padding = 'text "' + 'unchanged ' * 1000 + '"\n'
        previous = 'a\n' * 80 + 'x\n' * 20 + padding
        current = 'b\n' * 80 + 'x\n' * 20 + padding
        diff = self.diff(previous, current)
        self.assertIsNotNone(diff)
        self.assertEqual(apply_tree_diff(previous, diff), current)

    def test_work_limit_falls_back_but_trims_unchanged_edges(self):
        self.assertIsNone(self.diff('a\n' * 2500, 'b\n' * 2500))
        padding = ''.join(f'keep {i}\n' for i in range(3000))
        diff = self.diff(padding + 'old\n' + padding, padding + 'new\n' + padding)
        self.assertIsNotNone(diff)
        self.assertEqual(apply_tree_diff(padding + 'old\n' + padding, diff), padding + 'new\n' + padding)
