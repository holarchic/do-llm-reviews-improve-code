import unittest

import ignore_match


def rules(*lines):
    return ignore_match.IgnoreRules(list(lines))


def ignored(lines, path, is_dir=False):
    return ignore_match.IgnoreRules(lines).is_ignored(path, is_dir)


def decision(lines, path, is_dir=False):
    found = ignore_match.IgnoreRules(lines).explain(path, is_dir)
    if found is None:
        return None
    return (found.line_number, found.negated, found.path)


class LineReadingTest(unittest.TestCase):  # rules 1, 2, 3
    def test_comment_line_skipped(self):
        self.assertFalse(ignored(["#foo"], "#foo"))

    def test_escaped_hash_is_literal(self):
        self.assertTrue(ignored(["\\#foo"], "#foo"))

    def test_leading_spaces_kept(self):
        self.assertTrue(ignored(["  foo"], "  foo"))
        self.assertFalse(ignored(["  foo"], "foo"))

    def test_crlf_removed(self):
        self.assertTrue(ignored(["foo\r\n"], "foo"))
        self.assertTrue(ignored(["dir/\r\n"], "dir", is_dir=True))

    def test_line_numbers_count_blank_and_comment_lines(self):
        self.assertEqual(decision(["\r\n", "# note\r\n", "", "bar\r\n"], "bar"), (4, False, "bar"))

    def test_generator_input(self):
        r = ignore_match.IgnoreRules(line for line in ["*.log", "!keep.log"])
        self.assertTrue(r.is_ignored("a.log"))
        self.assertFalse(r.is_ignored("keep.log"))

    def test_source_list_mutation_has_no_effect(self):
        lines = ["*.log"]
        r = ignore_match.IgnoreRules(lines)
        lines.append("!keep.log")
        lines[0] = "*.tmp"
        self.assertTrue(r.is_ignored("keep.log"))
        self.assertFalse(r.is_ignored("a.tmp"))


class TrailingSpaceTest(unittest.TestCase):  # rules 4, 5
    def test_trailing_spaces_removed(self):
        self.assertTrue(ignored(["foo   "], "foo"))

    def test_escaped_trailing_space_kept(self):
        self.assertTrue(ignored(["foo\\ "], "foo "))
        self.assertFalse(ignored(["foo\\ "], "foo"))

    def test_crlf_removed_before_trailing_spaces(self):
        self.assertTrue(ignored(["foo\\ \r\n"], "foo "))
        self.assertTrue(ignored(["foo  \r\n"], "foo"))

    def test_unescaped_space_after_escaped_space_removed(self):
        self.assertTrue(ignored(["foo\\  "], "foo "))
        self.assertFalse(ignored(["foo\\  "], "foo  "))

    def test_tab_not_removed(self):
        self.assertTrue(ignored(["foo\t"], "foo\t"))
        self.assertFalse(ignored(["foo\t"], "foo"))

    def test_spaces_only_line_is_blank(self):
        self.assertEqual(decision(["   ", "a"], "a"), (2, False, "a"))
        self.assertFalse(ignored(["   "], "   "))


class NegationTest(unittest.TestCase):  # rules 6, 20
    def test_negation_reincludes(self):
        lines = ["*.log", "!keep.log"]
        self.assertFalse(ignored(lines, "keep.log"))
        self.assertTrue(ignored(lines, "other.log"))

    def test_last_match_wins(self):
        self.assertTrue(ignored(["!keep.log", "*.log"], "keep.log"))
        self.assertTrue(ignored(["*", "!*.txt", "a.txt"], "a.txt"))
        self.assertFalse(ignored(["*", "!*.txt", "a.txt"], "b.txt"))

    def test_escaped_bang_is_literal(self):
        self.assertTrue(ignored(["\\!important"], "!important"))
        self.assertFalse(ignored(["\\!important"], "important"))

    def test_negated_escaped_bang(self):
        lines = ["!*", "!\\!x"]
        self.assertEqual(decision(lines, "!x"), (2, True, "!x"))

    def test_bang_then_hash_is_not_comment(self):
        self.assertFalse(ignored(["\\#x", "!#x"], "#x"))
        self.assertEqual(decision(["\\#x", "!#x"], "#x"), (2, True, "#x"))


class EmptyPatternTest(unittest.TestCase):  # rule 8
    def test_bare_markers_skipped(self):
        lines = ["!", "/", "!/", "foo"]
        self.assertEqual(decision(lines, "foo"), (4, False, "foo"))
        self.assertFalse(ignored(lines, "x"))
        self.assertFalse(ignored(lines, "x", is_dir=True))


class DirectoryOnlyTest(unittest.TestCase):  # rules 7, 18, 21
    def test_matches_directory_not_file(self):
        self.assertTrue(ignored(["build/"], "build", is_dir=True))
        self.assertFalse(ignored(["build/"], "build"))

    def test_unanchored_at_any_depth(self):
        self.assertTrue(ignored(["build/"], "a/b/build", is_dir=True))
        self.assertFalse(ignored(["build/"], "a/b/build"))

    def test_contents_ignored_through_directory(self):
        self.assertTrue(ignored(["build/"], "build/out.o"))
        self.assertTrue(ignored(["build/"], "src/build/deep/out.o"))


class AnchoringTest(unittest.TestCase):  # rules 9, 15, 16
    def test_leading_slash_anchors(self):
        self.assertTrue(ignored(["/foo"], "foo"))
        self.assertFalse(ignored(["/foo"], "a/foo"))

    def test_middle_slash_anchors(self):
        self.assertTrue(ignored(["a/b"], "a/b"))
        self.assertFalse(ignored(["a/b"], "x/a/b"))

    def test_trailing_slash_removed_before_anchoring(self):
        self.assertTrue(ignored(["foo/"], "x/foo", is_dir=True))
        self.assertTrue(ignored(["foo/"], "x/y/foo/z"))

    def test_middle_slash_with_trailing_slash_anchors(self):
        self.assertTrue(ignored(["a/b/"], "a/b", is_dir=True))
        self.assertFalse(ignored(["a/b/"], "x/a/b", is_dir=True))
        self.assertFalse(ignored(["doc/frotz/"], "a/doc/frotz", is_dir=True))

    def test_negated_anchored(self):
        lines = ["foo", "!/foo"]
        self.assertFalse(ignored(lines, "foo"))
        self.assertTrue(ignored(lines, "a/foo"))

    def test_anchored_does_not_match_longer_path_itself(self):
        self.assertEqual(decision(["a/b"], "a/b/c"), (1, False, "a/b"))
        self.assertIsNone(decision(["a/b"], "a"))


class WildcardTest(unittest.TestCase):  # rule 11
    def test_star_suffix(self):
        self.assertTrue(ignored(["*.txt"], "a.txt"))
        self.assertTrue(ignored(["*.txt"], "dir/a.txt"))
        self.assertFalse(ignored(["*.txt"], "a.txt.bak"))

    def test_star_matches_dotfile(self):
        self.assertTrue(ignored(["*"], ".env"))
        self.assertTrue(ignored(["*rc"], ".bashrc"))

    def test_question_mark_exactly_one(self):
        self.assertTrue(ignored(["a?c"], "abc"))
        self.assertFalse(ignored(["a?c"], "ac"))
        self.assertFalse(ignored(["a?c"], "abbc"))

    def test_star_does_not_cross_slash(self):
        self.assertFalse(ignored(["a/*.txt"], "a/b/c.txt"))
        self.assertTrue(ignored(["a/*.txt"], "a/c.txt"))

    def test_question_mark_does_not_match_slash(self):
        self.assertFalse(ignored(["x/a?b"], "x/a/b"))

    def test_regex_characters_literal(self):
        self.assertFalse(ignored(["a.b"], "axb"))
        self.assertTrue(ignored(["a.b"], "a.b"))
        self.assertFalse(ignored(["a+b"], "aab"))
        self.assertTrue(ignored(["a+b"], "a+b"))
        self.assertTrue(ignored(["(x)|$y^"], "(x)|$y^"))
        self.assertTrue(ignored(["{a,b}"], "{a,b}"))
        self.assertFalse(ignored(["{a,b}"], "a"))

    def test_case_sensitive(self):
        self.assertFalse(ignored(["*.TXT"], "a.txt"))


class EscapeTest(unittest.TestCase):  # rule 12
    def test_escaped_star(self):
        self.assertTrue(ignored(["\\*"], "*"))
        self.assertFalse(ignored(["\\*"], "a"))

    def test_escaped_question_mark(self):
        self.assertTrue(ignored(["\\?"], "?"))
        self.assertFalse(ignored(["\\?"], "a"))

    def test_escaped_bracket(self):
        self.assertTrue(ignored(["\\[a]"], "[a]"))
        self.assertFalse(ignored(["\\[a]"], "a"))

    def test_escaped_backslash(self):
        self.assertTrue(ignored(["a\\\\b"], "a\\b"))

    def test_escaped_double_star_is_literal(self):
        self.assertFalse(ignored(["\\*\\*/foo"], "x/foo"))
        self.assertTrue(ignored(["\\*\\*/foo"], "**/foo"))


class BracketTest(unittest.TestCase):  # rules 13, 14
    def test_range_inclusive(self):
        lines = ["[a-c]x"]
        self.assertTrue(ignored(lines, "ax"))
        self.assertTrue(ignored(lines, "cx"))
        self.assertFalse(ignored(lines, "dx"))

    def test_negated_set(self):
        lines = ["[!a-c]x"]
        self.assertTrue(ignored(lines, "dx"))
        self.assertFalse(ignored(lines, "bx"))

    def test_caret_is_literal(self):
        self.assertTrue(ignored(["[^a]"], "^"))
        self.assertTrue(ignored(["[^a]"], "a"))
        self.assertFalse(ignored(["[^a]"], "b"))

    def test_leading_close_bracket_is_member(self):
        self.assertTrue(ignored(["[]a]"], "]"))
        self.assertTrue(ignored(["[]a]"], "a"))
        self.assertFalse(ignored(["[]a]"], "b"))

    def test_negated_leading_close_bracket(self):
        self.assertTrue(ignored(["[!]]x"], "ax"))
        self.assertFalse(ignored(["[!]]x"], "]x"))

    def test_dash_first_or_last_is_member(self):
        self.assertTrue(ignored(["[a-]"], "-"))
        self.assertTrue(ignored(["[-a]"], "-"))
        self.assertFalse(ignored(["[a-]"], "b"))

    def test_escaped_close_bracket_member(self):
        self.assertTrue(ignored(["[\\]]"], "]"))
        self.assertFalse(ignored(["[\\]]"], "\\"))

    def test_single_character_range(self):
        self.assertTrue(ignored(["[a-a]"], "a"))
        self.assertFalse(ignored(["[a-a]"], "b"))


class PatternErrorTest(unittest.TestCase):  # rule 10
    def assert_error(self, lines, line_number):
        with self.assertRaises(ignore_match.PatternError) as caught:
            ignore_match.IgnoreRules(lines)
        self.assertIsInstance(caught.exception, ValueError)
        self.assertEqual(caught.exception.line_number, line_number)
        self.assertEqual(caught.exception.line, lines[line_number - 1])

    def test_unclosed_bracket(self):
        self.assert_error(["[abc"], 1)

    def test_line_number_and_raw_line(self):
        self.assert_error(["# c", "", "ok", "bad[\n"], 4)

    def test_first_invalid_line_reported(self):
        self.assert_error(["fine", "x[", "[z-a]"], 2)

    def test_empty_brackets_unclosed(self):
        self.assert_error(["[]"], 1)
        self.assert_error(["[!]"], 1)

    def test_slash_in_brackets(self):
        self.assert_error(["a[/]b"], 1)

    def test_reversed_range(self):
        self.assert_error(["ok", "[z-a]"], 2)

    def test_trailing_backslash(self):
        self.assert_error(["foo\\"], 1)

    def test_valid_lines_not_errors(self):
        r = rules("#[", "foo\\ ", "[]]", "\\[")
        self.assertTrue(r.is_ignored("]"))
        self.assertTrue(r.is_ignored("["))


class DoubleStarTest(unittest.TestCase):  # rule 17
    def test_leading(self):
        lines = ["**/foo"]
        self.assertTrue(ignored(lines, "foo"))
        self.assertTrue(ignored(lines, "a/foo"))
        self.assertTrue(ignored(lines, "a/b/foo"))

    def test_leading_with_more_segments(self):
        lines = ["**/foo/bar"]
        self.assertTrue(ignored(lines, "foo/bar"))
        self.assertTrue(ignored(lines, "x/y/foo/bar"))
        self.assertFalse(ignored(lines, "foo/x/bar"))

    def test_trailing_excludes_directory_itself(self):
        self.assertFalse(ignored(["abc/**"], "abc", is_dir=True))
        self.assertIsNone(decision(["abc/**"], "abc", is_dir=True))

    def test_trailing_matches_contents(self):
        self.assertEqual(decision(["abc/**"], "abc/x"), (1, False, "abc/x"))
        self.assertTrue(ignored(["abc/**"], "abc/x/y"))

    def test_trailing_reports_first_ignored_ancestor(self):
        self.assertEqual(decision(["abc/**"], "abc/x/y"), (1, False, "abc/x"))

    def test_trailing_allows_reinclusion(self):
        lines = ["abc/**", "!abc/keep"]
        self.assertFalse(ignored(lines, "abc/keep"))
        self.assertTrue(ignored(lines, "abc/other"))

    def test_trailing_directory_only(self):
        lines = ["abc/**/"]
        self.assertFalse(ignored(lines, "abc/x"))
        self.assertTrue(ignored(lines, "abc/x", is_dir=True))
        self.assertTrue(ignored(lines, "abc/x/y"))

    def test_middle_zero_components(self):
        self.assertTrue(ignored(["a/**/b"], "a/b"))

    def test_middle_many_components(self):
        self.assertTrue(ignored(["a/**/b"], "a/x/b"))
        self.assertTrue(ignored(["a/**/b"], "a/x/y/b"))
        self.assertFalse(ignored(["a/**/b"], "a/xb"))
        self.assertFalse(ignored(["a/**/b"], "x/a/b"))

    def test_consecutive_middle(self):
        self.assertTrue(ignored(["a/**/**/b"], "a/b"))
        self.assertTrue(ignored(["a/**/**/b"], "a/x/y/z/b"))

    def test_other_double_stars_are_single(self):
        self.assertTrue(ignored(["a**b"], "axxb"))
        self.assertTrue(ignored(["foo/**bar"], "foo/xbar"))
        self.assertFalse(ignored(["foo/**bar"], "foo/x/bar"))

    def test_lone_double_star(self):
        self.assertTrue(ignored(["**"], "anything"))
        self.assertTrue(ignored(["**"], "a/b/c", is_dir=True))


class AncestorTest(unittest.TestCase):  # rule 21
    def test_negation_cannot_reinclude_under_excluded_directory(self):
        self.assertTrue(ignored(["build/", "!build/keep.txt"], "build/keep.txt"))

    def test_directory_only_negation(self):
        lines = ["*", "!*/"]
        self.assertTrue(ignored(lines, "a"))
        self.assertFalse(ignored(lines, "a", is_dir=True))
        self.assertTrue(ignored(lines, "a/b"))
        self.assertFalse(ignored(lines, "a/b", is_dir=True))

    def test_git_documentation_example(self):
        lines = ["/*", "!/foo", "/foo/*", "!/foo/bar"]
        self.assertFalse(ignored(lines, "foo/bar"))
        self.assertTrue(ignored(lines, "foo/baz"))
        self.assertTrue(ignored(lines, "other/x"))
        self.assertFalse(ignored(lines, "foo", is_dir=True))

    def test_reincluded_subdirectory(self):
        lines = ["dir/*", "!dir/sub/"]
        self.assertFalse(ignored(lines, "dir/sub/file"))
        self.assertTrue(ignored(lines, "dir/other"))

    def test_unanchored_negation_without_excluded_parent(self):
        lines = ["*.log", "!important.log"]
        self.assertFalse(ignored(lines, "logs/important.log"))

    def test_file_pattern_excludes_directory_contents(self):
        self.assertTrue(ignored(["logs", "!logs/a"], "logs/a"))

    def test_ancestor_is_treated_as_directory(self):
        self.assertTrue(ignored(["sub/"], "sub/file", is_dir=False))

    def test_negated_ancestor_does_not_exclude(self):
        self.assertFalse(ignored(["a/", "!a/"], "a/x"))
        self.assertFalse(ignored(["a", "!a/"], "a/x"))


class PathArgumentTest(unittest.TestCase):  # rule 19
    def test_empty_path(self):
        with self.assertRaises(ValueError):
            rules("a").is_ignored("")

    def test_absolute_path(self):
        with self.assertRaises(ValueError):
            rules("a").is_ignored("/a")

    def test_empty_component(self):
        with self.assertRaises(ValueError):
            rules("a").is_ignored("a//b")
        with self.assertRaises(ValueError):
            rules("a").explain("a//")

    def test_dot_components(self):
        for path in ("./a", "a/../b", "a/.", ".."):
            with self.subTest(path=path), self.assertRaises(ValueError):
                rules("a").is_ignored(path)

    def test_trailing_slash_means_directory(self):
        self.assertTrue(rules("build/").is_ignored("build/"))
        self.assertTrue(rules("build/").is_ignored("x/build/", is_dir=False))


class ExplainTest(unittest.TestCase):  # rules 20, 22, 23
    def test_no_match(self):
        self.assertIsNone(decision(["*.log"], "a.txt"))

    def test_negated_decision(self):
        self.assertEqual(decision(["*.log", "!keep.log"], "keep.log"), (2, True, "keep.log"))

    def test_last_matching_line(self):
        self.assertEqual(decision(["*.log", "", "# x", "a.log", "!b.log"], "a.log"), (4, False, "a.log"))

    def test_ancestor_reported(self):
        self.assertEqual(decision(["build/", "!build/keep.txt"], "build/keep.txt"), (1, False, "build"))

    def test_shortest_ancestor_first(self):
        self.assertEqual(decision(["a/", "a/b/"], "a/b/c"), (1, False, "a"))
        self.assertEqual(decision(["a/b/", "a/"], "a/b/c"), (2, False, "a"))

    def test_negated_ancestor_never_reported(self):
        self.assertEqual(decision(["a/", "!a/", "*.txt"], "a/x.txt"), (3, False, "a/x.txt"))
        self.assertIsNone(decision(["a/", "!a/"], "a/x"))

    def test_ancestor_deciding_line_is_its_last_match(self):
        self.assertEqual(decision(["/a", "a/", "!b"], "a/b"), (2, False, "a"))

    def test_trailing_slash_not_in_decision_path(self):
        self.assertEqual(decision(["build/"], "out/build/"), (1, False, "out/build"))

    def test_is_ignored_agrees_with_explain(self):
        r = rules("*.log", "!keep.log", "build/", "!build/x", "docs/**/tmp", "a/**")
        for path in ("a.log", "keep.log", "build/x", "build", "docs/tmp", "docs/q/tmp", "a/b", "a", "z"):
            for is_dir in (False, True):
                found = r.explain(path, is_dir)
                expected = found is not None and not found.negated
                with self.subTest(path=path, is_dir=is_dir):
                    self.assertEqual(r.is_ignored(path, is_dir), expected)


if __name__ == "__main__":
    unittest.main()
