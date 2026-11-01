"""The cost of an experiment's recorded builds at the dated list prices, from raw records.

Descriptive: it changes no verdict, so it needs no verifying freeze. A build's cost covers every
attempt of its scored execution, retries included; every other saved execution of the run is
spend on executions that were not scored. `collect` reads records into plain values and `render`
is the only place numbers become text.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import ledger, loop, manifest, pilot, prices
from .experiments import Experiment


@dataclass
class ArmCost:
    by_task: dict[str, ledger.Totals] = field(default_factory=dict)
    by_step: dict[str, ledger.Totals] = field(default_factory=dict)
    total: ledger.Totals = field(default_factory=ledger.Totals)
    unscored: ledger.Totals = field(default_factory=ledger.Totals)
    builds: int = 0
    missing: list[str] = field(default_factory=list)  # run ids with no scored execution


@dataclass
class CostReport:
    experiment: str
    digest: str | None
    arms: dict[str, ArmCost]


def frozen_digest(experiment: Experiment) -> str | None:
    """The digest a manifest file records, read without verifying it: builds are matched to it."""
    try:
        return json.loads(manifest.manifest_path(experiment).read_text(encoding="utf-8"))["digest"]
    except FileNotFoundError:
        return None


def collect(data_root: Path, experiment: Experiment, arm_names: list[str] | None = None) -> CostReport:
    digest = frozen_digest(experiment)
    report = CostReport(experiment.name, digest, {})
    for arm_name in arm_names or experiment.arm_names:
        arm, cost = experiment.arm(arm_name), ArmCost()
        for task_id in experiment.tasks:
            for rep in experiment.rep_ids:
                run_id = loop.run_id(task_id, arm_name, rep)
                scored = None if digest is None else pilot.complete_build(data_root / "runs", task_id, arm_name,
                                                                             digest, rep)
                run = ledger.run_cost(data_root / "runs" / run_id, scored, arm)
                for unscored in run.unscored:
                    cost.unscored.merge(unscored.totals())
                if run.scored is None:
                    cost.missing.append(run_id)
                    continue
                cost.builds += 1
                cost.by_task.setdefault(task_id, ledger.Totals()).merge(run.scored.totals())
                cost.total.merge(run.scored.totals())
                for kind, totals in run.scored.by_step_kind().items():
                    cost.by_step.setdefault(kind, ledger.Totals()).merge(totals)
        report.arms[arm_name] = cost
    return report


def render(report: CostReport) -> str:
    lines = [f"# Cost of {report.experiment}'s builds at list prices", "",
             f"- Prices: list prices read {prices.AS_OF} (`eval/harness/prices.py`, sha256 `{prices.TABLE_SHA256[:12]}`); "
             "a build recorded before that date is priced retrospectively.",
             f"- Builds matched to manifest digest `{report.digest}`." if report.digest else
             "- No manifest: no build is scored, so all spend is listed as unscored.",
             "- A range is low–high: a Codex session without per-request counts whose total prompt crosses its "
             "model's per-request tier, or cache writes of unknown duration. Unknown attempts (no usage, or "
             "usage that cannot be priced) add nothing and are counted.", ""]
    lines += ["## Per arm", ""]
    rows = [[arm, str(cost.builds), _money(cost.total.cost) if cost.builds else "—",
             _money_per(cost.total.cost, cost.builds), f"{cost.total.seconds / 60:.0f}", _flags(cost.total),
             (_money(cost.unscored.cost) + _unscored(cost)) if cost.unscored.attempts else "—"]
            for arm, cost in report.arms.items()]
    lines += _table(["Arm", "Builds", "Cost", "Per build", "Minutes", "Unknown / ranged / turn-only",
                     "Not scored"], rows) + [""]
    for arm, cost in report.arms.items():
        if not cost.builds:
            continue
        lines += [f"## {arm}", "", "### Per task", ""]
        lines += _table(["Task", "Cost", "Minutes", "Unknown / ranged / turn-only"],
                        [[task, _money(t.cost), f"{t.seconds / 60:.0f}", _flags(t)]
                         for task, t in cost.by_task.items()]) + [""]
        lines += ["### Per step", ""]
        lines += _table(["Step", "Attempts", "Cost", "Minutes", "Unknown / ranged / turn-only"],
                        [[kind, str(t.attempts), _money(t.cost), f"{t.seconds / 60:.0f}", _flags(t)]
                         for kind, t in cost.by_step.items()]) + [""]
        lines += ["### Tokens by model (millions)", ""]
        lines += _table(["Model", "Input", "Cache read", "Cache write", "Output", "Reasoning"],
                        [[model, *(f"{n / 1e6:.2f}" for n in (tok.input, tok.cache_read,
                                                              sum(c for _, c in tok.cache_write),
                                                              tok.output, tok.reasoning))]
                         for model, tok in sorted(cost.total.tokens.items())]) + [""]
        if cost.missing:
            lines += [f"- No scored execution: {', '.join(cost.missing)}", ""]
    return "\n".join(lines)


def _money(cost: ledger.Cost) -> str:
    if abs(cost.high - cost.low) < 0.005:
        return f"${cost.low:,.2f}"
    return f"${cost.low:,.2f}–{cost.high:,.2f}"


def _money_per(cost: ledger.Cost, builds: int) -> str:
    return _money(ledger.Cost(cost.low / builds, cost.high / builds)) if builds else "—"


def _flags(totals: ledger.Totals) -> str:
    return f"{totals.unknown} / {totals.ranged} / {totals.turn_only}"


def _unscored(cost: ArmCost) -> str:
    return f" ({cost.unscored.attempts} attempts, {cost.unscored.unknown} unknown)" if cost.unscored.attempts else ""


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + ["| " + " | ".join(r) + " |" for r in rows]
