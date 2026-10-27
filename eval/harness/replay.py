"""The report of a replay experiment, from raw run records.

Each task × arm cell holds `reps` builds; the unit of comparison is the task mean over them. Caught tasks
carry defects review caught historically; control tasks carry none and count only towards the regression
guard and the descriptive tables. The arms are staged: A1 and A3 always run, A2 only once the loop effect
(A3 vs A1) passes, so a run without A2 is complete when the loop effect does not pass. `collect` reads records into plain values, `decide` applies the
pre-registered rule (eval/prereg/replay.md), and `render` is the only place numbers become text.
"""

import math
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean, stdev

from . import loop, manifest, pilot, records
from .experiments import Experiment

REVIEW_THRESHOLD = 15.0
FEATURE_LOSS_LIMIT = 5.0
WIN_SHARE = 2 / 3
# Above this comparator mean, +REVIEW_THRESHOLD is out of reach.
HEADROOM = 100.0 - REVIEW_THRESHOLD
# Pass fractions over different denominators make exact thresholds land a few ulps short.
TOLERANCE = 1e-9
REVIEW, FEATURE, REGRESSIONS = "review", "feature", "regressions"
ALWAYS_RUN = ("A1", "A3")


@dataclass
class Results:
    digest: str
    caught: list[str]
    controls: list[str]
    arms: list[str]
    missing: dict[str, list[str]] = field(default_factory=dict)  # arm -> run ids without a complete build
    builds: dict[str, dict[str, list[dict]]] = field(default_factory=dict)  # arm -> task -> records

    @property
    def tasks(self) -> list[str]:
        return self.caught + self.controls

    @property
    def complete_arms(self) -> list[str]:
        return [arm for arm in self.arms if not self.missing.get(arm)]


@dataclass(frozen=True)
class Comparison:
    name: str
    treatment: str
    comparator: str
    outcome: str  # effect | no effect | uninformative | not run
    reasons: list[str]


@dataclass(frozen=True)
class Decision:
    verdict: str  # DECIDED | INCOMPLETE
    comparisons: list[Comparison]
    reasons: list[str]


def collect(data_root: Path, experiment: Experiment) -> Results:
    digest = manifest.verify(experiment)
    task_list = pilot.scored_tasks(experiment)
    results = Results(digest=digest, caught=[t.id for t in task_list if not t.source.control],
                      controls=[t.id for t in task_list if t.source.control], arms=list(experiment.arm_names))
    for arm in experiment.arm_names:
        for task in task_list:
            for rep in experiment.rep_ids:
                exec_dir = pilot.complete_build(data_root / "runs", task.id, arm, digest, rep)
                record = None if exec_dir is None else records.build(exec_dir / "record.json", digest)
                if record is None or "replay" not in record["scores"]:
                    results.missing.setdefault(arm, []).append(loop.run_id(task.id, arm, rep))
                    continue
                results.builds.setdefault(arm, {}).setdefault(task.id, []).append(record)
    return results


def task_mean(results: Results, arm: str, task: str, measure: str) -> float:
    return mean(100 * record["scores"]["replay"][measure] for record in results.builds[arm][task])


def arm_mean(results: Results, arm: str, measure: str, tasks: list[str]) -> float:
    return mean(task_mean(results, arm, task, measure) for task in tasks)


def wins_needed(results: Results) -> int:
    return math.ceil(WIN_SHARE * len(results.caught))


def compare(results: Results, name: str, treatment: str, comparator: str) -> Comparison:
    caught = results.caught
    base_mean = arm_mean(results, comparator, REVIEW, caught)
    open_tasks = sum(task_mean(results, comparator, task, REVIEW) < 100 - TOLERANCE for task in caught)
    needed = wins_needed(results)
    if base_mean > HEADROOM + TOLERANCE or open_tasks < needed:
        return Comparison(name, treatment, comparator, "uninformative",
                          [f"{comparator} review mean {base_mean:.1f}, {open_tasks} caught tasks below 100 "
                           f"({needed} wins needed)"])
    gain = arm_mean(results, treatment, REVIEW, caught) - base_mean
    wins = sum(task_mean(results, treatment, task, REVIEW) - task_mean(results, comparator, task, REVIEW) > TOLERANCE
               for task in caught)
    feature_change = arm_mean(results, treatment, FEATURE, caught) - arm_mean(results, comparator, FEATURE, caught)
    regression_change = (arm_mean(results, treatment, REGRESSIONS, results.tasks)
                         - arm_mean(results, comparator, REGRESSIONS, results.tasks))
    checks = [
        (gain >= REVIEW_THRESHOLD - TOLERANCE, f"review {gain:+.1f} points (needs +{REVIEW_THRESHOLD:.0f})"),
        (wins >= needed, f"higher on {wins}/{len(caught)} caught tasks (needs {needed})"),
        (feature_change >= -FEATURE_LOSS_LIMIT - TOLERANCE,
         f"feature {feature_change:+.1f} points (floor −{FEATURE_LOSS_LIMIT:.0f})"),
        (regression_change <= TOLERANCE, f"regressions {regression_change:+.1f} points (must not rise)"),
    ]
    outcome = "effect" if all(ok for ok, _ in checks) else "no effect"
    return Comparison(name, treatment, comparator, outcome, [text for _, text in checks])


def decide(results: Results, experiment: Experiment) -> Decision:
    if not results.caught:
        return Decision("INCOMPLETE", [], ["no caught tasks"])
    gaps = sum(len(results.missing.get(arm, [])) for arm in ALWAYS_RUN)
    if gaps:
        return Decision("INCOMPLETE", [], [f"{gaps} A1/A3 builds missing"])
    loop_effect = compare(results, "loop", "A3", "A1")
    if loop_effect.outcome != "effect":
        not_run = Comparison("cross-family", "A3", "A2", "not run", ["A2 runs only when the loop effect passes"])
        return Decision("DECIDED", [loop_effect, not_run], [])
    if results.missing.get("A2"):
        return Decision("INCOMPLETE", [loop_effect],
                        [f"the loop effect passed, so A2 is required: {len(results.missing['A2'])} A2 builds missing"])
    return Decision("DECIDED", [loop_effect, compare(results, "cross-family", "A3", "A2")], [])


def render(results: Results, decision: Decision, experiment: Experiment) -> str:
    lines = [f"# {experiment.title}", ""]
    if decision.verdict == "INCOMPLETE":
        lines += ["**Verdict:** INCOMPLETE, no verdict — " + "; ".join(decision.reasons), ""]
    lines += [f"**{c.name.capitalize()} ({c.treatment} vs {c.comparator}):** {c.outcome} — "
              + "; ".join(c.reasons) for c in decision.comparisons] + ([""] if decision.comparisons else [])
    lines += [f"- Manifest digest: `{results.digest}`",
              f"- Caught tasks: {', '.join(results.caught) or 'none'}; controls: {', '.join(results.controls) or 'none'}",
              f"- Arms: {', '.join(results.arms)}; replicates per cell: {experiment.reps}", ""]
    if decision.verdict == "INCOMPLETE":
        missing = [f"- build {run_id}" for arm in results.arms for run_id in results.missing.get(arm, [])]
        return "\n".join(lines + ["## Missing", "", *missing, ""])
    arms = results.complete_arms
    tables = ((REVIEW, "Review-caught defects avoided (%)", results.caught),
              (FEATURE, "Feature tests passed (%)", results.tasks),
              (REGRESSIONS, "Existing tests broken (%)", results.tasks))
    for measure, heading, task_ids in tables:
        lines += [f"## {heading}", "", "Task mean ± SD over replicates; arm mean is the mean of task means.", ""]
        rows = [[arm, *[_cell(results, arm, task, measure) for task in task_ids],
                 f"{arm_mean(results, arm, measure, task_ids):.1f}"] for arm in arms]
        lines += _table(["Arm", *task_ids, "Arm mean"], rows) + [""]
    if "A2" in arms:
        a2_gain = arm_mean(results, "A2", REVIEW, results.caught) - arm_mean(results, "A1", REVIEW, results.caught)
        lines += [f"- A2 − A1 review (descriptive): {a2_gain:+.1f}", ""]
    else:
        lines += ["- A2 not run: the loop effect did not pass.", ""]
    lines += ["## Diagnostics", ""]
    lines += _table(["Arm", "Plan rounds", "Phase rounds", "Capped", "Timeouts", "Model minutes"],
                    [_diagnostics_row(results, arm) for arm in arms]) + [""]
    lines += ["## Limits", "",
              "- One source repository, built with this same loop; its caught tasks are selected on phases where"
              " review found something, so controls check damage to existing behaviour, not selection.",
              "- A3 vs A1 measures the whole loop package (plan, rubric, review, extra compute); A3 vs A2 measures"
              " the reviewer configuration, its CLI included.",
              "- A few tasks with two replicates each is an exploratory continuation rule, not a significance test.", ""]
    return "\n".join(lines)


def _diagnostics_row(results: Results, arm: str) -> list[str]:
    builds = [record for by_task in results.builds[arm].values() for record in by_task]
    arm_flags = [flag for record in builds for flag in record["flags"]]
    seconds = sum(attempt["seconds"] for record in builds for step in record["steps"] for attempt in step["attempts"])
    return [arm,
            f"{mean(record['rounds'].get('plan', 0) for record in builds):.1f}",
            f"{mean(record['rounds'].get('phase', 0) for record in builds):.1f}",
            str(sum(flag.endswith("_capped") for flag in arm_flags)),
            str(sum(flag.startswith("timeout:") for flag in arm_flags)),
            f"{seconds / 60:.1f}"]


def _cell(results: Results, arm: str, task: str, measure: str) -> str:
    values = [100 * record["scores"]["replay"][measure] for record in results.builds[arm][task]]
    return f"{mean(values):.1f} ± {stdev(values) if len(values) > 1 else 0.0:.1f}"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + ["| " + " | ".join(row) + " |" for row in rows]
