# Traps (private)

| Trap | TASK.md rule | Hidden tests | Why a hurried build misses it |
|---|---|---|---|
| Whitespace is exactly four characters | 1 | `WhitespaceTest.test_non_breaking_space_invalid`, `test_vertical_tab_invalid`, `test_form_feed_invalid` | `str.isspace()` and regex `\s` accept ` `, `\x0b`, `\x0c`. |
| ASCII-only digits and letters | 2, 6 | `InvalidCharacterTest.*` | `isdigit()`/`isalpha()`/`isalnum()` and regex `\d`/`\w` accept `²`, `２`, `é`. |
| Maximal munch; whitespace splits operators | 3 | `OperatorTokenTest.*` | Stripping whitespace before lexing turns `* *` into `**`. |
| Literal edges: `1.`, `.5`, `1.e2`, `e` without digits, `1.2.3`, lone `.`, `_` | 5 | `LiteralTest.test_trailing_dot` … `test_underscore_not_in_literal` | Greedy `[0-9.]+` scans take `1.2.3` as one token; `2e` is swallowed or crashes `float()`; `1e3` returned as `int`. |
| `**` above unary on its left, unary allowed on its right; unary above `* / // %` | 7 | `PrecedenceTest.test_unary_minus_below_power`, `test_power_exponent_takes_unary`, `test_power_exponent_unary_then_power`, `test_unary_above_floor_division_and_modulo`; `DomainTest.test_unary_minus_outside_power_is_fine` | The textbook "unary binds tightest" gives `-2 ** 2 == 4`; putting unary at the expression level gives `-7 // 2 == -3`. |
| No implicit multiplication | 7, 21 | `ImplicitMultiplicationTest.*` | Usually right; cheap check. |
| Division-by-zero coverage and column | 12 | `DivisionByZeroTest.*` | `%` and `0 ** -1` forgotten (raw Python `ZeroDivisionError` escapes); column of the left operand or `None`. |
| Domain and overflow of `**` | 13, 14 | `DomainTest.*`, `OverflowTest.*` | Python returns a `complex` for `(-8) ** 0.5` and raises bare `OverflowError` for `10.0 ** 400`; `-8 ** 0.5` must not raise. |
| Variable and function namespaces | 15, 16 | `VariableTest.test_function_name_as_variable`, `test_function_name_without_call_is_unknown`, `FunctionTest.test_call_ignores_variable_of_same_name`, `test_calling_a_variable_is_unknown` | One merged namespace: functions as reserved words, or variables shadowing calls. |
| `min`/`max` with one argument; ties keep the first | 18 | `FunctionTest.test_min_max_single_argument`, `test_max_tie_returns_first`, `test_min_tie_returns_first` | `max(*args)` with one argument raises `TypeError`; `sorted(args)[-1]` returns the last tie. |
| Banker's rounding returning `int` | 17 | `FunctionTest.test_round_halves_to_even`, `test_round_returns_int` | `int(x + 0.5)` / `math.floor(x + 0.5)`. |
| Syntax before evaluation; left-then-right; existence, then arity, then arguments | 19 | `EvaluationOrderTest.*` | Evaluating while parsing raises `DivisionByZeroError` for `1/0 +`; evaluating arguments before the arity check. |
| Columns are whole-string indices | 20 | `ColumnTest.*` | Per-line columns, or columns into a whitespace-stripped copy. |
| Leftmost bad token, not first invalid character | 21 | `LeftmostTokenTest.test_bad_token_before_invalid_character`, `test_bad_token_in_parens_before_invalid_character` | An eager lexer raises on `$` before the parser sees the earlier stray `2`. |
| End of input: `len(source)` column; innermost unclosed `(` wins even where an operand is expected | 22 | `EndOfInputTest.*` | Column of the last token's end; outermost `(`; `UnexpectedTokenError` for `(1 +` because the parser fails in `primary` before reaching the `)` check. |

Other assertion groups: whitespace accepted (1); integer/decimal literals (4, 5); binary precedence, right-associative `**`, repeated unary, parentheses (7); int/float result types and floor semantics (9, 10, 11); lookup, case sensitivity, unknown name attributes, `variables=None`, mapping unchanged (6, 15); `abs`/`min`/`max` basics, whitespace before `(`, unknown function (8, 16); arity attributes (16); misplaced commas (8, 21); unmatched `)` and empty parens (21); class hierarchy (Interface).

## Mutants

| Mutant | Rule |
|---|---|
| `whitespace-isspace` | 1 |
| `exponent-sign-plus-only` | 5 |
| `power-exponent-skips-unary` | 7 |
| `floor-division-truncates` | 9 |
| `modulo-zero-unchecked` | 12 |
| `domain-error-on-whole-float` | 13 |
| `power-overflow-unwrapped` | 14 |
| `round-half-up` | 17 |
| `max-tie-returns-last` | 18 |
| `right-operand-evaluated-first` | 19 |
| `invalid-char-raises-in-lexer` | 21 |
| `unclosed-paren-reports-outermost` | 22 |

## Wrong implementations

- `eval-while-parsing`: single pass that computes during parsing, `isspace` plus regex `\d`/`\w`, eager lexer errors, no overflow handling, operand-position EOF inside parens reported as `UnexpectedTokenError`.
- `unary-binds-tightest`: unary above `**`, `isdigit`/`isalpha`/`isspace`, greedy `[0-9.]` scan, EOF column at the last token's end, `max(*values)`, no domain/overflow or `**` zero check.
- `strips-whitespace-first`: removes whitespace before lexing, so every column after the first space is wrong and `* *` becomes `**`.
