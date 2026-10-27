"""Reading run and judgement records from disk, validated once at the boundary.

Every consumer — resume, follow-ups, judging and the report — reads records
through these loaders, so a record one of them would reject is never reused by
another. A malformed record reads as absent: its job runs again, and the report
counts it as missing.
"""

import json
import math
from pathlib import Path

_STATIC_KEYS = ("ruff_violations", "source_loc", "test_loc", "max_function_branches")
_FOLLOWUP_LISTS = ("regressions", "persistent_failures", "repaired")
_IDENTITY = ("run_id", "task", "arm")
_TOKEN_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
_OWN_TEST_KEYS = ("collected", "pass_on_reference", "fail_on_reference", "load_errors")


def build(path: Path, digest: str) -> dict | None:
    """A complete build record under this manifest, or None."""
    record = _complete(path, digest)
    if record is None:
        return None
    scores = record.get("scores")
    valid = (
        _identity(record)
        and isinstance(scores, dict)
        and (_replay(scores["replay"]) and _staged(record) if "replay" in scores else _hidden(scores))
        and _valid_steps(record.get("steps"))
        and isinstance(record.get("flags"), list) and all(isinstance(flag, str) for flag in record["flags"])
        and isinstance(record.get("rounds"), dict) and all(isinstance(v, int) for v in record["rounds"].values())
        and (record.get("rep") is None or (_count(record["rep"]) and isinstance(record["rep"], int)))
    )
    return record if valid else None


def _hidden(scores: dict) -> bool:
    """A build scored on a task's hidden suite (and, when it has planted bugs, on them)."""
    return (
        _fraction(scores.get("primary"))
        and isinstance(scores.get("hidden_outcomes"), dict)
        and all(isinstance(v, str) for v in scores["hidden_outcomes"].values())
        and isinstance(scores.get("static"), dict)
        and all(isinstance(scores["static"].get(key), int) for key in _STATIC_KEYS)
        and ("planted" not in scores or _planted(scores["planted"]))
    )


def _staged(record: dict) -> bool:
    """What a replay report needs to date each stage and compare its CLI versions."""
    versions = record.get("cli_versions")
    return (isinstance(record.get("started"), str) and isinstance(record.get("finished"), str)
            and isinstance(versions, dict) and all(isinstance(v, str) for v in versions.values()))


def _replay(replay) -> bool:
    """A build scored on a replay task: `review` is None exactly when the task has no defects (a control)."""
    if not isinstance(replay, dict) or not isinstance(replay.get("defects"), list):
        return False
    defects = replay["defects"]
    return (
        all(isinstance(d, dict) and isinstance(d.get("id"), str) and isinstance(d.get("avoided"), bool)
            for d in defects)
        and (replay.get("review") is None if not defects else _fraction(replay.get("review")))
        and _fraction(replay.get("feature"))
        and _fraction(replay.get("regressions"))
        and isinstance(replay.get("outcomes"), dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in replay["outcomes"].items())
    )


def _planted(planted) -> bool:
    return (
        isinstance(planted, dict)
        and _fraction(planted.get("fraction"))
        and all(isinstance(planted.get(key), list) and all(isinstance(name, str) for name in planted[key])
                for key in ("caught", "missed"))
        and isinstance(planted.get("own_tests"), dict)
        and all(isinstance(planted["own_tests"].get(key), int) for key in _OWN_TEST_KEYS)
    )


def followup(path: Path, digest: str) -> dict | None:
    record = _complete(path, digest)
    if record is None:
        return None
    scores = record.get("scores")
    valid = (
        _identity(record)
        and isinstance(scores, dict)
        and _fraction(scores.get("new_fraction"))
        and all(isinstance(scores.get(key), list) and all(isinstance(i, str) for i in scores[key])
                for key in _FOLLOWUP_LISTS)
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
            if not (isinstance(attempt, dict) and _count(attempt.get("seconds"))
                    and isinstance(attempt.get("usage"), dict)):
                return False
            usage = attempt["usage"]
            if not all(_count(usage[key]) for key in _TOKEN_KEYS if key in usage):
                return False
    return True


def _identity(record: dict) -> bool:
    return all(isinstance(record.get(key), str) for key in _IDENTITY)


def _count(value) -> bool:
    """A finite, non-negative number."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < math.inf


def _fraction(value) -> bool:
    return _count(value) and value <= 1


def _object(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None
