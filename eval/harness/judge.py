"""Blind pairwise judging by one frontier model per family, and its calibration.

A judge sees TASK.md and the Python files of two builds, labelled X and Y, inline
in a frozen prompt. SPEC, plans, reviews and git history never reach it. Each
pair is judged in both orders; only a pick that survives the swap is a win.
"""

import json
import re
import shutil
from pathlib import Path

from . import agents
from .loop import MAX_ATTEMPTS, InfraExhausted
from .tasks import Task

JUDGES = {
    "fable": ("claude", agents.CLAUDE_JUDGE_MODEL),
    "astra": ("codex", agents.CODEX_JUDGE_MODEL),
}
LEAK_WORDS = ("review", "rubric", "plan", "spec", "round")
MALFORMED_RETRIES = 1

JUDGE_PROMPT = """You are comparing two implementations of the same task. TASK.md below is the contract. Version X and \
Version Y each contain the source files and tests one developer wrote.

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


def export_build(repo: Path, dest: Path) -> list[str]:
    """Copy only the build's Python files (source and tests) into dest."""
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


def leak_count(export_dir: Path) -> int:
    pattern = re.compile(r"\b(" + "|".join(LEAK_WORDS) + r")s?\b", re.IGNORECASE)
    return sum(len(pattern.findall(p.read_text(encoding="utf-8", errors="replace")))
               for p in export_dir.rglob("*.py"))


def build_prompt(task_text: str, x_dir: Path, y_dir: Path) -> str:
    parts = [JUDGE_PROMPT, "# TASK.md\n", task_text]
    for label, folder in (("X", x_dir), ("Y", y_dir)):
        parts.append(f"\n# Version {label}\n")
        for path in sorted(folder.rglob("*.py")):
            parts.append(f"\n## {path.relative_to(folder)}\n\n```python\n{path.read_text(encoding='utf-8')}\n```\n")
    return "".join(parts)


def parse_judgement(text: str) -> str | None:
    """The winner from the last JSON object in the reply, or None if there is none."""
    decoder = json.JSONDecoder()
    for start in reversed([i for i, ch in enumerate(text) if ch == "{"]):
        try:
            data, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        winner = data.get("winner") if isinstance(data, dict) else None
        if winner in ("X", "Y", "TIE"):
            return winner
    return None


def judge_pair(judge: str, task: Task, a_repo: Path, b_repo: Path, out_dir: Path) -> dict:
    """Judge builds a and b in both orders. Returns per-order picks and the combined result."""
    out_dir.mkdir(parents=True, exist_ok=False)
    exports = {"a": out_dir / "export-a", "b": out_dir / "export-b"}
    export_build(a_repo, exports["a"])
    export_build(b_repo, exports["b"])
    task_text = task.path("TASK.md").read_text(encoding="utf-8")
    orders = []
    for order, (x, y) in enumerate((("a", "b"), ("b", "a")), start=1):
        order_dir = out_dir / f"order-{order}"
        cwd = order_dir / "judge"
        shutil.copytree(exports[x], cwd / "X")
        shutil.copytree(exports[y], cwd / "Y")
        pick, attempts = _judge_once(judge, build_prompt(task_text, cwd / "X", cwd / "Y"), cwd, order_dir)
        orders.append({"x": x, "y": y, "pick": pick, "winner": {"X": x, "Y": y}.get(pick, "tie"),
                       "attempts": attempts})
    winners = {o["winner"] for o in orders}
    result = {
        "judge": judge,
        "orders": orders,
        "result": winners.pop() if len(winners) == 1 else "tie",
        "leak_words": {name: leak_count(path) for name, path in exports.items()},
    }
    (out_dir / "judgement.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def calibrate(judge: str, task: Task, out_dir: Path) -> dict:
    """Reference (a) vs degraded reference (b): the judge passes the task if it picks a in both orders."""
    result = judge_pair(judge, task, task.path("reference"), task.path("degraded"), out_dir)
    return {**result, "prefers_reference": result["result"] == "a"}


def _judge_once(judge: str, prompt: str, cwd: Path, order_dir: Path) -> tuple[str, list[dict]]:
    cli, model = JUDGES[judge]
    run = agents.run_claude if cli == "claude" else agents.run_codex
    attempts, infra, malformed = [], 0, 0
    while True:
        session = run(model, prompt, role=agents.JUDGE, cwd=cwd, out_dir=order_dir / f"attempt-{len(attempts) + 1}")
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
