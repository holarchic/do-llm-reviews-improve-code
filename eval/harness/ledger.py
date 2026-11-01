"""Tokens and economic cost of one session's recorded usage, at the dated list prices of prices.py.

Exact where the CLIs report enough, a low–high range where they don't, and unknown, never $0,
where usage is missing or malformed. Parsing raw usage is this module's boundary: an attempt
becomes an `AttemptCost` whose tokens are typed and checked, or an unknown with its reason. It
knows CLIs and prices, not runs: `spend.py` walks the recorded executions.

Sources, most authoritative first:
- Claude Code: the cumulative per-model counters of the session's last `result` event
  (`modelUsage`), recorded as `usage["by_model"]` or, for records made before that, read back
  from the attempt's transcript. A resumed session writes one `result` per turn; its `usage` covers
  that turn only, so the recorded `usage` is the last resort, counted as `turn_only`.
- Codex: the per-request counts from its session rollout (`usage["requests"]`) when they
  reconcile with the session totals; otherwise the session totals, which price a per-request tier
  as a range. Codex's `input_tokens` include its cached and written tokens.
"""

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from . import prices

PER_REQUEST, SESSION, TURN_ONLY, UNKNOWN = "per_request", "session", "turn_only", "unknown"
UNDATED = "undated"  # cache writes whose duration the CLI does not report
_STANDARD = {None, "standard"}
_STANDARD_GEO = {None, "global", "not_available"}


class Unusable(Exception):
    """Usage that cannot be priced: missing, malformed or inconsistent."""


class Seat(NamedTuple):
    """Who served a session, as pricing needs it: the CLI, the price-table provider and the model."""
    cli: str  # "claude" | "codex"
    provider: str  # a provider of prices.py
    model: str


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
    cost: Cost | None = None  # None exactly when basis is UNKNOWN
    reason: str = ""

    def __post_init__(self):
        if (self.cost is None) != (self.basis == UNKNOWN):
            raise ValueError(f"{self.step}: basis {self.basis} with cost {self.cost}")


@dataclass
class Totals:
    tokens: dict[str, Tokens] = field(default_factory=dict)
    cost: Cost = Cost(0.0, 0.0)
    seconds: float = 0.0
    attempts: int = 0
    unknown: int = 0  # attempts, or whole executions whose record could not be read
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

    @property
    def empty(self) -> bool:
        return not (self.attempts or self.unknown)


class _Piece(NamedTuple):
    model: str
    tokens: Tokens
    one_request: bool  # its tier is then known


def step_kind(name: str) -> str:
    """`plan-review-3` -> `plan-review`; `build` stays `build`."""
    return re.sub(r"-\d+$", "", name)


def attempt_cost(step: str, raw, seat: Seat, attempt_dir: Path) -> AttemptCost:
    """The cost of one recorded attempt; its session files, the transcript included, are in `attempt_dir`."""
    seconds = raw.get("seconds", 0.0) if isinstance(raw, dict) else 0.0
    seconds = seconds if _number(seconds) else 0.0
    try:
        usage = raw.get("usage") if isinstance(raw, dict) else None
        if not isinstance(usage, dict) or not usage:
            raise Unusable("no usage recorded")
        _check_service(usage)
        if seat.cli == "claude":
            basis, pieces, reason = _claude(usage, seat, attempt_dir)
        elif seat.cli == "codex":
            basis, pieces, reason = _codex(usage, seat)
        else:
            raise Unusable(f"no usage format for CLI {seat.cli!r}")
        tokens: dict[str, Tokens] = {}
        cost = Cost(0.0, 0.0)
        for piece in pieces:
            cost += price_tokens(piece.tokens, prices.lookup(seat.provider, piece.model), one_request=piece.one_request)
            tokens[piece.model] = tokens.get(piece.model, Tokens()) + piece.tokens
    except Unusable as exc:
        return AttemptCost(step, UNKNOWN, seconds, reason=str(exc))
    return AttemptCost(step, basis, seconds, tokens, cost, reason)


def price_tokens(tokens: Tokens, price: prices.Price, *, one_request: bool) -> Cost:
    """A session total whose prompt crosses a per-request tier could be any mix of requests, so it is
    priced at both tiers; one request's tier is known."""
    if one_request or price.tier is None or tokens.prompt <= price.tier.above:
        return _at(tokens, price.for_prompt(tokens.prompt))
    low, high = _at(tokens, price.rates), _at(tokens, price.tier.rates)
    return Cost(min(low.low, high.low), max(low.high, high.high))


def _at(tokens: Tokens, rates: prices.Rates) -> Cost:
    fixed = tokens.input * rates.input + tokens.cache_read * rates.cache_read + tokens.output * rates.output
    writes = dict(rates.cache_write)
    low = high = fixed
    for duration, n in tokens.cache_write:
        if duration == UNDATED:
            if not writes:
                raise Unusable("cache writes on a model with no write price")
            low += n * min(writes.values())
            high += n * max(writes.values())
        elif duration in writes:
            low += n * writes[duration]
            high += n * writes[duration]
        else:
            raise Unusable(f"no {duration} cache-write price")
    return Cost(low / 1e6, high / 1e6)


def _check_service(usage: dict) -> None:
    for key, allowed in (("service_tier", _STANDARD), ("speed", _STANDARD), ("inference_geo", _STANDARD_GEO)):
        value = usage.get(key)
        if not (value is None or isinstance(value, str)) or value not in allowed:
            raise Unusable(f"{key} {value!r} is not priced (fast mode, priority tier or regional uplift)")


def _number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value >= 0 and (isinstance(value, int) or math.isfinite(value))


def _count(container: dict, key: str, *, required: bool = True) -> int:
    if key not in container:
        if required:
            raise Unusable(f"{key} missing")
        return 0
    value = container[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Unusable(f"{key} is {value!r}, not a token count")
    return value


def _claude(usage: dict, seat: Seat, attempt_dir: Path) -> tuple[str, list[_Piece], str]:
    by_model = usage.get("by_model")
    if by_model is None:
        by_model = _last_model_usage(attempt_dir / "transcript.jsonl")
    if by_model is not None:
        if not isinstance(by_model, dict) or not by_model:
            raise Unusable("by_model is empty or not a table")
        return SESSION, [_Piece(model, _claude_model_tokens(c), False) for model, c in sorted(by_model.items())], ""
    return TURN_ONLY, [_Piece(seat.model, _claude_turn_tokens(usage), False)], "no per-model counters: the last turn only"


def _claude_model_tokens(counters) -> Tokens:
    if not isinstance(counters, dict):
        raise Unusable("a model's counters are not a table")
    created = _count(counters, "cacheCreationInputTokens")
    dated = {"5m": _count(counters, "cacheCreationEphemeral5mInputTokens", required=False),
             "1h": _count(counters, "cacheCreationEphemeral1hInputTokens", required=False)}
    return _claude_tokens(_count(counters, "inputTokens"), _count(counters, "cacheReadInputTokens"), created, dated,
                          _count(counters, "outputTokens"), _count(counters, "thinkingTokens", required=False))


def _claude_turn_tokens(usage: dict) -> Tokens:
    created = _count(usage, "cache_creation_input_tokens", required=False)
    split = {} if usage.get("cache_creation") is None else usage["cache_creation"]
    details = {} if usage.get("output_tokens_details") is None else usage["output_tokens_details"]
    if not isinstance(split, dict) or not isinstance(details, dict):
        raise Unusable("cache_creation or output_tokens_details is not a table")
    dated = {"5m": _count(split, "ephemeral_5m_input_tokens", required=False),
             "1h": _count(split, "ephemeral_1h_input_tokens", required=False)}
    return _claude_tokens(_count(usage, "input_tokens"), _count(usage, "cache_read_input_tokens", required=False),
                          created, dated, _count(usage, "output_tokens"),
                          _count(details, "thinking_tokens", required=False))


def _claude_tokens(uncached: int, read: int, created: int, dated: dict[str, int], output: int,
                   reasoning: int) -> Tokens:
    if sum(dated.values()) > created:
        raise Unusable("dated cache writes exceed the cache writes")
    if reasoning > output:
        raise Unusable("thinking tokens exceed output tokens")
    writes = {**dated, UNDATED: created - sum(dated.values())}
    return Tokens(uncached, read, tuple(sorted((d, n) for d, n in writes.items() if n)), output, reasoning)


def _last_model_usage(transcript: Path) -> dict | None:
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


def _codex(usage: dict, seat: Seat) -> tuple[str, list[_Piece], str]:
    total = _codex_tokens(usage)
    requests = usage.get("requests")
    if requests is None:
        return SESSION, [_Piece(seat.model, total, False)], ""
    if not isinstance(requests, list) or not requests or not all(isinstance(r, dict) for r in requests):
        raise Unusable("requests is not a non-empty list of tables")
    pieces = [_codex_tokens(request) for request in requests]
    summed = Tokens()
    for piece in pieces:
        summed += piece
    if summed != total:
        return SESSION, [_Piece(seat.model, total, False)], "requests do not reconcile with the session totals"
    return PER_REQUEST, [_Piece(seat.model, piece, True) for piece in pieces], ""


def _codex_tokens(counters: dict) -> Tokens:
    prompt = _count(counters, "input_tokens")
    cached = _count(counters, "cached_input_tokens")
    written = _count(counters, "cache_write_input_tokens", required=False)
    output = _count(counters, "output_tokens")
    reasoning = _count(counters, "reasoning_output_tokens", required=False)
    if cached + written > prompt:
        raise Unusable("cached and written tokens exceed input tokens")
    if reasoning > output:
        raise Unusable("reasoning tokens exceed output tokens")
    writes = ((UNDATED, written),) if written else ()
    return Tokens(prompt - cached - written, cached, writes, output, reasoning)
