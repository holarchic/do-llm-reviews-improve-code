"""JSON Patch, written against the RFC summary rather than TASK.md's numbered rules."""

import copy


class JsonPatchError(ValueError):
    def __init__(self, message, pointer=None, index=None):
        super().__init__(message)
        self.pointer = pointer
        self.index = index


class InvalidPointerError(JsonPatchError):
    pass


class PathNotFoundError(JsonPatchError):
    pass


class FailedTestError(JsonPatchError):
    pass


class InvalidOperationError(JsonPatchError):
    pass


def parse_pointer(pointer):
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise InvalidPointerError("bad pointer", pointer)
    return [t.replace("~1", "/").replace("~0", "~") for t in pointer.split("/")[1:]]


def _index(container, token, pointer, allow_end=False):
    if token == "-":
        if allow_end:
            return len(container)
        raise PathNotFoundError("dash", pointer)
    if not token.isdigit():
        raise InvalidPointerError("bad index", pointer)
    i = int(token)
    limit = len(container) + (1 if allow_end else 0)
    if i >= limit:
        raise PathNotFoundError("out of range", pointer)
    return i


def _walk(doc, tokens, pointer):
    for token in tokens:
        if isinstance(doc, dict):
            if token not in doc:
                raise PathNotFoundError("missing", pointer)
            doc = doc[token]
        elif isinstance(doc, list):
            doc = doc[_index(doc, token, pointer)]
        else:
            raise PathNotFoundError("scalar", pointer)
    return doc


def resolve(document, pointer):
    return _walk(document, parse_pointer(pointer), pointer)


def _add(doc, path, value):
    tokens = parse_pointer(path)
    if not tokens:
        return value
    parent = _walk(doc, tokens[:-1], path)
    if isinstance(parent, dict):
        parent[tokens[-1]] = value
    elif isinstance(parent, list):
        parent.insert(_index(parent, tokens[-1], path, allow_end=True), value)
    else:
        raise PathNotFoundError("scalar parent", path)
    return doc


def _remove(doc, path):
    tokens = parse_pointer(path)
    if not tokens:
        raise InvalidOperationError("cannot remove root")
    parent = _walk(doc, tokens[:-1], path)
    if isinstance(parent, dict):
        if tokens[-1] not in parent:
            raise PathNotFoundError("missing", path)
        return parent.pop(tokens[-1])
    if isinstance(parent, list):
        return parent.pop(_index(parent, tokens[-1], path))
    raise PathNotFoundError("scalar parent", path)


def apply_patch(document, patch):
    if not isinstance(patch, list):
        raise InvalidOperationError("patch must be a list")
    doc = copy.deepcopy(document)
    for i, op in enumerate(patch):
        try:
            doc = _apply(doc, op)
        except JsonPatchError as e:
            e.index = i
            raise
    return doc


def _apply(doc, op):
    if not isinstance(op, dict) or op.get("op") not in ("add", "remove", "replace", "move", "copy", "test"):
        raise InvalidOperationError("bad op")
    kind = op["op"]
    if not isinstance(op.get("path"), str):
        raise InvalidOperationError("bad path")
    path = op["path"]
    if kind in ("add", "replace", "test") and "value" not in op:
        raise InvalidOperationError("missing value")
    if kind in ("move", "copy") and not isinstance(op.get("from"), str):
        raise InvalidOperationError("bad from")
    if kind == "add":
        return _add(doc, path, op["value"])
    if kind == "remove":
        _remove(doc, path)
        return doc
    if kind == "replace":
        resolve(doc, path)
        if path == "":
            return op["value"]
        _remove(doc, path)
        return _add(doc, path, op["value"])
    if kind == "move":
        if path.startswith(op["from"] + "/"):
            raise InvalidOperationError("move into child")
        value = _remove(doc, op["from"])
        return _add(doc, path, value)
    if kind == "copy":
        return _add(doc, path, copy.deepcopy(resolve(doc, op["from"])))
    if resolve(doc, path) != op["value"]:
        raise FailedTestError("test failed", path)
    return doc
