"""One eval run: a task built by one arm, from an empty repo to a scored build.

The harness, not the model, sequences the loop. Each step is one fresh
headless session; after every writer step the harness commits, so HEAD always
holds the latest attempt and an infrastructure retry restarts from it.
Terminal rules are the pre-registered ones in PREREG.md.
"""

import json
import re
import secrets
import shutil
import subprocess
import sys
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
    exec_dir = _next_exec_dir(runs_root / run_id)
    work = audit.RUNS_ROOT / f"{run_id}-{secrets.token_hex(4)}"
    run = _Run(repo=work / "repo", exec_dir=exec_dir, arm=arm)
    record = {"run_id": run_id, "task": task.id, "arm": arm.name, "exec": exec_dir.name,
              "manifest": manifest_hash, "cli_versions": _cli_versions(), "started": _now()}
    try:
        _init_repo(run, task)
        (_loop if arm.reviewer else _solo)(run)
        record["status"] = "complete"
    except InfraExhausted as exc:
        record.update(status="invalid", invalid_reason=str(exc))
    except Exception:
        # A harness exception is an infrastructure failure of this run, not a model outcome.
        record.update(status="invalid", invalid_reason="harness error:\n" + traceback.format_exc())
    finally:
        if run.repo.exists():
            shutil.copytree(run.repo, exec_dir / "repo", symlinks=True)
        shutil.rmtree(work, ignore_errors=True)
    if record["status"] == "complete":
        record["scores"] = score.score_build(exec_dir / "repo", task, exec_dir / "scoring")
    record.update(finished=_now(), flags=run.flags, rounds=run.rounds, steps=run.steps)
    (exec_dir / "record.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


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
    if session.terminal == agents.TIMEOUT:
        run.flags.append(f"timeout:{name}")
    run.last_commit = _commit(run.repo, name)


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
    if session.terminal == agents.TIMEOUT:
        run.flags.append(f"timeout:{name}")
    if _git(run.repo, "status", "--porcelain"):
        run.flags.append(f"reviewer_wrote:{name}")
        _reset(run.repo, run.last_commit)
    verdict = parse_verdict(session.final_text)
    if verdict.malformed:
        run.flags.append(f"malformed_verdict:{name}")
    run.steps[-1]["verdict"] = {"label": verdict.label, "blocking": verdict.blocking, "malformed": verdict.malformed}
    return verdict


def _step(run: _Run, name: str, call) -> agents.Session:
    step_dir = run.exec_dir / "steps" / f"{len(run.steps):02d}-{name}"
    attempts = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        session = call(step_dir / f"attempt-{attempt}")
        attempts.append(session.to_record())
        if session.terminal != agents.INFRA:
            run.steps.append({"name": name, "attempts": attempts})
            return session
        _reset(run.repo, run.last_commit)
    run.steps.append({"name": name, "attempts": attempts})
    raise InfraExhausted(f"{name}: {session.reason}")


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


def _git(repo: Path, *args: str) -> str:
    env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(Path.home()), **_GIT_ENV}
    completed = subprocess.run(["git", *args], cwd=repo, env=env,
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
