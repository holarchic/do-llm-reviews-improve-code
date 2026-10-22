# Traps (private)

| Trap | TASK.md rule | Hidden tests | Why a hurried build gets it wrong |
|---|---|---|---|
| Leading spaces kept | 3 | `test_leading_spaces_kept` | `line.strip()` is the reflex. |
| CRLF removed before trailing-space trimming | 2, 4 | `test_crlf_removed`, `test_crlf_removed_before_trailing_spaces`, `test_line_numbers_count_blank_and_comment_lines` | Only `"\n"` stripped, or `rstrip()` used, which also eats escaped spaces and tabs. |
| Escaped trailing space kept, tab kept | 4 | `test_escaped_trailing_space_kept`, `test_unescaped_space_after_escaped_space_removed`, `test_tab_not_removed`, `test_valid_lines_not_errors` | `rstrip()` removes the escaped space and the tab; trimming without escape tracking turns `"foo\\ "` into a trailing-backslash error. |
| `!` then `#`; escaped `!` | 6 | `test_bang_then_hash_is_not_comment`, `test_negated_escaped_bang`, `test_escaped_bang_is_literal` | Comment check done after `!` removal; only one leading backslash handled. |
| Bare `!`, `/`, `!/` skipped | 8 | `test_bare_markers_skipped` | Empty pattern compiled as match-everything or raised. |
| Anchoring after trailing-slash removal | 9 | `test_trailing_slash_removed_before_anchoring`, `test_middle_slash_with_trailing_slash_anchors`, `test_unanchored_at_any_depth` | `"/" in pattern` checked on the raw text, so `build/` becomes anchored. |
| PatternError cases and attributes | 1, 10 | all of `PatternErrorTest` | fnmatch/regex translation treats an unclosed `[` as literal; `[]`, `[!]`, `[/]`, reversed range and trailing backslash not detected; `line_number` counted over patterns only; `line` stored stripped. |
| `*`/`?` never cross `/` | 11, 16 | `test_star_does_not_cross_slash`, `test_question_mark_does_not_match_slash` | `fnmatch` on the whole path lets `*` and `?` match `/`. |
| Regex metacharacters literal | 11 | `test_regex_characters_literal` | Hand-rolled regex without `re.escape`. |
| Backslash escapes | 12 | all of `EscapeTest` | fnmatch has no escapes; `\*\*` treated as `**`. |
| Bracket edge cases | 13, 14 | `test_caret_is_literal`, `test_leading_close_bracket_is_member`, `test_negated_leading_close_bracket`, `test_dash_first_or_last_is_member`, `test_escaped_close_bracket_member`, `test_single_character_range` | Brackets passed straight to `re` (where `^` negates, `[]a]` and `[\]]` differ); `]`-after-`[!` rule forgotten; `<=` on range bounds. |
| `**` positions | 17 | all of `DoubleStarTest` | Trailing `/**` allowed to match zero components (matches `abc` itself); middle `/**/` requires at least one; `a**b` or `foo/**bar` treated as recursive; consecutive `**` segments. |
| Directory-only | 7, 18 | `DirectoryOnlyTest`, `test_trailing_directory_only`, `test_directory_only_negation` | `is_dir` ignored, or ancestors not treated as directories. |
| Parent exclusion | 21 | all of `AncestorTest` | The common "match the path or any prefix, last pattern wins" approach lets a negation re-include a file under an excluded directory; ancestors checked with `is_dir=False`. |
| `explain` reporting | 20, 22 | `ExplainTest` (except `test_no_match`), `test_trailing_reports_first_ignored_ancestor`, `test_trailing_matches_contents`, `test_anchored_does_not_match_longer_path_itself` | Reports the path instead of the ancestor; longest ancestor first; reports a negated ancestor; first rather than last matching line of the ancestor; trailing `/` kept in `path`. |
| Path validation | 19 | `PathArgumentTest` | Paths not validated, or a trailing `/` not turned into a directory. |

Other assertion groups: generator and list mutation (1); trailing spaces and blank lines (4, 5); last match wins (20); unanchored and anchored basics (15, 16); `*` matches dotfiles and empty, case sensitivity (11); `is_ignored`/`explain` agreement (23).

## Wrong implementations

- `fnmatch-whole-path`: `strip()`s lines, uses `fnmatch` for every pattern (anchored ones on the whole path), approximates `**` by string rewriting, detects only bracket-count imbalance. Ancestor rule and `explain` are right.
- `prefix-last-match`: the reference's matcher, but each pattern is tried against the path and every prefix, with the last pattern deciding (so negations re-include under an excluded directory, and `explain` reports a different path/line), and lines are `rstrip()`ped.

## Mutants

| Mutant | Rule |
|---|---|
| `carriage-return-kept` | 2 |
| `line-numbers-skip-comments` | 1 |
| `leading-spaces-stripped` | 3 |
| `escaped-trailing-space-trimmed` | 4 |
| `anchored-before-dir-slash-removed` | 9 |
| `single-char-range-rejected` | 10 |
| `caret-negates-bracket` | 13 |
| `close-bracket-after-bang-ends-class` | 14 |
| `trailing-doublestar-matches-zero` | 17 |
| `dir-only-matches-files` | 18 |
| `ancestor-checked-as-file` | 21 |
| `ancestors-longest-first` | 22 |
