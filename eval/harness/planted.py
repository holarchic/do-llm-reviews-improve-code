"""Planted bugs: the share of a task's known bugs that a build's own tests catch.

The bugs live in copies of the reference (`mutants/<name>/`), behind the
interface TASK.md fixes, so every build faces the same bugs. Each run overlays
an implementation — the reference, or one mutant — onto a fresh copy of the
build: the reference's files replace the build's, while the build's tests,
extra modules and data files stay. The build's tests then run as
`python3 -m unittest discover` would run them there.

Only tests that pass on the reference count: a test that fails there is
incompatible with the reference (it reaches into the build's internals, or is
wrong), and failing on a mutant would prove nothing. So the score measures the
build's tests that transfer to the reference's public interface; a build whose
tests only exercise its own helper modules catches nothing.
"""

import shutil
from pathlib import Path

from . import score
from .tasks import PLACEHOLDER_PREFIX, Task


def score_build(artifact: Path, task: Task, work_dir: Path) -> dict:
    reference = _run(artifact, task.path("reference"), work_dir / "reference")
    eligible = reference.passed()
    caught, missed = [], []
    for mutant in task.variants("mutants"):
        outcome = _run(artifact, mutant, work_dir / "mutants" / mutant.name)
        found = any(outcome.outcomes.get(test_id) != "pass" for test_id in eligible)
        (caught if found else missed).append(mutant.name)
    load_errors = sum(1 for test_id in reference.outcomes if test_id.startswith(PLACEHOLDER_PREFIX))
    return {
        "fraction": len(caught) / (len(caught) + len(missed)),
        "caught": caught,
        "missed": missed,
        "own_tests": {
            "collected": len(reference.outcomes) - load_errors,
            "pass_on_reference": len(eligible),
            "fail_on_reference": len(reference.outcomes) - load_errors - len(eligible),
            "load_errors": load_errors,
        },
    }


def _run(artifact: Path, implementation: Path, work_dir: Path) -> score.SuiteOutcome:
    overlay = work_dir / "overlay"
    shutil.copytree(artifact, overlay, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    for source in implementation.rglob("*"):
        if source.is_file() and "__pycache__" not in source.parts:
            target = overlay / source.relative_to(implementation)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return score.run_in_place(overlay, work_dir / "run")
