"""Tokens and economic cost of every recorded step, at the dated list prices of prices.py.

Exact where the CLIs report enough, a low–high range where they don't, and unknown, never $0,
where usage is missing or malformed. Parsing raw usage is this module's boundary: each attempt
becomes an `AttemptCost` whose tokens are typed and checked, or an unknown with its reason.

Sources, most authoritative first:
- Claude Code: the cumulative per-model counters of the session's last `result` event
  (`modelUsage`), recorded as `usage["by_model"]` or, for records made before that, read back
  from the attempt's transcript. A resumed session writes one `result` per turn; its `usage` covers
  that turn only, so the recorded `usage` is the last resort, counted as `turn_only`.
- Codex: the per-request counts from its session rollout (`usage["requests"]`) when they
  reconcile with the session totals; otherwise the session totals, which price a per-request tier
  as a range. Codex's `input_tokens` include its cached and written tokens.

Every saved execution counts: the scored one, and every other, whatever its status, as spend on
executions that were not scored.
"""

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import agents, prices

PER_REQUEST, SESSION, TURN_ONLY, UNKNOWN = "per_request", "session", "turn_only", "unknown"
UNDATED = "undated"  # cache writes whose duration the CLI does not report
_STANDARD_SERVICE = {None, "standard"}
_STANDARD_GEO = {None, "global", "not_available"}


class Unusable(Exception):
    """Usage that cannot be priced: missing, malformed or inconsistent."""


@dataclass(frozen=True)
class Tokens:
    input: int = 0  # uncached input
    cache_read: int = 0
    cache_write: tuple[tuple[str, int], ...] = ()  # (duration, tokens), durations as in prices.Rates
    output: int = 0  # reasoning included
    reasoning: int = 0

    @property
    def prompt(self) -> int:
        return self.input + self.cache_read + sum(n for _, n in self.cache_write)

    def __add__(self, other: "Tokens") -> "Tokens":
        writes = dict(self.cache_write)
        for duration, n in other.cache_write:
            writes[duration] = writes.get(duration, 0) + n
        return Tokens(self.input + other.input, self.cache_read + other.cache_read,
                      tuple(sorted((d, n) for d, n in writes.items() if n)),
                      self.output + other.output, self.reasoning + other.reasoning)


@dataclass(frozen=True)
class Cost:
    low: float
    high: float

    def __add__(self, other: "Cost") -> "Cost":
        return Cost(self.low + other.low, self.high + other.high)


@dataclass(frozen=True)
class AttemptCost:
    step: str
    basis: str  # PER_REQUEST | SESSION | TURN_ONLY | UNKNOWN
    seconds: float
    tokens: dict[str, Tokens] = field(default_factory=dict)  # model -> tokens
    cost: Cost | None = None  # None when unknown
    reason: str = ""


@dataclass
class Totals:
    tokens: dict[str, Tokens] = field(default_factory=dict)
    cost: Cost = Cost(0.0, 0.0)
    seconds: float = 0.0
    attempts: int = 0
    unknown: int = 0
    ranged: int = 0
    turn_only: int = 0

    def add(self, attempt: AttemptCost) -> None:
        self.attempts += 1
        self.seconds += attempt.seconds
        for model, tokens in attempt.tokens.items():
            self.tokens[model] = self.tokens.get(model, Tokens()) + tokens
        if attempt.cost is None:
            self.unknown += 1
            return
        self.cost += attempt.cost
        self.ranged += attempt.cost.high > attempt.cost.low
        self.turn_only += attempt.basis == TURN_ONLY

    def merge(self, other: "Totals") -> None:
        for model, tokens in other.tokens.items():
            self.tokens[model] = self.tokens.get(model, Tokens()) + tokens
        self.cost += other.cost
        self.seconds += other.seconds
        self.attempts += other.attempts
        self.unknown += other.unknown
        self.ranged += other.ranged
        self.turn_only += other.turn_only


@dataclass(frozen=True)
class ExecutionCost:
    exec_dir: Path
    attempts: tuple[AttemptCost, ...]
    unreadable: str = ""  # why the record could not be read; its spend is then unknown

    def totals(self) -> Totals:
        totals = Totals()
        for attempt in self.attempts:
            totals.add(attempt)
        if self.unreadable:
            totals.unknown += 1
        return totals

    def by_step_kind(self) -> dict[str, Totals]:
        kinds: dict[str, Totals] = {}
        for attempt in self.attempts:
            kinds.setdefault(step_kind(attempt.step), Totals()).add(attempt)
        return kinds


@dataclass(frozen=True)
class RunCost:
    scored: ExecutionCost | None
    unscored: tuple[ExecutionCost, ...]


def step_kind(name: str) -> str:
    """`plan-review-3` -> `plan-review`; `build` stays `build`."""
    return re.sub(r"-\d+$", "", name)


def list_provider(agent: agents.Agent) -> str:
    if agent.provider != agents.NATIVE:
        return agent.provider
    return prices.ANTHROPIC if agent.cli == "claude" else prices.OPENAI


def run_cost(run_root: Path, scored_exec: Path | None, arm) -> RunCost:
    """Every saved execution of one run; `scored_exec` is the one the report scores, if any."""
    execs = sorted((p for p in run_root.glob("exec-*") if p.is_dir()), key=lambda p: int(p.name.split("-")[1]))
    costs = [execution_cost(exec_dir, arm) for exec_dir in execs]
    scored = next((c for c in costs if scored_exec is not None and c.exec_dir == scored_exec), None)
    return RunCost(scored, tuple(c for c in costs if c is not scored))


def execution_cost(exec_dir: Path, arm) -> ExecutionCost:
    try:
        record = json.loads((exec_dir / "record.json").read_text(encoding="utf-8"))
        steps = record["steps"]
        if not isinstance(steps, list):
            raise TypeError("steps is not a list")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return ExecutionCost(exec_dir, (), unreadable=f"{type(exc).__name__}: {exc}")
    attempts = []
    for index, step in enumerate(steps):
        name = step.get("name", "?") if isinstance(step, dict) else "?"
        agent = arm.reviewer if "review" in name else arm.writer
        raw_attempts = step.get("attempts") if isinstance(step, dict) else None
        if not isinstance(raw_attempts, list):
            attempts.append(AttemptCost(name, UNKNOWN, 0.0, reason="step has no attempts list"))
            continue
        for number, raw in enumerate(raw_attempts, start=1):
            attempt_dir = exec_dir / "steps" / f"{index:02d}-{name}" / f"attempt-{number}"
            attempts.append(attempt_cost(name, raw, agent, attempt_dir))
    return ExecutionCost(exec_dir, tuple(attempts))


def attempt_cost(step: str, raw: dict, agent: agents.Agent, attempt_dir: Path) -> AttemptCost:
    seconds = raw.get("seconds", 0.0) if isinstance(raw, dict) else 0.0
    seconds = seconds if isinstance(seconds, (int, float)) and math.isfinite(seconds) and seconds >= 0 else 0.0
    try:
        usage = raw["usage"] if isinstance(raw, dict) else None
        if not isinstance(usage, dict) or not usage:
            raise Unusable("no usage recorded")
        _check_service(usage)
        if agent.cli == "claude":
            basis, pieces, reason = _claude(usage, agent, attempt_dir)
        else:
            basis, pieces, reason = _codex(usage, agent)
        provider = list_provider(agent)
        tokens: dict[str, Tokens] = {}
        cost = Cost(0.0, 0.0)
        for model, piece_tokens, exact in pieces:
            cost += price_tokens(piece_tokens, prices.lookup(provider, model), exact=exact)
            tokens[model] = tokens.get(model, Tokens()) + piece_tokens
    except Unusable as exc:
        return AttemptCost(step, UNKNOWN, seconds, reason=str(exc))
    return AttemptCost(step, basis, seconds, tokens, cost, reason)


def price_tokens(tokens: Tokens, price: prices.Price, *, exact: bool) -> Cost:
    """`exact`: the tokens are one request, so its tier is known. Otherwise a session total whose
    prompt crosses the tier could be any mix of requests, so it is priced at both tiers."""
    if exact or price.tier_above is None or tokens.prompt <= price.tier_above:
        return _at(tokens, price.for_prompt(tokens.prompt))
    low, high = _at(tokens, price.rates), _at(tokens, price.tiered)
    return Cost(min(low.low, high.low), max(low.high, high.high))


def _at(tokens: Tokens, rates: prices.Rates) -> Cost:
    fixed = tokens.input * rates.input + tokens.cache_read * rates.cache_read + tokens.output * rates.output
    low = high = fixed
    for duration, n in tokens.cache_write:
        if duration == UNDATED:
            if not rates.cache_write:
                raise Unusable("cache writes on a model with no write price")
            low += n * min(rates.cache_write.values())
            high += n * max(rates.cache_write.values())
        else:
            if duration not in rates.cache_write:
                raise Unusable(f"no {duration} cache-write price")
            low += n * rates.cache_write[duration]
            high += n * rates.cache_write[duration]
    return Cost(low / 1e6, high / 1e6)


def _check_service(usage: dict) -> None:
    if usage.get("service_tier") not in _STANDARD_SERVICE:
        raise Unusable(f"service tier {usage.get('service_tier')!r} is not priced")
    if usage.get("speed") not in _STANDARD_SERVICE:
        raise Unusable(f"speed {usage.get('speed')!r} (fast mode) is not priced")
    if usage.get("inference_geo") not in _STANDARD_GEO:
        raise Unusable(f"inference geography {usage.get('inference_geo')!r} is not priced")


def _count(container: dict, key: str) -> int:
    value = container.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Unusable(f"{key} is {value!r}, not a token count")
    return value


Pieces = list[tuple[str, Tokens, bool]]  # (model, tokens, whether the tokens are one request)


def _claude(usage: dict, agent: agents.Agent, attempt_dir: Path) -> tuple[str, Pieces, str]:
    by_model = usage.get("by_model")
    if by_model is None:
        by_model = last_model_usage(attempt_dir / "transcript.jsonl")
    if by_model is not None:
        if not isinstance(by_model, dict) or not by_model:
            raise Unusable("by_model is empty or not a table")
        return SESSION, [(model, _claude_model_tokens(c), False) for model, c in sorted(by_model.items())], ""
    return TURN_ONLY, [(agent.model, _claude_turn_tokens(usage), False)], "no per-model counters: the last turn only"


def _claude_model_tokens(counters) -> Tokens:
    if not isinstance(counters, dict):
        raise Unusable("a model's counters are not a table")
    created = _count(counters, "cacheCreationInputTokens")
    dated = {"5m": _count(counters, "cacheCreationEphemeral5mInputTokens"),
             "1h": _count(counters, "cacheCreationEphemeral1hInputTokens")}
    return _claude_tokens(_count(counters, "inputTokens"), _count(counters, "cacheReadInputTokens"), created, dated,
                          _count(counters, "outputTokens"), _count(counters, "thinkingTokens"))


def _claude_turn_tokens(usage: dict) -> Tokens:
    created = _count(usage, "cache_creation_input_tokens")
    split = usage.get("cache_creation") or {}
    if not isinstance(split, dict):
        raise Unusable("cache_creation is not a table")
    dated = {"5m": _count(split, "ephemeral_5m_input_tokens"), "1h": _count(split, "ephemeral_1h_input_tokens")}
    details = usage.get("output_tokens_details") or {}
    if not isinstance(details, dict):
        raise Unusable("output_tokens_details is not a table")
    return _claude_tokens(_count(usage, "input_tokens"), _count(usage, "cache_read_input_tokens"), created, dated,
                          _count(usage, "output_tokens"), _count(details, "thinking_tokens"))


def _claude_tokens(uncached: int, read: int, created: int, dated: dict[str, int], output: int,
                   reasoning: int) -> Tokens:
    if sum(dated.values()) > created:
        raise Unusable("dated cache writes exceed the cache writes")
    if reasoning > output:
        raise Unusable("thinking tokens exceed output tokens")
    writes = {**dated, UNDATED: created - sum(dated.values())}
    return Tokens(uncached, read, tuple(sorted((d, n) for d, n in writes.items() if n)), output, reasoning)


def last_model_usage(transcript: Path) -> dict | None:
    """The cumulative `modelUsage` of a Claude transcript's last `result` event, or None."""
    try:
        lines = transcript.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise Unusable(f"transcript unreadable: {exc!r}") from None
    last = None
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue  # a stream cut mid-line by a timeout
        if isinstance(event, dict) and event.get("type") == "result" and "modelUsage" in event:
            last = event["modelUsage"]
    return last


def _codex(usage: dict, agent: agents.Agent) -> tuple[str, Pieces, str]:
    total = _codex_tokens(usage)
    requests = usage.get("requests")
    if requests is None:
        return SESSION, [(agent.model, total, False)], ""
    if not isinstance(requests, list) or not requests:
        raise Unusable("requests is empty or not a list")
    pieces = [_codex_tokens(request) if isinstance(request, dict) else None for request in requests]
    if None in pieces:
        raise Unusable("a request is not a table")
    summed = Tokens()
    for piece in pieces:
        summed += piece
    if summed != total:
        return SESSION, [(agent.model, total, False)], "requests do not reconcile with the session totals"
    return PER_REQUEST, [(agent.model, piece, True) for piece in pieces], ""


def _codex_tokens(counters: dict) -> Tokens:
    prompt = _count(counters, "input_tokens")
    cached, written = _count(counters, "cached_input_tokens"), _count(counters, "cache_write_input_tokens")
    output, reasoning = _count(counters, "output_tokens"), _count(counters, "reasoning_output_tokens")
    if cached + written > prompt:
        raise Unusable("cached and written tokens exceed input tokens")
    if reasoning > output:
        raise Unusable("reasoning tokens exceed output tokens")
    writes = ((UNDATED, written),) if written else ()
    return Tokens(prompt - cached - written, cached, writes, output, reasoning)
