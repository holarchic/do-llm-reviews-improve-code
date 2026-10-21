import tempfile
import unittest
from pathlib import Path

import confload


class OptionalIncludeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_missing_optional_include_is_skipped(self):
        path = self.write("a.conf", "x = 1\ninclude? local.conf\ny = 2\n")
        self.assertEqual(confload.load(path), {"x": "1", "y": "2"})

    def test_present_optional_include_is_processed(self):
        self.write("local.conf", "x = override\n")
        path = self.write("a.conf", "x = 1\ninclude? local.conf\n")
        self.assertEqual(confload.load(path), {"x": "override"})

    def test_optional_include_relative_to_including_file(self):
        self.write("sub/local.conf", "z = 3\n")
        self.write("sub/inner.conf", "include? local.conf\n")
        path = self.write("a.conf", "include sub/inner.conf\n")
        self.assertEqual(confload.load(path), {"z": "3"})

    def test_cycle_through_optional_include_still_raises(self):
        a = self.write("a.conf", "include? b.conf\n")
        self.write("b.conf", "include? a.conf\n")
        with self.assertRaises(confload.ConfigError) as caught:
            confload.load(a)
        self.assertIn("cycle", str(caught.exception))

    def test_syntax_error_inside_optional_include_still_raises(self):
        self.write("local.conf", "\nnot valid\n")
        path = self.write("a.conf", "include? local.conf\n")
        with self.assertRaises(confload.ConfigError) as caught:
            confload.load(path)
        self.assertEqual(caught.exception.line, 2)
