# Task: JSON Patch

Write a Python module `json_patch.py` at the repository root (standard library only). Tests import it as `import json_patch`.

It implements JSON Pointer (RFC 6901) and JSON Patch (RFC 6902) over plain Python JSON values. A JSON value here is a `dict` with `str` keys (a JSON object), a `list` (a JSON array), `str`, `int`, `float`, `bool` or `None` (JSON null), nested to any depth. Behaviour on any other type is not specified.

## Interface

- `parse_pointer(pointer: str) -> list[str]` — the decoded reference tokens of a pointer.
- `resolve(document, pointer: str)` — the value the pointer locates in `document`.
- `apply_patch(document, patch) -> object` — the result of applying the operations in `patch` to `document`. `document` is a JSON value; `patch` is a list of operations, each a `dict`.
- `class JsonPatchError(ValueError)` with attribute `index: int | None`. Every error below is a subclass of it.
- `class InvalidPointerError(JsonPatchError)` with attribute `pointer: str`.
- `class PathNotFoundError(JsonPatchError)` with attribute `pointer: str`.
- `class FailedTestError(JsonPatchError)` with attribute `pointer: str`.
- `class InvalidOperationError(JsonPatchError)` (no extra attribute).

Constructors and messages are your choice; only the class, `index` and `pointer` are specified.

## Behaviour

### Pointers

1. `parse_pointer("")` returns `[]` (the whole document). Any other pointer must begin with `/`, or `InvalidPointerError` is raised (e.g. `"a/b"`, `"#/a"`). After the leading `/`, the pointer is split on every `/` into tokens, keeping empty tokens: `"/"` → `[""]`, `"/a//b/"` → `["a", "", "b", ""]`.
2. Inside a token, `~1` decodes to `/` and `~0` decodes to `~`. A `~` that is not immediately followed by `0` or `1` (including a `~` at the end of a token) makes the pointer invalid: `InvalidPointerError`. A `~` produced by decoding never combines with the character after it: `"/~01"` → `["~1"]`, `"/~10"` → `["/0"]`, `"/~0~1"` → `["~/"]`, and `"/~~0"` is invalid.
3. `resolve`, and `apply_patch` for every `path` and `from` it uses, parse pointers by rules 1–2.

### Locating values

4. Tokens are applied one at a time, left to right, starting from the whole document. Against a `dict`, a token is a key, matched exactly; `"-"`, `"01"` and `""` are ordinary keys there. A key that is not present raises `PathNotFoundError`.
5. Against a `list`, a token must be `-` or an array index. An array index is `0`, or an ASCII digit `1`–`9` followed by zero or more ASCII digits `0`–`9`. Any other token used against a list raises `InvalidPointerError`: for example `"01"`, `"-1"`, `"+1"`, `"1.0"`, `" 1"`, `""`, or non-ASCII digits such as `"١"`.
6. Where a pointer must locate an existing value — every pointer given to `resolve`, the `path` of `remove`, `replace` and `test`, the `from` of `move` and `copy`, and the parent that `add` (rule 16) locates — a `-` token, or an index greater than or equal to the list's length, raises `PathNotFoundError`.
7. A token applied to anything other than a `dict` or a `list` (a string, number, boolean or `None`) raises `PathNotFoundError`. Strings are not indexable: `resolve({"s": "abc"}, "/s/0")` raises `PathNotFoundError`.
8. The first token that fails decides the error. `resolve({"a": [1]}, "/b/01")` raises `PathNotFoundError` (no key `b`); `resolve({"a": [1]}, "/a/01/b")` raises `InvalidPointerError`; `resolve({"a": [1]}, "/a/5/01")` raises `PathNotFoundError`.
9. The `pointer` attribute of `InvalidPointerError`, `PathNotFoundError` and `FailedTestError` is the complete pointer string exactly as given (the whole `path` or `from` value, not the failing token).

### Patch structure

10. If `patch` is not a `list`, `apply_patch` raises `InvalidOperationError` with `index` `None`. An element of `patch` that is not a `dict` raises `InvalidOperationError`.
11. Every operation must have an `op` member whose value is exactly one of the strings `"add"`, `"remove"`, `"replace"`, `"move"`, `"copy"`, `"test"` (case-sensitive). A missing `op`, any other string, or any non-string value (including a list) raises `InvalidOperationError`.
12. Required members: `add`, `replace` and `test` need `path` and `value`; `remove` needs `path`; `move` and `copy` need `from` and `path`. A missing required member raises `InvalidOperationError`. A member whose value is `None` is present: `{"op": "add", "path": "/a", "value": None}` adds `null`. A `path` or required `from` that is not a string raises `InvalidOperationError`.
13. Members that the operation does not require are ignored completely, whatever their value: the `from` of an `add` is never parsed or looked up, and a `value` on a `remove` has no effect.
14. Operations run one at a time in list order; each is fully checked and applied before the next is looked at, so an earlier operation's error wins over a later operation's. Within one operation the checks run in this order, and the first that fails decides the error: (a) structure, rules 10–12; (b) the syntax of `path` by rules 1–2, then of `from` (`move` and `copy` only); (c) for `move`, rule 20; (d) applying the operation, including locating values (rules 4–8) and the `test` comparison.

### Operations

15. `add` with `path` `""` makes `value` the whole result document.
16. Otherwise `add` locates the parent (every token but the last) by rules 4–8; the parent must exist. If the parent is a `dict`, the last token is set as a key, adding it or overwriting an existing value. If the parent is a `list`, `-` appends; an index `i` with `0 <= i <= len` inserts before position `i`, shifting later elements up (so `i == len` appends); an index greater than the length raises `PathNotFoundError`; a token that is not `-` or an array index raises `InvalidPointerError` (rule 5). If the parent is anything else, `PathNotFoundError`.
17. `remove` deletes the existing value at `path` (rule 6 applies): a `dict` member is deleted; a `list` element is removed and later elements shift down. `remove` with `path` `""` raises `InvalidOperationError`.
18. `replace` replaces the existing value at `path` with `value` (rule 6 applies; `replace` never creates a member or element, and a list keeps its length). `replace` with `path` `""` makes `value` the whole result document.
19. `move` removes the value at `from` and then adds it at `path` exactly as `add` would, with `path` located in the document as it is after the removal. For example, `move` from `/0` to `/2` on `["a", "b", "c", "d"]` gives `["b", "c", "a", "d"]`. If `from` and `path` are equal, the document is unchanged, but `from` must still locate an existing value (else `PathNotFoundError`).
20. A `move` whose `path` is a proper descendant of `from` raises `InvalidOperationError`. "Proper descendant" means `from`'s token list is a proper prefix of `path`'s token list. Compare token lists, not strings: from `/a` to `/ab` is allowed, from `/a/b` to `/a` (onto an ancestor) is allowed, and from `""` to any non-empty `path` is a descendant.
21. `copy` adds a deep copy of the value at `from` at `path`, exactly as `add` would. Later changes made through either location never affect the other. Copying into a descendant of `from` is allowed and copies the value as it was before the add: `copy` from `/a` to `/a/b` on `{"a": {"x": 1}}` gives `{"a": {"x": 1, "b": {"x": 1}}}`.
22. `test` compares the existing value at `path` (rule 6 applies, so a missing value raises `PathNotFoundError`, not `FailedTestError`) with `value` by rule 23. If they differ it raises `FailedTestError` with `pointer` the `path`. `test` never changes the document.
23. JSON equality: a boolean equals only a boolean with the same value (`True != 1`, `False != 0`, `False != 0.0`, `None != False`); an `int` and a `float` are equal when numerically equal (`1 == 1.0`); strings are equal when identical; `None` equals only `None`; lists are equal when they have the same length and equal elements in the same order; dicts are equal when they have the same keys and equal values for each key, in any order. Values of different kinds are never equal (`"1" != 1`, `[] != {}`), apart from `int` and `float`. The rule applies at every depth: `[1, True]` does not equal `[1, 1]`.

### Errors, atomicity and copying

24. When an operation fails, `apply_patch` raises its error with `index` set to that operation's 0-based position in `patch`, and the caller's document is left exactly as it was, at every depth. Errors raised by `parse_pointer` or `resolve` called directly, and the not-a-list error of rule 10, have `index` `None`.
25. `apply_patch` never modifies `document` or `patch`, at any depth (including the `value` members of operations). The returned document shares no `dict` or `list` object with `document` or `patch`, so mutating the result never changes either input. This holds for an empty patch too: the result equals `document` but is a distinct object when `document` is a `dict` or `list`.

Include your own tests.
