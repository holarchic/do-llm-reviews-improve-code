# Traps (private)

| Trap | TASK.md clause | Hidden tests |
|---|---|---|
| Split at the first `=`, not the last | 3 | `test_split_on_first_equals` |
| `include = x` is an assignment | 5 | `test_include_named_key_is_assignment` |
| Includes resolve against the including file's directory, not the cwd | 5 | `test_relative_to_including_file_not_cwd` |
| Diamond includes are allowed and processed twice | 7 | `test_diamond_is_not_a_cycle` |
| Cycle error points at the include that closes the cycle | 7 | `test_self_include_is_cycle`, `test_indirect_cycle_reports_closing_include` |
| Interpolation binds at assignment time | 9 | `test_later_change_does_not_rewrite`, `test_self_reference_uses_previous_value` |
| `$$` escapes, stray `$` kept | 11 | `test_dollar_escapes`, `test_escaped_brace_not_interpolated` |

Other assertion groups: basic assignments, str/PathLike (Interface, 3); comments (2); empty value (3); keys (4); error path/line for invalid key and unparseable line (4); override order (6); include in place, absolute include (5, 6); missing include (8); interpolation, across include, unknown and forward names (9, 10).

Follow-up: missing optional include skipped; present one processed; relative resolution; cycle and syntax errors inside still raise (FOLLOWUP).

## Degraded reference (judge calibration)

`degraded/` passes every hidden test. Injected defects: one nested closure doing all parsing, include and interpolation work; cryptic one-letter names and a hand-rolled character loop in place of a regex substitution; commented-out code.
