# Task: config loader with includes

Write a Python module `confload.py` at the repository root (standard library only). Tests import it as `import confload`.

## Interface

- `load(path: str | os.PathLike) -> dict[str, str]` — read the config file at `path` and return its final key/value pairs.
- `class ConfigError(ValueError)` — raised for every problem below. It has attributes `path` (the `str` path of the file where the problem is) and `line` (the 1-based line number, or `None` when the problem has no line).

## File format

1. The file is UTF-8 text, processed line by line, top to bottom.
2. Blank lines and lines whose first non-whitespace character is `#` are ignored.
3. An assignment line is `key = value`. The line splits at the **first** `=`; key and value are stripped of surrounding whitespace. The value may be empty and may contain further `=` characters.
4. A key must match `[A-Za-z_][A-Za-z0-9_.]*`. Keys are case-sensitive. An invalid key, or a non-blank, non-comment line that is neither an assignment nor an include, raises `ConfigError` with that file's path and line number.
5. A line containing `=` is always an assignment. Any other line of the form `include <path>` (the word `include`, whitespace, then a path, stripped) is an include line. A relative include path is resolved against the directory of the file containing the include line, not the current working directory. The included file is processed at that point, as if its lines appeared there.
6. Assignments apply in the order they are processed, so a later assignment of a key overrides an earlier one, whether either comes from an included file or not.
7. A file that includes itself, directly or through other files, raises `ConfigError` whose message contains the word `cycle`; `line` is the line of the include that closes the cycle. Including the same file twice from different places without a cycle (for example `a` includes `b` and `c`, and both include `d`) is allowed, and `d` is processed each time.
8. An include of a file that does not exist raises `ConfigError` with the path and line of the include line.

## Interpolation

9. In a value, `${name}` is replaced by the current value of key `name` at the moment the assignment is processed. A later change to `name` does not change values already assigned.
10. `${name}` naming a key that has no value yet raises `ConfigError` with the path and line of the assignment.
11. `$$` produces a literal `$`. A `$` followed by anything other than `{` or `$` is kept as is.

Include your own tests.
