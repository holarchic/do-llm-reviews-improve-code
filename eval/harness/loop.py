"""One eval run: a task built by one arm, from an empty repo to a scored build.

The harness, not the model, sequences the loop. Each step is one fresh
headless session; after every writer step the harness commits, so HEAD always
holds the latest attempt and an infrastructure retry restarts from it.
Terminal rules are the pre-registered ones in PREREG.md.
"""

import io
import json
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from . import agents, audit, procs, prompts, score
from .tasks import Task

MAX_ATTEMPTS = 3
MAX_ROUNDS = 5
REVIEW_PASSES = ("correctness", "architecture")
# The operator's git config (signing, hooks, templates) stays out of run repos.
_GIT_ENV = {"GIT_AUTHOR_NAME": "eval", "GIT_AUTHOR_EMAIL": "eval@localhost",
            "GIT_COMMITTER_NAME": "eval", "GIT_COMMITTER_EMAIL": "eval@localhost",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
_REPO_GITIGNORE = "__pycache__/\n*.pyc\n"


@dataclass(frozen=True)
class Arm:
    name: str
    reviewer: str | None  # None = solo; else "claude" or "codex"


ARMS = {
    "A1": Arm("A1", None),
    "A1r": Arm("A1r", None),
    "A2": Arm("A2", "claude"),
    "A3": Arm("A3", "codex"),
}


@dataclass(frozen=True)
class Verdict:
    label: str
    blocking: bool
    malformed: bool
    text: str


class InfraExhausted(Exception):
    pass


class ModelTimeout(Exception):
    """A writer or reviewer ran out of wall clock; the run stops and HEAD is scored."""


@dataclass
class _Run:
    repo: Path
    exec_dir: Path
    arm: Arm
    last_commit: str = ""
    steps: list[dict] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    rounds: dict[str, int] = field(default_factory=dict)


def parse_verdict(text: str) -> Verdict:
    """A missing verdict line is malformed and counts as YELLOW. LOW-only findings never block;
    a non-GREEN verdict without any severity tags can't be read as LOW-only, so it blocks."""
    match = re.search(r"^\W*VERDICT:\W*(GREEN|YELLOW|RED)\b", text, flags=re.MULTILINE)
    if match is None:
        return Verdict("YELLOW", True, True, text)
    label = match.group(1)
    tags = re.findall(r"\[(HIGH|MEDIUM|LOW)\]", text)
    blocking = label != "GREEN" and (not tags or any(tag != "LOW" for tag in tags))
    return Verdict(label, blocking, False, text)


def execute_run(task: Task, arm_name: str, runs_root: Path, *, manifest_hash: str | None) -> dict:
    arm = ARMS[arm_name]
    run_id = f"{task.id}--{arm.name}"
    exec_dir = _next_exec_dir(runs_root.resolve() / run_id)
    run = _Run(repo=_anonymous_workdir() / "repo", exec_dir=exec_dir, arm=arm)
    record = {"run_id": run_id, "task": task.id, "arm": arm.name, "exec": exec_dir.name,
              "manifest": manifest_hash, "cli_versions": _cli_versions(), "started": _now()}

    def work():
        _init_repo(run, task)
        (_loop if arm.reviewer else _solo)(run)

    def scores(artifact: Path) -> dict:
        return score.score_build(artifact, task, exec_dir / "scoring")

    _execute(run, record, work, scores)
    record.update(rounds=run.rounds)
    (exec_dir / "record.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def execute_followup(task: Task, build_exec_dir: Path, *, manifest_hash: str | None) -> dict:
    """A fresh solo writer makes the FOLLOWUP.md change in a copy of a finished build.

    Regressions are base test IDs that passed on the build and fail after the change;
    tests that failed before are reported apart, so a weak build isn't penalised twice.
    """
    build_exec_dir = build_exec_dir.resolve()
    build = json.loads((build_exec_dir / "record.json").read_text(encoding="utf-8"))
    out_dir = build_exec_dir / "followup"
    out_dir.mkdir()
    run = _Run(repo=_anonymous_workdir() / "repo", exec_dir=out_dir, arm=ARMS[build["arm"]])
    record = {"run_id": build["run_id"], "task": task.id, "arm": build["arm"], "build_exec": build_exec_dir.name,
              "manifest": manifest_hash, "cli_versions": _cli_versions(), "started": _now()}

    def work():
        shutil.copytree(build_exec_dir / "repo", run.repo, symlinks=True)
        # The follow-up starts from the build's final commit, not from files it left untracked.
        _reset(run.repo, "HEAD")
        shutil.copy2(task.path("FOLLOWUP.md"), run.repo / "FOLLOWUP.md")
        run.last_commit = _commit(run.repo, "followup task")
        _writer(run, "followup", prompts.followup_writer())

    def scores(artifact: Path) -> dict:
        return _followup_scores(task, build["scores"]["hidden_outcomes"], artifact, out_dir)

    _execute(run, record, work, scores)
    (out_dir / "record.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def _execute(run: "_Run", record: dict, work, scores) -> None:
    """Run the work, keep the repo, then score the final commit's files (`artifact/`).

    Any failure — retries exhausted, a harness exception, a scoring crash — makes the
    record invalid; a harness failure is never a model outcome.
    """
    try:
        work()
        record["status"] = "complete"
    except ModelTimeout:
        # Pre-registered: a writer or reviewer timeout ends the run and HEAD is scored.
        record["status"] = "complete"
    except InfraExhausted as exc:
        record.update(status="invalid", invalid_reason=str(exc))
    except Exception:
        record.update(status="invalid", invalid_reason="harness error:\n" + traceback.format_exc())
    try:
        if run.repo.exists():
            shutil.copytree(run.repo, run.exec_dir / "repo", symlinks=True)
    except Exception:
        record.update(status="invalid", invalid_reason="preserving the repo failed:\n" + traceback.format_exc())
    finally:
        shutil.rmtree(run.repo.parent, ignore_errors=True)
    if record["status"] == "complete":
        try:
            artifact = run.exec_dir / "artifact"
            materialize_head(run.exec_dir / "repo", artifact)
            record["scores"] = scores(artifact)
        except Exception:
            record.update(status="invalid", invalid_reason="scoring error:\n" + traceback.format_exc())
    record.update(finished=_now(), flags=run.flags, steps=run.steps)


def materialize_head(repo: Path, dest: Path) -> None:
    """Write the tracked files of repo's HEAD commit to dest: what the run actually committed."""
    archive = subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=repo, env=_git_env(),
                             capture_output=True, check=True).stdout
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")


def _anonymous_workdir() -> Path:
    # A neutral name: the path a session sees must not reveal its task or arm.
    path = audit.RUNS_ROOT / secrets.token_hex(8)
    path.mkdir(parents=True)
    return path


def _followup_scores(task: Task, before: dict[str, str], artifact: Path, out_dir: Path) -> dict:
    expected = task.expected()
    base = score.run_suite(artifact, task.path("hidden"), out_dir / "scoring" / "hidden")
    new = score.run_suite(artifact, task.path("hidden_followup"), out_dir / "scoring" / "followup")
    passed_before = {test_id for test_id, outcome in before.items() if outcome == "pass"}
    passed_after = base.passed()
    base_ids = set(expected["hidden"])
    return {
        "new_fraction": score.pass_fraction(new, expected["hidden_followup"]),
        "regressions": sorted(passed_before - passed_after),
        "persistent_failures": sorted(base_ids - passed_before - passed_after),
        "repaired": sorted((passed_after & base_ids) - passed_before),
        "base_fraction_after": score.pass_fraction(base, expected["hidden"]),
    }


def latest_complete_exec(runs_root: Path, run_id: str) -> Path:
    """The newest exec of run_id whose record is complete."""
    execs = sorted((runs_root / run_id).glob("exec-*"), key=lambda p: int(p.name.split("-")[1]), reverse=True)
    for exec_dir in execs:
        record = exec_dir / "record.json"
        if record.exists() and json.loads(record.read_text(encoding="utf-8")).get("status") == "complete":
            return exec_dir
    raise FileNotFoundError(f"no complete exec for {run_id}")


def _solo(run: _Run) -> None:
    _writer(run, "build", prompts.solo_writer())


def _loop(run: _Run) -> None:
    _writer(run, "plan", prompts.loop_plan_writer())
    for round_no in range(1, MAX_ROUNDS + 1):
        run.rounds["plan"] = round_no
        verdict = _review(run, f"plan-review-{round_no}", prompts.plan_review())
        if not verdict.blocking:
            break
        if round_no == MAX_ROUNDS:
            run.flags.append("plan_capped")
            break
        _writer(run, f"plan-revise-{round_no}", prompts.loop_plan_revision_writer(verdict.text))

    base = run.last_commit
    _writer(run, "build", prompts.loop_build_writer())
    for round_no in range(1, MAX_ROUNDS + 1):
        run.rounds["phase"] = round_no
        commits = _git(run.repo, "log", "--oneline", f"{base}..HEAD")
        final_state = _own_tests_summary(run)
        verdicts = {
            name: _review(run, f"{name}-review-{round_no}",
                          prompts.code_review(name, base, commits=commits, final_state=final_state))
            for name in REVIEW_PASSES
        }
        if not any(v.blocking for v in verdicts.values()):
            break
        if round_no == MAX_ROUNDS:
            run.flags.append("review_capped")
            break
        _writer(run, f"fix-{round_no}", prompts.loop_fix_writer({n: v.text for n, v in verdicts.items()}))


def _writer(run: _Run, name: str, prompt: str) -> None:
    session = _step(run, name, lambda out: agents.run_claude(
        agents.WRITER_MODEL, prompt, role=agents.WRITER, cwd=run.repo, out_dir=out))
    run.last_commit = _commit(run.repo, name)
    if session.terminal == agents.TIMEOUT:
        run.flags.append(f"timeout:{name}")
        raise ModelTimeout(name)


def _review(run: _Run, name: str, prompt: str) -> Verdict:
    if run.arm.reviewer == "claude":
        def call(out):
            return agents.run_claude(agents.SAME_REVIEWER_MODEL, prompt, role=agents.REVIEWER,
                                     cwd=run.repo, out_dir=out)
    else:
        def call(out):
            return agents.run_codex(agents.CROSS_REVIEWER_MODEL, prompt, role=agents.REVIEWER,
                                    cwd=run.repo, out_dir=out)
    session = _step(run, name, call)
    if _git(run.repo, "status", "--porcelain"):
        run.flags.append(f"reviewer_wrote:{name}")
        _reset(run.repo, run.last_commit)
    if session.terminal == agents.TIMEOUT:
        run.flags.append(f"timeout:{name}")
        raise ModelTimeout(name)
    verdict = parse_verdict(session.final_text)
    if verdict.malformed:
        run.flags.append(f"malformed_verdict:{name}")
    run.steps[-1]["verdict"] = {"label": verdict.label, "blocking": verdict.blocking, "malformed": verdict.malformed}
    return verdict


def _step(run: _Run, name: str, call) -> agents.Session:
    step_dir = run.exec_dir / "steps" / f"{len(run.steps):02d}-{name}"
    attempts = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        session = guarded_call(call, step_dir / f"attempt-{attempt}")
        attempts.append(session.to_record())
        if session.terminal != agents.INFRA:
            run.steps.append({"name": name, "attempts": attempts})
            return session
        _reset(run.repo, run.last_commit)
    run.steps.append({"name": name, "attempts": attempts})
    raise InfraExhausted(f"{name}: {session.reason}")


def guarded_call(call, out_dir: Path) -> agents.Session:
    """A session that raises is an infrastructure attempt like any other, so it is recorded and retried."""
    try:
        return call(out_dir)
    except Exception:
        out_dir.mkdir(parents=True, exist_ok=True)
        return agents.Session(agents.INFRA, "harness exception:\n" + traceback.format_exc(), "",
                              out_dir / "transcript.jsonl", 0.0)


def _own_tests_summary(run: _Run) -> str:
    """The builder's own test run, reported to reviewers the way a builder reports Final state."""
    log = run.exec_dir / "own-tests.log"
    result = procs.run([sys.executable, "-B", "-m", "unittest", "discover"], cwd=run.repo,
                       timeout=score.SUITE_SECONDS, stdout_path=log)
    tail = [line for line in log.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.startswith(("Ran ", "OK", "FAILED"))]
    outcome = "timed out" if result.timed_out else " / ".join(tail) or f"exit {result.exit_code}, no summary"
    return f"- Builder's own tests (`python3 -m unittest discover`): {outcome}"


def _init_repo(run: _Run, task: Task) -> None:
    run.repo.mkdir(parents=True)
    shutil.copy2(task.path("TASK.md"), run.repo / "TASK.md")
    (run.repo / ".gitignore").write_text(_REPO_GITIGNORE, encoding="utf-8")
    _git(run.repo, "init", "-q")
    run.last_commit = _commit(run.repo, "task")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "--no-verify", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _reset(repo: Path, commit: str) -> None:
    _git(repo, "reset", "-q", "--hard", commit)
    _git(repo, "clean", "-q", "-fdx")


def _git_env() -> dict[str, str]:
    return {"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(Path.home()), **_GIT_ENV}


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=repo, env=_git_env(),
                               capture_output=True, text=True, check=True)
    return completed.stdout


def _cli_versions() -> dict[str, str]:
    versions = {}
    for cli in ("claude", "codex"):
        completed = subprocess.run([cli, "--version"], capture_output=True, text=True)
        versions[cli] = completed.stdout.strip()
    return versions


def _next_exec_dir(run_root: Path) -> Path:
    run_root.mkdir(parents=True, exist_ok=True)
    n = len([p for p in run_root.iterdir() if p.name.startswith("exec-")]) + 1
    path = run_root / f"exec-{n}"
    path.mkdir()
    return path


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
