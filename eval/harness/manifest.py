"""The frozen inputs of the pilot and the check that they are unchanged.

Frozen: the pre-registration, every pilot task file, the harness code (prompt
assembly, loop, scoring, judging, report) and the shipped skill files the
prompts are built from. Model ids, timeouts, the judge prompt and the schedule
seed live in that code, so they are frozen with it. CLI versions are recorded
per run, not frozen: Claude Code updates itself.
"""

import hashlib
import json
from pathlib import Path

from .prompts import BUILD_LOOP_SKILL, CODEX_REVIEW_SKILL, REPO_ROOT, RUBRIC

EVAL_ROOT = REPO_ROOT / "eval"
MANIFEST = EVAL_ROOT / "MANIFEST.json"


class ManifestMismatch(Exception):
    pass


def frozen_files() -> list[Path]:
    files = [EVAL_ROOT / "PREREG.md", EVAL_ROOT / "__init__.py", EVAL_ROOT / "__main__.py",
             CODEX_REVIEW_SKILL, BUILD_LOOP_SKILL, RUBRIC]
    files += sorted((EVAL_ROOT / "harness").glob("*.py"))
    for task_dir in sorted((EVAL_ROOT / "tasks").iterdir()):
        if task_dir.is_dir() and not task_dir.name.startswith(("dev-", ".")):
            files += sorted(p for p in task_dir.rglob("*")
                            if p.is_file() and "__pycache__" not in p.parts and p.name != ".DS_Store")
    return files


def compute() -> dict[str, str]:
    return {str(path.relative_to(REPO_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in frozen_files()}


def digest(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def freeze() -> str:
    files = compute()
    MANIFEST.write_text(json.dumps({"files": files, "digest": digest(files)}, indent=2) + "\n", encoding="utf-8")
    return digest(files)


def verify() -> str:
    """The frozen digest, if every frozen file matches it; ManifestMismatch otherwise."""
    if not MANIFEST.exists():
        raise ManifestMismatch("no MANIFEST.json: run `python3 -m eval freeze` first")
    frozen = json.loads(MANIFEST.read_text(encoding="utf-8"))["files"]
    current = compute()
    changed = sorted(name for name in frozen.keys() | current.keys() if frozen.get(name) != current.get(name))
    if changed:
        raise ManifestMismatch("frozen inputs changed: " + ", ".join(changed))
    return digest(frozen)
