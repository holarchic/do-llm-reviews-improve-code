# Task: npm-style semantic version ranges

Write a Python module `semver_range.py` at the repository root (standard library only). Tests import it as `import semver_range`.

## Interface

- `Version`: a frozen dataclass (`@dataclass(frozen=True)`) with these fields, in this order:
  - `major: int`
  - `minor: int`
  - `patch: int`
  - `prerelease: tuple[int | str, ...]`
  - `build: tuple[str, ...]`
- `parse(text: str) -> Version`
- `compare(a: str, b: str) -> int`: returns `-1`, `0` or `1`.
- `satisfies(version: str, range_text: str) -> bool`
- `max_satisfying(versions: Iterable[str], range_text: str) -> str | None`
- `class InvalidVersionError(ValueError)` with attribute `text: str`.
- `class InvalidRangeError(ValueError)` with attribute `text: str`.

Every version and range argument is a `str`, and `versions` is an iterable of `str`. Behaviour for other types is not specified.

## Behaviour

### Versions

1. A version is `MAJOR.MINOR.PATCH`, optionally followed by `-PRERELEASE`, optionally followed by `+BUILD`, in that order.
   - MAJOR, MINOR and PATCH are non-empty strings of ASCII digits `0`–`9` with no leading zero: `0` is fine, `01` is not.
   - Nothing else is allowed: no surrounding whitespace, no `v` or `=` prefix, exactly three numeric parts.
   - Valid: `0.0.0`, `1.2.3`, `10.20.30`, `1.2.3-rc.1+build.5`.
   - Invalid: `1.2`, `1.2.3.4`, `v1.2.3`, `=1.2.3`, ` 1.2.3`, `01.2.3`, `1.2.x`, `1.2.3-`, `1.2.3+`.
2. PRERELEASE and BUILD are each one or more identifiers separated by `.`. An identifier is a non-empty string of ASCII letters, ASCII digits and `-`. So `1.0.0-a..b` and `1.0.0+a.` are invalid (empty identifier), and `1.0.0-al_pha` is invalid (`_`).
3. A prerelease identifier made only of digits is *numeric* and must not have a leading zero. Any other prerelease identifier is *alphanumeric* and may start with `0`.
   - Valid: `1.0.0-0`, `1.0.0-alpha.10`, `1.0.0-0a`.
   - Invalid: `1.0.0-01`, `1.0.0-alpha.007`.
   - Build identifiers may have leading zeros: `1.0.0+001` is valid.
4. The first `-` after PATCH starts PRERELEASE and the first `+` starts BUILD. Any further `-` belongs to an identifier.
   - `1.0.0--x-1` has the single prerelease identifier `-x-1`.
   - `1.0.0-a+b-c` has prerelease `("a",)` and build `("b-c",)`.
5. `parse` returns a `Version`.
   - `prerelease` holds numeric identifiers as `int` and alphanumeric identifiers as `str`, in order.
   - `build` holds every build identifier as its original `str`.
   - A part that is absent is an empty tuple.
   - Numeric parts have no upper limit.
   - Example: `parse("1.2.3-beta.11.0a+exp.007")` returns `Version(1, 2, 3, ("beta", 11, "0a"), ("exp", "007"))`.
6. An invalid version raises `InvalidVersionError` whose `text` is the exact string that was passed.

### Precedence

7. Versions are ordered by precedence:
   - Compare MAJOR, then MINOR, then PATCH, numerically: `1.10.0` is greater than `1.9.0`.
   - If those are equal, a version without a prerelease is greater than one with a prerelease: `1.0.0` is greater than `1.0.0-rc.1`.
   - Otherwise compare prerelease identifiers left to right until two differ:
     - two numeric identifiers compare numerically (`beta.2` < `beta.11`);
     - two alphanumeric identifiers compare character by character in ASCII order, so `Beta` < `alpha` because uppercase letters sort before lowercase; if one is a prefix of the other, the shorter is lower (`a` < `a-b`);
     - a numeric identifier is always lower than an alphanumeric one.
   - If every identifier of the shorter prerelease equals the corresponding identifier of the longer one, the shorter prerelease is lower: `1.0.0-alpha` < `1.0.0-alpha.0`.
   - Build metadata is ignored: `1.0.0+a` and `1.0.0+b` have equal precedence.
   - Example, ascending: `1.0.0-alpha` < `1.0.0-alpha.1` < `1.0.0-alpha.beta` < `1.0.0-beta` < `1.0.0-beta.2` < `1.0.0-beta.11` < `1.0.0-rc.1` < `1.0.0`.
8. `compare(a, b)` returns `-1` if `a` has lower precedence than `b`, `0` if they are equal, and `1` if `a` is greater. It validates `a` before `b`: if both are invalid, the error's `text` is `a`.

### Range syntax

9. A range is one or more *sets* separated by `||`. Whitespace around `||` is optional: `1.x||2.x` is valid. A version satisfies a range when it satisfies at least one of its sets.
10. A set is a sequence of comparators separated by whitespace (one or more spaces or tabs). Leading and trailing whitespace is ignored.
    - A version satisfies a set when it satisfies every comparator in it and also passes rule 22.
    - A set that is empty or only whitespace means the same as `*`. So the range `""` is one empty set, and `"1.0.0 ||"` ends with an empty set.
11. A comparator is an optional operator, one of `<`, `<=`, `>`, `>=`, `=`, `~`, `^`, followed by a version pattern.
    - Whitespace may separate the operator from its pattern: `>= 1.2.3` is a single comparator equal to `>=1.2.3`, and `^ 1.2` equals `^1.2`.
    - An operator with no pattern after it makes the range invalid: `>=`, `1.2.3 <`.
    - Any other operator makes the range invalid: `=>1.2.3`, `==1.2.3`, `~>1.2`, `!1`.
12. A version pattern is either a full version or a partial version.
    - A full version follows rules 1–4. Its build metadata is ignored for matching.
    - A partial version is `*`, `x` or `X` alone, or one to three `.`-separated parts. Each part is a number (ASCII digits, no leading zero) or a wildcard: `x`, `X` or `*`.
    - Missing parts count as wildcards: `1.2` means `1.2.x`.
    - Once a part is a wildcard, every later part must be a wildcard too: `1.x.3` and `*.2` are invalid.
    - Only a pattern whose three parts are all numbers may carry a prerelease or build: `1.2-beta`, `1.x-beta` and `1.2.x+b` are invalid.
    - `01.2` and `1.02` are invalid.
13. A set may instead be a *hyphen range*: exactly a pattern, then `-` with whitespace on both sides, then a pattern, as in `1.2.3 - 2.3.4`.
    - Neither end may have an operator.
    - A set with a standalone `-` anywhere else is invalid: `1 -`, `- 2`, `1 - 2 - 3`, `>1 - 2`, `1 - 2 >1.5`.
    - Without whitespace the hyphen is part of a version: `1.2.3-2.3.4` is the full version `1.2.3` with prerelease `2.3.4`, not a hyphen range.
14. Range text not described by rules 9–13 is invalid, for example `abc`, `1.2.3 | 2.0.0` and `>=1.2.3.4`. An invalid range raises `InvalidRangeError` whose `text` is the exact range string that was passed, including any surrounding whitespace.

### Range meaning

In the expansions below, `X.Y.Z-0` is the lowest possible version with that MAJOR.MINOR.PATCH: it sorts below every other prerelease of `X.Y.Z`. So `<2.0.0-0` excludes `2.0.0` and every prerelease of `2.0.0`. "Any" means the comparator places no bound; rule 22 still applies.

15. With a full version `V`, the operators `<`, `<=`, `>` and `>=` compare precedence. `=V` and a bare `V` both mean equal precedence, so `=1.2.3` is matched by `1.2.3+build`.
16. X-ranges are partial patterns with no operator or with `=`:
    - `*`, `x`, `X` → any;
    - `1`, `1.x`, `1.x.x` → `>=1.0.0 <2.0.0-0`;
    - `1.2`, `1.2.x` → `>=1.2.0 <1.3.0-0`.
17. X-ranges with a comparison operator, shown for `1` and `1.2`. Wildcards in place of missing parts give the same result.

    | Operator | `1` | `1.2` |
    |---|---|---|
    | `>` | `>=2.0.0` | `>=1.3.0` |
    | `>=` | `>=1.0.0` | `>=1.2.0` |
    | `<` | `<1.0.0-0` | `<1.2.0-0` |
    | `<=` | `<2.0.0-0` | `<1.3.0-0` |

    With a lone wildcard: `>*` and `<*` match no version; `>=*`, `<=*` and `=*` are any.
18. Tilde allows changes below the minor part when MINOR is given, and below the major part otherwise. The lower bound fills missing parts with `0` and keeps a prerelease written on the pattern.
    - `~1.2.3` → `>=1.2.3 <1.3.0-0`
    - `~1.2`, `~1.2.x` → `>=1.2.0 <1.3.0-0`
    - `~1`, `~1.x` → `>=1.0.0 <2.0.0-0`
    - `~0.2.3` → `>=0.2.3 <0.3.0-0`
    - `~1.2.3-beta.2` → `>=1.2.3-beta.2 <1.3.0-0`
    - `~*` → any
19. Caret allows changes that do not modify the leftmost non-zero part of MAJOR.MINOR.PATCH. The lower bound is as for tilde. When MAJOR is non-zero, or MAJOR is the only number given, the upper bound is the next major:
    - `^1.2.3` → `>=1.2.3 <2.0.0-0`
    - `^1.2`, `^1.2.x` → `>=1.2.0 <2.0.0-0`
    - `^1`, `^1.x` → `>=1.0.0 <2.0.0-0`
    - `^0`, `^0.x` → `>=0.0.0 <1.0.0-0`
    - `^1.2.3-beta.2` → `>=1.2.3-beta.2 <2.0.0-0`
    - `^*` → any
20. Caret with MAJOR `0` and MINOR given:
    - MINOR non-zero → next minor: `^0.2.3` → `>=0.2.3 <0.3.0-0`; `^0.2`, `^0.2.x` → `>=0.2.0 <0.3.0-0`.
    - MINOR `0`, PATCH missing or a wildcard → next minor: `^0.0`, `^0.0.x` → `>=0.0.0 <0.1.0-0`.
    - MINOR `0`, PATCH a number → next patch: `^0.0.3` → `>=0.0.3 <0.0.4-0`; `^0.0.0` → `>=0.0.0 <0.0.1-0`; `^0.0.3-beta` → `>=0.0.3-beta <0.0.4-0`.
21. A hyphen range `A - B` means a lower bound from `A` and an upper bound from `B`:
    - Lower: `>=A`, with missing parts of `A` filled with `0`: `1.2 - 2.3.4` → `>=1.2.0 <=2.3.4`. If `A` is a lone wildcard, there is no lower bound.
    - Upper, when `B` is a full version: `<=B`. Example: `1.2.3 - 2.3.4` → `>=1.2.3 <=2.3.4`.
    - Upper, when `B` is partial: the same bound as `<=B` in rule 17. Examples: `1.2.3 - 2.3` → `>=1.2.3 <2.4.0-0`; `1.2.3 - 2` → `>=1.2.3 <3.0.0-0`.
    - If `B` is a lone wildcard, there is no upper bound.
22. A version with a prerelease satisfies a set only if, besides passing every comparator of the set, the set contains a pattern written as a full version with a prerelease and the same MAJOR.MINOR.PATCH as the version.
    - Such patterns include the operands of `~` and `^` and both ends of a hyphen range.
    - The `-0` bounds produced by the expansions above don't count, and neither do patterns in other sets of the range.
    - A version without a prerelease is unaffected by this rule.
    - Examples:
      - `1.0.0-beta` satisfies none of `*`, `""` or `<2.0.0`.
      - For `>1.2.3-alpha.3`: `1.2.3-alpha.7` satisfies it; `3.4.5-alpha.9` does not (different MAJOR.MINOR.PATCH), although it is greater; `3.4.5` satisfies it.
      - For `>=1.0.0 <2.0.0 || =1.5.0-alpha`: `1.5.0-beta` does not satisfy it. The first set has no prerelease pattern and the second set doesn't match.

### Functions

23. `satisfies(version, range_text)` returns `True` or `False`.
    - It validates the range first, then the version.
    - An invalid range raises `InvalidRangeError`, even if the version is also invalid.
    - Otherwise an invalid version raises `InvalidVersionError`.
24. `max_satisfying(versions, range_text)` returns the string from `versions`, unchanged, that satisfies the range and has the highest precedence. It returns `None` if no string satisfies the range.
    - Strings in `versions` that are not valid versions are skipped without error.
    - If several satisfying strings share the highest precedence (they differ only in build metadata), the one that comes first in `versions` is returned.
    - The range is validated first, so an invalid range raises `InvalidRangeError` even when `versions` is empty.
    - `versions` may be any iterable, including a generator.

Include your own tests.
