"""Score a build: hidden-suite pass fraction and static code metrics.

The denominator is always the frozen expected test IDs collected from the
reference, never what the candidate's run collected: a candidate whose module
fails to import collects one placeholder per test file, which must not shrink
the denominator.
"""

import ast
import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from . import procs

RUNNER = Path(__file__).with_name("runner.py")
SUITE_SECONDS = 120
RUFF_RULES = "E,F,W,B,C90,SIM,UP"
_COPY_IGNORE = shutil.ignore_patterns(".git", "__pycache__", "*.pyc")
_BRANCH_NODES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.ExceptHandler, ast.match_case)


@dataclass(frozen=True)
class SuiteOutcome:
    outcomes: dict[str, str]
    timed_out: bool

    def passed(self) -> frozenset[str]:
        return frozenset(test_id for test_id, outcome in self.outcomes.items() if outcome == "pass")


@dataclass(frozen=True)
class StaticMetrics:
    ruff_violations: int
    source_loc: int
    test_loc: int
    max_function_branches: int


def run_suite(code_dir: Path, suite_dir: Path, work_dir: Path, *, timeout: float = SUITE_SECONDS) -> SuiteOutcome:
    """Run suite_dir's tests against a copy of code_dir inside work_dir."""
    results = _execute(code_dir, suite_dir, work_dir, timeout=timeout, list_only=False)
    outcomes: dict[str, str] = {}
    for record in results.records:
        outcomes[record["id"]] = record["outcome"]
    return SuiteOutcome(outcomes=outcomes, timed_out=results.timed_out)


def run_in_place(code_dir: Path, work_dir: Path, *, pattern: str = "test*.py",
                 timeout: float = SUITE_SECONDS) -> SuiteOutcome:
    """Run the tests that live in code_dir, from one copy of it that is discovery root, import root
    and cwd at once — what `python3 -m unittest discover` does there."""
    results = _execute(code_dir, None, work_dir, timeout=timeout, list_only=False, pattern=pattern)
    return SuiteOutcome(outcomes={record["id"]: record["outcome"] for record in results.records},
                        timed_out=results.timed_out)


def list_suite(code_dir: Path, suite_dir: Path, work_dir: Path) -> list[str]:
    """Test IDs the suite collects against code_dir (used on references only)."""
    results = _execute(code_dir, suite_dir, work_dir, timeout=SUITE_SECONDS, list_only=True)
    return sorted(record["id"] for record in results.records)


def pass_fraction(outcome: SuiteOutcome, expected: list[str]) -> float:
    if not expected:
        raise ValueError("empty expected test list")
    return len(outcome.passed() & set(expected)) / len(expected)


@dataclass(frozen=True)
class _RunnerOutput:
    records: list[dict]
    timed_out: bool


def _execute(code_dir: Path, suite_dir: Path | None, work_dir: Path, *, timeout: float, list_only: bool,
             pattern: str | None = None) -> _RunnerOutput:
    """Run suite_dir's tests against a copy of code_dir; with no suite_dir, the tests inside code_dir."""
    # Absolute: the runner's cwd is the code copy, where relative paths would not resolve.
    work_dir = work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=False)
    code_copy = work_dir / "code"
    shutil.copytree(code_dir, code_copy, ignore=_COPY_IGNORE)
    suite_copy = code_copy if suite_dir is None else work_dir / "suite"
    if suite_dir is not None:
        shutil.copytree(suite_dir, suite_copy, ignore=_COPY_IGNORE)
    results_path = work_dir / "results.jsonl"
    results_path.touch()
    argv = [sys.executable, "-I", "-B", str(RUNNER), str(suite_copy), str(results_path)]
    if list_only:
        argv.append("--list")
    if pattern:
        argv += ["--pattern", pattern]
    proc = procs.run(argv, cwd=code_copy, timeout=timeout, stdout_path=work_dir / "runner.log")
    records = []
    for line in results_path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            # A kill mid-write leaves a torn last line; that test is unfinished.
            continue
    return _RunnerOutput(records=records, timed_out=proc.timed_out)


def score_build(repo: Path, task, work_dir: Path) -> dict:
    """Primary and static scores of a finished build (task is a tasks.Task)."""
    expected = task.expected()["hidden"]
    outcome = run_suite(repo, task.path("hidden"), work_dir / "hidden")
    return {
        "primary": pass_fraction(outcome, expected),
        "hidden_outcomes": {test_id: outcome.outcomes.get(test_id, "missing") for test_id in expected},
        "hidden_timed_out": outcome.timed_out,
        "static": asdict(static_metrics(repo)),
    }


def static_metrics(code_dir: Path) -> StaticMetrics:
    source_loc = test_loc = max_branches = 0
    for path in sorted(code_dir.rglob("*.py")):
        if ".git" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        loc = sum(1 for line in text.splitlines() if line.strip())
        if _is_test_file(path.relative_to(code_dir)):
            test_loc += loc
        else:
            source_loc += loc
        max_branches = max(max_branches, _max_function_branches(text))
    return StaticMetrics(
        ruff_violations=_ruff_violations(code_dir),
        source_loc=source_loc,
        test_loc=test_loc,
        max_function_branches=max_branches,
    )


def _is_test_file(relative: Path) -> bool:
    return relative.name.startswith("test_") or any(part in ("test", "tests") for part in relative.parts[:-1])


def _max_function_branches(text: str) -> int:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return 0
    best = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            best = max(best, _branches(node))
    return best


def _branches(function: ast.AST) -> int:
    """Branches in this function's own body; nested functions and classes are measured on their own."""
    count = 0
    pending = list(ast.iter_child_nodes(function))
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, _BRANCH_NODES):
            count += 1
        elif isinstance(node, ast.BoolOp):
            count += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            count += len(node.ifs)
        pending.extend(ast.iter_child_nodes(node))
    return count


def _ruff_violations(code_dir: Path) -> int:
    argv = ["ruff", "check", "--isolated", "--no-cache", "--exit-zero", "--select", RUFF_RULES,
            "--output-format", "json", str(code_dir)]
    completed = subprocess.run(argv, capture_output=True, text=True, check=True)
    return len(json.loads(completed.stdout))
