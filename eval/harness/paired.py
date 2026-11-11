"""The report of a paired experiment, from raw run records.

Each task has one solo build and, for every cell arm, `reps` review cells that each ran on a copy of that
build. A cell is compared with the build it started from, so every build is its own control. `collect`
reads records into plain values, `decide` applies the registered rule (eval/prereg/<experiment>.md), and
`render` is the only place numbers become text.

Each cell arm gets two statements against the build: gain (review-caught defects avoided) and harm
(feature tests passed, existing tests broken). The contrast between two cell arms is an estimate with an
interval and no verdict word. A number with nothing to average is unavailable, never zero.
"""

import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

from . import cells, guards, ledger, loop, manifest, pilot, records, spend
from .experiments import Experiment

REVIEW_THRESHOLD = 15.0
RESAMPLES = 10_000
INTERVAL = (0.025, 0.975)
REVIEW, FEATURE, REGRESSIONS = "review", "feature", "regressions"
REMOVED, BROUGHT_BACK = "removed", "brought back"
ENDPOINTS = (REVIEW, REMOVED, BROUGHT_BACK, FEATURE, REGRESSIONS)
INCOMPLETE, UNINFORMATIVE, DECIDED = "INCOMPLETE", "UNINFORMATIVE", "DECIDED"
UNAVAILABLE = "unavailable"

_DISPOSITION = re.compile(r"^\W*(?:finding\s+)?(\d+)\W+(fixed|left)\b", re.IGNORECASE | re.MULTILINE)
_NO_FINDINGS = re.compile(r"^\W*no findings\W*$", re.IGNORECASE | re.MULTILINE)


@dataclass
class RunCosts:
    """What an arm's scheduled runs cost: the executions that were scored, and every other saved one."""
    runs: int = 0
    scored: ledger.Totals = field(default_factory=ledger.Totals)
    unscored: ledger.Totals = field(default_factory=ledger.Totals)


@dataclass
class Results:
    digest: str
    baseline: str
    cell_arms: list[str]
    caught: list[str]
    controls: list[str]
    builds: dict[str, dict] = field(default_factory=dict)  # task -> its build's record
    cells: dict[str, dict[str, list[dict]]] = field(default_factory=dict)  # cell arm -> task -> records, by run
    regression_tests: dict[str, list[str]] = field(default_factory=dict)  # task -> its frozen regression test ids
    costs: dict[str, RunCosts] = field(default_factory=dict)  # arm, the baseline included
    missing: list[str] = field(default_factory=list)  # scheduled runs with no complete record
    invalid: list[str] = field(default_factory=list)  # cells that started from another build than their task's

    @property
    def tasks(self) -> list[str]:
        return self.caught + self.controls


@dataclass(frozen=True)
class Comparison:
    """One cell arm against the builds its cells started from."""
    arm: str
    gain: float  # review, mean over caught tasks of cell minus build, in points
    wins: int  # caught tasks on which the cell is higher
    needed: int
    feature: float
    feature_build: float
    regressions: guards.RegressionGuard

    @property
    def gain_met(self) -> bool:
        return self.gain >= REVIEW_THRESHOLD - guards.TOLERANCE and self.wins >= self.needed

    @property
    def feature_ok(self) -> bool:
        return guards.feature_ok(self.feature, self.feature_build)

    @property
    def harm_ok(self) -> bool:
        return self.feature_ok and self.regressions.passed


@dataclass(frozen=True)
class Estimate:
    """The second cell arm minus the first on one endpoint, over the endpoint's eligible tasks."""
    endpoint: str
    tasks: int
    mean: float | None  # None: no eligible task
    interval: tuple[float, float] | None  # None: fewer than two tasks
    leave_one_out: tuple[float, float] | None


@dataclass(frozen=True)
class Decision:
    verdict: str  # INCOMPLETE | UNINFORMATIVE | DECIDED
    headroom: guards.Headroom | None = None
    comparisons: tuple[Comparison, ...] = ()  # only when DECIDED
    estimates: tuple[Estimate, ...] = ()  # only with two cell arms
    run_spread: dict[str, float] = field(default_factory=dict)  # cell arm -> review points between its runs


@dataclass(frozen=True)
class FixReport:
    """What a fixer said it did with the reviewer's findings; both zero when it said there were none."""
    fixed: int
    left: int


def collect(data_root: Path, experiment: Experiment) -> Results:
    digest = manifest.verify(experiment)
    task_list = pilot.scored_tasks(experiment)
    runs_root = data_root / "runs"
    results = Results(digest=digest, baseline=experiment.baseline, cell_arms=list(experiment.cell_arms),
                      caught=[t.id for t in task_list if not t.source.control],
                      controls=[t.id for t in task_list if t.source.control])
    for task in task_list:
        results.regression_tests[task.id] = task.expected()["regression"]
        build_id = loop.run_id(task.id, experiment.baseline)
        build_exec = pilot.complete_build(runs_root, task.id, experiment.baseline, digest)
        _add_cost(results, experiment, experiment.baseline, runs_root / build_id, build_exec)
        if build_exec is None:
            results.missing.append(build_id)
        else:
            results.builds[task.id] = records.build(build_exec / "record.json", digest)
        build = None if build_exec is None else cells.pairing(build_exec)
        for arm in experiment.cell_arms:
            for rep in cells.cell_runs(experiment):
                run_id = loop.run_id(task.id, arm, rep)
                exec_dir = cells.complete_cell(runs_root, task.id, arm, digest, rep)
                _add_cost(results, experiment, arm, runs_root / run_id, exec_dir)
                if exec_dir is None:
                    results.missing.append(run_id)
                elif build is not None:  # without its build a cell has no comparator; the build is listed missing
                    record = records.cell(exec_dir / "record.json", digest)
                    if cells.pairing_of(record) != build:
                        results.invalid.append(run_id)
                    else:
                        results.cells.setdefault(arm, {}).setdefault(task.id, []).append(record)
    return results


def _add_cost(results: Results, experiment: Experiment, arm: str, run_root: Path, scored_exec: Path | None) -> None:
    run = spend.run_cost(run_root, scored_exec, experiment.arm(arm))
    costs = results.costs.setdefault(arm, RunCosts())
    if run.scored is not None:
        costs.runs += 1
        costs.scored.merge(run.scored.totals())
    for unscored in run.unscored:
        costs.unscored.merge(unscored.totals())


def value(record: dict, build: dict, endpoint: str) -> float | None:
    """One run's value on an endpoint, in points; None where the build leaves nothing to count (a control
    has no review score; a build that shipped no defect has none to remove)."""
    replay = record["scores"]["replay"]
    if endpoint in (REVIEW, FEATURE, REGRESSIONS):
        return None if replay[endpoint] is None else 100 * replay[endpoint]
    kept = _avoided(build)
    pool = kept if endpoint == BROUGHT_BACK else {d["id"] for d in build["scores"]["replay"]["defects"]} - kept
    if not pool:
        return None
    now = _avoided(record)
    return 100 * len(pool - now if endpoint == BROUGHT_BACK else pool & now) / len(pool)


def _avoided(record: dict) -> set[str]:
    return {d["id"] for d in record["scores"]["replay"]["defects"] if d["avoided"]}


def eligible(results: Results, endpoint: str) -> list[str]:
    """The endpoint's tasks. The builds alone decide them, so every cell arm is read on the same tasks."""
    pool = results.tasks if endpoint == REGRESSIONS else results.caught
    return [task for task in pool if build_value(results, task, endpoint) is not None]


def build_value(results: Results, task: str, endpoint: str) -> float | None:
    return value(results.builds[task], results.builds[task], endpoint)


def cell_value(results: Results, arm: str, task: str, endpoint: str) -> float:
    """A cell's task value on an endpoint the task is eligible for: the mean of its runs."""
    return mean(value(record, results.builds[task], endpoint) for record in results.cells[arm][task])


def decide(results: Results, experiment: Experiment) -> Decision:
    if results.missing or results.invalid or not results.caught:
        return Decision(INCOMPLETE)
    room = guards.headroom({task: build_value(results, task, REVIEW) for task in results.caught}, REVIEW_THRESHOLD)
    arms = results.cell_arms
    estimates = tuple(estimate(results, arms[0], arms[1], endpoint, experiment.seed)
                      for endpoint in ENDPOINTS) if len(arms) == 2 else ()
    spread = {arm: mean(_run_range(results, arm, task) for task in results.caught) for arm in arms}
    if not room.ok:
        return Decision(UNINFORMATIVE, room, (), estimates, spread)
    return Decision(DECIDED, room, tuple(compare(results, arm) for arm in arms), estimates, spread)


def _run_range(results: Results, arm: str, task: str) -> float:
    values = [value(record, results.builds[task], REVIEW) for record in results.cells[arm][task]]
    return max(values) - min(values)


def compare(results: Results, arm: str) -> Comparison:
    caught = results.caught
    gains = [cell_value(results, arm, task, REVIEW) - build_value(results, task, REVIEW) for task in caught]
    return Comparison(
        arm=arm, gain=mean(gains), wins=sum(gain > guards.TOLERANCE for gain in gains),
        needed=guards.wins_needed(len(caught)),
        feature=mean(cell_value(results, arm, task, FEATURE) for task in caught),
        feature_build=mean(build_value(results, task, FEATURE) for task in caught),
        regressions=guards.regression_guard([_task_regressions(results, arm, task) for task in results.tasks]))


def _task_regressions(results: Results, arm: str, task: str) -> guards.TaskRegressions:
    """Each run of the cell against the build it started from: the build's own failures are the comparator."""
    tests = results.regression_tests[task]
    runs = results.cells[arm][task]
    return guards.TaskRegressions(task, len(tests), tuple(_failures(record, tests) for record in runs),
                                  (_failures(results.builds[task], tests),) * len(runs))


def _failures(record: dict, tests: list[str]) -> int:
    return guards.failures(record["scores"]["replay"]["outcomes"], tests)


def estimate(results: Results, first: str, second: str, endpoint: str, seed: int) -> Estimate:
    """`second` minus `first`: the mean over the endpoint's tasks, a percentile interval from resampling those
    tasks, and the range of the mean with each task left out in turn."""
    diffs = [cell_value(results, second, task, endpoint) - cell_value(results, first, task, endpoint)
             for task in eligible(results, endpoint)]
    if len(diffs) < 2:
        return Estimate(endpoint, len(diffs), diffs[0] if diffs else None, None, None)
    rng = random.Random(f"{seed}:{endpoint}")
    means = sorted(sum(rng.choices(diffs, k=len(diffs))) / len(diffs) for _ in range(RESAMPLES))
    low, high = (means[round(share * (RESAMPLES - 1))] for share in INTERVAL)
    without = [(sum(diffs) - diff) / (len(diffs) - 1) for diff in diffs]
    return Estimate(endpoint, len(diffs), mean(diffs), (low, high), (min(without), max(without)))


def fix_report(fix_text: str) -> FixReport | None:
    """The fixer's own account of each finding, from its closing lines; None when it gave none (no reply, or
    neither a `fixed`/`left` line nor `no findings`). A later line for the same finding replaces an earlier."""
    said = {int(number): word.lower() for number, word in _DISPOSITION.findall(fix_text)}
    if said:
        return FixReport(sum(word == "fixed" for word in said.values()), sum(word == "left" for word in said.values()))
    return FixReport(0, 0) if _NO_FINDINGS.search(fix_text) else None


def render(results: Results, decision: Decision, experiment: Experiment) -> str:
    lines = [f"# {experiment.title}", "", *_verdict_lines(results, decision), "",
             f"- Manifest digest: `{results.digest}`",
             f"- Caught tasks: {', '.join(results.caught) or 'none'}; controls: {', '.join(results.controls) or 'none'}",
             f"- Cells: {', '.join(results.cell_arms)}, each run {experiment.reps} times on its task's one "
             f"{results.baseline} build", ""]
    if decision.verdict == INCOMPLETE:
        return "\n".join(lines + _gap_lines(results))
    arms = results.cell_arms
    for endpoint, heading, tasks, mean_of in (
            (REVIEW, "Review-caught defects avoided (%)", results.caught, "Mean, caught tasks"),
            (FEATURE, "Feature tests passed (%)", results.tasks, "Mean, caught tasks"),
            (REGRESSIONS, "Existing tests broken (%)", results.tasks, "Mean, all tasks")):
        lines += [f"## {heading}", "", f"A cell's value is the mean of its runs, shown after it; {results.baseline} "
                  "is the build each cell started from.", ""]
        rows = [[task, _points(build_value(results, task, endpoint)),
                 *[_cell(results, arm, task, endpoint) for arm in arms]] for task in tasks]
        counted = eligible(results, endpoint)
        rows.append([mean_of, _points(mean(build_value(results, task, endpoint) for task in counted)),
                     *[_points(mean(cell_value(results, arm, task, endpoint) for task in counted)) for arm in arms]])
        lines += _table(["Task", results.baseline, *arms], rows) + [""]
    lines += _defect_lines(results) + _fixer_lines(results) + _cost_lines(results) + _diagnostic_lines(results)
    lines += _estimate_lines(results, decision)
    lines += ["## Limits", "",
              "- The caught tasks are phases where a Codex reviewer historically found something in Claude's code, "
              "so a gap in the other family's favour may be home turf; only its absence is informative.",
              "- The source repositories are one operator's, built with the same loop.",
              "- One writer, one round of plain review, on audited specs. This says nothing about the build loop "
              "the plugin ships, nor about design quality.",
              "- A dozen or so tasks give a continuation rule, not a significance test.", ""]
    return "\n".join(lines)


def _verdict_lines(results: Results, decision: Decision) -> list[str]:
    if decision.verdict == INCOMPLETE:
        gaps = [f"scheduled runs missing: {len(results.missing)}" if results.missing else "",
                f"cells paired with another build: {len(results.invalid)}" if results.invalid else "",
                "" if results.caught else "no caught task"]
        return ["**Verdict:** INCOMPLETE, no statement — " + "; ".join(gap for gap in gaps if gap)]
    room = decision.headroom
    if decision.verdict == UNINFORMATIVE:
        return [f"**Verdict:** uninformative, no statement — the builds' review mean is {room.comparator_mean:.1f} "
                f"(limit {room.limit:.1f}) with {room.open_tasks} caught tasks below 100 ({room.needed} needed): "
                f"no room for a +{REVIEW_THRESHOLD:.0f} gain"]
    return [f"- **{c.arm} vs {results.baseline}:** gain {'met' if c.gain_met else 'not met'} — review {c.gain:+.1f} "
            f"points (needs +{REVIEW_THRESHOLD:.0f}), higher on {c.wins}/{len(results.caught)} caught tasks (needs "
            f"{c.needed}); harm {'within' if c.harm_ok else 'outside'} tolerance — feature "
            f"{c.feature - c.feature_build:+.1f} points (floor −{guards.FEATURE_FLOOR:.0f}), existing tests broken "
            f"mean Δ {float(c.regressions.mean):+.2f} (limit {float(guards.MEAN_LIMIT):.1f}), tasks over the "
            f"{float(guards.TASK_LIMIT):.1f} cap: {', '.join(c.regressions.over_cap) or 'none'}"
            for c in decision.comparisons]


def _gap_lines(results: Results) -> list[str]:
    lines = []
    if results.missing:
        lines += ["## Missing", "", *[f"- {run_id}" for run_id in results.missing], ""]
    if results.invalid:
        lines += ["## Paired with another build", "", *[f"- {run_id}" for run_id in results.invalid], ""]
    return lines


def _defect_lines(results: Results) -> list[str]:
    arms = results.cell_arms
    lines = ["## Defects removed and brought back (%)", "",
             "Removed: of the defects a build shipped, the share a cell avoided. Brought back: of the defects a build "
             "avoided, the share a cell shipped. Each is a mean over the tasks that have such defects.", ""]
    rows = []
    for arm in arms:
        row = [arm]
        for endpoint in (REMOVED, BROUGHT_BACK):
            tasks = eligible(results, endpoint)
            row += [_points(mean(cell_value(results, arm, task, endpoint) for task in tasks)) if tasks
                    else UNAVAILABLE, str(len(tasks))]
        rows.append(row)
    lines += _table(["Cell", "Removed", "Tasks", "Brought back", "Tasks"], rows) + [""]
    lines += ["## Each defect, by cell", "", "Runs of each cell that avoided the defect.", ""]
    rows = [[task, defect["id"], "avoided" if defect["avoided"] else "shipped",
             *[f"{sum(defect['id'] in _avoided(record) for record in results.cells[arm][task])}"
               f"/{len(results.cells[arm][task])}" for arm in arms]]
            for task in results.caught for defect in results.builds[task]["scores"]["replay"]["defects"]]
    return lines + _table(["Task", "Defect", results.baseline, *arms], rows) + [""]


def _fixer_lines(results: Results) -> list[str]:
    lines = ["## What each fixer reported", "",
             "From the closing lines the fix prompt asks for: the fixer's own account of each finding, not checked "
             "against its diff. Nothing is counted from the reviewer's prose. Unavailable: the fix did not finish, "
             "or its reply gave no closing lines.", ""]
    rows = []
    for arm in results.cell_arms:
        reports = [fix_report(record["fix_text"]) for by_task in results.cells[arm].values() for record in by_task]
        counted = [report for report in reports if report is not None and report.fixed + report.left]
        rows.append([arm, str(len(reports)), str(len(counted)), str(sum(report.fixed for report in counted)),
                     str(sum(report.left for report in counted)),
                     str(sum(report is not None and not report.fixed + report.left for report in reports)),
                     str(sum(report is None for report in reports))])
    return lines + _table(["Cell", "Runs", "With findings", "Fixed", "Left", "No findings", "Unavailable"], rows) + [""]


def _cost_lines(results: Results) -> list[str]:
    lines = ["## Cost at list prices", "",
             f"A cell's cost is its review and its fix; the solo build is the {results.baseline} row. A range is "
             "low–high. Unknown attempts (no usage, or usage that cannot be priced) add nothing to the dollars and "
             "are counted. Not scored: failed or superseded executions of the same runs.", ""]
    rows = []
    for arm, costs in results.costs.items():
        removed = _defects_removed(results, arm) if arm != results.baseline else None
        rows.append([arm, str(costs.runs), f"{costs.scored.seconds / 60:.0f}", _money(costs.scored.cost),
                     _flags(costs.scored),
                     "—" if costs.unscored.empty else f"{_money(costs.unscored.cost)} ({costs.unscored.attempts} "
                     f"attempts, {costs.unscored.unknown} unknown)",
                     "—" if removed is None else UNAVAILABLE if not removed else
                     f"{_money(ledger.Cost(costs.scored.cost.low / removed, costs.scored.cost.high / removed))} "
                     f"({removed} removed)"])
    lines += _table(["Arm", "Scored runs", "Minutes", "Cost", "Unknown / ranged / turn-only", "Not scored",
                     "Per defect removed"], rows) + [""]
    lines += ["Tokens by model, scored runs (millions):", ""]
    rows = [[arm, model, *(f"{count / 1e6:.2f}" for count in (
                tokens.input, tokens.cache_read, sum(n for _, n in tokens.cache_write), tokens.output,
                tokens.reasoning))]
            for arm, costs in results.costs.items() for model, tokens in sorted(costs.scored.tokens.items())]
    return lines + _table(["Arm", "Model", "Input", "Cache read", "Cache write", "Output", "Reasoning"], rows) + [""]


def _defects_removed(results: Results, arm: str) -> int:
    """Defects the builds shipped that a run of this cell arm avoided, counted once per run."""
    return sum(len(_avoided(record) - _avoided(results.builds[task]))
               for task in results.caught for record in results.cells[arm][task])


def _diagnostic_lines(results: Results) -> list[str]:
    lines = ["## Diagnostics", "",
             "A cell that timed out is scored as it stands, so a reviewer that times out leaves the build's own "
             "score.", ""]
    rows = []
    for arm in results.cell_arms:
        flags = [flag for by_task in results.cells[arm].values() for record in by_task for flag in record["flags"]]
        rows.append([arm, str(flags.count(f"timeout:{loop.REVIEW_STEP}")), str(flags.count(f"timeout:{loop.FIX_STEP}")),
                     str(sum(flag.startswith("reviewer_wrote:") for flag in flags))])
    return lines + _table(["Cell", "Review timeouts", "Fix timeouts", "Reviews that wrote"], rows) + [""]


def _estimate_lines(results: Results, decision: Decision) -> list[str]:
    if not decision.estimates:
        return []
    first, second = results.cell_arms
    lines = [f"## {second} minus {first} (an estimate, no verdict)", "",
             f"Points, over each endpoint's tasks. The interval is the 2.5th to 97.5th percentile of {RESAMPLES:,} "
             "resamples of those tasks; the range is the mean with each task left out in turn.", ""]
    names = {REVIEW: "Review-caught defects avoided", REMOVED: "Defects removed", BROUGHT_BACK: "Defects brought back",
             FEATURE: "Feature tests passed", REGRESSIONS: "Existing tests broken"}
    rows = [[names[e.endpoint], str(e.tasks), UNAVAILABLE if e.mean is None else f"{e.mean:+.1f}",
             _span(e.interval), _span(e.leave_one_out)] for e in decision.estimates]
    lines += _table(["Endpoint", "Tasks", "Mean difference", "95% interval", "Leave-one-out range"], rows) + [""]
    lines += [f"- Between a cell's runs on the same build, review differs by {decision.run_spread[arm]:.1f} points on "
              f"average for {arm}." for arm in results.cell_arms]
    return lines + [""]


def _cell(results: Results, arm: str, task: str, endpoint: str) -> str:
    runs = [value(record, results.builds[task], endpoint) for record in results.cells[arm][task]]
    if None in runs:
        return "n/a"
    return f"{mean(runs):.1f} ({', '.join(f'{run:.1f}' for run in runs)})"


def _points(number: float | None) -> str:
    return "n/a" if number is None else f"{number:.1f}"


def _span(pair: tuple[float, float] | None) -> str:
    return UNAVAILABLE if pair is None else f"{pair[0]:+.1f} to {pair[1]:+.1f}"


def _money(cost: ledger.Cost) -> str:
    if abs(cost.high - cost.low) < 0.005:
        return f"${cost.low:,.2f}"
    return f"${cost.low:,.2f}–{cost.high:,.2f}"


def _flags(totals: ledger.Totals) -> str:
    return f"{totals.unknown} / {totals.ranged} / {totals.turn_only}"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + ["| " + " | ".join(r) + " |" for r in rows]
