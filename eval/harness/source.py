"""Replay tasks: one historical phase of a git repository, rebuilt from its starting tree.

A replay task's folder holds TASK.md, source.json, defects.json and hidden/:

    source.json   {"url", "base", "pre_review", "reference", "control": bool}
                  base: the run's starting tree; pre_review: the historical writer's build before review;
                  reference: the landed phase.
    defects.json  {"defects": [{"id", "fix_commit", "basis", "tests": [test ids], "crash_is_defect": bool}]}
    hidden/       test_hidden_feature_*.py and test_hidden_review_*.py, each carrying its own fixtures.

The repository is mirrored once (bare) under eval/data/sources/ and trees leave it only through
`git archive`, so no history reaches a run. Every test run uses a scoring tree: the code under test with
its tests/ replaced by the frozen base tests plus the hidden modules, so a build's own test edits never
touch a score. validate writes expected.json: feature ids, review ids, defects with their observed
pre_review failures, and the regression set.
"""

import hashlib
import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import score

SOURCES_ROOT = Path(__file__).resolve().parents[1] / "data" / "sources"
TESTS = "tests"
FEATURE_PREFIX = "test_hidden_feature_"
REVIEW_PREFIX = "test_hidden_review_"
ANCHORS = ("base", "pre_review", "reference")
# The base suite of a real repo runs for minutes, and a hung test only dies at the runner's per-test alarm.
SUITE_SECONDS = 900
_MIRROR_LOCK = threading.Lock()
# Validate's third round runs every anchor this many times at once: the scored run's load, roughly.
CONCURRENT_COPIES = 2
# A review test that errors for these reasons on pre_review never reached the defect.
NAME_ERRORS = frozenset({"ImportError", "ModuleNotFoundError", "AttributeError", "NameError", "TypeError"})


class SourceError(Exception):
    pass


@dataclass(frozen=True)
class Source:
    url: str
    base: str
    pre_review: str
    reference: str
    control: bool = False

    @classmethod
    def load(cls, path: Path) -> "Source":
        data = json.loads(path.read_text(encoding="utf-8"))
        control = data.get("control", False)
        if not isinstance(control, bool):
            raise SourceError(f"{path}: control must be true or false, not {control!r}")
        return cls(url=data["url"], base=data["base"], pre_review=data["pre_review"],
                   reference=data["reference"], control=control)

    def commit(self, anchor: str) -> str:
        return {"base": self.base, "pre_review": self.pre_review, "reference": self.reference}[anchor]


def mirror(source: Source) -> Path:
    """The bare mirror holding every anchor commit; fetched once, refreshed only when a commit is missing.
    Concurrent runs of one source share it, so they take turns preparing it."""
    git_dir = SOURCES_ROOT / (hashlib.sha256(source.url.encode()).hexdigest()[:16] + ".git")
    with _MIRROR_LOCK:
        if not git_dir.exists():
            SOURCES_ROOT.mkdir(parents=True, exist_ok=True)
            _git(None, "clone", "--quiet", "--bare", source.url, str(git_dir))
        if _missing(git_dir, source):
            _git(git_dir, "fetch", "--quiet", "origin", "+refs/heads/*:refs/heads/*")
        missing = _missing(git_dir, source)
    if missing:
        raise SourceError(f"{source.url} has no commit {', '.join(missing)}")
    return git_dir


def export(source: Source, commit: str, dest: Path, *paths: str) -> None:
    """Write commit's tracked tree (or just `paths` of it) to dest, without history."""
    archive = _git(mirror(source), "archive", "--format=tar", commit, *paths, text=False)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")


def tracked_files(source: Source, commit: str) -> set[str]:
    return set(_git(mirror(source), "ls-tree", "-r", "--name-only", commit).splitlines())


def seed(task, dest: Path) -> None:
    """A run's starting repo: base's tracked tree plus TASK.md."""
    export(task.source, task.source.base, dest)
    shutil.copy2(task.path("TASK.md"), dest / "TASK.md")


def scoring_tree(task, dest: Path, *, code: Path | None = None, commit: str | None = None) -> Path:
    """Code from a build directory or an anchor commit, its tests/ replaced by base's tests and the hidden
    modules."""
    if code is not None:
        shutil.copytree(code, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    else:
        export(task.source, commit, dest)
    shutil.rmtree(dest / TESTS, ignore_errors=True)
    if TESTS in {Path(p).parts[0] for p in tracked_files(task.source, task.source.base)}:
        export(task.source, task.source.base, dest, TESTS)
    (dest / TESTS).mkdir(exist_ok=True)
    for module in sorted(task.path("hidden").glob("test_hidden_*.py")):
        shutil.copy2(module, dest / TESTS / module.name)
    return dest


def hidden_ids(ids: list[str]) -> tuple[list[str], list[str]]:
    """Feature and review test ids, by the module they live in."""
    def module(test_id: str) -> str:
        return test_id.split(".")[1] if test_id.startswith(f"{TESTS}.") else ""
    return ([i for i in ids if module(i).startswith(FEATURE_PREFIX)],
            [i for i in ids if module(i).startswith(REVIEW_PREFIX)])


def score_build(artifact: Path, task, work_dir: Path) -> dict:
    """The replay scores of a finished build: defects avoided, feature tests passed, regressions."""
    expected = task.expected()
    wanted = expected["feature"] + expected["review"] + expected["regression"]
    tree = scoring_tree(task, work_dir / "tree", code=artifact)
    outcome = score.run_package(tree, TESTS, work_dir / "run", ids=wanted, timeout=SUITE_SECONDS)
    passed = outcome.passed()
    defects = [{"id": d["id"], "avoided": all(t in passed for t in d["tests"])} for d in expected["defects"]]
    regression = expected["regression"]
    return {
        "replay": {
            "review": sum(d["avoided"] for d in defects) / len(defects) if defects else None,
            "feature": len(passed & set(expected["feature"])) / len(expected["feature"]),
            "regressions": (len(set(regression) - passed) / len(regression)) if regression else 0.0,
            "defects": defects,
            "outcomes": {test_id: outcome.outcomes.get(test_id, "missing") for test_id in wanted},
            "timed_out": outcome.timed_out,
        },
    }


def validate(task) -> list[str]:
    """Problems with a replay task; when there are none, write expected.json."""
    missing = [n for n in ("TASK.md", "source.json", "defects.json", "hidden") if not task.path(n).exists()]
    if missing:
        return [f"{task.id}: missing {', '.join(missing)}"]
    with tempfile.TemporaryDirectory(prefix="eval-validate-") as tmp:
        try:
            return _validate_in(task, Path(tmp))
        except SourceError as exc:
            return [f"{task.id}: {exc}"]


def _validate_in(task, work: Path) -> list[str]:
    source = task.source
    defects = json.loads(task.path("defects.json").read_text(encoding="utf-8"))["defects"]
    problems = _seed_problems(task, work / "seed")
    trees = {anchor: scoring_tree(task, work / "trees" / anchor, commit=source.commit(anchor)) for anchor in ANCHORS}
    listed = score.list_package(trees["reference"], TESTS, work / "list")
    feature, review = hidden_ids(listed)
    problems += _shape_problems(task, listed, feature, review, defects)
    if problems:
        return problems
    rounds = _anchor_rounds(trees, work / "runs")
    flaky = sorted({test_id for anchor in ANCHORS for test_id in _varying(rounds[anchor])})
    problems += [f"{task.id}: flaky test {test_id}" for test_id in flaky if test_id in feature + review]
    stable = {anchor: rounds[anchor][0] for anchor in ANCHORS}
    problems += _anchor_problems(task, stable, feature, defects, rounds["pre_review"])
    if problems:
        return problems
    base_suite = sorted(set(listed) - set(feature) - set(review))
    regression = [t for t in base_suite if t not in flaky
                  and stable["base"].outcomes.get(t) == "pass" and stable["reference"].outcomes.get(t) == "pass"]
    expected = {
        "feature": feature,
        "review": review,
        "defects": [{"id": d["id"], "tests": d["tests"],
                     "pre_review": {t: _evidence(stable["pre_review"], t) for t in d["tests"]}} for d in defects],
        "regression": regression,
        "flaky_excluded": [t for t in flaky if t in base_suite],
    }
    task.path("expected.json").write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    return []


def _seed_problems(task, dest: Path) -> list[str]:
    seed(task, dest)
    present = {str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file() or p.is_symlink()}
    expected = tracked_files(task.source, task.source.base) | {"TASK.md"}
    problems = [f"{task.id}: seed holds history"] if (dest / ".git").exists() else []
    if present != expected:
        extra, lacking = sorted(present - expected)[:5], sorted(expected - present)[:5]
        problems.append(f"{task.id}: seed differs from base's tree (extra {extra}, missing {lacking})")
    return problems


def _shape_problems(task, listed: list[str], feature: list[str], review: list[str], defects: list[dict]) -> list[str]:
    problems = [f"{task.id}: a module fails to load: {t}" for t in listed if t.startswith(score.PLACEHOLDER_PREFIX)]
    if not feature:
        problems.append(f"{task.id}: empty feature group")
    if not task.source.control and not review:
        problems.append(f"{task.id}: a caught task needs review tests")
    claimed = [t for d in defects for t in d["tests"]]
    if sorted(claimed) != sorted(review) or len(set(claimed)) != len(claimed):
        problems.append(f"{task.id}: defects.json must claim each review test exactly once")
    if any(not d["tests"] for d in defects):
        problems.append(f"{task.id}: a defect with no tests")
    return problems


def _anchor_rounds(trees: dict[str, Path], work: Path) -> dict[str, list[score.SuiteOutcome]]:
    """Three rounds per anchor: two runs in turn, then a third round of CONCURRENT_COPIES copies of every
    anchor at once, the scored run's concurrency."""
    rounds: dict[str, list[score.SuiteOutcome]] = {anchor: [] for anchor in ANCHORS}
    for n in (1, 2):
        for anchor in ANCHORS:
            rounds[anchor].append(score.run_package(trees[anchor], TESTS, work / f"{anchor}-{n}", detail=True,
                                                    timeout=SUITE_SECONDS))
    copies = [(anchor, copy) for anchor in ANCHORS for copy in range(CONCURRENT_COPIES)]

    def run_copy(anchor: str, copy: int) -> score.SuiteOutcome:
        return score.run_package(trees[anchor], TESTS, work / f"{anchor}-3-{copy}", detail=True,
                                 timeout=SUITE_SECONDS)

    with ThreadPoolExecutor(len(copies)) as pool:
        outcomes = pool.map(lambda job: run_copy(*job), copies)
        for (anchor, _), outcome in zip(copies, outcomes):
            rounds[anchor].append(outcome)
    return rounds


def _varying(runs: list[score.SuiteOutcome]) -> set[str]:
    ids = set().union(*(run.outcomes for run in runs))
    return {t for t in ids if len({run.outcomes.get(t, "missing") for run in runs}) > 1}


def _anchor_problems(task, stable: dict[str, score.SuiteOutcome], feature: list[str], defects: list[dict],
                     pre_review_runs: list[score.SuiteOutcome]) -> list[str]:
    """Attribution must hold on every pre_review run, not just the first: same outcome, same stage, same kind."""
    problems = []
    hidden = feature + [t for d in defects for t in d["tests"]]
    for test_id in hidden:
        if stable["reference"].outcomes.get(test_id) != "pass":
            problems.append(f"{task.id}: fails on reference: {test_id}")
    for test_id in feature:
        if stable["base"].outcomes.get(test_id) == "pass":
            problems.append(f"{task.id}: feature test passes on base: {test_id}")
    for defect in defects:
        for test_id in defect["tests"]:
            reason = next(filter(None, (_attribution_problem(run, test_id, defect.get("crash_is_defect", False))
                                        for run in pre_review_runs)), None)
            if reason:
                problems.append(f"{task.id}: review test {test_id} ({defect['id']}) {reason}")
    return problems


def _attribution_problem(pre_review: score.SuiteOutcome, test_id: str, crash_is_defect: bool) -> str | None:
    """Why a review test's pre_review run does not show the defect, or None when it does."""
    outcome = pre_review.outcomes.get(test_id, "missing")
    detail = pre_review.details.get(test_id, {})
    if outcome == "pass":
        return "passes on pre_review"
    if outcome == "fail" and detail.get("stage") == "call":
        return None
    if outcome == "error" and crash_is_defect and detail.get("stage") == "call" and detail.get("exc") not in NAME_ERRORS:
        return None
    return f"does not reach the defect on pre_review ({outcome}, {detail.get('stage')}, {detail.get('exc')})"


def _evidence(outcome: score.SuiteOutcome, test_id: str) -> dict:
    detail = outcome.details.get(test_id, {})
    return {"outcome": outcome.outcomes.get(test_id, "missing"), "exc": detail.get("exc"),
            "stage": detail.get("stage"), "trace": detail.get("trace", "")[-1500:]}


def _missing(git_dir: Path, source: Source) -> list[str]:
    absent = []
    for anchor in ANCHORS:
        commit = source.commit(anchor)
        probe = subprocess.run(["git", "--git-dir", str(git_dir), "cat-file", "-e", f"{commit}^{{commit}}"],
                               capture_output=True)
        if probe.returncode != 0:
            absent.append(f"{anchor} {commit}")
    return absent


# Git with none of the host's configuration or attributes, so an export is the commit's tree on any machine.
_GIT_ENV = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ATTR_NOSYSTEM": "1", "HOME": "/nonexistent", "GIT_TERMINAL_PROMPT": "0"}


def _git(git_dir: Path | None, *args: str, text: bool = True):
    argv = (["git", "-c", "core.attributesFile=/dev/null"] + (["--git-dir", str(git_dir)] if git_dir else [])
            + list(args))
    completed = subprocess.run(argv, capture_output=True, text=text, env=_GIT_ENV)
    if completed.returncode != 0:
        error = completed.stderr if text else completed.stderr.decode(errors="replace")
        raise SourceError(f"git {' '.join(args[:2])} failed: {error.strip()}")
    return completed.stdout
