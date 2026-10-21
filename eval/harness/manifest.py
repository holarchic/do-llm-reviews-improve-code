"""The frozen inputs of an experiment and the check that they are unchanged.

Frozen: the experiment's pre-registration, every scored task file, the harness
code (prompt assembly, loop, scoring, judging, report, the experiment table) and
the shipped skill files the prompts are built from. Model ids, arms, timeouts,
the judge prompt and the schedule seed live in that code, so they are frozen with
it. CLI versions are recorded per run, not frozen: Claude Code updates itself.

The pilot keeps `eval/MANIFEST.json`; any other experiment has its own
`eval/manifests/<name>.json`, so freezing one never disturbs another. Once the
harness changes, an older experiment's manifest no longer verifies: its report
is regenerated from a checkout of its freeze commit.
"""

import hashlib
import json
from pathlib import Path

from .prompts import BUILD_LOOP_SKILL, CODEX_REVIEW_SKILL, REPO_ROOT, RUBRIC

EVAL_ROOT = REPO_ROOT / "eval"
MANIFEST = EVAL_ROOT / "MANIFEST.json"
MANIFESTS = EVAL_ROOT / "manifests"
PILOT_NAME = "pilot"


class ManifestMismatch(Exception):
    pass


def manifest_path(experiment) -> Path:
    return MANIFEST if experiment.name == PILOT_NAME else MANIFESTS / f"{experiment.name}.json"


def frozen_files(experiment) -> list[Path]:
    files = [EVAL_ROOT / experiment.prereg, EVAL_ROOT / "__init__.py", EVAL_ROOT / "__main__.py",
             CODEX_REVIEW_SKILL, BUILD_LOOP_SKILL, RUBRIC]
    files += sorted((EVAL_ROOT / "harness").glob("*.py"))
    for task_dir in sorted((EVAL_ROOT / "tasks").iterdir()):
        if task_dir.is_dir() and not task_dir.name.startswith(("dev-", ".")):
            files += sorted(p for p in task_dir.rglob("*")
                            if p.is_file() and "__pycache__" not in p.parts and p.name != ".DS_Store")
    return files


def compute(experiment) -> dict[str, str]:
    return {str(path.relative_to(REPO_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in frozen_files(experiment)}


def digest(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def freeze(experiment) -> str:
    prereg = EVAL_ROOT / experiment.prereg
    if not prereg.is_file():
        raise ManifestMismatch(f"no pre-registration for {experiment.name}: write {prereg.relative_to(REPO_ROOT)}")
    files = compute(experiment)
    payload = {"files": files, "digest": digest(files)}
    if experiment.name != PILOT_NAME:
        payload["experiment"] = experiment.name
    path = manifest_path(experiment)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return digest(files)


def verify(experiment) -> str:
    """The frozen digest, if every frozen file matches it; ManifestMismatch otherwise."""
    path = manifest_path(experiment)
    if not path.exists():
        raise ManifestMismatch(f"no manifest for {experiment.name}: run `python3 -m eval freeze --experiment "
                               f"{experiment.name}` first")
    try:
        frozen = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ManifestMismatch(f"{path.name} is not valid JSON: {exc}") from None
    if not (isinstance(frozen, dict) and isinstance(frozen.get("files"), dict)):
        raise ManifestMismatch(f"{path.name} has no files table")
    if frozen.get("experiment", PILOT_NAME) != experiment.name:
        raise ManifestMismatch(f"{path.name} freezes {frozen.get('experiment', PILOT_NAME)}, not {experiment.name}")
    current = compute(experiment)
    changed = sorted(name for name in frozen["files"].keys() | current.keys()
                     if frozen["files"].get(name) != current.get(name))
    if changed:
        raise ManifestMismatch("frozen inputs changed: " + ", ".join(changed))
    return digest(frozen["files"])
