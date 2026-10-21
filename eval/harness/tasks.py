"""Eval tasks on disk and the validate gate that freezes their expected test IDs.

Layout of tasks/<id>/:
    TASK.md, FOLLOWUP.md        writer-visible
    traps.md                    private: each trap and assertion group -> TASK.md clause
    hidden/, hidden_followup/   unittest suites (follow-up suite holds new behaviour only)
    reference/, reference_followup/
    wrong/<variant>/, wrong_followup/<variant>/
    degraded/                   reference with injected quality defects, behaviour unchanged (judge calibration)
    expected.json               written by validate: collected test IDs per suite
"""

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import score

TASKS_ROOT = Path(__file__).resolve().parent.parent / "tasks"
_PLACEHOLDER_PREFIX = "unittest.loader._FailedTest"


@dataclass(frozen=True)
class Task:
    root: Path

    @property
    def id(self) -> str:
        return self.root.name

    @property
    def is_dev(self) -> bool:
        return self.id.startswith("dev-")

    def path(self, name: str) -> Path:
        return self.root / name

    def variants(self, kind: str) -> list[Path]:
        folder = self.root / kind
        return sorted(p for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []

    def expected(self) -> dict[str, list[str]]:
        return json.loads(self.path("expected.json").read_text(encoding="utf-8"))


def load_tasks(root: Path = TASKS_ROOT) -> list[Task]:
    return [Task(p) for p in sorted(root.iterdir()) if p.is_dir() and not p.name.startswith(".")]


def validate(task: Task) -> list[str]:
    """Return the task's problems; when there are none, write expected.json."""
    missing = [name for name in ("TASK.md", "FOLLOWUP.md", "traps.md", "hidden", "hidden_followup",
                                 "reference", "reference_followup", "degraded") if not task.path(name).exists()]
    if missing:
        return [f"{task.id}: missing {', '.join(missing)}"]
    if not task.variants("wrong") or not task.variants("wrong_followup"):
        return [f"{task.id}: needs at least one wrong/ and one wrong_followup/ variant"]
    with tempfile.TemporaryDirectory(prefix="eval-validate-") as tmp:
        return _validate_in(task, _WorkDirs(Path(tmp)))


class _WorkDirs:
    def __init__(self, root: Path):
        self._root, self._n = root, 0

    def next(self) -> Path:
        self._n += 1
        return self._root / f"w{self._n}"


def _validate_in(task: Task, work: _WorkDirs) -> list[str]:
    hidden, followup = task.path("hidden"), task.path("hidden_followup")
    reference, reference_followup = task.path("reference"), task.path("reference_followup")
    expected = {
        "hidden": score.list_suite(reference, hidden, work.next()),
        "hidden_followup": score.list_suite(reference_followup, followup, work.next()),
    }
    problems = [f"{task.id}: {suite} collected no tests or a load failure"
                for suite, ids in expected.items()
                if not ids or any(i.startswith(_PLACEHOLDER_PREFIX) for i in ids)]
    if problems:
        return problems

    def fraction(code: Path, suite: Path, suite_name: str) -> float:
        return score.pass_fraction(score.run_suite(code, suite, work.next()), expected[suite_name])

    checks = [
        ("reference passes hidden", fraction(reference, hidden, "hidden") == 1.0),
        ("reference_followup passes hidden", fraction(reference_followup, hidden, "hidden") == 1.0),
        ("reference_followup passes hidden_followup", fraction(reference_followup, followup, "hidden_followup") == 1.0),
        ("reference fails every follow-up test", fraction(reference, followup, "hidden_followup") == 0.0),
        ("degraded passes hidden", fraction(task.path("degraded"), hidden, "hidden") == 1.0),
    ]
    for variant in task.variants("wrong"):
        checks.append((f"wrong/{variant.name} fails hidden", fraction(variant, hidden, "hidden") < 1.0))
    for variant in task.variants("wrong_followup"):
        checks.append((f"wrong_followup/{variant.name} fails a suite",
                       fraction(variant, hidden, "hidden") < 1.0
                       or fraction(variant, followup, "hidden_followup") < 1.0))
    problems = [f"{task.id}: check failed — {name}" for name, ok in checks if not ok]
    if not problems:
        task.path("expected.json").write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    return problems
