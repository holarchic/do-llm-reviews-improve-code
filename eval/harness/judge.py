"""Blind pairwise judging by one frontier model per family, and its calibration.

A judge sees TASK.md and two builds, labelled X and Y, inline in a frozen prompt:
the Python files of a build from an empty repository, or, for a task replayed from
a source repository, the build's patch from the task's starting tree. SPEC, plans,
markdown, reviews and git history never reach it. Each pair is judged in both
orders; only a pick that survives the swap is a win.
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import agents, audit, source
from .loop import MAX_ATTEMPTS, InfraExhausted, guarded_call
from .tasks import Task

JUDGES = {
    "fable": ("claude", agents.CLAUDE_JUDGE_MODEL),
    "astra": ("codex", agents.CODEX_JUDGE_MODEL),
}
LEAK_WORDS = ("review", "rubric", "plan", "spec", "round")
MALFORMED_RETRIES = 1

# The whole of a changed file fits in one hunk at this context, so a patch shows every changed file in full.
WHOLE_FILE_CONTEXT = 1_000_000
# A conservative character budget for one assembled prompt, under every judge's context window. A pair of
# patches over it is judged on its source patch alone (tests/ left out), and the judgement says so.
PROMPT_CHARS = 1_200_000
PATCH = "patch.diff"

FILES, PATCH_SCOPE, SOURCE_SCOPE = "python files", "patch", "source patch"  # what a judgement saw
FILES_INTRO = """You are comparing two implementations of the same task. TASK.md below is the contract. Version X and \
Version Y each contain the source files and tests one developer wrote.
"""
PATCH_INTRO = """You are comparing two changes to the same existing repository. TASK.md below is the contract. Version X \
and Version Y are each one developer's patch from the same starting tree: a diff of every file they changed, added or \
deleted, each shown in full, with removed lines marked - and added lines marked +.
"""
CRITERIA = """
Decide which version is the better code, weighing in this order:
1. Correctness risk against TASK.md: bugs, required cases left unhandled, contract mismatches.
2. Clarity: how easily a reader sees what the code does and why.
3. Structure and ease of change: how hard the next change to this code would be.
4. Test quality: whether the tests pin the behaviour TASK.md requires.

More tests, more code, or comments that cite rules or reviews earn nothing by themselves. Do not run anything; \
everything you need is below. Use TIE only when neither version is meaningfully better.

End your reply with a JSON object, and nothing after it:
{"winner": "X" | "Y" | "TIE", "reasons": ["...", "..."]}
"""
JUDGE_PROMPT = FILES_INTRO + CRITERIA
PATCH_JUDGE_PROMPT = PATCH_INTRO + CRITERIA
SOURCE_ONLY_NOTE = "\nFor length, both patches leave out files under tests/.\n"


def export_build(repo: Path, dest: Path) -> list[str]:
    """Copy only the build's Python files (source and tests) into dest; dest exists even when there are none."""
    dest.mkdir(parents=True, exist_ok=True)
    files = []
    for path in sorted(repo.rglob("*.py")):
        relative = path.relative_to(repo)
        if relative.parts[0] in (".git", "plans") or "__pycache__" in relative.parts:
            continue
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        files.append(str(relative))
    return files


def export_patch(task: Task, build: Path, dest: Path, *, tests: bool = True) -> list[str]:
    """Write the build's change from the task's starting tree (base plus TASK.md) to dest/patch.diff, renames as
    a deletion and an addition. Markdown files, plans/ and, unless `tests`, tests/ are left out. Returns the
    changed paths."""
    dest.mkdir(parents=True, exist_ok=True)
    left_out = ["*.md", "plans"] + ([] if tests else ["tests"])
    pathspec = ["--", ".", *(f":(exclude){pattern}" for pattern in left_out)]
    with tempfile.TemporaryDirectory(prefix="eval-patch-") as tmp:
        start = Path(tmp)
        source.seed(task, start)
        _git(start, "init", "-q")
        _git(start, "add", "-A", "-f")
        _git(start, "commit", "-q", "--no-verify", "-m", "start")
        # The build's files staged over the starting tree: deletions included, its .gitignore no filter.
        _git(start, f"--work-tree={build}", "add", "-A", "-f")
        patch = _git(start, "diff", "--cached", "--no-renames", "--no-ext-diff", f"-U{WHOLE_FILE_CONTEXT}", *pathspec)
        changed = _git(start, "diff", "--cached", "--no-renames", "--name-only", *pathspec).splitlines()
    (dest / PATCH).write_text(patch, encoding="utf-8")
    return changed


def leak_count(export_dir: Path) -> int:
    pattern = re.compile(r"\b(" + "|".join(LEAK_WORDS) + r")s?\b", re.IGNORECASE)
    return sum(len(pattern.findall(p.read_text(encoding="utf-8", errors="replace")))
               for p in export_dir.rglob("*") if p.is_file())


def build_prompt(task_text: str, x_dir: Path, y_dir: Path, *, scope: str = FILES) -> str:
    if scope != FILES:
        return _patch_prompt(task_text, x_dir, y_dir, source_only=scope == SOURCE_SCOPE)
    parts = [JUDGE_PROMPT, "# TASK.md\n", task_text]
    for label, folder in (("X", x_dir), ("Y", y_dir)):
        parts.append(f"\n# Version {label}\n")
        files = sorted(folder.rglob("*.py"))
        if not files:
            parts.append("\n(This version contains no Python files.)\n")
        for path in files:
            parts.append(f"\n## {path.relative_to(folder)}\n\n```python\n{path.read_text(encoding='utf-8')}\n```\n")
    return "".join(parts)


def _patch_prompt(task_text: str, x_dir: Path, y_dir: Path, *, source_only: bool) -> str:
    parts = [PATCH_JUDGE_PROMPT, SOURCE_ONLY_NOTE if source_only else "", "# TASK.md\n", task_text]
    for label, folder in (("X", x_dir), ("Y", y_dir)):
        patch = (folder / PATCH).read_text(encoding="utf-8")
        body = f"```diff\n{patch}```\n" if patch else "(This version changes nothing.)\n"
        parts.append(f"\n# Version {label}\n\n{body}")
    return "".join(parts)


def parse_judgement(text: str) -> str | None:
    """The winner from the last JSON object in the reply, or None if there is none."""
    decoder = json.JSONDecoder()
    for start in reversed([i for i, ch in enumerate(text) if ch == "{"]):
        try:
            data, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or not isinstance(data.get("reasons"), list):
            continue
        if data.get("winner") in ("X", "Y", "TIE"):
            return data["winner"]
    return None


def judge_pair(judge: str, task: Task, a_repo: Path, b_repo: Path, out_dir: Path, *,
               manifest_hash: str | None = None, builds: dict | None = None, offline: bool = False) -> dict:
    """Judge builds a and b in both orders. Returns per-order picks and the combined result. `builds` identifies
    what a and b are (exec and artifact hash), so a re-run build invalidates this. Offline, a judge can't look
    up the source repository's history."""
    out_dir.mkdir(parents=True, exist_ok=False)
    exports = {"a": out_dir / "export-a", "b": out_dir / "export-b"}
    task_text = task.path("TASK.md").read_text(encoding="utf-8")
    if task.source is not None:
        scope = _export_patches(task, task_text, {"a": a_repo, "b": b_repo}, exports)
    else:
        export_build(a_repo, exports["a"])
        export_build(b_repo, exports["b"])
        scope = FILES
    orders = []
    for order, (x, y) in enumerate((("a", "b"), ("b", "a")), start=1):
        order_dir = out_dir / f"order-{order}"
        # The judge's cwd is a neutral folder outside the repo: no project instructions, no arm names in paths.
        cwd = audit.anonymous_workdir()
        try:
            shutil.copytree(exports[x], cwd / "X")
            shutil.copytree(exports[y], cwd / "Y")
            prompt = build_prompt(task_text, cwd / "X", cwd / "Y", scope=scope)
            pick, attempts = _judge_once(judge, prompt, cwd, order_dir, offline)
        finally:
            shutil.rmtree(cwd, ignore_errors=True)
        orders.append({"x": x, "y": y, "pick": pick, "winner": {"X": x, "Y": y}.get(pick, "tie"),
                       "attempts": attempts})
    winners = {o["winner"] for o in orders}
    result = {
        "judge": judge,
        "manifest": manifest_hash,
        "builds": builds,
        "scope": scope,
        "orders": orders,
        "result": winners.pop() if len(winners) == 1 else "tie",
        "leak_words": {name: leak_count(path) for name, path in exports.items()},
    }
    (out_dir / "judgement.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def _export_patches(task: Task, task_text: str, builds: dict[str, Path], exports: dict[str, Path]) -> str:
    """Both builds' patches with their tests, or their source alone when that prompt would pass PROMPT_CHARS."""
    for scope, tests in ((PATCH_SCOPE, True), (SOURCE_SCOPE, False)):
        for side, build in builds.items():
            shutil.rmtree(exports[side], ignore_errors=True)
            export_patch(task, build, exports[side], tests=tests)
        if len(build_prompt(task_text, exports["a"], exports["b"], scope=scope)) <= PROMPT_CHARS:
            break
    return scope


def calibrate(judge: str, task: Task, out_dir: Path, *, manifest_hash: str | None = None) -> dict:
    """Reference (a) vs degraded reference (b): the judge passes the task if it picks a in both orders."""
    result = judge_pair(judge, task, task.path("reference"), task.path("degraded"), out_dir,
                        manifest_hash=manifest_hash)
    return {**result, "prefers_reference": result["result"] == "a"}


def _judge_once(judge: str, prompt: str, cwd: Path, order_dir: Path, offline: bool) -> tuple[str, list[dict]]:
    cli, model = JUDGES[judge]
    run = agents.run_claude if cli == "claude" else agents.run_codex
    attempts, infra, malformed = [], 0, 0
    while True:
        session = guarded_call(lambda out: run(model, prompt, role=agents.JUDGE, cwd=cwd, out_dir=out, offline=offline),
                               order_dir / f"attempt-{len(attempts) + 1}")
        attempts.append(session.to_record())
        if session.terminal == agents.INFRA:
            infra += 1
            if infra == MAX_ATTEMPTS:
                raise InfraExhausted(f"judge {judge}: {session.reason}")
            continue
        pick = parse_judgement(session.final_text)
        if pick is not None:
            return pick, attempts
        malformed += 1
        if malformed > MALFORMED_RETRIES:
            return "TIE", attempts


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(["git", "-c", "user.name=eval", "-c", "user.email=eval@localhost",
                                "-c", "commit.gpgsign=false", *args], cwd=repo, capture_output=True,
                               encoding="utf-8", errors="replace", check=True)
    return completed.stdout
