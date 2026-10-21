"""The pilot report, regenerated from raw run and judgement records.

`collect` reads records into plain numbers; `decide` applies the
pre-registered rule; `render` is the only place numbers become text.
"""

from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

from . import loop, manifest, pilot, records

PRIMARY_THRESHOLD = 10.0
PRIMARY_LOSS_LIMIT = 5.0
JUDGE_WINS_NEEDED = 4
CALIBRATION_NEEDED = 4
# Pass fractions over different denominators make exact thresholds land a few ulps short.
TOLERANCE = 1e-9
TREATMENTS = tuple(name for name, arm in loop.ARMS.items() if arm.reviewer)


@dataclass
class Results:
    digest: str
    tasks: list[str]
    missing: list[str] = field(default_factory=list)
    primary: dict[str, dict[str, float]] = field(default_factory=dict)  # arm -> task -> pp
    followup: dict[str, dict[str, dict]] = field(default_factory=dict)  # arm -> task -> scores
    static: dict[str, dict[str, dict]] = field(default_factory=dict)  # arm -> task -> metrics
    diagnostics: dict[str, dict[str, dict]] = field(default_factory=dict)  # arm -> task -> diag
    calibration: dict[str, dict[str, bool]] = field(default_factory=dict)  # judge -> task -> passed
    pairs: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)  # judge -> pair -> task -> a|b|tie
    leaks: dict[str, list[int]] = field(default_factory=dict)  # arm -> leak counts per export


def collect(data_root: Path) -> Results:
    digest = manifest.verify()
    runs_root = data_root / "runs"
    results = Results(digest=digest, tasks=[task.id for task in pilot.pilot_tasks()])
    for arm in pilot.BUILD_ARMS:
        for task_id in results.tasks:
            exec_dir = pilot.complete_build(runs_root, task_id, arm, digest)
            if exec_dir is None:
                results.missing.append(f"build {task_id}--{arm}")
                continue
            record = records.build(exec_dir / "record.json", digest)
            results.primary.setdefault(arm, {})[task_id] = 100 * record["scores"]["primary"]
            results.static.setdefault(arm, {})[task_id] = record["scores"]["static"]
            results.diagnostics.setdefault(arm, {})[task_id] = _diagnostics(record)
            if arm in pilot.FOLLOWUP_ARMS:
                followup = records.followup(exec_dir / "followup" / "record.json", digest)
                if followup is not None:
                    results.followup.setdefault(arm, {})[task_id] = followup["scores"]
                else:
                    results.missing.append(f"followup {task_id}--{arm}")
    for name in pilot.judge.JUDGES:
        for task_id in results.tasks:
            for pair in (None, *pilot.PAIRS):
                judgement = records.judgement(pilot.judging_dir(data_root, name, task_id, pair) / "judgement.json",
                                              digest)
                if judgement is not None and pair is not None:
                    # A judgement counts only for the builds now on record.
                    execs = [pilot.complete_build(runs_root, task_id, arm, digest) for arm in pair]
                    current = None if None in execs else {side: pilot.build_identity(e)
                                                          for side, e in zip(("a", "b"), execs)}
                    if judgement.get("builds") != current:
                        judgement = None
                if judgement is None:
                    results.missing.append(f"judge {name} {task_id} {pair or 'calibration'}")
                    continue
                if pair is None:
                    results.calibration.setdefault(name, {})[task_id] = judgement["result"] == "a"
                    continue
                results.pairs.setdefault(name, {}).setdefault("-".join(pair), {})[task_id] = judgement["result"]
                for side, arm in zip(("a", "b"), pair):
                    results.leaks.setdefault(arm, []).append(judgement["leak_words"][side])
    return results


@dataclass(frozen=True)
class Decision:
    verdict: str  # GO | NO-GO | INCOMPLETE
    reasons: list[str]
    calibrated_judges: list[str]
    primary_sensitivity: str


def decide(results: Results) -> Decision:
    if results.missing:
        return Decision("INCOMPLETE", [f"{len(results.missing)} records missing"], [], "n/a")
    calibrated = [name for name, by_task in results.calibration.items()
                  if sum(by_task.values()) >= CALIBRATION_NEEDED]
    reasons = []
    baseline = _arm_mean(results, "A1")
    for arm in TREATMENTS:
        gain = _arm_mean(results, arm) - baseline
        if _at_least(gain, PRIMARY_THRESHOLD):
            reasons.append(f"{arm} beats A1 on the primary by {gain:+.1f} points")
        if len(calibrated) == len(results.calibration) == 2:
            wins = agreed_wins(results, arm)
            if wins >= JUDGE_WINS_NEEDED and _at_least(gain, -PRIMARY_LOSS_LIMIT):
                reasons.append(f"{arm} wins {wins}/5 judge-agreed comparisons against A1 (primary {gain:+.1f})")
    flips = _primary_trigger(results, "A1") != _primary_trigger(results, "A1r")
    sensitivity = "changes with A1r in place of A1" if flips else "unchanged with A1r in place of A1"
    return Decision("GO" if reasons else "NO-GO", reasons, calibrated, sensitivity)


def _at_least(value: float, bound: float) -> bool:
    return value >= bound - TOLERANCE


def _primary_trigger(results: Results, baseline_arm: str) -> bool:
    """Whether the primary branch fires for any treatment against this baseline."""
    baseline = _arm_mean(results, baseline_arm)
    return any(_at_least(_arm_mean(results, arm) - baseline, PRIMARY_THRESHOLD) for arm in TREATMENTS)


def agreed_wins(results: Results, arm: str) -> int:
    """Tasks where both judges pick `arm` over A1 (A1 is side a of A1-vs-arm)."""
    pair = f"A1-{arm}"
    judges = list(results.pairs)
    return sum(all(results.pairs[name][pair].get(task) == "b" for name in judges) for task in results.tasks)


def render(results: Results, decision: Decision) -> str:
    arms = [arm for arm in pilot.BUILD_ARMS if arm in results.primary]
    lines = [
        "# Eval pilot report",
        "",
        f"Manifest digest `{results.digest[:16]}`. Five tasks, one run per task and arm: directional evidence for a"
        " spending decision, not a significance test.",
        "",
        f"**Verdict: {_verdict_text(decision)}**",
        "",
    ]
    lines += [f"- {reason}" for reason in decision.reasons]
    if results.missing:
        lines += ["", "Missing records:", *[f"- {item}" for item in results.missing]]
    lines += ["", "## Primary: hidden-test pass rate (%)", "", _table(["task", *arms], [
        [task, *[_num(results.primary.get(arm, {}).get(task)) for arm in arms]] for task in results.tasks
    ] + [["**mean**", *[_num(_arm_mean(results, arm)) for arm in arms]]])]
    lines += ["", f"A1 − A1r spread: {_num(_spread(results))} points; "
              f"primary-trigger sensitivity: {decision.primary_sensitivity}.", "",
              "Per-task paired differences (points):", "",
              _table(["task", *[f"{arm} − A1" for arm in TREATMENTS]], [
                  [task, *[_num(_diff(results, arm, task)) for arm in TREATMENTS]] for task in results.tasks])]
    lines += ["", "## Follow-up change", "", _table(
        ["arm", "new-behaviour pass %", "regressions", "persistent failures", "repaired"],
        [[arm, _num(_mean_of(results.followup.get(arm, {}), lambda s: 100 * s["new_fraction"])),
          _sum_of(results.followup.get(arm, {}), lambda s: len(s["regressions"])),
          _sum_of(results.followup.get(arm, {}), lambda s: len(s["persistent_failures"])),
          _sum_of(results.followup.get(arm, {}), lambda s: len(s["repaired"]))]
         for arm in pilot.FOLLOWUP_ARMS])]
    lines += ["", "## Judges", "", "Calibration (reference preferred in both orders):", "", _table(
        ["judge", *results.tasks, "passed"],
        [[name, *["yes" if by_task.get(task) else "no" for task in results.tasks],
          "yes" if name in decision.calibrated_judges else "no"]
         for name, by_task in results.calibration.items()])]
    for pair in ("-".join(p) for p in pilot.PAIRS):
        a, b = pair.split("-")
        rows = [[name, *[{"a": a, "b": b}.get(by_pair.get(pair, {}).get(task), "tie") for task in results.tasks]]
                for name, by_pair in results.pairs.items()]
        lines += ["", f"{a} vs {b} (winner per task):", "", _table(["judge", *results.tasks], rows)]
    lines += ["", "Leak words in exports (review/rubric/plan/spec/round), mean per export: " + ", ".join(
        f"{arm} {_num(mean(counts))}" for arm, counts in sorted(results.leaks.items()))]
    lines += ["", "## Static metrics (mean per task)", "", _table(
        ["arm", "ruff violations", "source LOC", "test LOC", "max branches/function"],
        [[arm, *[_num(_mean_of(results.static.get(arm, {}), lambda m, k=key: m[k]))
                 for key in ("ruff_violations", "source_loc", "test_loc", "max_function_branches")]]
         for arm in arms])]
    lines += ["", "## Diagnostics (per task mean)", "", _table(
        ["arm", "model minutes", "input tokens", "output tokens", "plan rounds", "phase rounds", "flags"],
        [[arm,
          _num(_mean_of(results.diagnostics.get(arm, {}), lambda d: d["seconds"] / 60)),
          _num(_mean_of(results.diagnostics.get(arm, {}), lambda d: d["input_tokens"]), 0),
          _num(_mean_of(results.diagnostics.get(arm, {}), lambda d: d["output_tokens"]), 0),
          _num(_mean_of(results.diagnostics.get(arm, {}), lambda d: d["rounds"].get("plan", 0))),
          _num(_mean_of(results.diagnostics.get(arm, {}), lambda d: d["rounds"].get("phase", 0))),
          "; ".join(sorted({f for d in results.diagnostics.get(arm, {}).values() for f in d["flags"]})) or "none"]
         for arm in arms])]
    lines += ["", "## Limits", "",
              "- Isolation is enforced by macOS sandboxes (Claude Code's Bash sandbox, an outer profile around Codex) and"
              " proved by probes; a command that reaches private material by a route the probes do not cover would go"
              " unnoticed — the transcript audit is diagnostic only.",
              "- Judge calibration shows the judges see injected defects, not that they rank close builds reliably.",
              "- The treatment bundles rules, rubric, review and extra compute; A2 vs A3 compares reviewer"
              " configurations (model, CLI and tool policy together).",
              "- Hidden tests run candidate code on the host without a sandbox, in the same process as the test"
              " runner: scores assume non-adversarial candidates (a build could forge its own results).", ""]
    return "\n".join(lines)


def _diagnostics(record: dict) -> dict:
    attempts = [attempt for step in record["steps"] for attempt in step["attempts"]]
    return {
        "seconds": sum(attempt["seconds"] for attempt in attempts),
        "input_tokens": sum(_input_tokens(attempt["usage"]) for attempt in attempts),
        "output_tokens": sum(attempt["usage"].get("output_tokens", 0) for attempt in attempts),
        "rounds": record.get("rounds", {}),
        "flags": [flag.split(":")[0] for flag in record.get("flags", [])],
    }


def _input_tokens(usage: dict) -> int:
    return sum(usage.get(key, 0) for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


def _arm_mean(results: Results, arm: str) -> float | None:
    by_task = results.primary.get(arm)
    return mean(by_task.values()) if by_task else None


def _spread(results: Results) -> float | None:
    a1, a1r = _arm_mean(results, "A1"), _arm_mean(results, "A1r")
    return None if a1 is None or a1r is None else a1 - a1r


def _diff(results: Results, arm: str, task: str) -> float | None:
    if task in results.primary.get(arm, {}) and task in results.primary.get("A1", {}):
        return results.primary[arm][task] - results.primary["A1"][task]
    return None


def _mean_of(by_task: dict, value) -> float | None:
    return mean(value(item) for item in by_task.values()) if by_task else None


def _sum_of(by_task: dict, value) -> str:
    return str(sum(value(item) for item in by_task.values())) if by_task else "–"


def _num(value, places: int = 1) -> str:
    return "–" if value is None else f"{value:.{places}f}"


def _table(header: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(out)


def _verdict_text(decision: Decision) -> str:
    return {"GO": "GO — met the continuation threshold",
            "NO-GO": "did not meet the continuation threshold",
            "INCOMPLETE": "INCOMPLETE — no verdict"}[decision.verdict]
