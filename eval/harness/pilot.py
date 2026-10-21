"""The pilot schedule: every build, follow-up, calibration and pairwise judgement.

Jobs run in a shuffled, interleaved order (fixed seed), a few at a time, so
provider conditions and quota pressure don't line up with one arm. Each job is
skipped when a complete record under the current manifest already exists, so
re-running resumes; failed attempts stay on disk beside the new ones.
"""

import itertools
import random
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import judge, loop, manifest, records, tasks

SEED = 20261021
PARALLEL = 3
BUILD_ARMS = tuple(loop.ARMS)
FOLLOWUP_ARMS = tuple(arm for arm in loop.ARMS if arm != "A1r")
PAIRS = (("A1", "A2"), ("A1", "A3"), ("A2", "A3"))


def pilot_tasks() -> list[tasks.Task]:
    return [task for task in tasks.load_tasks() if not task.is_dev]


def build_schedule() -> list[tuple[str, str]]:
    jobs = [(task.id, arm) for task, arm in itertools.product(pilot_tasks(), BUILD_ARMS)]
    random.Random(SEED).shuffle(jobs)
    return jobs


def judge_schedule() -> list[tuple[str, str, tuple[str, str] | None]]:
    """(judge, task, pair); a None pair is that task's calibration."""
    jobs = [(name, task.id, pair) for name in judge.JUDGES for task in pilot_tasks()
            for pair in (None, *PAIRS)]
    random.Random(SEED + 1).shuffle(jobs)
    return jobs


def complete_build(runs_root: Path, task_id: str, arm: str, digest: str) -> Path | None:
    """The newest complete exec of task × arm under this manifest, if any."""
    run_root = runs_root / f"{task_id}--{arm}"
    for exec_dir in sorted(run_root.glob("exec-*"), key=lambda p: int(p.name.split("-")[1]), reverse=True):
        if records.build(exec_dir / "record.json", digest) is not None:
            return exec_dir
    return None


def complete_followup(exec_dir: Path, digest: str) -> bool:
    return records.followup(exec_dir / "followup" / "record.json", digest) is not None


def run_builds(data_root: Path) -> int:
    digest = manifest.verify()
    runs_root = data_root / "runs"
    by_id = {task.id: task for task in pilot_tasks()}
    jobs = [(by_id[task_id], arm) for task_id, arm in build_schedule()]

    @_contained
    def job(item):
        task, arm = item
        exec_dir = complete_build(runs_root, task.id, arm, digest)
        if exec_dir is None:
            record = loop.execute_run(task, arm, runs_root, manifest_hash=digest)
            _log(f"build {record['run_id']} {record['exec']}: {record['status']} {record.get('flags')}")
            if record["status"] != "complete":
                return False
            exec_dir = runs_root / record["run_id"] / record["exec"]
        if arm not in FOLLOWUP_ARMS or complete_followup(exec_dir, digest):
            return True
        _set_aside(exec_dir / "followup")
        followup = loop.execute_followup(task, exec_dir, manifest_hash=digest)
        _log(f"followup {task.id}--{arm}: {followup['status']}")
        return followup["status"] == "complete"

    with ThreadPoolExecutor(PARALLEL) as pool:
        results = list(pool.map(job, jobs))
    _log(f"builds: {sum(results)}/{len(results)} complete")
    return 0 if all(results) else 1


def run_judging(data_root: Path) -> int:
    digest = manifest.verify()
    runs_root = data_root / "runs"
    by_id = {task.id: task for task in pilot_tasks()}
    builds = {(task_id, arm): complete_build(runs_root, task_id, arm, digest)
              for task_id in by_id for arm in FOLLOWUP_ARMS}
    missing = sorted(f"{t}--{a}" for (t, a), path in builds.items() if path is None)
    if missing:
        _log(f"judge: builds incomplete, pilot INCOMPLETE: {missing}")
        return 1

    @_contained
    def job(item):
        name, task_id, pair = item
        out_dir = judging_dir(data_root, name, task_id, pair)
        identity = None if pair is None else {side: build_identity(builds[(task_id, arm)])
                                              for side, arm in zip(("a", "b"), pair)}
        existing = records.judgement(out_dir / "judgement.json", digest)
        if existing is not None and existing.get("builds") == identity:
            return True
        _set_aside(out_dir)
        task = by_id[task_id]
        try:
            if pair is None:
                result = judge.calibrate(name, task, out_dir, manifest_hash=digest)
            else:
                a, b = (builds[(task_id, arm)] / "artifact" for arm in pair)
                result = judge.judge_pair(name, task, a, b, out_dir, manifest_hash=digest, builds=identity)
        except loop.InfraExhausted as exc:
            _log(f"judge {name} {task_id} {pair}: infrastructure failure: {exc}")
            return False
        _log(f"judge {name} {task_id} {pair or 'calibration'}: {result['result']}")
        return True

    with ThreadPoolExecutor(PARALLEL) as pool:
        results = list(pool.map(job, judge_schedule()))
    _log(f"judgements: {sum(results)}/{len(results)} complete")
    return 0 if all(results) else 1


def build_identity(exec_dir: Path) -> dict:
    return {"exec": exec_dir.name, "artifact": loop.artifact_digest(exec_dir / "artifact")}


def judging_dir(data_root: Path, name: str, task_id: str, pair: tuple[str, str] | None) -> Path:
    leaf = "calibration" if pair is None else f"{pair[0]}-vs-{pair[1]}"
    return data_root / "judging" / name / task_id / leaf


def _contained(job):
    """A job that raises fails alone instead of stopping the pool."""
    def wrapped(item):
        try:
            return job(item)
        except Exception:
            _log(f"job {item} raised:\n{traceback.format_exc()}")
            return False
    return wrapped


def _set_aside(path: Path) -> None:
    """Keep a failed or stale attempt's files under a numbered name."""
    if not path.exists():
        return
    n = 1
    while path.with_name(f"{path.name}.attempt-{n}").exists():
        n += 1
    path.rename(path.with_name(f"{path.name}.attempt-{n}"))


def _log(line: str) -> None:
    print(line, flush=True, file=sys.stdout)
