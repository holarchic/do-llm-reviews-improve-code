"""List prices per provider and model, in USD per million tokens, as read on AS_OF.

A `.py` module so that every manifest freezes it with the harness. Every cost the ledger renders
names AS_OF and TABLE_SHA256, so pricing old records from a later table shows as retrospective.
A model absent from the table is an error, never a free model.

Anthropic prices standard (non-fast, global) inference and offers 5-minute and 1-hour cache
writes. OpenAI prices Standard-tier requests and offers 30-minute and 12-hour cache writes. Both
tiers below are per request, decided by the request's whole prompt (every input bucket).

Routed models are priced as their route bills them. DeepSeek's own API is priced at its peak rates
(01:00–04:00 and 06:00–10:00 UTC on weekdays; every other hour is half price), so a metered
settlement is never below the real charge at any hour. GLM through OpenRouter is priced at Z.AI's
endpoint, the only provider the route admits. Neither charges a cache write apart from input, so a
write is priced as input.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

AS_OF = "2026-11-01"
TABLE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

ANTHROPIC, OPENAI, OPENROUTER, DEEPSEEK = "anthropic", "openai", "openrouter", "deepseek"
_ANTHROPIC_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
_OPENAI_SOURCE = "https://developers.openai.com/api/docs/pricing"
_DEEPSEEK_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing (peak rates)"
_ZAI_ON_OPENROUTER_SOURCE = "https://openrouter.ai/api/v1/models/{model}/endpoints (provider Z.AI)"


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


def _no_write_charge(provider: str, model: str, *, input: float, cached: float, output: float, source: str) -> Price:
    # Whatever duration a CLI reports a write under, it is billed as input.
    return Price(provider, model, Rates(input, cached, output, tuple((d, input) for d in ("5m", "1h", "30m", "12h"))),
                 source)


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
    # Off-peak: Pro $0.66 / $0.022 / $1.98, Flash $0.15 / $0.003 / $0.60.
    _no_write_charge(DEEPSEEK, "deepseek-v4-pro", input=1.32, cached=0.044, output=3.96, source=_DEEPSEEK_SOURCE),
    _no_write_charge(DEEPSEEK, "deepseek-flash", input=0.30, cached=0.006, output=1.20, source=_DEEPSEEK_SOURCE),
    _no_write_charge(OPENROUTER, "z-ai/glm-5.3", input=1.40, cached=0.26, output=4.40,
                     source=_ZAI_ON_OPENROUTER_SOURCE.format(model="z-ai/glm-5.3")),
    _no_write_charge(OPENROUTER, "z-ai/glm-5.3-flash", input=0.15, cached=0.03, output=0.50,
                     source=_ZAI_ON_OPENROUTER_SOURCE.format(model="z-ai/glm-5.3-flash")),
)}


def request_charge_bound(price: Price, *, max_input_tokens: int, max_output_tokens: int,
                         per_request_usd: float = 0.0) -> float:
    """One request's worst case: all its input at the dearest input-side rate the price allows
    (uncached, cached, any cache write, the long-context tier), all its output at the dearest output
    rate. `max_input_tokens` is the endpoint's context window, which counts whatever the provider
    adds to the request."""
    candidates = [price.rates] + ([price.tier.rates] if price.tier is not None else [])
    input_rate = max(max([r.input, r.cache_read] + [p for _, p in r.cache_write]) for r in candidates)
    output_rate = max(r.output for r in candidates)
    return (max_input_tokens * input_rate + max_output_tokens * output_rate) / 1e6 + per_request_usd


def lookup(provider: str, model: str) -> Price:
    try:
        return PRICES[(provider, model)]
    except KeyError:
        raise UnpricedModel(f"no {AS_OF} price for {model} on {provider}") from None
