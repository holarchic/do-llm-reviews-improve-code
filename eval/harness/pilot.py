"""An experiment's schedule: every build, follow-up, calibration and pairwise judgement.

Jobs run in a shuffled, interleaved order (the experiment's seed), a few at a time, so
provider conditions and quota pressure don't line up with one arm. Each job is
skipped when a complete record under the current manifest already exists, so
re-running resumes; failed attempts stay on disk beside the new ones. A routed
experiment admits each job only under its spend cap, and scrubs the key from its
data on every start.
"""

import itertools
import random
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

from . import judge, loop, manifest, openrouter, records, tasks
from .experiments import Experiment

SPEND_LEDGER = "spend.json"
CALIBRATION_CAP_USD = 5.0


def scored_tasks(experiment: Experiment) -> list[tasks.Task]:
    return [tasks.Task(tasks.TASKS_ROOT / task_id) for task_id in experiment.tasks]


class BuildJob(NamedTuple):
    task: str
    arm: str
    rep: int | None


class JudgeJob(NamedTuple):
    judge: str
    task: str
    pair: tuple[str, str] | None  # None: the task's calibration
    rep: int | None  # replicate rep of each arm of the pair


def build_schedule(experiment: Experiment) -> list[BuildJob]:
    """Shuffling depends only on the list's length and the seed, so an experiment without replicates keeps
    the order it had before replicates existed."""
    jobs = [BuildJob(task.id, arm, rep) for task, arm, rep in
            itertools.product(scored_tasks(experiment), experiment.arm_names, experiment.rep_ids)]
    random.Random(experiment.seed).shuffle(jobs)
    return jobs


def judged_tasks(experiment: Experiment) -> list[tasks.Task]:
    """The scored tasks whose builds are judged: all of them but a replay's controls."""
    return [task for task in scored_tasks(experiment) if task.source is None or not task.source.control]


def judge_schedule(experiment: Experiment) -> list[JudgeJob]:
    """Nothing for a judge-free experiment. A task replayed from a source repository has no calibration job:
    a degraded reference has no brownfield counterpart."""
    if not experiment.pairs:
        return []
    jobs = [JudgeJob(name, task.id, pair, rep) for name in judge.JUDGES for task in judged_tasks(experiment)
            for pair in (None, *experiment.pairs) if pair is not None or task.source is None
            for rep in (experiment.rep_ids if pair else (None,))]
    random.Random(experiment.seed + 1).shuffle(jobs)
    return jobs


def complete_build(runs_root: Path, task_id: str, arm: str, digest: str, rep: int | None = None) -> Path | None:
    """The newest complete exec of task × arm (× rep) under this manifest, if any."""
    run_root = runs_root / loop.run_id(task_id, arm, rep)
    for exec_dir in sorted(run_root.glob("exec-*"), key=lambda p: int(p.name.split("-")[1]), reverse=True):
        if records.build(exec_dir / "record.json", digest) is not None:
            return exec_dir
    return None


def complete_followup(exec_dir: Path, digest: str) -> bool:
    return records.followup(exec_dir / "followup" / "record.json", digest) is not None


def run_builds(data_root: Path, experiment: Experiment, only_arm: str | None = None) -> int:
    """data_root is the experiment's own data dir; `only_arm` runs one arm's builds (a staged experiment)."""
    digest = manifest.verify(experiment)
    runs_root = data_root / "runs"
    by_id = {task.id: task for task in scored_tasks(experiment)}
    jobs = [job for job in build_schedule(experiment) if only_arm is None or job.arm == only_arm]
    cap = None
    if experiment.routed:
        # A run killed mid-way leaves files the end-of-run scrub never reached.
        if runs_root.exists():
            openrouter.scrub(runs_root)
        cap = openrouter.SpendCap(data_root / SPEND_LEDGER, "scored", experiment.budget_usd)

    task_locks = {task_id: threading.Lock() for task_id in by_id}

    @_contained
    def job(item: BuildJob):
        # One job per task at a time: whatever a session could glimpse of a concurrent one
        # (process lists, open files) then belongs to a different task.
        with task_locks[item.task]:
            return build_job(item)

    def build_job(item: BuildJob):
        task, arm, rep = by_id[item.task], item.arm, item.rep
        exec_dir = complete_build(runs_root, task.id, arm, digest, rep)
        if exec_dir is None:
            if cap is not None and not cap.admit():
                _log(f"build {loop.run_id(task.id, arm, rep)}: not admitted (spend cap or unreadable usage)")
                return False
            record = loop.execute_run(task, experiment.arm(arm), runs_root, rep=rep, manifest_hash=digest,
                                     offline=experiment.offline)
            _log(f"build {record['run_id']} {record['exec']}: {record['status']} {record.get('flags')}")
            if record["status"] != "complete":
                return False
            exec_dir = runs_root / record["run_id"] / record["exec"]
        if arm not in experiment.followup_arms or complete_followup(exec_dir, digest):
            return True
        _set_aside(exec_dir / "followup")
        followup = loop.execute_followup(task, exec_dir, experiment.arm(arm), manifest_hash=digest,
                                         offline=experiment.offline)
        _log(f"followup {task.id}--{arm}: {followup['status']}")
        return followup["status"] == "complete"

    with ThreadPoolExecutor(experiment.parallel) as pool:
        results = list(pool.map(job, jobs))
    if cap is not None:
        cap.settle()
    _log(f"builds: {sum(results)}/{len(results)} complete")
    return 0 if all(results) else 1


def run_calibration(data_root: Path, experiment: Experiment, task_ids: list[str], arm: str, reps: int) -> list[dict]:
    """Unscored builds of the experiment's tasks before its freeze: difficulty screening (baseline arm)
    and cost samples (a loop arm). Stored apart under calibration/, never read by the report; a routed
    experiment's calibration spend has its own cap."""
    runs_root = data_root / "calibration" / "runs"
    cap = (openrouter.SpendCap(data_root / SPEND_LEDGER, "calibration", CALIBRATION_CAP_USD)
           if experiment.routed else None)
    by_id = {task.id: task for task in scored_tasks(experiment)}

    @_contained
    def job(task_id):
        built = []
        for rep in range(1, reps + 1):
            if cap is not None and not cap.admit():
                _log(f"calibration {task_id} r{rep}: not admitted (spend cap or unreadable usage)")
                break
            built.append(loop.execute_run(by_id[task_id], experiment.arm(arm), runs_root, rep=rep, manifest_hash=None,
                                         offline=experiment.offline))
        return built

    # One worker per task: a task's replicates run one after another.
    with ThreadPoolExecutor(max(1, len(task_ids))) as pool:
        built = [record for records_ in pool.map(job, task_ids) if records_ for record in records_]
    if cap is not None:
        cap.settle()
    return built


def run_judging(data_root: Path, experiment: Experiment) -> int:
    digest = manifest.verify(experiment)
    runs_root = data_root / "runs"
    by_id = {task.id: task for task in judged_tasks(experiment)}
    schedule = judge_schedule(experiment)
    builds = {(job.task, arm, job.rep): complete_build(runs_root, job.task, arm, digest, job.rep)
              for job in schedule if job.pair for arm in job.pair}
    missing = sorted(loop.run_id(t, a, r) for (t, a, r), path in builds.items() if path is None)
    if missing:
        _log(f"judge: builds incomplete, {experiment.name} INCOMPLETE: {missing}")
        return 1

    @_contained
    def job(item: JudgeJob):
        name, task_id, pair, rep = item.judge, item.task, item.pair, item.rep
        out_dir = judging_dir(data_root, name, task_id, pair, rep)
        identity = None if pair is None else {side: build_identity(builds[(task_id, arm, rep)])
                                              for side, arm in zip(("a", "b"), pair)}
        existing = records.judgement(out_dir / "judgement.json", digest)
        if existing is not None and existing.get("builds") == identity:
            return True
        _set_aside(out_dir)
        task = by_id[task_id]
        try:
            if pair is None:
                result = judge.calibrate(name, task, out_dir, manifest_hash=digest, offline=experiment.offline)
            else:
                a, b = (builds[(task_id, arm, rep)] / "artifact" for arm in pair)
                result = judge.judge_pair(name, task, a, b, out_dir, manifest_hash=digest, builds=identity,
                                          offline=experiment.offline)
        except (loop.InfraExhausted, judge.PromptTooLong) as exc:
            _log(f"judge {name} {out_dir.relative_to(data_root)}: not judged: {exc}")
            return False
        _log(f"judge {name} {out_dir.relative_to(data_root)}: {result['result']}")
        return True

    with ThreadPoolExecutor(experiment.parallel) as pool:
        results = list(pool.map(job, schedule))
    _log(f"judgements: {sum(results)}/{len(results)} complete")
    return 0 if all(results) else 1


def build_identity(exec_dir: Path) -> dict:
    return {"exec": exec_dir.name, "artifact": loop.artifact_digest(exec_dir / "artifact")}


def judging_dir(data_root: Path, name: str, task_id: str, pair: tuple[str, str] | None,
                rep: int | None = None) -> Path:
    leaf = "calibration" if pair is None else f"{pair[0]}-vs-{pair[1]}" + ("" if rep is None else f"--r{rep}")
    return data_root / "judging" / name / task_id / leaf


def current_judgement(data_root: Path, digest: str, name: str, task_id: str, pair: tuple[str, str] | None,
                      rep: int | None = None) -> dict | None:
    """The judgement on record, if it judged the builds now on record: a rebuilt build makes it stale."""
    judgement = records.judgement(judging_dir(data_root, name, task_id, pair, rep) / "judgement.json", digest)
    if judgement is None or pair is None:
        return judgement
    execs = [complete_build(data_root / "runs", task_id, arm, digest, rep) for arm in pair]
    current = None if None in execs else {side: build_identity(e) for side, e in zip(("a", "b"), execs)}
    return judgement if judgement.get("builds") == current else None


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
