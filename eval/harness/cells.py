"""A paired experiment's schedule: one solo build per task, then every review cell on that build.

The builds run first, in an order shuffled by the experiment's seed. The cells follow, shuffled across
reviewers and runs, so no reviewer lines up with a stretch of the clock. A job whose complete record
under the current manifest exists is skipped, so re-running resumes. One job per task runs at a time.

Every cell of a task is paired with the task's one build, the newest complete one. A cell record names
the build it started from; when a task's cells name different builds, none of that task's cells run
until the operator has looked. Review cells run on native sessions only: nothing here reserves spend.
"""

import random
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

from . import agents, loop, manifest, pilot, records
from .experiments import Experiment


class Schedule(NamedTuple):
    builds: list[pilot.BuildJob]
    cells: list[pilot.BuildJob]


def cell_runs(experiment: Experiment) -> range:
    """A review cell's run numbers: `reps` of them, from 1, whatever the count."""
    return range(1, experiment.reps + 1)


def schedule(experiment: Experiment) -> Schedule:
    """Each order depends only on its list's length and the seed."""
    task_ids = [task.id for task in pilot.scored_tasks(experiment)]
    builds = [pilot.BuildJob(task_id, experiment.baseline, None) for task_id in task_ids]
    cells = [pilot.BuildJob(task_id, arm, rep) for task_id in task_ids for arm in experiment.cell_arms
             for rep in cell_runs(experiment)]
    random.Random(experiment.seed).shuffle(builds)
    random.Random(experiment.seed + 1).shuffle(cells)
    return Schedule(builds, cells)


def pairing(build_exec: Path) -> tuple[str, str, str]:
    """What a cell record names when it started from this build: the build's run, exec and artifact digest."""
    return build_exec.parent.name, build_exec.name, loop.artifact_digest(build_exec / "artifact")


def pairing_of(cell: dict) -> tuple[str, str, str]:
    return cell["build_run"], cell["build_exec"], cell["build_artifact"]


def complete_cell(runs_root: Path, task_id: str, arm: str, digest: str | None, rep: int) -> Path | None:
    """The newest exec of a cell whose record is a complete cell record under this manifest, if any."""
    run_root = runs_root / loop.run_id(task_id, arm, rep)
    for exec_dir in sorted(run_root.glob("exec-*"), key=lambda p: int(p.name.split("-")[1]), reverse=True):
        if records.cell(exec_dir / "record.json", digest) is not None:
            return exec_dir
    return None


def finished(record: dict) -> bool:
    """Whether a cell's review and fix both ran to the end and each said something. A cell that timed out
    is complete and scored, as registered, and not finished: its last session did not complete."""
    steps = record["steps"]
    return (record["status"] == "complete"
            and [step["name"] for step in steps] == [loop.REVIEW_STEP, loop.FIX_STEP]
            and all(step["attempts"][-1].get("terminal") == agents.COMPLETED for step in steps)
            and bool(record["review_text"].strip()) and bool(record["fix_text"].strip()))


def run(data_root: Path, experiment: Experiment, only_arm: str | None = None) -> int:
    """data_root is the experiment's own data dir. `only_arm` runs the baseline's builds, or one cell
    arm's cells on the builds that exist."""
    digest = manifest.verify(experiment)
    _native_only(experiment)
    runs_root = data_root / "runs"
    by_id = {task.id: task for task in pilot.scored_tasks(experiment)}
    plan = schedule(experiment)

    def build(job: pilot.BuildJob) -> bool:
        if pilot.complete_build(runs_root, job.task, job.arm, digest) is not None:
            return True
        record = loop.execute_run(by_id[job.task], experiment.arm(job.arm), runs_root, manifest_hash=digest,
                                  offline=experiment.offline)
        _log(f"build {record['run_id']} {record['exec']}: {record['status']} {record.get('flags')}")
        return record["status"] == "complete"

    def cell(job: pilot.BuildJob) -> bool:
        name = loop.run_id(job.task, job.arm, job.rep)
        build_exec = pilot.complete_build(runs_root, job.task, experiment.baseline, digest)
        if build_exec is None:
            _log(f"cell {name}: no complete build of {job.task}")
            return False
        # Before the resume skip: a build replaced after its cells finished must fail the run, not pass it.
        strays = _paired_elsewhere(runs_root, experiment, job.task, digest, pairing(build_exec))
        if strays:
            _log(f"cell {name}: refused, {', '.join(strays)} ran on another build of {job.task}")
            return False
        if complete_cell(runs_root, job.task, job.arm, digest, job.rep) is not None:
            return True
        record = loop.execute_cell(by_id[job.task], build_exec, experiment.arm(job.arm), runs_root, rep=job.rep,
                                   manifest_hash=digest, offline=experiment.offline)
        _log(f"cell {record['run_id']} {record['exec']}: {record['status']} {record.get('flags')}")
        return record["status"] == "complete"

    stages = []
    if only_arm in (None, experiment.baseline):
        stages.append((build, plan.builds))
    if only_arm != experiment.baseline:
        stages.append((cell, [job for job in plan.cells if only_arm in (None, job.arm)]))
    locks = {task_id: threading.Lock() for task_id in by_id}
    results: list[bool] = []
    with ThreadPoolExecutor(experiment.parallel) as pool:
        for job, items in stages:  # every build has finished before the first cell starts
            results += pool.map(_one_per_task(job, locks), items)
    _log(f"paired runs: {sum(results)}/{len(results)} complete")
    return 0 if all(results) else 1


def calibrate(data_root: Path, experiment: Experiment, task_ids: list[str], arm: str, reps: int) -> list[dict]:
    """Per task, a fresh unscored build and `reps` cells of `arm` on it, under calibration/: the wall clock
    of a plain review and an end-to-end check before the freeze. Returns the cell records; no report reads
    them."""
    _native_only(experiment)
    runs_root = data_root / "calibration" / "runs"
    by_id = {task.id: task for task in pilot.scored_tasks(experiment)}

    def job(task_id: str) -> list[dict]:
        build = loop.execute_run(by_id[task_id], experiment.arm(experiment.baseline), runs_root, manifest_hash=None,
                                 offline=experiment.offline)
        _log(f"calibration build {build['run_id']} {build['exec']}: {build['status']} {build.get('flags')}")
        if build["status"] != "complete":
            return []
        build_exec = runs_root / build["run_id"] / build["exec"]
        return [loop.execute_cell(by_id[task_id], build_exec, experiment.arm(arm), runs_root, rep=rep,
                                  manifest_hash=None, offline=experiment.offline) for rep in range(1, reps + 1)]

    # One worker per task: a task's cells run one after another, on its one build.
    with ThreadPoolExecutor(max(1, len(task_ids))) as pool:
        return [record for built in pool.map(_contained(job), task_ids) for record in built or []]


def _paired_elsewhere(runs_root: Path, experiment: Experiment, task_id: str, digest: str,
                      build: tuple[str, str, str]) -> list[str]:
    """The task's complete cells that started from another build than `build`."""
    strays = []
    for arm in experiment.cell_arms:
        for rep in cell_runs(experiment):
            exec_dir = complete_cell(runs_root, task_id, arm, digest, rep)
            if exec_dir is not None and pairing_of(records.cell(exec_dir / "record.json", digest)) != build:
                strays.append(loop.run_id(task_id, arm, rep))
    return strays


def _native_only(experiment: Experiment) -> None:
    if experiment.routed:
        raise ValueError(f"{experiment.name}: review cells run on native sessions only")


def _one_per_task(job, locks: dict[str, threading.Lock]):
    """Whatever a session could glimpse of a concurrent one (process lists, open files) then belongs to
    another task."""
    def locked(item: pilot.BuildJob):
        with locks[item.task]:
            return job(item)
    return _contained(locked)


def _contained(job):
    """A job that raises fails alone instead of stopping the pool."""
    def wrapped(item):
        try:
            return job(item)
        except Exception:
            _log(f"job {item} raised:\n{traceback.format_exc()}")
            return False
    return wrapped


def _log(line: str) -> None:
    print(line, flush=True, file=sys.stdout)
