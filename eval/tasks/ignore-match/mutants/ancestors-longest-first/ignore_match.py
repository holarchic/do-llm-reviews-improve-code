"""Gitignore-style matching of slash-separated relative paths."""

import re
from dataclasses import dataclass
from typing import Iterable

_DOUBLESTAR = object()


class PatternError(ValueError):
    def __init__(self, line_number: int, line: str):
        super().__init__(f"invalid pattern on line {line_number}: {line!r}")
        self.line_number = line_number
        self.line = line


@dataclass(frozen=True)
class Decision:
    line_number: int
    negated: bool
    path: str


@dataclass(frozen=True)
class _Pattern:
    line_number: int
    negated: bool
    dir_only: bool
    anchored: bool
    segments: tuple

    def matches(self, components: list[str], is_dir: bool) -> bool:
        if self.dir_only and not is_dir:
            return False
        if not self.anchored:
            return _match_segments(self.segments, 0, components[-1:], 0)
        return _match_segments(self.segments, 0, components, 0)


def _match_segments(segments, i: int, components: list[str], j: int) -> bool:
    if i == len(segments):
        return j == len(components)
    segment = segments[i]
    if segment is _DOUBLESTAR:
        if i == len(segments) - 1:
            # Trailing `/**` needs at least one component; a lone `**` is the same test.
            return j < len(components)
        return any(_match_segments(segments, i + 1, components, k) for k in range(j, len(components) + 1))
    return j < len(components) and segment.fullmatch(components[j]) is not None \
        and _match_segments(segments, i + 1, components, j + 1)


class _Invalid(Exception):
    pass


def _escape_flags(text: str) -> list[bool]:
    """escaped[i] is True when text[i] is preceded by an escaping backslash."""
    escaped, pending = [], False
    for char in text:
        escaped.append(pending)
        pending = char == "\\" and not pending
    return escaped


def _trim_trailing_spaces(text: str) -> str:
    escaped = _escape_flags(text)
    end = len(text)
    while end > 0 and text[end - 1] == " " and not escaped[end - 1]:
        end -= 1
    return text[:end]


def _parse_class(body: str, start: int) -> tuple[str, int]:
    """Translate the bracket expression opening at body[start]; return (regex, index after `]`)."""
    i = start + 1
    negated = i < len(body) and body[i] == "!"
    if negated:
        i += 1
    items: list[str] = []
    first = True
    while True:
        if i >= len(body):
            raise _Invalid
        char = body[i]
        if char == "]" and not first:
            break
        first = False
        if char == "\\":
            i += 1
            if i >= len(body):
                raise _Invalid
            char = body[i]
        if char == "/":
            raise _Invalid
        i += 1
        if i + 1 < len(body) and body[i] == "-" and body[i + 1] != "]":
            end = body[i + 1]
            i += 2
            if end == "\\":
                if i >= len(body):
                    raise _Invalid
                end = body[i]
                i += 1
            if end == "/" or end < char:
                raise _Invalid
            items.append(f"{re.escape(char)}-{re.escape(end)}")
        else:
            items.append(re.escape(char))
    return ("[^" if negated else "[") + "".join(items) + "]", i + 1


def _parse_segments(body: str) -> tuple:
    segments: list = []
    regex: list[str] = []
    stars_only = True
    star_count = 0
    i = 0

    def close_segment():
        if stars_only and star_count == 2:
            segments.append(_DOUBLESTAR)
        else:
            segments.append(re.compile("".join(regex), re.DOTALL))

    while i < len(body):
        char = body[i]
        if char == "/":
            close_segment()
            regex, stars_only, star_count = [], True, 0
            i += 1
            continue
        if char == "*":
            regex.append(".*")
            star_count += 1
            i += 1
            continue
        stars_only = False
        if char == "\\":
            if i + 1 >= len(body):
                raise _Invalid
            regex.append(re.escape(body[i + 1]))
            i += 2
        elif char == "?":
            regex.append(".")
            i += 1
        elif char == "[":
            class_regex, i = _parse_class(body, i)
            regex.append(class_regex)
        else:
            regex.append(re.escape(char))
            i += 1
    close_segment()
    return tuple(segments)


def _parse_line(line_number: int, line: str) -> "_Pattern | None":
    text = line.removesuffix("\n").removesuffix("\r")
    if text.startswith("#"):
        return None
    text = _trim_trailing_spaces(text)
    if not text:
        return None
    negated = text.startswith("!")
    if negated:
        text = text[1:]
    dir_only = text.endswith("/")
    if dir_only:
        text = text[:-1]
    if not text:
        return None
    anchored = "/" in text
    if text.startswith("/"):
        text = text[1:]
    try:
        segments = _parse_segments(text)
    except _Invalid:
        raise PatternError(line_number, line) from None
    return _Pattern(line_number, negated, dir_only, anchored, segments)


def _split_path(path: str, is_dir: bool) -> tuple[list[str], bool]:
    if path.endswith("/"):
        path, is_dir = path[:-1], True
    if not path or path.startswith("/"):
        raise ValueError(f"invalid path: {path!r}")
    components = path.split("/")
    if any(part in ("", ".", "..") for part in components):
        raise ValueError(f"invalid path: {path!r}")
    return components, is_dir


class IgnoreRules:
    def __init__(self, lines: Iterable[str]):
        patterns = []
        for line_number, line in enumerate(lines, start=1):
            pattern = _parse_line(line_number, line)
            if pattern is not None:
                patterns.append(pattern)
        self._patterns = tuple(patterns)

    def _last_match(self, components: list[str], is_dir: bool) -> "_Pattern | None":
        for pattern in reversed(self._patterns):
            if pattern.matches(components, is_dir):
                return pattern
        return None

    def explain(self, path: str, is_dir: bool = False) -> "Decision | None":
        components, is_dir = _split_path(path, is_dir)
        for depth in reversed(range(1, len(components))):
            ancestor = components[:depth]
            pattern = self._last_match(ancestor, True)
            if pattern is not None and not pattern.negated:
                return Decision(pattern.line_number, False, "/".join(ancestor))
        pattern = self._last_match(components, is_dir)
        if pattern is None:
            return None
        return Decision(pattern.line_number, pattern.negated, "/".join(components))

    def is_ignored(self, path: str, is_dir: bool = False) -> bool:
        decision = self.explain(path, is_dir)
        return decision is not None and not decision.negated
