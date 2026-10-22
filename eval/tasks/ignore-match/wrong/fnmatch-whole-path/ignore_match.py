"""Gitignore-style matching built on fnmatch."""

import fnmatch
from dataclasses import dataclass
from typing import Iterable


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
    text: str


def _fnmatch(name: str, pattern: str) -> bool:
    candidates = {pattern, pattern.replace("/**/", "/")}
    if pattern.startswith("**/"):
        candidates.add(pattern[3:])
    return any(fnmatch.fnmatchcase(name, c) for c in candidates)


class IgnoreRules:
    def __init__(self, lines: Iterable[str]):
        self._patterns = []
        for number, raw in enumerate(lines, start=1):
            text = raw.strip()
            if not text or text.startswith("#"):
                continue
            negated = text.startswith("!")
            if negated:
                text = text[1:]
            if text.startswith("\\"):
                text = text[1:]
            if text.endswith("\\") or text.count("[") > text.count("]"):
                raise PatternError(number, raw)
            dir_only = text.endswith("/")
            if dir_only:
                text = text[:-1]
            if not text:
                continue
            anchored = "/" in text
            self._patterns.append(_Pattern(number, negated, dir_only, anchored, text.lstrip("/")))

    def _last_match(self, path: str, is_dir: bool):
        found = None
        for pattern in self._patterns:
            if pattern.dir_only and not is_dir:
                continue
            target = path if pattern.anchored else path.rsplit("/", 1)[-1]
            if _fnmatch(target, pattern.text):
                found = pattern
        return found

    def explain(self, path: str, is_dir: bool = False):
        if path.endswith("/"):
            path, is_dir = path[:-1], True
        parts = path.split("/")
        if not path or path.startswith("/") or any(p in ("", ".", "..") for p in parts):
            raise ValueError(path)
        for depth in range(1, len(parts)):
            ancestor = "/".join(parts[:depth])
            pattern = self._last_match(ancestor, True)
            if pattern is not None and not pattern.negated:
                return Decision(pattern.line_number, False, ancestor)
        pattern = self._last_match(path, is_dir)
        if pattern is None:
            return None
        return Decision(pattern.line_number, pattern.negated, path)

    def is_ignored(self, path: str, is_dir: bool = False) -> bool:
        decision = self.explain(path, is_dir)
        return decision is not None and not decision.negated
