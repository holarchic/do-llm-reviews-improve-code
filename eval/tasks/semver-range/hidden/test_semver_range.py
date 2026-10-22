import functools
import unittest

import semver_range as sr


class RangeCase(unittest.TestCase):
    def matches(self, range_text, *versions):
        for version in versions:
            self.assertIs(sr.satisfies(version, range_text), True, f"{version} should satisfy {range_text!r}")

    def rejects(self, range_text, *versions):
        for version in versions:
            self.assertIs(sr.satisfies(version, range_text), False, f"{version} should not satisfy {range_text!r}")

    def invalid_range(self, range_text):
        with self.assertRaises(sr.InvalidRangeError, msg=repr(range_text)) as caught:
            sr.satisfies("1.0.0", range_text)
        self.assertEqual(caught.exception.text, range_text)


def invalid_version(test, text):
    with test.assertRaises(sr.InvalidVersionError, msg=repr(text)) as caught:
        sr.parse(text)
    test.assertEqual(caught.exception.text, text)


class ParseTest(unittest.TestCase):  # rules 1, 5
    def test_plain_version_fields(self):
        self.assertEqual(sr.parse("10.20.30"), sr.Version(10, 20, 30, (), ()))
        self.assertEqual(sr.parse("0.0.0"), sr.Version(0, 0, 0, (), ()))

    def test_version_is_frozen_dataclass_with_field_order(self):
        version = sr.parse("1.2.3")
        self.assertEqual((version.major, version.minor, version.patch, version.prerelease, version.build),
                         (1, 2, 3, (), ()))
        with self.assertRaises(Exception):
            version.major = 5

    def test_prerelease_types(self):  # rule 5
        self.assertEqual(sr.parse("1.2.3-beta.11.0a+exp.007"),
                         sr.Version(1, 2, 3, ("beta", 11, "0a"), ("exp", "007")))
        version = sr.parse("1.0.0-0.3.7")
        self.assertEqual(version.prerelease, (0, 3, 7))
        self.assertTrue(all(type(i) is int for i in version.prerelease))

    def test_build_stays_strings(self):  # rule 5
        version = sr.parse("1.0.0+20130313144700.1")
        self.assertEqual(version.prerelease, ())
        self.assertEqual(version.build, ("20130313144700", "1"))

    def test_huge_numbers(self):  # rule 5
        self.assertEqual(sr.parse("1.2.99999999999999999999").patch, 99999999999999999999)


class VersionSyntaxTest(unittest.TestCase):  # rules 1, 2, 3, 4, 6
    def test_wrong_part_count(self):  # rule 1
        for text in ("1", "1.2", "1.2.3.4", "1..3", "1.2.", ""):
            invalid_version(self, text)

    def test_prefixes_whitespace_and_letters(self):  # rule 1
        for text in ("v1.2.3", "=1.2.3", " 1.2.3", "1.2.3 ", "1.2.3\n"):
            invalid_version(self, text)
        for text in ("1.2.x", "1.*.3", "a.b.c", "1.2.3a"):
            invalid_version(self, text)

    def test_leading_zero_in_core(self):  # rule 1
        for text in ("01.2.3", "1.02.3", "1.2.03", "00.0.0"):
            invalid_version(self, text)

    def test_non_ascii_digits(self):  # rule 1
        invalid_version(self, "١.2.3")
        invalid_version(self, "1.2.3-١")

    def test_empty_identifiers(self):  # rule 2
        for text in ("1.2.3-", "1.2.3+", "1.0.0-a..b", "1.0.0+a.", "1.0.0-.a", "1.0.0-a+"):
            invalid_version(self, text)

    def test_bad_identifier_characters(self):  # rule 2
        for text in ("1.0.0-al_pha", "1.0.0+b$", "1.0.0-café", "1.0.0-a b"):
            invalid_version(self, text)

    def test_numeric_prerelease_leading_zero(self):  # rule 3
        for text in ("1.0.0-01", "1.0.0-alpha.007", "1.0.0-00"):
            invalid_version(self, text)

    def test_alphanumeric_may_start_with_zero(self):  # rule 3
        self.assertEqual(sr.parse("1.0.0-0a").prerelease, ("0a",))
        self.assertEqual(sr.parse("1.0.0-0").prerelease, (0,))
        self.assertEqual(sr.parse("1.0.0-alpha.10").prerelease, ("alpha", 10))

    def test_build_may_have_leading_zero(self):  # rule 3
        self.assertEqual(sr.parse("1.0.0+001").build, ("001",))
        self.assertEqual(sr.parse("1.0.0-rc.1+00.01").build, ("00", "01"))

    def test_hyphens_inside_identifiers(self):  # rule 4
        self.assertEqual(sr.parse("1.0.0--x-1").prerelease, ("-x-1",))
        self.assertEqual(sr.parse("1.0.0-a+b-c"), sr.Version(1, 0, 0, ("a",), ("b-c",)))
        self.assertEqual(sr.parse("1.0.0-x.-1").prerelease, ("x", "-1"))
        self.assertEqual(sr.parse("1.0.0-01-a").prerelease, ("01-a",))

    def test_error_type_and_text(self):  # rule 6
        with self.assertRaises(sr.InvalidVersionError) as caught:
            sr.parse(" 1.2.3 ")
        self.assertEqual(caught.exception.text, " 1.2.3 ")
        self.assertIsInstance(caught.exception, ValueError)


class CompareTest(unittest.TestCase):  # rules 7, 8
    def test_numeric_not_lexical(self):
        self.assertEqual(sr.compare("1.10.0", "1.9.0"), 1)
        self.assertEqual(sr.compare("2.0.0", "10.0.0"), -1)
        self.assertEqual(sr.compare("1.2.10", "1.2.9"), 1)

    def test_release_above_prerelease(self):
        self.assertEqual(sr.compare("1.0.0", "1.0.0-rc.1"), 1)
        self.assertEqual(sr.compare("1.0.0-rc.1", "1.0.0"), -1)
        self.assertEqual(sr.compare("1.0.0-rc.1", "0.9.9"), 1)

    def test_spec_chain(self):
        chain = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2",
                 "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0"]
        shuffled = [chain[i] for i in (5, 2, 7, 0, 6, 3, 1, 4)]
        self.assertEqual(sorted(shuffled, key=functools.cmp_to_key(sr.compare)), chain)

    def test_numeric_identifiers_compare_numerically(self):
        self.assertEqual(sr.compare("1.0.0-beta.11", "1.0.0-beta.2"), 1)
        self.assertEqual(sr.compare("1.0.0-10", "1.0.0-9"), 1)

    def test_numeric_below_alphanumeric(self):
        self.assertEqual(sr.compare("1.0.0-1", "1.0.0-a"), -1)
        self.assertEqual(sr.compare("1.0.0-999", "1.0.0-0a"), -1)
        self.assertEqual(sr.compare("1.0.0-alpha.beta", "1.0.0-alpha.1"), 1)

    def test_ascii_order_of_alphanumerics(self):
        self.assertEqual(sr.compare("1.0.0-Beta", "1.0.0-alpha"), -1)
        self.assertEqual(sr.compare("1.0.0-a-b", "1.0.0-a"), 1)
        self.assertEqual(sr.compare("1.0.0-rc", "1.0.0-RC"), 1)

    def test_shorter_prerelease_lower(self):
        self.assertEqual(sr.compare("1.0.0-alpha", "1.0.0-alpha.0"), -1)
        self.assertEqual(sr.compare("1.0.0-a.b.c", "1.0.0-a.b"), 1)

    def test_build_ignored(self):
        self.assertEqual(sr.compare("1.0.0+a", "1.0.0+b"), 0)
        self.assertEqual(sr.compare("1.0.0-rc.1+x.y", "1.0.0-rc.1"), 0)

    def test_unit_values_and_antisymmetry(self):  # rule 8
        self.assertEqual(sr.compare("1.0.0", "5.0.0"), -1)
        self.assertEqual(sr.compare("9.0.0", "1.0.0"), 1)
        self.assertEqual(sr.compare("1.2.3", "1.2.3"), 0)
        pairs = [("1.0.0-alpha", "1.0.0-alpha.1"), ("1.0.0-1", "1.0.0-a"), ("0.1.0", "0.0.9")]
        for a, b in pairs:
            self.assertEqual(sr.compare(a, b), -sr.compare(b, a))

    def test_invalid_operands(self):  # rule 8
        with self.assertRaises(sr.InvalidVersionError) as caught:
            sr.compare("1.2.3", "1.2")
        self.assertEqual(caught.exception.text, "1.2")
        with self.assertRaises(sr.InvalidVersionError) as caught:
            sr.compare("bad", "worse")
        self.assertEqual(caught.exception.text, "bad")


class PrimitiveComparatorTest(RangeCase):  # rule 15
    def test_greater_than_is_strict(self):
        self.matches(">1.2.3", "1.2.4", "2.0.0")
        self.rejects(">1.2.3", "1.2.3", "1.2.2")

    def test_inclusive_bounds(self):
        self.matches(">=1.2.3", "1.2.3", "1.3.0")
        self.rejects(">=1.2.3", "1.2.2")
        self.matches("<=1.2.3", "1.2.3", "1.0.0")
        self.rejects("<=1.2.3", "1.2.4")

    def test_less_than_is_strict(self):
        self.matches("<1.2.3", "1.2.2", "0.0.0")
        self.rejects("<1.2.3", "1.2.3", "1.2.4")

    def test_equal_and_bare(self):
        self.matches("=1.2.3", "1.2.3", "1.2.3+build.1")
        self.matches("1.2.3", "1.2.3")
        self.rejects("1.2.3", "1.2.4", "1.2.2")

    def test_build_in_range_ignored(self):
        self.matches("1.2.3+abc", "1.2.3", "1.2.3+other")
        self.matches(">1.2.3+zzz", "1.2.4")
        self.rejects(">1.2.3+zzz", "1.2.3+zzzz")

    def test_and_set(self):
        self.matches(">=1.2.3 <2.0.0", "1.2.3", "1.9.9")
        self.rejects(">=1.2.3 <2.0.0", "2.0.0", "1.2.2")
        self.rejects(">2.0.0 <1.0.0", "1.5.0", "3.0.0")


class WhitespaceTest(RangeCase):  # rules 9, 10, 11
    def test_space_after_comparison_operator(self):  # rule 11
        self.matches(">= 1.2.3", "1.2.3")
        self.rejects(">= 1.2.3", "1.2.2")
        self.matches("> 1.0.0 < 2.0.0", "1.5.0")
        self.rejects("> 1.0.0 < 2.0.0", "2.0.0", "1.0.0")

    def test_space_after_other_operators(self):  # rule 11
        self.matches("^ 1.2", "1.9.0")
        self.rejects("^ 1.2", "2.0.0")
        self.matches("~ 1.2.3", "1.2.9")
        self.rejects("~ 1.2.3", "1.3.0")
        self.matches("= 1.2.3", "1.2.3")
        self.rejects("= 1.2.3", "1.2.4")

    def test_extra_whitespace(self):  # rule 10
        self.matches("  >=1.0.0    <2.0.0  ", "1.5.0")
        self.matches(">=1.0.0\t<2.0.0", "1.5.0")
        self.rejects(">=1.0.0\t<2.0.0", "2.5.0")

    def test_or_sets(self):  # rule 9
        self.matches("<1.0.0 || >=2.0.0 <3.0.0 || 5.0.0", "0.5.0", "2.5.0", "5.0.0")
        self.rejects("<1.0.0 || >=2.0.0 <3.0.0 || 5.0.0", "1.5.0", "3.0.0", "4.0.0")
        self.matches("1.x||>=3.0.0", "1.4.0", "3.2.0")
        self.rejects("1.x||>=3.0.0", "2.0.0")

    def test_empty_range_is_any(self):  # rule 10
        self.matches("", "0.0.0", "9.9.9")
        self.matches("   ", "1.2.3")

    def test_empty_set_in_or(self):  # rule 10
        self.matches("1.0.0 ||", "3.0.0")
        self.matches("|| 2.x", "7.0.0")

    def test_dangling_operator(self):  # rule 11
        for text in (">=", "1.2.3 <", "^", "1.2.3 || ~"):
            self.invalid_range(text)

    def test_unknown_operators(self):  # rule 11
        for text in ("=>1.2.3", "==1.2.3", "~>1.2", "!1", "<>1.0.0", "> =1.2.3"):
            self.invalid_range(text)


class PatternSyntaxTest(RangeCase):  # rules 12, 14
    def test_wildcard_then_number(self):
        for text in ("1.x.3", "*.2", "x.1.1", "^1.*.0"):
            self.invalid_range(text)

    def test_prerelease_on_partial(self):
        for text in ("1.2-beta", "1.x-beta", "1.2.x+b", "~1-rc.1", "1.2+build"):
            self.invalid_range(text)

    def test_leading_zeros(self):
        for text in ("01.2", "1.02", "^01.2.3", ">=1.2.03", "1.2.3-01"):
            self.invalid_range(text)

    def test_wildcard_spellings(self):
        self.matches("1.X", "1.5.5")
        self.matches("1.2.*", "1.2.7")
        self.matches("1.*.*", "1.0.0")
        self.rejects("1.X", "2.0.0")

    def test_malformed(self):  # rule 14
        for text in ("abc", "1.2.3 | 2.0.0", ">=1.2.3.4", "1.2.3.", "1..3", "1.2.3 ||| 2.0.0", "v1.2.3"):
            self.invalid_range(text)

    def test_error_text_exact(self):  # rule 14
        with self.assertRaises(sr.InvalidRangeError) as caught:
            sr.satisfies("1.0.0", "  >=1.0.0 || bogus  ")
        self.assertEqual(caught.exception.text, "  >=1.0.0 || bogus  ")
        self.assertIsInstance(caught.exception, ValueError)


class XRangeTest(RangeCase):  # rules 16, 17
    def test_major_only(self):
        self.matches("1", "1.0.0", "1.9.9")
        self.rejects("1", "2.0.0", "0.9.9")
        self.matches("=1.x", "1.4.0")
        self.rejects("=1.x", "2.0.0")
        self.matches("1.2", "1.2.0", "1.2.99")
        self.rejects("1.2", "1.3.0", "1.1.9")
        self.matches("1.2.x", "1.2.5")

    def test_bare_wildcards(self):
        for text in ("*", "x", "X", "=*", ">=*", "<=*"):
            self.matches(text, "0.0.0", "42.1.7")

    def test_greater_than_partial(self):  # rule 17
        self.matches(">1", "2.0.0")
        self.rejects(">1", "1.9.9")
        self.matches(">1.2", "1.3.0")
        self.rejects(">1.2", "1.2.9")

    def test_greater_or_equal_partial(self):  # rule 17
        self.matches(">=1.2", "1.2.0")
        self.rejects(">=1.2", "1.1.9")
        self.matches(">=1", "1.0.0")

    def test_less_than_partial(self):  # rule 17
        self.matches("<1.2", "1.1.9")
        self.rejects("<1.2", "1.2.0")
        self.rejects("<1", "1.0.0")

    def test_less_or_equal_partial(self):  # rule 17
        self.matches("<=1.2", "1.2.9")
        self.rejects("<=1.2", "1.3.0")
        self.matches("<=1", "1.9.9")
        self.rejects("<=1.x", "2.0.0")

    def test_strict_against_lone_wildcard(self):  # rule 17
        self.rejects(">*", "0.0.0", "5.0.0")
        self.rejects("<*", "0.0.0", "5.0.0")

    def test_upper_bound_excludes_next_prerelease(self):  # rules 16, 22
        self.rejects("1.x", "2.0.0-alpha")
        self.rejects("<=1.2 >=1.3.0-a", "1.3.0-b")


class TildeTest(RangeCase):  # rule 18
    def test_full(self):
        self.matches("~1.2.3", "1.2.3", "1.2.9")
        self.rejects("~1.2.3", "1.3.0", "1.2.2")
        self.matches("~0.2.3", "0.2.5")
        self.rejects("~0.2.3", "0.3.0", "0.2.2")

    def test_major_minor(self):
        self.matches("~1.2", "1.2.0", "1.2.9")
        self.rejects("~1.2", "1.3.0")
        self.matches("~1.2.x", "1.2.4")

    def test_major_only(self):
        self.matches("~1", "1.0.0", "1.9.0")
        self.rejects("~1", "2.0.0")
        self.matches("~1.x", "1.5.0")
        self.matches("~*", "0.0.1", "7.7.7")

    def test_prerelease_operand(self):
        self.matches("~1.2.3-beta.2", "1.2.3-beta.4", "1.2.3", "1.2.8")
        self.rejects("~1.2.3-beta.2", "1.2.3-beta.1", "1.2.4-beta", "1.3.0")


class CaretTest(RangeCase):  # rule 19
    def test_full(self):
        self.matches("^1.2.3", "1.2.3", "1.9.9")
        self.rejects("^1.2.3", "2.0.0", "1.2.2")

    def test_partial_nonzero_major(self):
        self.matches("^1.2", "1.2.0", "1.9.0")
        self.rejects("^1.2", "1.1.9", "2.0.0")
        self.matches("^1.2.x", "1.3.0")
        self.matches("^1", "1.0.0", "1.9.9")
        self.rejects("^1.x", "2.0.0")

    def test_zero_major_only(self):
        self.matches("^0", "0.0.0", "0.9.9")
        self.rejects("^0", "1.0.0")
        self.matches("^0.x", "0.5.0")
        self.matches("^*", "0.0.0", "3.1.4")

    def test_prerelease_operand(self):
        self.matches("^1.2.3-beta.2", "1.2.3-beta.4", "1.5.0")
        self.rejects("^1.2.3-beta.2", "1.2.4-beta", "1.2.3-beta.1", "2.0.0")


class CaretZeroTest(RangeCase):  # rule 20
    def test_zero_major_nonzero_minor(self):
        self.matches("^0.2.3", "0.2.3", "0.2.9")
        self.rejects("^0.2.3", "0.3.0", "0.2.2")

    def test_zero_major_partial_minor(self):
        self.matches("^0.2", "0.2.0")
        self.rejects("^0.2", "0.3.0")
        self.matches("^0.1.x", "0.1.5")
        self.rejects("^0.1.x", "0.2.0")

    def test_zero_zero_partial(self):
        self.matches("^0.0", "0.0.0", "0.0.9")
        self.rejects("^0.0", "0.1.0")
        self.matches("^0.0.x", "0.0.4")
        self.rejects("^0.0.x", "0.1.0")

    def test_zero_zero_patch(self):
        self.matches("^0.0.3", "0.0.3")
        self.rejects("^0.0.3", "0.0.4", "0.0.2")
        self.matches("^0.0.0", "0.0.0")
        self.rejects("^0.0.0", "0.0.1")

    def test_zero_zero_prerelease(self):
        self.matches("^0.0.3-beta", "0.0.3-pr.2", "0.0.3")
        self.rejects("^0.0.3-beta", "0.0.4", "0.0.3-alpha")


class HyphenTest(RangeCase):  # rules 13, 21
    def test_ends_inclusive_and_partial_lower(self):
        self.matches("1.2.3 - 2.3.4", "1.2.3", "2.3.4", "2.0.0")
        self.rejects("1.2.3 - 2.3.4", "1.2.2", "2.3.5")
        self.matches("1.2 - 2.3.4", "1.2.0")
        self.rejects("1.2 - 2.3.4", "1.1.9")

    def test_partial_upper(self):
        self.matches("1.2.3 - 2.3", "2.3.9")
        self.rejects("1.2.3 - 2.3", "2.4.0")
        self.matches("1.2.3 - 2", "2.9.9")
        self.rejects("1.2.3 - 2", "3.0.0")
        self.matches("1.2.3 - 2.x", "2.5.0")

    def test_wildcard_ends(self):
        self.matches("* - 2", "0.0.0", "2.1.0")
        self.rejects("* - 2", "3.0.0")
        self.matches("1.2.3 - *", "99.0.0")
        self.rejects("1.2.3 - *", "1.2.2")

    def test_without_spaces_is_a_version(self):  # rule 13
        self.matches("1.2.3-2.3.4", "1.2.3-2.3.4")
        self.rejects("1.2.3-2.3.4", "2.0.0", "1.2.3")

    def test_prerelease_ends(self):  # rules 21, 22
        self.matches("1.0.0 - 1.2.3-rc.1", "1.2.3-beta", "1.1.0")
        self.rejects("1.0.0 - 1.2.3-rc.1", "1.2.3", "1.2.3-rc.2", "1.1.0-rc.1")
        self.matches("1.0.0-alpha - 1.0.0", "1.0.0-beta")

    def test_in_or_set(self):  # rule 13
        self.matches("1 - 2 || 5.x", "2.5.0", "5.1.0")
        self.rejects("1 - 2 || 5.x", "4.0.0")

    def test_invalid_hyphen_sets(self):  # rule 13
        for text in ("1 -", "- 2", "1 - 2 - 3", ">1 - 2", "1 - <2", "1 - 2 >1.5", "~1 - 2"):
            self.invalid_range(text)


class PrereleaseRuleTest(RangeCase):  # rule 22
    def test_wildcards_exclude_prereleases(self):
        self.rejects("*", "1.0.0-beta")
        self.rejects("", "1.0.0-beta")
        self.rejects("1.x", "1.5.0-beta")

    def test_plain_bound_excludes_prereleases(self):
        self.rejects("<2.0.0", "1.0.0-beta", "2.0.0-beta")
        self.rejects(">=1.0.0", "1.5.0-rc.1")

    def test_same_triple_required(self):
        self.matches(">1.2.3-alpha.3", "1.2.3-alpha.7", "3.4.5")
        self.rejects(">1.2.3-alpha.3", "3.4.5-alpha.9", "1.2.3-alpha.3")

    def test_lower_triple_does_not_admit_higher(self):
        self.matches(">=1.2.3-alpha", "1.2.3-beta")
        self.rejects(">=1.2.3-alpha", "1.2.4-alpha", "1.3.0-alpha")

    def test_other_set_does_not_count(self):
        self.rejects(">=1.0.0 <2.0.0 || =1.5.0-alpha", "1.5.0-beta")
        self.matches(">=1.0.0 <2.0.0 || =1.5.0-alpha", "1.5.0-alpha", "1.6.0")

    def test_any_comparator_in_set_counts(self):
        self.matches(">1.0.0 <=1.2.3-rc.5", "1.2.3-rc.2")
        self.matches("<2.0.0 >=1.0.0 >=1.4.0-0", "1.4.0-beta")
        self.rejects("<2.0.0 >=1.0.0 >=1.4.0-0", "1.5.0-beta")

    def test_exact_prerelease(self):
        self.matches("1.2.3-beta", "1.2.3-beta", "1.2.3-beta+b.1")
        self.rejects("1.2.3-beta", "1.2.3-beta.1", "1.2.3")


class SatisfiesTest(RangeCase):  # rule 23
    def test_invalid_version(self):
        with self.assertRaises(sr.InvalidVersionError) as caught:
            sr.satisfies("1.2", "*")
        self.assertEqual(caught.exception.text, "1.2")

    def test_range_checked_first(self):
        with self.assertRaises(sr.InvalidRangeError) as caught:
            sr.satisfies("not-a-version", ">>1")
        self.assertEqual(caught.exception.text, ">>1")

    def test_invalid_version_even_when_no_set_could_match(self):
        with self.assertRaises(sr.InvalidVersionError):
            sr.satisfies("v1.0.0", ">*")

    def test_returns_bool(self):
        self.assertIs(sr.satisfies("1.2.3", "^1"), True)
        self.assertIs(sr.satisfies("2.2.3", "^1"), False)


class MaxSatisfyingTest(unittest.TestCase):  # rule 24
    def test_highest_by_precedence(self):
        self.assertEqual(sr.max_satisfying(["1.2.3", "1.10.0", "1.9.0", "2.0.0"], "^1"), "1.10.0")

    def test_none_when_nothing_matches(self):
        self.assertIsNone(sr.max_satisfying(["1.0.0", "1.1.0"], ">=2"))
        self.assertIsNone(sr.max_satisfying([], "*"))

    def test_skips_invalid_entries(self):
        self.assertEqual(sr.max_satisfying(["junk", "1.2.3", "1.5", "v1.9.0", "01.0.0"], "1.x"), "1.2.3")

    def test_tie_keeps_first(self):
        self.assertEqual(sr.max_satisfying(["1.2.3+b", "1.0.0", "1.2.3+a", "1.2.3"], "^1"), "1.2.3+b")

    def test_prerelease_rule_applies(self):
        versions = ["1.2.3", "1.3.0-beta", "1.2.9-rc.1"]
        self.assertEqual(sr.max_satisfying(versions, "^1.2"), "1.2.3")
        self.assertEqual(sr.max_satisfying(versions, ">=1.3.0-alpha"), "1.3.0-beta")

    def test_generator(self):
        self.assertEqual(sr.max_satisfying((v for v in ["0.1.0", "0.1.7", "0.2.0"]), "^0.1"), "0.1.7")

    def test_invalid_range_with_empty_versions(self):
        with self.assertRaises(sr.InvalidRangeError) as caught:
            sr.max_satisfying([], "1.x.3")
        self.assertEqual(caught.exception.text, "1.x.3")

    def test_returns_original_string(self):
        self.assertEqual(sr.max_satisfying(["2.0.0-rc.1+sha.5", "1.0.0"], "2.0.0-rc.1"), "2.0.0-rc.1+sha.5")


if __name__ == "__main__":
    unittest.main()
