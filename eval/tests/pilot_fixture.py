"""Deterministic synthetic pilot records, shared by the pilot regression test and the script that
captured its golden report from the pilot's frozen code (commit 071f3d0)."""

import json
from pathlib import Path

ARMS = ("A1", "A1r", "A2", "A3")
FOLLOWUP_ARMS = ("A1", "A2", "A3")
PAIRS = (("A1", "A2"), ("A1", "A3"), ("A2", "A3"))
JUDGES = ("fable", "astra")
STATIC = {"ruff_violations": 1, "source_loc": 50, "test_loc": 40, "max_function_branches": 4}


def populate(root: Path, tasks: list[str], judging_dir, build_identity, digest: str = "d") -> None:
    """Write builds, follow-ups and judgements under root. `judging_dir` and `build_identity` come from
    the code under test, so the layout is whatever that code reads."""
    for t, task in enumerate(tasks):
        for a, arm in enumerate(ARMS):
            exec_dir = root / "runs" / f"{task}--{arm}" / "exec-1"
            identity = {"run_id": f"{task}--{arm}", "task": task, "arm": arm}
            (exec_dir / "artifact").mkdir(parents=True, exist_ok=True)
            (exec_dir / "artifact" / "m.py").write_text(f"# {task} {arm}\n")
            primary = [1.0, 0.95, 0.9, 0.85, 0.8][(t + a) % 5]
            steps = [{"name": "build", "attempts": [{"seconds": 60.0 + t, "usage": {"input_tokens": 1000 + a,
                                                                                   "output_tokens": 200}}]}]
            _write(exec_dir / "record.json", {
                **identity, "status": "complete", "manifest": digest, "steps": steps,
                "rounds": {"plan": a, "phase": t} if arm in ("A2", "A3") else {},
                "flags": ["plan_capped"] if arm == "A2" else [],
                "scores": {"primary": primary, "static": STATIC, "hidden_outcomes": {}}})
            if arm in FOLLOWUP_ARMS:
                _write(exec_dir / "followup" / "record.json", {
                    **identity, "status": "complete", "manifest": digest,
                    "scores": {"new_fraction": 1.0 - 0.25 * (t % 2), "regressions": ["x"] * (a % 2),
                               "persistent_failures": [], "repaired": []}})
    for j, name in enumerate(JUDGES):
        for t, task in enumerate(tasks):
            for p, pair in enumerate((None, *PAIRS)):
                if pair is None:
                    result, builds = ("a" if (t + j) % 5 else "tie"), None
                else:
                    result = ("a", "b", "tie")[(t + p + j) % 3]
                    builds = {side: build_identity(root / "runs" / f"{task}--{arm}" / "exec-1")
                              for side, arm in zip(("a", "b"), pair)}
                _write(judging_dir(root, name, task, pair) / "judgement.json",
                       {"manifest": digest, "result": result, "builds": builds,
                        "leak_words": {"a": t, "b": p}})


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
