"""RFC 6902 JSON Patch over RFC 6901 JSON Pointer, applied to plain Python JSON values."""

import copy
import re

_INDEX = re.compile(r"0|[1-9][0-9]*")
_ESCAPE = re.compile(r"~[01]")
_OPS = ("add", "remove", "replace", "move", "copy", "test")
_REQUIRED = {
    "add": ("path", "value"),
    "remove": ("path",),
    "replace": ("path", "value"),
    "move": ("from", "path"),
    "copy": ("from", "path"),
    "test": ("path", "value"),
}


class JsonPatchError(ValueError):
    def __init__(self, message, *, index=None):
        super().__init__(message)
        self.index = index


class InvalidPointerError(JsonPatchError):
    def __init__(self, message, pointer, *, index=None):
        super().__init__(message, index=index)
        self.pointer = pointer


class PathNotFoundError(JsonPatchError):
    def __init__(self, message, pointer, *, index=None):
        super().__init__(message, index=index)
        self.pointer = pointer


class FailedTestError(JsonPatchError):
    def __init__(self, message, pointer, *, index=None):
        super().__init__(message, index=index)
        self.pointer = pointer


class InvalidOperationError(JsonPatchError):
    pass


def parse_pointer(pointer):
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise InvalidPointerError(f"pointer must start with '/': {pointer!r}", pointer)
    return [_unescape(token, pointer) for token in pointer[1:].split("/")]


def _unescape(token, pointer):
    # A '~' not consumed by a valid escape is an error; a single left-to-right
    # substitution keeps a decoded '~' from pairing with the next character.
    if "~" in _ESCAPE.sub("", token):
        raise InvalidPointerError(f"invalid escape in {pointer!r}", pointer)
    return _ESCAPE.sub(lambda m: "/" if m.group() == "~1" else "~", token)


def resolve(document, pointer):
    return _get(document, parse_pointer(pointer), pointer)


def apply_patch(document, patch):
    if not isinstance(patch, list):
        raise InvalidOperationError("patch must be a list")
    result = copy.deepcopy(document)
    for index, operation in enumerate(patch):
        try:
            result = _apply_one(result, operation)
        except JsonPatchError as error:
            error.index = index
            raise
    return result


def _apply_one(doc, operation):
    if not isinstance(operation, dict):
        raise InvalidOperationError("operation must be an object")
    name = operation.get("op")
    if not isinstance(name, str) or name not in _OPS:
        raise InvalidOperationError(f"unknown op {name!r}")
    for member in _REQUIRED[name]:
        if member not in operation:
            raise InvalidOperationError(f"{name} requires {member!r}")
    for member in ("path", "from"):
        if member in _REQUIRED[name] and not isinstance(operation[member], str):
            raise InvalidOperationError(f"{member!r} must be a string")
    path = operation["path"]
    if name in ("move", "copy"):
        source = operation["from"]
        source_tokens = parse_pointer(source)
    tokens = parse_pointer(path)
    if name == "add":
        return _add(doc, tokens, copy.deepcopy(operation["value"]), path)
    if name == "remove":
        if not tokens:
            raise InvalidOperationError("cannot remove the whole document")
        _remove(doc, tokens, path)
        return doc
    if name == "replace":
        return _replace(doc, tokens, copy.deepcopy(operation["value"]), path)
    if name == "move":
        if len(tokens) > len(source_tokens) and tokens[: len(source_tokens)] == source_tokens:
            raise InvalidOperationError("cannot move a value into its own child")
        if tokens == source_tokens:
            _get(doc, source_tokens, source)
            return doc
        value = _remove(doc, source_tokens, source)
        return _add(doc, tokens, value, path)
    if name == "copy":
        value = copy.deepcopy(_get(doc, source_tokens, source))
        return _add(doc, tokens, value, path)
    if not _json_equal(_get(doc, tokens, path), operation["value"]):
        raise FailedTestError(f"test failed at {path!r}", path)
    return doc


def _array_index(token, pointer):
    """The int index for token, or None for '-'."""
    if token == "-":
        return None
    if not _INDEX.fullmatch(token):
        raise InvalidPointerError(f"invalid array index {token!r} in {pointer!r}", pointer)
    return int(token)


def _existing_index(array, token, pointer):
    index = _array_index(token, pointer)
    if index is None or index >= len(array):
        raise PathNotFoundError(f"no element {token!r} in {pointer!r}", pointer)
    return index


def _child(container, token, pointer):
    if isinstance(container, dict):
        if token not in container:
            raise PathNotFoundError(f"no member {token!r} in {pointer!r}", pointer)
        return container[token]
    if isinstance(container, list):
        return container[_existing_index(container, token, pointer)]
    raise PathNotFoundError(f"cannot descend into a scalar at {pointer!r}", pointer)


def _get(doc, tokens, pointer):
    current = doc
    for token in tokens:
        current = _child(current, token, pointer)
    return current


def _add(doc, tokens, value, pointer):
    if not tokens:
        return value
    parent, last = _get(doc, tokens[:-1], pointer), tokens[-1]
    if isinstance(parent, dict):
        parent[last] = value
    elif isinstance(parent, list):
        index = _array_index(last, pointer)
        if index is None:
            parent.append(value)
        elif index > len(parent):
            raise PathNotFoundError(f"index out of range in {pointer!r}", pointer)
        else:
            parent.insert(index, value)
    else:
        raise PathNotFoundError(f"cannot add into a scalar at {pointer!r}", pointer)
    return doc


def _remove(doc, tokens, pointer):
    """Remove and return the value at tokens (non-empty)."""
    parent, last = _get(doc, tokens[:-1], pointer), tokens[-1]
    if isinstance(parent, dict):
        if last not in parent:
            raise PathNotFoundError(f"no member {last!r} in {pointer!r}", pointer)
        return parent.pop(last)
    if isinstance(parent, list):
        return parent.pop(_existing_index(parent, last, pointer))
    raise PathNotFoundError(f"cannot remove from a scalar at {pointer!r}", pointer)


def _replace(doc, tokens, value, pointer):
    if not tokens:
        return value
    parent, last = _get(doc, tokens[:-1], pointer), tokens[-1]
    if isinstance(parent, dict):
        if last not in parent:
            raise PathNotFoundError(f"no member {last!r} in {pointer!r}", pointer)
        parent[last] = value
    elif isinstance(parent, list):
        parent[_existing_index(parent, last, pointer)] = value
    else:
        raise PathNotFoundError(f"cannot replace inside a scalar at {pointer!r}", pointer)
    return doc


def _json_equal(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    return a is None and b is None
