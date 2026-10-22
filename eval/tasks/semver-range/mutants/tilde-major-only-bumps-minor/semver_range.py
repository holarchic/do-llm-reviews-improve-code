"""npm-style semantic versions and ranges."""

import re
from dataclasses import dataclass


class InvalidVersionError(ValueError):
    def __init__(self, text):
        super().__init__(f"invalid version: {text!r}")
        self.text = text


class InvalidRangeError(ValueError):
    def __init__(self, text):
        super().__init__(f"invalid range: {text!r}")
        self.text = text


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: tuple = ()
    build: tuple = ()


_NUM = r"0|[1-9][0-9]*"
_IDENTS = r"[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*"
_VERSION_RE = re.compile(rf"({_NUM})\.({_NUM})\.({_NUM})(?:-({_IDENTS}))?(?:\+({_IDENTS}))?")
_PART = r"0|[1-9][0-9]*|[xX*]"
_PARTIAL_RE = re.compile(rf"({_PART})(?:\.({_PART})(?:\.({_PART}))?)?")
_WILDCARDS = ("x", "X", "*")
# Longest first, so "<=" is not read as "<" followed by "=...".
_OPERATORS = ("<=", ">=", "<", ">", "=", "~", "^")
_DASH = object()
# Lowest possible version: nothing sorts below 0.0.0-0, so "<" it matches nothing.
_NOTHING = ("<", Version(0, 0, 0, (0,)))


def parse(text):
    match = _VERSION_RE.fullmatch(text)
    if not match:
        raise InvalidVersionError(text)
    major, minor, patch, pre, build = match.groups()
    prerelease = []
    for ident in pre.split(".") if pre else ():
        if ident.isdigit():
            if len(ident) > 1 and ident[0] == "0":
                raise InvalidVersionError(text)
            prerelease.append(int(ident))
        else:
            prerelease.append(ident)
    return Version(int(major), int(minor), int(patch), tuple(prerelease),
                   tuple(build.split(".")) if build else ())


def compare(a, b):
    return _compare(parse(a), parse(b))


def satisfies(version, range_text):
    sets = _parse_range(range_text)
    parsed = parse(version)
    return any(_set_matches(s, parsed) for s in sets)


def max_satisfying(versions, range_text):
    sets = _parse_range(range_text)
    best = best_version = None
    for text in versions:
        try:
            parsed = parse(text)
        except InvalidVersionError:
            continue
        if not any(_set_matches(s, parsed) for s in sets):
            continue
        if best is None or _compare(parsed, best_version) > 0:
            best, best_version = text, parsed
    return best


def _compare(a, b):
    left, right = (a.major, a.minor, a.patch), (b.major, b.minor, b.patch)
    if left != right:
        return -1 if left < right else 1
    if not a.prerelease or not b.prerelease:
        return bool(b.prerelease) - bool(a.prerelease)
    for x, y in zip(a.prerelease, b.prerelease):
        if x == y:
            continue
        x_numeric, y_numeric = isinstance(x, int), isinstance(y, int)
        if x_numeric != y_numeric:
            return -1 if x_numeric else 1
        return -1 if x < y else 1
    return (len(a.prerelease) > len(b.prerelease)) - (len(a.prerelease) < len(b.prerelease))


@dataclass(frozen=True)
class _Set:
    comparators: tuple
    prerelease_triples: frozenset


@dataclass(frozen=True)
class _Pattern:
    """Parts are None where wildcard or missing; full is set only for a complete version."""
    major: int | None
    minor: int | None
    patch: int | None
    full: Version | None


def _parse_range(range_text):
    return [_parse_set(part, range_text) for part in range_text.split("||")]


def _parse_set(text, range_text):
    tokens = text.split()
    items = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "-":
            items.append(_DASH)
        else:
            op, pattern = _split_operator(token)
            if not pattern:
                if i + 1 == len(tokens):
                    raise InvalidRangeError(range_text)
                i += 1
                pattern = tokens[i]
            items.append((op, _parse_pattern(pattern, range_text)))
        i += 1
    if _DASH in items:
        return _hyphen_set(items, range_text)
    comparators = []
    for op, pattern in items:
        comparators.extend(_expand(op, pattern))
    return _Set(tuple(comparators), _prerelease_triples(pattern for _, pattern in items))


def _split_operator(token):
    for op in _OPERATORS:
        if token.startswith(op):
            return op, token[len(op):]
    return "", token


def _parse_pattern(text, range_text):
    try:
        full = parse(text)
    except InvalidVersionError:
        full = None
    if full is not None:
        return _Pattern(full.major, full.minor, full.patch, full)
    match = _PARTIAL_RE.fullmatch(text)
    if not match:
        raise InvalidRangeError(range_text)
    parts = [None if g is None or g in _WILDCARDS else int(g) for g in match.groups()]
    for earlier, later in zip(parts, parts[1:]):
        if earlier is None and later is not None:
            raise InvalidRangeError(range_text)
    return _Pattern(*parts, None)


def _prerelease_triples(patterns):
    return frozenset((p.major, p.minor, p.patch) for p in patterns if p.full is not None and p.full.prerelease)


def _hyphen_set(items, range_text):
    if len(items) != 3 or items[1] is not _DASH or _DASH in (items[0], items[2]):
        raise InvalidRangeError(range_text)
    (low_op, low), (high_op, high) = items[0], items[2]
    if low_op or high_op:
        raise InvalidRangeError(range_text)
    comparators = []
    if low.major is not None:
        comparators.append((">=", _lower_bound(low)))
    if high.full is not None:
        comparators.append(("<=", high.full))
    elif high.major is not None:
        comparators.append(("<", _next_above(high)))
    return _Set(tuple(comparators), _prerelease_triples((low, high)))


def _expand(op, p):
    if op == "~":
        return _tilde(p)
    if op == "^":
        return _caret(p)
    if p.full is not None:
        return [("=" if op == "" else op, p.full)]
    if p.major is None:
        return [_NOTHING] if op in ("<", ">") else []
    if op in ("", "="):
        return [(">=", _lower_bound(p)), ("<", _next_above(p))]
    if op == ">":
        return [(">=", _next_above(p, prerelease=()))]
    if op == ">=":
        return [(">=", _lower_bound(p))]
    if op == "<":
        return [("<", Version(p.major, p.minor or 0, 0, (0,)))]
    return [("<", _next_above(p))]


def _tilde(p):
    if p.major is None:
        return []
    # The bound comes from major and minor only: tilde never lets the minor part change once given.
    return [(">=", _lower_bound(p)), ("<", Version(p.major, (p.minor or 0) + 1, 0, (0,)))]


def _caret(p):
    if p.major is None:
        return []
    if p.major > 0 or p.minor is None:
        upper = Version(p.major + 1, 0, 0, (0,))
    elif p.minor > 0 or p.patch is None:
        upper = Version(0, p.minor + 1, 0, (0,))
    else:
        upper = Version(0, 0, p.patch + 1, (0,))
    return [(">=", _lower_bound(p)), ("<", upper)]


def _lower_bound(p):
    return p.full if p.full is not None else Version(p.major, p.minor or 0, p.patch or 0)


def _next_above(p, prerelease=(0,)):
    """First version past everything major.minor (or major alone) covers; the patch is not consulted."""
    if p.minor is None:
        return Version(p.major + 1, 0, 0, prerelease)
    return Version(p.major, p.minor + 1, 0, prerelease)


def _set_matches(s, version):
    for op, bound in s.comparators:
        order = _compare(version, bound)
        ok = {"<": order < 0, "<=": order <= 0, ">": order > 0, ">=": order >= 0, "=": order == 0}[op]
        if not ok:
            return False
    if version.prerelease and (version.major, version.minor, version.patch) not in s.prerelease_triples:
        return False
    return True
