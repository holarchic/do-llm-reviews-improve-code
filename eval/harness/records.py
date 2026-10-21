"""Reading run and judgement records from disk, validated once at the boundary.

Every consumer — resume, follow-ups, judging and the report — reads records
through these loaders, so a record one of them would reject is never reused by
another. A malformed record reads as absent: its job runs again, and the report
counts it as missing.
"""

import json
from pathlib import Path

_STATIC_KEYS = ("ruff_violations", "source_loc", "test_loc", "max_function_branches")
_FOLLOWUP_LISTS = ("regressions", "persistent_failures", "repaired")


def build(path: Path, digest: str) -> dict | None:
    """A complete build record under this manifest, or None."""
    record = _complete(path, digest)
    if record is None:
        return None
    scores = record.get("scores")
    valid = (
        isinstance(scores, dict)
        and _number(scores.get("primary")) and 0 <= scores["primary"] <= 1
        and isinstance(scores.get("hidden_outcomes"), dict)
        and isinstance(scores.get("static"), dict)
        and all(isinstance(scores["static"].get(key), int) for key in _STATIC_KEYS)
        and _valid_steps(record.get("steps"))
        and isinstance(record.get("flags"), list) and all(isinstance(flag, str) for flag in record["flags"])
        and isinstance(record.get("rounds"), dict) and all(isinstance(v, int) for v in record["rounds"].values())
    )
    return record if valid else None


def followup(path: Path, digest: str) -> dict | None:
    record = _complete(path, digest)
    if record is None:
        return None
    scores = record.get("scores")
    valid = (
        isinstance(scores, dict)
        and _number(scores.get("new_fraction"))
        and all(isinstance(scores.get(key), list) for key in _FOLLOWUP_LISTS)
    )
    return record if valid else None


def judgement(path: Path, digest: str) -> dict | None:
    record = _object(path)
    if record is None or record.get("manifest") != digest:
        return None
    leaks = record.get("leak_words")
    valid = (
        record.get("result") in ("a", "b", "tie")
        and isinstance(leaks, dict)
        and all(isinstance(leaks.get(side), int) for side in ("a", "b"))
    )
    return record if valid else None


def _complete(path: Path, digest: str) -> dict | None:
    record = _object(path)
    if record is None or record.get("status") != "complete" or record.get("manifest") != digest:
        return None
    return record


def _valid_steps(steps) -> bool:
    if not isinstance(steps, list):
        return False
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("attempts"), list):
            return False
        for attempt in step["attempts"]:
            if not (isinstance(attempt, dict) and _number(attempt.get("seconds"))
                    and isinstance(attempt.get("usage"), dict)):
                return False
    return True


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _object(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None
