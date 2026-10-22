# Traps (private)

| Hidden test group | TASK.md rule | Why a hurried implementation gets it wrong |
|---|---|---|
| `ParseTest.test_prerelease_types`, `test_build_stays_strings`, `test_huge_numbers` | 5 | Keeps every prerelease identifier as `str`, or converts build identifiers to `int` and loses `007`. |
| `VersionSyntaxTest.test_prefixes_whitespace_and_letters` | 1 | `re.match(... "$")` accepts `"1.2.3\n"`; a lenient parser strips `v`/`=` or whitespace the way npm's loose mode does. |
| `VersionSyntaxTest.test_non_ascii_digits` | 1, 2 | `\d` and `str.isdigit()` accept Unicode digits such as `١`. |
| `VersionSyntaxTest.test_empty_identifiers`, `test_bad_identifier_characters` | 2 | `split(".")` without a non-empty check; `\w` lets `_` and `é` through. |
| `VersionSyntaxTest.test_numeric_prerelease_leading_zero`, `test_alphanumeric_may_start_with_zero`, `test_build_may_have_leading_zero` | 3 | Applies the leading-zero ban to every identifier (rejects `0a`, `+001`), or to none (accepts `-01`). |
| `VersionSyntaxTest.test_hyphens_inside_identifiers` | 4 | Splits on every `-`, or on the last one, instead of the first after PATCH. |
| `CompareTest.test_numeric_identifiers_compare_numerically`, `test_numeric_below_alphanumeric`, `test_spec_chain` | 7 | Compares identifiers as strings (`11` < `2`) or ranks numeric above alphanumeric. |
| `CompareTest.test_ascii_order_of_alphanumerics`, `test_shorter_prerelease_lower` | 7 | Case-insensitive compare; treats a longer prerelease as lower. |
| `CompareTest.test_unit_values_and_antisymmetry`, `test_invalid_operands` | 8 | Returns the raw difference (`-4`), or validates `b` first. |
| `PrimitiveComparatorTest.*` | 15 | `>` written as `>=`; build metadata on the range operand compared. |
| `WhitespaceTest.test_space_after_comparison_operator`, `test_space_after_other_operators` | 11 | Splitting a set on whitespace leaves `>=` as a separate token with no version. |
| `WhitespaceTest.test_empty_range_is_any`, `test_empty_set_in_or` | 10 | Filters out empty sets after splitting on `||`, so `""` matches nothing. |
| `WhitespaceTest.test_dangling_operator`, `test_unknown_operators` | 11 | Accepts npm's `~>`, or reads `=>`/`==` as `>=`/`=`. |
| `PatternSyntaxTest.test_wildcard_then_number`, `test_prerelease_on_partial`, `test_leading_zeros` | 12 | A loose partial regex accepts `1.x.3`, `1.2-beta` or `01.2`. |
| `PatternSyntaxTest.test_malformed`, `test_error_text_exact` | 14 | A single `\|` is ignored; the error carries the stripped or per-set text instead of the original string. |
| `XRangeTest.test_greater_than_partial` | 17 | Reads `>1.2` as `>1.2.0`, which admits `1.2.9`. |
| `XRangeTest.test_less_than_partial`, `test_less_or_equal_partial`, `test_strict_against_lone_wildcard` | 17 | Fills missing parts with zeros for every operator (`<=1.2` → `<=1.2.0`); treats `>*` as any. |
| `XRangeTest.test_upper_bound_excludes_next_prerelease` | 16, 17, 22 | Exclusive upper bounds written as `<X.Y.Z` instead of `<X.Y.Z-0`. |
| `TildeTest.*` | 18 | `~1` bumps the minor; the patch is used when computing the upper bound. |
| `CaretTest.*` | 19 | `^0`/`^0.x` handled by the zero-major branch and given a minor bound. |
| `CaretZeroTest.*` | 20 | No `0.0.x` case: `^0.0.3` allows `0.0.4`; or `^0.0` / `^0.0.x` treated as a patch-level bound. |
| `HyphenTest.test_partial_upper`, `test_wildcard_ends` | 21 | A partial upper end filled with zeros and used inclusively (`- 2.3` → `<=2.3.0`). |
| `HyphenTest.test_without_spaces_is_a_version`, `test_invalid_hyphen_sets`, `test_in_or_set` | 13 | Splits on any `-`; accepts operators on hyphen ends or extra comparators beside a hyphen range. |
| `PrereleaseRuleTest.*`, `HyphenTest.test_prerelease_ends`, `TildeTest/CaretTest/CaretZeroTest` prerelease tests | 22 | Omits the npm prerelease rule, applies it across the whole range rather than per set, or lets any prerelease comparator admit any prerelease version regardless of MAJOR.MINOR.PATCH. |
| `SatisfiesTest.*` | 23 | Parses the version before the range. |
| `MaxSatisfyingTest.test_skips_invalid_entries`, `test_tie_keeps_first`, `test_invalid_range_with_empty_versions`, `test_returns_original_string` | 24 | Lets invalid entries raise; `>=` in the running max keeps the last tie; parses the range lazily inside the loop; returns a normalised string. |

## Wrong implementations

| Variant | Pass | What it skips |
|---|---|---|
| `naive-tokenizer` | 0.871 | Rule 11 operator whitespace, rule 1 trailing newline, rule 21 partial upper end, rule 22 triple match. |
| `plain-intervals` | 0.860 | Rule 22 entirely, the `-0` upper bounds, and the rule 20 `0.0.x` case. |
| `string-prerelease` | 0.914 | Rules 3, 5 and 7 for numeric identifiers, and rule 24 skipping of invalid entries. |

## Mutants (one line each)

| Mutant | Rule | Pass |
|---|---|---|
| `numeric-prerelease-leading-zero-allowed` | 3 | 0.978 |
| `numeric-sorts-above-alphanumeric` | 7 | 0.957 |
| `empty-set-matches-nothing` | 10 | 0.978 |
| `operator-space-not-joined` | 11 | 0.978 |
| `wildcard-then-number-allowed-in-minor` | 12 | 0.989 |
| `greater-than-inclusive` | 15 | 0.957 |
| `xrange-greater-than-not-bumped` | 17 | 0.989 |
| `tilde-major-only-bumps-minor` | 18 | 0.989 |
| `caret-zero-zero-patch-bumps-minor` | 20 | 0.978 |
| `hyphen-partial-upper-inclusive` | 21 | 0.968 |
| `prerelease-rule-ignores-triple` | 22 | 0.935 |
| `max-satisfying-tie-keeps-last` | 24 | 0.989 |
