"""List prices per provider and model, in USD per million tokens, as read on AS_OF.

A `.py` module so that every manifest freezes it with the harness. Every cost the ledger renders
names AS_OF and TABLE_SHA256, so pricing old records from a later table shows as retrospective.
A model absent from the table is an error, never a free model.

Anthropic prices standard (non-fast, global) inference and offers 5-minute and 1-hour cache
writes. OpenAI prices Standard-tier requests and offers 30-minute and 12-hour cache writes. Both
tiers below are per request, decided by the request's whole prompt (every input bucket).
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

AS_OF = "2026-11-01"
TABLE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

ANTHROPIC, OPENAI = "anthropic", "openai"
_ANTHROPIC_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
_OPENAI_SOURCE = "https://developers.openai.com/api/docs/pricing"


@dataclass(frozen=True)
class Rates:
    input: float
    cache_read: float
    output: float
    cache_write: tuple[tuple[str, float], ...] = ()  # (duration, price)


@dataclass(frozen=True)
class Tier:
    """A request whose prompt exceeds `above` tokens pays `rates` instead."""
    above: int
    rates: Rates


@dataclass(frozen=True)
class Price:
    provider: str
    model: str
    rates: Rates
    source: str
    tier: Tier | None = None

    def for_prompt(self, prompt_tokens: int) -> Rates:
        return self.tier.rates if self.tier is not None and prompt_tokens > self.tier.above else self.rates


class UnpricedModel(Exception):
    pass


def _anthropic(model: str, base: float, write_5m: float, write_1h: float, read: float, output: float) -> Price:
    return Price(ANTHROPIC, model, Rates(base, read, output, (("5m", write_5m), ("1h", write_1h))), _ANTHROPIC_SOURCE)


def _openai_rates(*, input: float, cached: float, write_30m: float, write_12h: float, output: float) -> Rates:
    return Rates(input, cached, output, (("30m", write_30m), ("12h", write_12h)))


def _openai(model: str, short: Rates, long: Rates) -> Price:
    """`short` up to 272K prompt tokens, `long` above."""
    return Price(OPENAI, model, short, _OPENAI_SOURCE, Tier(272_000, long))


_HAIKU = _anthropic("claude-haiku-5-5", 0.10, 0.125, 0.20, 0.01, 0.50)

PRICES: dict[tuple[str, str], Price] = {(price.provider, price.model): price for price in (
    _anthropic("claude-fable-5-1", 10.0, 12.50, 20.0, 0.25, 50.0),
    _anthropic("claude-opus-5-5", 4.0, 5.0, 8.0, 0.20, 20.0),
    _anthropic("claude-sonnet-5-5", 2.0, 2.50, 4.0, 0.10, 10.0),
    Price(ANTHROPIC, "claude-haiku-5-5", _HAIKU.rates, _ANTHROPIC_SOURCE,
          Tier(100_000, Rates(0.50, 0.05, 2.50, (("5m", 0.625), ("1h", 1.0))))),
    _openai("gpt-6.1-sol",
            _openai_rates(input=2.0, cached=0.10, write_30m=2.50, write_12h=5.0, output=10.0),
            _openai_rates(input=4.0, cached=0.20, write_30m=5.0, write_12h=10.0, output=15.0)),
    _openai("gpt-6-astra",
            _openai_rates(input=10.0, cached=1.0, write_30m=12.50, write_12h=25.0, output=50.0),
            _openai_rates(input=20.0, cached=2.0, write_30m=25.0, write_12h=50.0, output=75.0)),
)}


def lookup(provider: str, model: str) -> Price:
    try:
        return PRICES[(provider, model)]
    except KeyError:
        raise UnpricedModel(f"no {AS_OF} price for {model} on {provider}") from None
