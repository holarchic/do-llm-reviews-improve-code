# Task: gitignore-style path matching

Write a Python module `ignore_match.py` at the repository root (standard library only). Tests import it as `import ignore_match`.

The module decides whether a relative path is ignored by an ordered list of ignore-pattern lines, in the style of a `.gitignore` file. The rules below are the complete specification; where they differ from git, follow the rules.

## Interface

- `class IgnoreRules` with:
  - `__init__(self, lines: Iterable[str])` — `lines` is the pattern file, one string per line, in order.
  - `is_ignored(self, path: str, is_dir: bool = False) -> bool`
  - `explain(self, path: str, is_dir: bool = False) -> Decision | None`
- `Decision` — an immutable value (for example a frozen dataclass) with attributes:
  - `line_number: int` — the 1-based line number of the deciding pattern (rule 1);
  - `negated: bool` — whether the deciding pattern starts with `!`;
  - `path: str` — the path the deciding pattern matched: either the path asked about or one of its ancestor directories, written without a trailing `/`.
- `class PatternError(ValueError)` with attributes `line_number: int` (rule 1) and `line: str` (the line exactly as it appeared in `lines`, unmodified).

`path` arguments are relative paths whose components are separated by `/`, for example `src/app/main.py`.

In this document, pattern lines and names shown in double quotes are Python string literals: `"\\*"` is the two characters backslash and asterisk, `"\t"` is one tab character, and `"\n"` is one newline character.

## Behaviour

### Reading pattern lines

1. Lines are numbered from 1 in the order `lines` yields them. Every line counts, including blank lines and comments. The constructor iterates `lines` exactly once, so a generator works; changing the original list after construction has no effect on the rules.
2. Before anything else, one trailing `"\n"` is removed from a line, then one trailing `"\r"` (so `"\r\n"` endings are removed too).
3. A line whose first character is `#` is a comment and is skipped. A `#` anywhere else is an ordinary character. Leading spaces are not removed: the line `"  foo"` is a pattern for the name `"  foo"`, not `"foo"`.
4. A backslash escapes the character after it (see rule 12). Trailing spaces (U+0020 only) are removed from the end of the line, except a space that is escaped by a backslash. Example: `"foo   "` is the pattern `foo`; `"foo\\ "` and `"foo\\  "` both match the name `"foo "` (with one trailing space) and not `"foo"`. Tab characters are never removed: `"foo\t"` matches only the name `"foo\t"`.
5. A line that is empty after rule 4 is blank and is skipped. So a line of only spaces is blank.
6. A line starting with `!` is a negated pattern; the `!` is removed and the rest is the pattern. After the `!` is removed, a leading `#` is an ordinary character: `"!#x"` is a negated pattern for the name `#x`, not a comment. To start a non-negated pattern with a literal `!` or `#`, escape it: `"\\!x"` matches the name `!x`, `"\\#x"` matches the name `#x`.
7. If the pattern (after rule 6) ends with `/`, it is a directory-only pattern and that one `/` is removed.
8. A line whose pattern is empty after rules 6 and 7 is skipped and is not an error. Examples: `"!"`, `"/"`, `"!/"`.
9. Anchoring is decided after rule 7. If the pattern still contains a `/` anywhere (at the start or in the middle), it is anchored: it is matched against the path from the root, and one leading `/`, if present, is removed. Otherwise the pattern is unanchored. Example: `"build/"` is unanchored (its only slash was removed by rule 7); `"src/build/"` and `"/build"` are anchored.
10. The constructor raises `PatternError` for the first invalid line, in line order. A pattern is invalid if it contains:
    - a `[` that has no closing `]` (see rule 14 for where a bracket expression ends; `"[]"` and `"[!]"` are unclosed);
    - a `/` inside a bracket expression, for example `"a[/]b"`;
    - a range whose end character is smaller than its start character (by code point), for example `"[z-a]"`; a range whose start and end are equal, such as `"[a-a]"`, is valid;
    - a backslash that is the last character of the pattern, with nothing to escape, for example `"foo\\"`.

    Comment lines and blank lines are never invalid.

### Matching one pattern against one path

11. Matching is done per path component. A pattern's text is split at `/` into segments. `*` matches any sequence of characters within one component, including the empty sequence; `?` matches exactly one character. Neither ever matches `/`, so neither can reach into another component. `*` also matches names that start with `.` (`"*"` matches `".env"`). Every other character matches only itself, case-sensitively; characters such as `.`, `+`, `(`, `)`, `$`, `^`, `{`, `|` have no special meaning (`"a.b"` does not match `"axb"`).
12. A backslash followed by any character matches that character literally, for example `"\\*"` matches only the name `*`, `"\\?"` only `?`, `"\\["` only `[`, `"\\\\"` only a name that is a single backslash, and `"\\a"` only `a`.
13. A bracket expression `[...]` matches exactly one character from its set. The set lists single characters and ranges `x-y` (every character whose code point is from `x` to `y`, both included). If the first character after `[` is `!`, the expression matches any one character *not* in the set. `^` has no special meaning anywhere in a bracket expression: `"[^a]"` matches `^` or `a`.
14. Inside a bracket expression: a `]` that comes immediately after the opening `[` (or after `[!`) is a literal member, so `"[]a]"` matches `]` or `a`, and `"[!]]"` matches any one character except `]`; any later `]` ends the expression. A `-` that is first or last in the set is a literal member (`"[-a]"` and `"[a-]"` both match `-` or `a`). A backslash escapes the next character, which becomes a literal member (`"[\\]]"` matches `]`).
15. An unanchored pattern matches a path when it matches the path's last component. Example: `"*.txt"` matches `a.txt` and `docs/a.txt`.
16. An anchored pattern matches a path when its segments match the path's components from the root, one segment per component, with the same number of each, except for `**` segments (rule 17). Example: `"a/b"` matches `a/b` but neither `x/a/b` nor `a/b/c`, and `"a/*.txt"` does not match `a/b/c.txt`.
17. A segment that is exactly `**` (two unescaped asterisks and nothing else) is special:
    - as the first segment, followed by more segments (`"**/foo"`), it matches zero or more leading components, so `"**/foo"` matches `foo`, `a/foo` and `a/b/foo`;
    - in the middle (`"a/**/b"`), it matches zero or more components, so it matches `a/b`, `a/x/b` and `a/x/y/b`;
    - as the last segment (`"abc/**"`), it matches one or more components, so it matches `abc/x` and `abc/x/y` but not `abc` itself.

    Asterisks in any other position are ordinary `*` wildcards: `"a**b"` behaves like `"a*b"`, and `"foo/**bar"` behaves like `"foo/*bar"`. A pattern that is only `"**"` matches every path.
18. A directory-only pattern matches a path only when that path is a directory. A pattern without a trailing `/` matches files and directories alike.

### Deciding a path

19. `path` must be a non-empty relative path. If it ends with one `/`, that `/` is removed and the path is a directory, whatever `is_dir` says. After that, `is_ignored` and `explain` raise `ValueError` if the path is empty, starts with `/`, or has an empty component (`"a//b"`) or a component that is `.` or `..`.
20. For a single path on its own, the last pattern (in line order) that matches it decides: if that pattern is negated, the path is not ignored; otherwise it is ignored. A path that no pattern matches is not ignored. When matching the path itself, it is a directory if `is_dir` is true (or rule 19 made it one).
21. A path is also ignored if any of its ancestor directories is ignored, whatever the patterns say about the path itself: once a directory is excluded, nothing inside it can be re-included by a negated pattern. The ancestors of `a/b/c` are `a` and `a/b`. Each ancestor is decided as a single directory by rule 20 (with `is_dir` true). Example: with `["build/", "!build/keep.txt"]`, `build/keep.txt` is ignored.
22. `explain(path, is_dir)` returns the `Decision` that settles the path:
    - Check the ancestors shortest first (`a`, then `a/b`). For the first ancestor that rule 20 ignores, return the `Decision` of that ancestor's deciding pattern, with `negated` `False` and `path` that ancestor.
    - Otherwise, if any pattern matches the path itself, return the `Decision` of the last such pattern, with `path` the path itself (without a trailing `/`). `negated` may be `True` here.
    - Otherwise return `None`.

    An ancestor that is matched only by a negated pattern, or not at all, does not stop the check and is never reported.
23. `is_ignored(path, is_dir)` returns `True` exactly when `explain(path, is_dir)` returns a `Decision` whose `negated` is `False`.

Include your own tests.
