"""Per-round scoring of a replay experiment's reviewed builds, under eval/prereg/<experiment>-rounds.md.

Round 0 is a build's `build` commit, round k its `fix-k` commit: the code after k code reviews. A round's
commit is the one whose subject is the writer step its run record names (the loop commits each writer step
under its name); the records hold no commit ids. Each round is exported and re-scored with the replay's own
scorer, so the last round doubles as a reproduction check against the record.

The builds predate this module, so the experiment's manifest no longer verifies as a whole: `verify_freeze`
checks every frozen file except the two edited to add this command, neither on the scoring path.
`score_rounds` does the work and resumes; `collect` reads the scores back, `decide` applies the trigger and
`render` is the only place numbers become text.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean, median

from . import loop, manifest, pilot, records, source
from .experiments import Experiment

LAST_ROUND = loop.MAX_ROUNDS - 1  # five code reviews, four fixes
# Edited after the replay freeze to add this command: the CLI table, and an optional commit for
# `loop.materialize_head` (its HEAD path, which scored the records, is unchanged).
UNFROZEN = frozenset({"eval/__main__.py", "eval/harness/loop.py"})
TRIGGER_SHARE = 0.75
TRIGGER_TASKS = 3
TOLERANCE = 1e-9
CAPPED, PASSED, TRUNCATED = "capped", "passed", "truncated"
REVIEW, FEATURE, REGRESSIONS = "review", "feature", "regressions"
_ROUND_STEP = re.compile(r"build|fix-(\d+)")
_GIT_ENV = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1"}


class RoundError(Exception):
    pass


@dataclass(frozen=True)
class Round:
    number: int
    step: str
    commit: str


@dataclass
class Build:
    run_id: str
    task: str
    caught: bool
    stop: str
    exec_dir: Path
    rounds: list[Round]
    record: dict
    scores: dict[int, dict] = field(default_factory=dict)  # round -> replay scores, once scored

    @property
    def last(self) -> int:
        return self.rounds[-1].number


@dataclass
class Results:
    digest: str
    arm: str
    prereg: str  # path relative to eval/
    prereg_sha256: str
    builds: list[Build]
    problems: list[str]  # scheduled builds missing, unmapped or with an unscored round


@dataclass(frozen=True)
class Eligible:
    run_id: str
    task: str
    gain_round_1: float
    final_gain: float

    @property
    def ratio(self) -> float:
        return self.gain_round_1 / self.final_gain


@dataclass(frozen=True)
class Trigger:
    verdict: str  # met | not met | INCOMPLETE
    eligible: list[Eligible]
    task_means: dict[str, float]
    reasons: list[str]

    @property
    def mean(self) -> float | None:
        return mean(self.task_means.values()) if self.task_means else None

    @property
    def median(self) -> float | None:
        return median(self.task_means.values()) if self.task_means else None


def prereg_path(experiment: Experiment) -> Path:
    return manifest.EVAL_ROOT / "prereg" / f"{experiment.name}-rounds.md"


def changed_inputs(frozen: dict[str, str], current: dict[str, str]) -> list[str]:
    """Frozen files whose hash differs now (or that are gone), the allowlisted edits aside."""
    return sorted(name for name, digest in frozen.items() if name not in UNFROZEN and current.get(name) != digest)


def verify_freeze(experiment: Experiment) -> str:
    """The experiment's frozen digest, if everything on the scoring path still matches it."""
    path = manifest.manifest_path(experiment)
    if not path.is_file():
        raise manifest.ManifestMismatch(f"no manifest for {experiment.name}")
    if not prereg_path(experiment).is_file():
        raise manifest.ManifestMismatch(f"no round pre-registration: write {prereg_path(experiment).name}")
    frozen = json.loads(path.read_text(encoding="utf-8"))["files"]
    changed = changed_inputs(frozen, manifest.compute(experiment))
    if changed:
        raise manifest.ManifestMismatch("frozen inputs changed: " + ", ".join(changed))
    return manifest.digest(frozen)


def stop_kind(record: dict) -> str:
    if any(flag.startswith("timeout:") for flag in record["flags"]):
        return TRUNCATED
    return CAPPED if "review_capped" in record["flags"] else PASSED


def code_rounds(record: dict, repo: Path) -> list[Round]:
    """The record's code rounds, each matched to the one commit carrying its step's name."""
    log = subprocess.run(["git", "log", "--format=%H %s"], cwd=repo, env=_GIT_ENV, capture_output=True, text=True,
                         check=True).stdout
    by_subject: dict[str, list[str]] = {}
    for line in log.splitlines():
        commit, _, subject = line.partition(" ")
        by_subject.setdefault(subject, []).append(commit)
    rounds = []
    for step in (s["name"] for s in record["steps"]):
        match = _ROUND_STEP.fullmatch(step)
        if match is None:
            continue
        commits = by_subject.get(step, [])
        if len(commits) != 1:
            raise RoundError(f"{len(commits)} commits named {step}")
        rounds.append(Round(int(match.group(1) or 0), step, commits[0]))
    if [r.number for r in rounds] != list(range(len(rounds))) or not rounds:
        raise RoundError(f"rounds out of order or absent: {[r.step for r in rounds]}")
    return rounds


def scheduled_builds(data_root: Path, experiment: Experiment, arm: str, digest: str) -> tuple[list[Build], list[str]]:
    builds, problems = [], []
    for task in pilot.scored_tasks(experiment):
        for rep in experiment.rep_ids:
            run_id = loop.run_id(task.id, arm, rep)
            exec_dir = pilot.complete_build(data_root / "runs", task.id, arm, digest, rep)
            if exec_dir is None:
                problems.append(f"{run_id}: no complete build")
                continue
            record = records.build(exec_dir / "record.json", digest)
            try:
                rounds = code_rounds(record, exec_dir / "repo")
            except (RoundError, subprocess.CalledProcessError) as exc:
                problems.append(f"{run_id}: {exc}")
                continue
            builds.append(Build(run_id, task.id, not task.source.control, stop_kind(record), exec_dir, rounds, record))
    return builds, problems


def round_dir(data_root: Path, build: Build, number: int) -> Path:
    return data_root / "rounds" / build.run_id / build.exec_dir.name / f"round-{number}"


def score_rounds(data_root: Path, experiment: Experiment, arm: str, digest: str) -> list[str]:
    """Score every unscored round of the arm's builds; returns the rounds that failed to score."""
    builds, _ = scheduled_builds(data_root, experiment, arm, digest)
    by_id = {task.id: task for task in pilot.scored_tasks(experiment)}
    jobs = [(build, r) for build in builds for r in build.rounds if _stored(round_dir(data_root, build, r.number), r) is None]

    def score(job: tuple[Build, Round]) -> str | None:
        build, r = job
        out = round_dir(data_root, build, r.number)
        try:
            shutil.rmtree(out, ignore_errors=True)  # an interrupted attempt leaves work dirs and no score.json
            artifact = out / "artifact"
            loop.materialize_head(build.exec_dir / "repo", artifact, commit=r.commit)
            scores = source.score_build(artifact, by_id[build.task], out / "scoring")
            partial = out / "score.json.partial"
            partial.write_text(json.dumps({"round": r.number, "step": r.step, "commit": r.commit, "scores": scores},
                                          indent=2) + "\n", encoding="utf-8")
            os.replace(partial, out / "score.json")
            return None
        except Exception as exc:
            return f"{build.run_id} round {r.number}: {exc}"

    with ThreadPoolExecutor(experiment.parallel) as pool:
        return [failure for failure in pool.map(score, jobs) if failure]


def _stored(out: Path, r: Round) -> dict | None:
    try:
        stored = json.loads((out / "score.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return stored["scores"]["replay"] if stored.get("commit") == r.commit else None


def collect(data_root: Path, experiment: Experiment, arm: str, digest: str) -> Results:
    builds, problems = scheduled_builds(data_root, experiment, arm, digest)
    for build in builds:
        for r in build.rounds:
            scores = _stored(round_dir(data_root, build, r.number), r)
            if scores is None:
                problems.append(f"{build.run_id}: round {r.number} unscored")
            else:
                build.scores[r.number] = scores
    prereg = prereg_path(experiment)
    return Results(digest, arm, str(prereg.relative_to(manifest.EVAL_ROOT)),
                   hashlib.sha256(prereg.read_bytes()).hexdigest(), builds, problems)


def value(build: Build, number: int, measure: str) -> float | None:
    """A build's score at a round in points: carried forward past a passed build's last round, absent past a
    truncated one's."""
    if number > build.last and build.stop != PASSED:
        return None
    scores = build.scores.get(min(number, build.last))
    return None if scores is None or scores[measure] is None else 100 * scores[measure]


def gain(build: Build, number: int, measure: str) -> float | None:
    now, start = value(build, number, measure), value(build, 0, measure)
    return None if now is None or start is None else now - start


def decide(results: Results) -> Trigger:
    if results.problems:
        return Trigger("INCOMPLETE", [], {}, results.problems)
    eligible = []
    for build in results.builds:
        if not build.caught or build.stop == TRUNCATED:
            continue
        final = gain(build, LAST_ROUND, REVIEW)
        if final > TOLERANCE:
            eligible.append(Eligible(build.run_id, build.task, gain(build, 1, REVIEW), final))
    task_means = {task: mean(e.ratio for e in eligible if e.task == task) for task in sorted({e.task for e in eligible})}
    reasons = [f"{len(task_means)} caught tasks contribute an eligible build (needs {TRIGGER_TASKS})"]
    met = False
    if task_means:
        share = mean(task_means.values())
        reasons.append(f"mean round-1 share {share:.3f} (needs {TRIGGER_SHARE})")
        met = len(task_means) >= TRIGGER_TASKS and share >= TRIGGER_SHARE - TOLERANCE
    return Trigger("met" if met else "not met", eligible, task_means, reasons)


def reproduction(build: Build) -> list[str]:
    """How the last round's re-score differs from the record's score: per test and per defect."""
    recorded, now = build.record["scores"]["replay"], build.scores.get(build.last)
    if now is None:
        return ["last round unscored"]
    differences = [f"{test_id}: {outcome} on record, {now['outcomes'].get(test_id, 'absent')} now"
                   for test_id, outcome in sorted(recorded["outcomes"].items())
                   if now["outcomes"].get(test_id) != outcome]
    differences += [f"{test_id}: absent on record, {now['outcomes'][test_id]} now"
                    for test_id in sorted(now["outcomes"].keys() - recorded["outcomes"].keys())]
    if now["defects"] != recorded["defects"]:
        differences.append(f"defects {recorded['defects']} on record, {now['defects']} now")
    return differences


def render(results: Results, trigger: Trigger, experiment: Experiment) -> str:
    lines = [f"# Per-round scores: {experiment.name}, arm {results.arm}", "",
             f"- Rule: `eval/{results.prereg}` (sha256 `{results.prereg_sha256}`)",
             f"- Builds under manifest digest `{results.digest}`; round 0 = `build`, round k = `fix-k`", ""]
    if trigger.verdict == "INCOMPLETE":
        lines += ["**A3-2 trigger:** INCOMPLETE, withheld; A3-2 is not admitted.", "",
                  *[f"- {problem}" for problem in trigger.reasons], ""]
    else:
        lines += [f"**A3-2 trigger:** {trigger.verdict}: " + "; ".join(trigger.reasons) + ".", ""]
        lines += _trigger_lines(trigger)
    lines += _curve_lines(results)
    lines += _build_lines(results)
    lines += ["## Reproduction", "",
              "Each build's last round, re-scored, against the score its record holds, test by test.", ""]
    for build in results.builds:
        differences = reproduction(build)
        lines.append(f"- {build.run_id}: " + ("matches" if not differences else "; ".join(differences)))
    return "\n".join(lines + [""])


def _trigger_lines(trigger: Trigger) -> list[str]:
    lines = ["## A3-2 trigger", "",
             "Eligible builds: caught task, not truncated, final gain in defects avoided above round 0 positive.", ""]
    lines += _table(["Build", "Gain at round 1", "Final gain", "Ratio"],
                    [[e.run_id, f"{e.gain_round_1:+.1f}", f"{e.final_gain:+.1f}", f"{e.ratio:.3f}"]
                     for e in trigger.eligible]) + [""]
    lines += _table(["Task", "Mean ratio"], [[task, f"{share:.3f}"] for task, share in trigger.task_means.items()])
    if trigger.mean is not None:
        lines += ["", f"- Mean of task means {trigger.mean:.3f}; median {trigger.median:.3f} (sensitivity).",
                  "- A ratio above 1 means later fixes erased part of an earlier gain. The trigger admits an arm; "
                  "it is not evidence that two rounds suffice."]
    return lines + [""]


def _curve_lines(results: Results) -> list[str]:
    caught = [b for b in results.builds if b.caught]
    lines = ["## Change from round 0, by round", "",
             "Mean over the builds with a state at that round (n). Defects avoided and feature tests over caught "
             "tasks; existing tests broken over every task. Build-weighted and descriptive: after a truncation the "
             "remaining builds weight the tasks unequally.", ""]
    rows = []
    for number in range(LAST_ROUND + 1):
        cells = []
        for measure, pool in ((REVIEW, caught), (FEATURE, caught), (REGRESSIONS, results.builds)):
            gains = [g for g in (gain(b, number, measure) for b in pool) if g is not None]
            cells.append(f"{mean(gains):+.1f} (n={len(gains)})" if gains else "—")
        rows.append([str(number), *cells])
    return lines + _table(["Round", "Defects avoided", "Feature tests passed", "Existing tests broken"], rows) + [""]


def _build_lines(results: Results) -> list[str]:
    lines = ["## Per build", "", "Defects avoided / feature tests passed / existing tests broken, in points; "
             "a carried-forward state is marked *.", ""]
    rows = []
    for build in results.builds:
        cells = []
        for number in range(LAST_ROUND + 1):
            if value(build, number, FEATURE) is None:  # past a truncated build's last round, or unscored
                cells.append("")
                continue
            values = [value(build, number, m) for m in (REVIEW, FEATURE, REGRESSIONS)]
            text = " / ".join("—" if v is None else f"{v:.1f}" for v in values)
            cells.append(text + ("*" if number > build.last else ""))
        rows.append([build.run_id, build.stop, *cells])
    return lines + _table(["Build", "Stop", *[f"Round {n}" for n in range(LAST_ROUND + 1)]], rows) + [""]


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + ["| " + " | ".join(row) + " |" for row in rows]
