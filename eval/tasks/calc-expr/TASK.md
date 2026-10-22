# Task: safe arithmetic expression evaluator

Write a Python module `calc_expr.py` at the repository root (standard library only). Tests import it as `import calc_expr`.

The module must not use `eval`, `exec`, `compile` or the `ast` module. Write your own tokenizer and parser.

## Interface

- `evaluate(source: str, variables: Mapping[str, int | float] | None = None) -> int | float`
- `class CalcError(Exception)`: the base of every error below. Attribute `column: int`.
- `class UnexpectedTokenError(CalcError)`
- `class UnclosedParenError(CalcError)`
- `class UnknownNameError(CalcError)`: extra attribute `name: str`.
- `class ArityError(CalcError)`: extra attributes `name: str` (the function name) and `given: int` (the number of arguments in the call).
- `class DivisionByZeroError(CalcError, ZeroDivisionError)`
- `class DomainError(CalcError)`

Each of the six error classes subclasses `CalcError` directly; none of them subclasses another of the six. Constructor signatures are your choice. `column` is always a 0-based index into `source` (rule 20).

## Behaviour

### Characters and tokens

1. Whitespace is exactly these four characters: space, tab (`\t`), newline (`\n`) and carriage return (`\r`). Whitespace may appear before, after and between tokens and is otherwise ignored. No other character counts as whitespace (a non-breaking space, U+00A0, does not).
2. The tokens are: number literals (rules 4–5), names (rule 6), the operators `+ - * / // % **`, `(`, `)` and `,`. Any other character, including every non-ASCII character (`é`, `²`, `٣`) and `^`, `$`, `!`, `=`, is an invalid character.
3. Tokens are read left to right, each as long as possible: `**` and `//` are single tokens, so `2***3` reads `2`, `**`, `*`, `3`. Whitespace separates tokens: `2 * * 3` contains two `*` tokens, not `**`, and `8 / / 2` contains two `/` tokens.
4. An integer literal is one or more ASCII digits `0`–`9`, with no sign. Leading zeros are allowed: `007` is `7`. Its value is an `int`.
5. A float literal is either digits, a `.`, then optional digits (`1.5`, `1.`), or a `.` followed by digits (`.5`); either form may be followed by an exponent. Digits followed directly by an exponent (`1e3`) are also a float literal. An exponent is `e` or `E`, an optional `+` or `-`, then one or more digits. The exponent belongs to the literal only when the `e`/`E` is followed by a digit, or by a sign and then a digit; otherwise the literal ends just before the `e`/`E` (so `2e` is the literal `2` followed by the name `e`). Every literal containing a `.` or an exponent is a `float` (`1e3` is `1000.0`, `1.5E-2` is `0.015`, `.5e+1` is `5.0`). Literals never contain `_`. A `.` that does not start or continue a literal is an invalid character. Examples: `1.2.3` reads as `1.2` then `.3`; `1_000` reads as `1` then the name `_000`.
6. A name is an ASCII letter or `_`, followed by any number of ASCII letters, ASCII digits and `_`. Names are case-sensitive.

### Grammar

7. The grammar, from lowest to highest precedence:

   ```
   expression := term (("+" | "-") term)*
   term       := unary (("*" | "/" | "//" | "%") unary)*
   unary      := ("+" | "-") unary | power
   power      := primary ("**" unary)?
   primary    := NUMBER | NAME | call | "(" expression ")"
   call       := NAME "(" (expression ("," expression)*)? ")"
   ```

   The whole source must be exactly one `expression`. Consequences: binary `+ - * / // %` are left-associative; `**` is right-associative and binds tighter than a unary operator on its left, while its right operand may itself start with unary operators. `-2 ** 2` is `-4`, `2 ** -1` is `0.5`, `2 ** 3 ** 2` is `512`, `2 ** -2 ** 2` is `2 ** -(2 ** 2)`. Unary operators bind tighter than `* / // %`: `-7 // 2` is `(-7) // 2`, which is `-4`. Unary operators may repeat: `--2` is `2`, `2---2` is `0`. There is no implicit multiplication: `2x`, `2(3)` and `(1)(2)` are errors.
8. A name followed by `(` is a call, with or without whitespace between them (`max (1, 2)` is a call). A call has zero or more comma-separated arguments; a trailing or leading comma is an error. A `,` outside a call's parentheses is an error.

### Values and operators

9. `+`, `-`, `*`, `//` and `%` follow Python's semantics on the two operand values: `int` with `int` gives an `int`; if either operand is a `float` the result is a `float`. `//` and `%` floor: `7 // -2` is `-4`, `7 % -2` is `-1`, `7.5 // 2` is `3.0`. Unary `-` and `+` follow Python's semantics.
10. `/` always returns a `float`: `4 / 2` is `2.0`.
11. `**` follows Python's semantics: an `int` raised to a non-negative `int` is an `int`; an `int` raised to a negative `int` is a `float` (`2 ** -2` is `0.25`); if either operand is a `float` the result is a `float`.
12. `/`, `//` or `%` whose right operand equals zero (`0`, `0.0` or `-0.0`), and `**` whose left operand equals zero and whose right operand is negative, raise `DivisionByZeroError`. Its `column` is the column of the operator.
13. `**` whose left operand is negative and whose right operand is a `float` that is not a whole number raises `DomainError`, with `column` the column of the `**`. `(-8) ** 0.5` raises; `-8 ** 0.5` does not (it is `-(8 ** 0.5)`); `(-8) ** 2.0` is `64.0`.
14. If Python's `**` raises `OverflowError` for the two operand values (for example `10.0 ** 400` or `2 ** 2000.0`), `DomainError` is raised with `column` the column of the `**`. An `int` raised to a non-negative `int` never overflows: `2 ** 2000` is an exact `int`.

### Names

15. A name that is not a call is a variable: its value is `variables[name]`. If `variables` is `None` or does not contain the name, `UnknownNameError` is raised with `name` the name and `column` the column of the name. Function names are not reserved: `max + 1` with `variables={"max": 2}` is `3`, and `abs` with no variables raises `UnknownNameError`. `evaluate` does not modify `variables`.
16. A call always calls a built-in function, even if `variables` contains the same name. The built-in functions are exactly `abs`, `round`, `min` and `max`. Calling any other name raises `UnknownNameError` (`name` the name, `column` the column of the name), even if `variables` contains it. `abs` and `round` take exactly one argument; `min` and `max` take one or more. A call with any other number of arguments raises `ArityError` with `name` the function name, `given` the number of arguments, and `column` the column of the function name.
17. `abs(x)` is Python's `abs(x)`. `round(x)` returns an `int`, rounding halves to the even neighbour: `round(2.5)` is `2`, `round(3.5)` is `4`, `round(-2.5)` is `-2`, `round(2.7)` is `3`, `round(7)` is `7`.
18. `min` and `max` return the smallest or largest argument. When several arguments tie, the first of them is returned unchanged, keeping its type: `max(1, 1.0)` is `1` (an `int`), `max(1.0, 1)` is `1.0`. With one argument they return it: `max(5)` is `5`.

### Errors and evaluation order

19. The whole source is checked for syntax errors (rules 20–22) before anything is evaluated, so a source with a syntax error never raises an evaluation error: `1/0 +` raises `UnexpectedTokenError`. Evaluation then proceeds as follows, and the first error met is raised. A unary or binary operator evaluates its left operand completely, then its right operand, then applies itself; there is no short-circuiting (`0 * (1/0)` raises). A call first checks that the function exists (rule 16), then checks the argument count, then evaluates its arguments left to right, then applies the function. So `x + 1/0` with no variables raises `UnknownNameError`, `1/0 + x` raises `DivisionByZeroError`, `abs(1/0, 2)` raises `ArityError` and `foo(1/0)` raises `UnknownNameError`.
20. A `column` is the index in `source` of the first character of the token the error concerns. It counts every character of `source`, newlines and tabs included; there are no per-line columns. For example, in `"1 +\n  y"` the name `y` is at column 6.
21. Syntax errors are found by reading tokens left to right. The first token that cannot continue a valid expression (per rule 7) raises `UnexpectedTokenError` with `column` the column of that token; an invalid character is a token that can never continue one. So `1 2 $` raises `UnexpectedTokenError` at column 2 (the `2`), not at the `$`. A `)` with no matching open `(` is such a token: `1)` and `(1))` raise `UnexpectedTokenError` at the `)`; `()` raises it at column 1.
22. If every token is acceptable but the source ends before the expression is complete: when at least one `(` (including a call's) is still open, `UnclosedParenError` is raised with `column` the column of the most recently opened `(` that is still open; otherwise `UnexpectedTokenError` is raised with `column` equal to `len(source)`, trailing whitespace included. Examples: `(1 +` → `UnclosedParenError` at 0; `(1 + (2` → `UnclosedParenError` at 5; `((1)` → `UnclosedParenError` at 0; `max(1,` → `UnclosedParenError` at 3; `1 +  ` → `UnexpectedTokenError` at 5; an empty or whitespace-only source → `UnexpectedTokenError` at `len(source)`. An invalid character or other unacceptable token before the end is reported by rule 21 instead: `( $` raises `UnexpectedTokenError` at 2.

Include your own tests.
