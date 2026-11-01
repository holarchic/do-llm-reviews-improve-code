"""The spend recorded under a run: every saved execution, each attempt priced by ledger.py.

This is where the runner's record conventions meet pricing. Each execution's `record.json` is read
for accounting on its own, independent of `records.build`'s scoring check, so an invalid or
unfinished execution still shows its spend; an unreadable record is an unknown, never nothing. A
step names the agent that served it (`loop._step` records it); a record made before that is
resolved the way the loop ran it, a step whose name holds "review" being the reviewer's.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from . import agents, ledger, loop, prices


@dataclass(frozen=True)
class ExecutionCost:
    exec_dir: Path
    attempts: tuple[ledger.AttemptCost, ...]
    unreadable: str = ""  # why the record could not be read; its spend is then one unknown

    def totals(self) -> ledger.Totals:
        totals = ledger.Totals()
        for attempt in self.attempts:
            totals.add(attempt)
        totals.unknown += bool(self.unreadable)
        return totals

    def by_step_kind(self) -> dict[str, ledger.Totals]:
        kinds: dict[str, ledger.Totals] = {}
        for attempt in self.attempts:
            kinds.setdefault(ledger.step_kind(attempt.step), ledger.Totals()).add(attempt)
        return kinds


@dataclass(frozen=True)
class RunCost:
    scored: ExecutionCost | None
    unscored: tuple[ExecutionCost, ...]


def seat(agent: agents.Agent) -> ledger.Seat:
    """The price-table provider of a natively served agent is its CLI's vendor."""
    if agent.provider != agents.NATIVE:
        return ledger.Seat(agent.cli, agent.provider, agent.model)
    return ledger.Seat(agent.cli, prices.ANTHROPIC if agent.cli == "claude" else prices.OPENAI, agent.model)


def run_cost(run_root: Path, scored_exec: Path | None, arm) -> RunCost:
    """Every saved execution of one run; `scored_exec` is the one the report scores, if any."""
    execs = sorted((p for p in run_root.glob("exec-*") if p.is_dir() and p.name[5:].isdigit()),
                   key=lambda p: int(p.name[5:]))
    costs = [execution_cost(exec_dir, arm) for exec_dir in execs]
    scored = next((c for c in costs if scored_exec is not None and c.exec_dir == scored_exec), None)
    return RunCost(scored, tuple(c for c in costs if c is not scored))


def execution_cost(exec_dir: Path, arm) -> ExecutionCost:
    try:
        steps = json.loads((exec_dir / "record.json").read_text(encoding="utf-8"))["steps"]
        if not isinstance(steps, list):
            raise TypeError("steps is not a list")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return ExecutionCost(exec_dir, (), unreadable=f"{type(exc).__name__}: {exc}")
    attempts = []
    for index, step in enumerate(steps):
        name = step.get("name") if isinstance(step, dict) else None
        raw_attempts = step.get("attempts") if isinstance(step, dict) else None
        if not isinstance(name, str) or not isinstance(raw_attempts, list) or not raw_attempts:
            # The loop records a step only after at least one attempt.
            attempts.append(ledger.AttemptCost(name if isinstance(name, str) else "?", ledger.UNKNOWN, 0.0,
                                               reason="step without a name or with no attempts"))
            continue
        served, problem = _step_seat(step, name, arm)
        for number, raw in enumerate(raw_attempts, start=1):
            if served is None:
                attempts.append(ledger.AttemptCost(name, ledger.UNKNOWN, 0.0, reason=problem))
                continue
            attempts.append(ledger.attempt_cost(name, raw, served, loop.attempt_dir(exec_dir, index, name, number)))
    return ExecutionCost(exec_dir, tuple(attempts))


def _step_seat(step: dict, name: str, arm) -> tuple[ledger.Seat | None, str]:
    """The step's recorded agent; a record made before steps named one resolves it by the step's name."""
    if "agent" in step:
        recorded = step["agent"]
        if isinstance(recorded, dict) and all(isinstance(recorded.get(k), str) for k in ("cli", "model", "provider")):
            return seat(agents.Agent(recorded["cli"], recorded["model"], recorded["provider"])), ""
        return None, "the step's recorded agent is malformed"
    agent = arm.reviewer if "review" in name else arm.writer
    return (None, "no agent for this step") if agent is None else (seat(agent), "")
