"""Eval tasks on disk and the validate gate that freezes their expected test IDs.

Layout of tasks/<id>/:
    TASK.md                     writer-visible
    traps.md                    private: each trap and assertion group -> TASK.md clause
    hidden/, reference/, wrong/<variant>/
    expected.json               written by validate: collected test IDs per suite
Optional parts, each checked when present and required by the experiments that use it:
    FOLLOWUP.md, hidden_followup/, reference_followup/, wrong_followup/<variant>/
                                the follow-up group, all or nothing (follow-up arms)
    degraded/                   reference with injected quality defects, behaviour unchanged (judge calibration)
    mutants/<name>/             planted bugs: reference copies with one bug each (planted-bug score)
"""

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import score

TASKS_ROOT = Path(__file__).resolve().parent.parent / "tasks"
PLACEHOLDER_PREFIX = "unittest.loader._FailedTest"
FOLLOWUP_PARTS = ("FOLLOWUP.md", "hidden_followup", "reference_followup", "wrong_followup")
MIN_MUTANTS = 8
# A planted bug must be subtle: caught by the hidden suite, but not by most of it.
MUTANT_MIN_PASS = 0.5


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

    @property
    def has_followup(self) -> bool:
        return self.path("FOLLOWUP.md").exists()


def missing_parts(task: Task, *, followup: bool, degraded: bool, mutants: bool) -> list[str]:
    """The optional parts an experiment needs that this task lacks."""
    needs = {"follow-up group": (followup, task.has_followup), "degraded/": (degraded, task.path("degraded").is_dir()),
             "mutants/": (mutants, bool(task.variants("mutants"))), "expected.json": (True, task.path("expected.json").exists())}
    return [f"{task.id}: no {name}" for name, (needed, present) in needs.items() if needed and not present]


def load_tasks(root: Path = TASKS_ROOT) -> list[Task]:
    return [Task(p) for p in sorted(root.iterdir()) if p.is_dir() and not p.name.startswith(".")]


def validate(task: Task) -> list[str]:
    """Return the task's problems; when there are none, write expected.json."""
    missing = [name for name in ("TASK.md", "traps.md", "hidden", "reference") if not task.path(name).exists()]
    followup = [name for name in FOLLOWUP_PARTS if task.path(name).exists()]
    if followup and len(followup) != len(FOLLOWUP_PARTS):
        missing += [name for name in FOLLOWUP_PARTS if name not in followup]
    if missing:
        return [f"{task.id}: missing {', '.join(missing)}"]
    if not task.variants("wrong") or (followup and not task.variants("wrong_followup")):
        return [f"{task.id}: needs at least one wrong/ variant (and wrong_followup/ with a follow-up)"]
    if task.path("mutants").exists() and len(task.variants("mutants")) < MIN_MUTANTS:
        return [f"{task.id}: needs at least {MIN_MUTANTS} mutants"]
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
    expected = {"hidden": score.list_suite(reference, hidden, work.next())}
    if task.has_followup:
        expected["hidden_followup"] = score.list_suite(reference_followup, followup, work.next())
    problems = [f"{task.id}: {suite} collected no tests or a load failure"
                for suite, ids in expected.items()
                if not ids or any(i.startswith(PLACEHOLDER_PREFIX) for i in ids)]
    if problems:
        return problems

    def fraction(code: Path, suite: Path, suite_name: str) -> float:
        return score.pass_fraction(score.run_suite(code, suite, work.next()), expected[suite_name])

    checks = [("reference passes hidden", fraction(reference, hidden, "hidden") == 1.0)]
    if task.has_followup:
        checks += [
            ("reference_followup passes hidden", fraction(reference_followup, hidden, "hidden") == 1.0),
            ("reference_followup passes hidden_followup",
             fraction(reference_followup, followup, "hidden_followup") == 1.0),
            ("reference fails every follow-up test", fraction(reference, followup, "hidden_followup") == 0.0),
        ]
    if task.path("degraded").exists():
        checks.append(("degraded passes hidden", fraction(task.path("degraded"), hidden, "hidden") == 1.0))
    for variant in task.variants("wrong"):
        checks.append((f"wrong/{variant.name} fails hidden", fraction(variant, hidden, "hidden") < 1.0))
    for variant in task.variants("wrong_followup"):
        checks.append((f"wrong_followup/{variant.name} fails a suite",
                       fraction(variant, hidden, "hidden") < 1.0
                       or fraction(variant, followup, "hidden_followup") < 1.0))
    for variant in task.variants("mutants"):
        passed = fraction(variant, hidden, "hidden")
        checks.append((f"mutants/{variant.name} fails hidden but passes at least {MUTANT_MIN_PASS:.0%} of it",
                       MUTANT_MIN_PASS <= passed < 1.0))
    problems = [f"{task.id}: check failed — {name}" for name, ok in checks if not ok]
    if not problems:
        task.path("expected.json").write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    return problems
