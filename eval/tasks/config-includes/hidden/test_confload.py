import os
import tempfile
import unittest
from pathlib import Path

import confload


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def error(self, path):
        with self.assertRaises(confload.ConfigError) as caught:
            confload.load(path)
        return caught.exception


class FormatTest(Case):
    def test_basic_assignments(self):  # clause 3
        path = self.write("a.conf", "name = demo\nport=8080\n")
        self.assertEqual(confload.load(path), {"name": "demo", "port": "8080"})

    def test_accepts_pathlike_and_str(self):  # interface
        path = self.write("a.conf", "x = 1\n")
        self.assertEqual(confload.load(str(path)), confload.load(path))

    def test_blank_and_comment_lines_ignored(self):  # clause 2
        path = self.write("a.conf", "\n# comment\n   # indented comment\n\nx = 1\n")
        self.assertEqual(confload.load(path), {"x": "1"})

    def test_split_on_first_equals(self):  # clause 3
        path = self.write("a.conf", "url = http://h/?a=1&b=2\n")
        self.assertEqual(confload.load(path), {"url": "http://h/?a=1&b=2"})

    def test_empty_value(self):  # clause 3
        path = self.write("a.conf", "empty =\n")
        self.assertEqual(confload.load(path), {"empty": ""})

    def test_keys_case_sensitive_and_dotted(self):  # clause 4
        path = self.write("a.conf", "Key = 1\nkey = 2\ndb.host_1 = h\n")
        self.assertEqual(confload.load(path), {"Key": "1", "key": "2", "db.host_1": "h"})

    def test_invalid_key_reports_path_and_line(self):  # clause 4
        path = self.write("a.conf", "ok = 1\n\n1bad = 2\n")
        err = self.error(path)
        self.assertIsInstance(err, ValueError)
        self.assertEqual((Path(err.path).resolve(), err.line), (path, 3))

    def test_unparseable_line_reports_path_and_line(self):  # clause 4
        path = self.write("a.conf", "# c\njust words\n")
        err = self.error(path)
        self.assertEqual((Path(err.path).resolve(), err.line), (path, 2))

    def test_include_named_key_is_assignment(self):  # clause 5
        path = self.write("a.conf", "include = yes\n")
        self.assertEqual(confload.load(path), {"include": "yes"})

    def test_later_assignment_overrides(self):  # clause 6
        path = self.write("a.conf", "x = 1\nx = 2\n")
        self.assertEqual(confload.load(path), {"x": "2"})


class IncludeTest(Case):
    def test_include_processed_in_place(self):  # clauses 5, 6
        self.write("base.conf", "x = base\ny = base\n")
        path = self.write("main.conf", "x = before\ninclude base.conf\ny = after\n")
        self.assertEqual(confload.load(path), {"x": "base", "y": "after"})

    def test_relative_to_including_file_not_cwd(self):  # clause 5
        self.write("conf/shared/common.conf", "level = deep\n")
        self.write("conf/sub.conf", "include shared/common.conf\n")
        path = self.write("main.conf", "include conf/sub.conf\n")
        elsewhere = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, elsewhere)
        old = os.getcwd()
        os.chdir(elsewhere)
        self.addCleanup(os.chdir, old)
        self.assertEqual(confload.load(path), {"level": "deep"})

    def test_absolute_include(self):  # clause 5
        other = self.write("x/abs.conf", "a = 1\n")
        path = self.write("main.conf", f"include {other}\n")
        self.assertEqual(confload.load(path), {"a": "1"})

    def test_diamond_is_not_a_cycle(self):  # clause 7
        self.write("d.conf", "count = ${count}x\n")
        self.write("b.conf", "include d.conf\n")
        self.write("c.conf", "include d.conf\n")
        path = self.write("a.conf", "count = \ninclude b.conf\ninclude c.conf\n")
        self.assertEqual(confload.load(path), {"count": "xx"})

    def test_self_include_is_cycle(self):  # clause 7
        path = self.write("a.conf", "x = 1\ninclude a.conf\n")
        err = self.error(path)
        self.assertIn("cycle", str(err))
        self.assertEqual((Path(err.path).resolve(), err.line), (path, 2))

    def test_indirect_cycle_reports_closing_include(self):  # clause 7
        a = self.write("a.conf", "include b.conf\n")
        b = self.write("b.conf", "y = 1\ninclude a.conf\n")
        err = self.error(a)
        self.assertIn("cycle", str(err))
        self.assertEqual((Path(err.path).resolve(), err.line), (b, 2))

    def test_missing_include_reports_include_line(self):  # clause 8
        b = self.write("b.conf", "\ninclude nope.conf\n")
        a = self.write("a.conf", "include b.conf\n")
        err = self.error(a)
        self.assertEqual((Path(err.path).resolve(), err.line), (b, 2))


class InterpolationTest(Case):
    def test_interpolates_current_value(self):  # clause 9
        path = self.write("a.conf", "host = h\nurl = http://${host}:${port}\n".replace("${port}", "80"))
        self.assertEqual(confload.load(path)["url"], "http://h:80")

    def test_later_change_does_not_rewrite(self):  # clause 9
        path = self.write("a.conf", "a = 1\nb = ${a}\na = 2\n")
        self.assertEqual(confload.load(path), {"a": "2", "b": "1"})

    def test_self_reference_uses_previous_value(self):  # clause 9
        path = self.write("a.conf", "p = /bin\np = ${p}:/usr/bin\n")
        self.assertEqual(confload.load(path)["p"], "/bin:/usr/bin")

    def test_interpolation_across_include(self):  # clauses 5, 9
        self.write("base.conf", "root = /srv\n")
        path = self.write("a.conf", "include base.conf\ndata = ${root}/data\n")
        self.assertEqual(confload.load(path)["data"], "/srv/data")

    def test_unknown_name_reports_line(self):  # clause 10
        path = self.write("a.conf", "a = 1\nb = ${missing}\n")
        err = self.error(path)
        self.assertEqual((Path(err.path).resolve(), err.line), (path, 2))

    def test_forward_reference_is_unknown(self):  # clause 10
        path = self.write("a.conf", "b = ${a}\na = 1\n")
        self.assertEqual(self.error(path).line, 1)

    def test_dollar_escapes(self):  # clause 11
        path = self.write("a.conf", "price = $$5 and $x and $\n")
        self.assertEqual(confload.load(path)["price"], "$5 and $x and $")

    def test_escaped_brace_not_interpolated(self):  # clause 11
        path = self.write("a.conf", "t = $${name}\n")
        self.assertEqual(confload.load(path)["t"], "${name}")
