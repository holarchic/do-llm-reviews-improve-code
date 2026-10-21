"""The report of a replicated, judge-free experiment (pilot2), from raw run records.

Each task × arm cell holds `reps` builds. The unit of comparison is the task
mean: the mean over a cell's replicates. `collect` reads records into plain
values, `decide` applies pilot2's pre-registered rule (eval/prereg/pilot2.md),
and `render` is the only place numbers become text.
"""

from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean, stdev

from . import loop, manifest, openrouter, pilot, records
from .experiments import Experiment

PRIMARY_THRESHOLD = 10.0
PLANTED_THRESHOLD = 15.0
PRIMARY_LOSS_LIMIT = 5.0
TASK_WINS_NEEDED = 4
CEILING = 90.0
BAND = (40.0, 70.0)
# Pass fractions over different denominators make exact thresholds land a few ulps short.
TOLERANCE = 1e-9
PRIMARY, PLANTED = "primary", "planted"


@dataclass
class Results:
    digest: str
    tasks: list[str]
    arms: list[str]
    missing: list[str] = field(default_factory=list)
    builds: dict[str, dict[str, list[dict]]] = field(default_factory=dict)  # arm -> task -> records
    spend: float | None = None


@dataclass(frozen=True)
class Decision:
    verdict: str  # LOOP EFFECT | NO LOOP EFFECT | INCOMPLETE | DESCRIPTIVE
    reasons: list[str]
    ceiling: bool


def collect(data_root: Path, experiment: Experiment) -> Results:
    """data_root is the experiment's own data dir."""
    digest = manifest.verify(experiment)
    runs_root = data_root / "runs"
    results = Results(digest=digest, tasks=list(experiment.tasks), arms=list(experiment.arm_names),
                      spend=openrouter.spent(data_root / pilot.SPEND_LEDGER, "scored"))
    for arm in experiment.arm_names:
        for task_id in experiment.tasks:
            for rep in experiment.rep_ids:
                exec_dir = pilot.complete_build(runs_root, task_id, arm, digest, rep)
                record = None if exec_dir is None else records.build(exec_dir / "record.json", digest)
                if record is None or "planted" not in record["scores"]:
                    results.missing.append(f"build {loop.run_id(task_id, arm, rep)}")
                    continue
                results.builds.setdefault(arm, {}).setdefault(task_id, []).append(record)
    return results


def value(record: dict, measure: str) -> float:
    scores = record["scores"]
    return 100 * (scores["primary"] if measure == PRIMARY else scores["planted"]["fraction"])


def task_mean(results: Results, arm: str, task: str, measure: str) -> float:
    return mean(value(record, measure) for record in results.builds[arm][task])


def arm_mean(results: Results, arm: str, measure: str) -> float:
    return mean(task_mean(results, arm, task, measure) for task in results.tasks)


def task_wins(results: Results, arm: str, base: str, measure: str) -> int:
    """Tasks where the arm's task mean is strictly above the baseline's."""
    return sum(task_mean(results, arm, task, measure) - task_mean(results, base, task, measure) > TOLERANCE
               for task in results.tasks)


def flags(results: Results, arm: str) -> list[str]:
    return [flag for by_task in results.builds.get(arm, {}).values() for record in by_task for flag in record["flags"]]


def decide(results: Results, experiment: Experiment) -> Decision:
    if results.missing:
        return Decision("INCOMPLETE", [f"{len(results.missing)} builds missing"], False)
    unrouted = [flag for arm in results.arms for flag in flags(results, arm) if flag.startswith("unrouted_model:")]
    if unrouted:
        return Decision("INCOMPLETE", [f"routing violation: {len(unrouted)} sessions ran an unrouted model"], False)
    base = experiment.baseline
    ceiling = arm_mean(results, base, PRIMARY) >= CEILING - TOLERANCE
    if experiment.decision_rule != "pilot2":
        return Decision("DESCRIPTIVE", [], ceiling)
    reasons = []
    for arm in experiment.treatments:
        primary_gain = arm_mean(results, arm, PRIMARY) - arm_mean(results, base, PRIMARY)
        planted_gain = arm_mean(results, arm, PLANTED) - arm_mean(results, base, PLANTED)
        primary_wins = task_wins(results, arm, base, PRIMARY)
        planted_wins = task_wins(results, arm, base, PLANTED)
        if (not ceiling and primary_gain >= PRIMARY_THRESHOLD - TOLERANCE
                and primary_wins >= TASK_WINS_NEEDED):
            reasons.append(f"correctness: {arm} beats {base} by {primary_gain:+.1f} points, on {primary_wins}/"
                           f"{len(results.tasks)} tasks")
        if (planted_gain >= PLANTED_THRESHOLD - TOLERANCE and planted_wins >= TASK_WINS_NEEDED
                and primary_gain >= -PRIMARY_LOSS_LIMIT - TOLERANCE):
            reasons.append(f"test quality: {arm} catches {planted_gain:+.1f} points more planted bugs than {base}, "
                           f"on {planted_wins}/{len(results.tasks)} tasks (primary {primary_gain:+.1f})")
    return Decision("LOOP EFFECT" if reasons else "NO LOOP EFFECT", reasons, ceiling)


def session_cost(model: str, usage: dict) -> float:
    """Estimated dollars at the pinned list prices. Claude Code reports cache reads apart from input;
    Codex counts cached tokens inside input_tokens."""
    price = openrouter.PRICES.get(model)
    if price is None:
        return 0.0
    if "cached_input_tokens" in usage:
        cached = usage.get("cached_input_tokens", 0)
        uncached = usage.get("input_tokens", 0) - cached
    else:
        cached = usage.get("cache_read_input_tokens", 0)
        uncached = usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
    return (uncached * price.input + cached * price.cache_read + usage.get("output_tokens", 0) * price.output) / 1e6


def build_cost(record: dict, experiment: Experiment) -> float:
    arm = experiment.arm(record["arm"])
    total = 0.0
    for step in record["steps"]:
        agent = arm.reviewer if "review" in step["name"] else arm.writer
        total += sum(session_cost(agent.model, attempt["usage"]) for attempt in step["attempts"])
    return total


def render(results: Results, decision: Decision, experiment: Experiment) -> str:
    lines = [f"# {experiment.title}", "", f"**Verdict:** {_verdict_text(decision)}", ""]
    lines += [f"- Manifest digest: `{results.digest}`",
              f"- Tasks: {len(results.tasks)}; arms: {', '.join(results.arms)}; replicates per cell: {experiment.reps}",
              f"- OpenRouter spend on scored runs: {_money(results.spend)} (cap {_money(experiment.budget_usd)},"
              " checked when each job starts)", ""]
    if results.missing:
        lines += ["## Missing", "", *[f"- {item}" for item in results.missing], ""]
        return "\n".join(lines)
    base = experiment.baseline
    for measure, heading in ((PRIMARY, "Hidden tests passed (%)"), (PLANTED, "Planted bugs caught by the build's own tests (%)")):
        lines += [f"## {heading}", "", "Task mean ± SD over replicates; arm mean is the mean of task means.", ""]
        rows = [[arm, *[_cell(results, arm, task, measure) for task in results.tasks],
                 _num(arm_mean(results, arm, measure))] for arm in results.arms]
        lines += _table(["Arm", *results.tasks, "Arm mean"], rows) + [""]
        for arm in experiment.treatments:
            diffs = [task_mean(results, arm, task, measure) - task_mean(results, base, task, measure)
                     for task in results.tasks]
            lines.append(f"- {arm} − {base}: {', '.join(f'{d:+.1f}' for d in diffs)}; arm mean "
                         f"{arm_mean(results, arm, measure) - arm_mean(results, base, measure):+.1f}; above {base} on "
                         f"{task_wins(results, arm, base, measure)}/{len(results.tasks)} tasks")
        if len(experiment.treatments) == 2:
            same, cross = experiment.treatments
            lines.append(f"- {cross} − {same} (descriptive): "
                         f"{arm_mean(results, cross, measure) - arm_mean(results, same, measure):+.1f}")
        lines.append("")
    low, high = BAND
    baseline = arm_mean(results, base, PRIMARY)
    if not low <= baseline <= high:
        lines += [f"- {base}'s mean primary ({baseline:.1f}) fell outside the calibration band {low:.0f}–{high:.0f}.", ""]
    lines += ["## Builds' own tests", ""]
    lines += _table(["Arm", "Collected", "Pass on reference", "Fail on reference", "Load errors"],
                    [[arm, *[_num(_mean_over_builds(results, arm, lambda r, k=key: r["scores"]["planted"]["own_tests"][k]))
                             for key in ("collected", "pass_on_reference", "fail_on_reference", "load_errors")]]
                     for arm in results.arms]) + [""]
    lines += ["## Static metrics (mean per build)", ""]
    static_keys = ("ruff_violations", "source_loc", "test_loc", "max_function_branches")
    lines += _table(["Arm", *static_keys],
                    [[arm, *[_num(_mean_over_builds(results, arm, lambda r, k=key: r["scores"]["static"][k]))
                             for key in static_keys]] for arm in results.arms]) + [""]
    lines += ["## Diagnostics", ""]
    lines += _table(["Arm", "Plan rounds", "Phase rounds", "Capped", "Timeouts", "Key exposed", "Model minutes",
                     "Est. cost ($)"],
                    [_diagnostics_row(results, arm, experiment) for arm in results.arms]) + [""]
    lines += ["## Limits", "",
              "- Five tasks with a few replicates each is a pilot, not a significance test.",
              "- The tasks were tuned until the solo arm passed about 40–70% (calibration runs discarded), so fresh"
              " solo runs can regress either way.",
              "- The planted-bug score counts only the builds' tests that pass on the reference implementation.",
              "- OpenRouter routes each request among providers and quantisations; every arm shares that routing.",
              "- Estimated cost uses list prices pinned at freeze; the spend line is the key's actual usage.", ""]
    return "\n".join(lines)


def _diagnostics_row(results: Results, arm: str, experiment: Experiment) -> list[str]:
    builds = [record for by_task in results.builds[arm].values() for record in by_task]
    arm_flags = flags(results, arm)
    seconds = sum(attempt["seconds"] for record in builds for step in record["steps"] for attempt in step["attempts"])
    return [arm,
            _num(mean(record["rounds"].get("plan", 0) for record in builds)),
            _num(mean(record["rounds"].get("phase", 0) for record in builds)),
            str(sum(flag.endswith("_capped") for flag in arm_flags)),
            str(sum(flag.startswith("timeout:") for flag in arm_flags)),
            str(sum(flag.startswith("key_exposed:") for flag in arm_flags)),
            _num(seconds / 60),
            f"{sum(build_cost(record, experiment) for record in builds):.2f}"]


def _mean_over_builds(results: Results, arm: str, pick) -> float:
    return mean(pick(record) for by_task in results.builds[arm].values() for record in by_task)


def _cell(results: Results, arm: str, task: str, measure: str) -> str:
    values = [value(record, measure) for record in results.builds[arm][task]]
    spread = stdev(values) if len(values) > 1 else 0.0
    return f"{mean(values):.1f} ± {spread:.1f}"


def _num(number: float) -> str:
    return f"{number:.1f}"


def _money(amount: float | None) -> str:
    return "n/a" if amount is None else f"${amount:.2f}"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + ["| " + " | ".join(row) + " |" for row in rows]


def _verdict_text(decision: Decision) -> str:
    if decision.verdict == "LOOP EFFECT":
        return "loop effect — " + "; ".join(decision.reasons)
    if decision.verdict == "NO LOOP EFFECT":
        note = " (correctness branch uninformative: solo at ceiling)" if decision.ceiling else ""
        return f"no loop effect at the pre-registered thresholds{note}"
    if decision.verdict == "INCOMPLETE":
        return "INCOMPLETE, no verdict — " + "; ".join(decision.reasons)
    return "descriptive (no pre-registered rule)"
