"""How each provider reports what it billed: the proxy's settlement adapters.

A settler reads a request's billing id from the response's parsed events, at fixed paths only (a
nested id elsewhere in the body is never taken), reports its charge, and reads the key's usage for
reconciliation. Every outcome is typed: a lookup that fails or answers without a usable charge is
`Pending`, which keeps the request's worst case counted. A settler belongs to one billing scope: it
holds that scope's key, checked against the fingerprint the scope was registered with.

Two kinds of settlement:
- **Billed** (OpenRouter): the provider's own figure for the request, looked up by its id.
- **Metered** (DeepSeek, which reports no per-request charge): the usage the response itself
  reports in its terminal event, priced at the route model's dearest (peak) rates. `read` takes one
  event at a time and keeps no state; the proxy records each reading on the request's own record.
  Every settled amount must be at least the real charge: that holds while the reported usage is
  complete, which A3b checks against the account balance before a route is admitted, and the
  one-sided reconciliation against the balance detects a breach afterwards.
"""

import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import envelope, prices, providers
from .route_config import Upstream

NAMES = ("openrouter", "deepseek")


class UsageUnavailable(Exception):
    """The key's usage could not be read: reconciliation has no snapshot."""


class NoSettler(Exception):
    """No settler can serve this scope: an unknown adapter, a key file holding another key, or a
    metered route whose model has no price."""


@dataclass(frozen=True)
class Billed:
    usd: float
    served: str | None = None  # who served it, when the provider says
    tokens: dict | None = None  # the provider's own counts: {"prompt", "completion", "cached"}

    def __post_init__(self):
        if isinstance(self.usd, bool) or not isinstance(self.usd, (int, float)) or not 0 <= self.usd < float("inf"):
            raise ValueError(f"billed {self.usd!r} is not a charge")


@dataclass(frozen=True)
class NotBilled:
    pass


@dataclass(frozen=True)
class Pending:
    reason: str = ""


Outcome = Billed | NotBilled | Pending


@dataclass(frozen=True)
class Usage:
    """A valid usage figure, priced, with the token counts it priced: {"fresh", "cached", "output"}."""
    usd: float
    tokens: dict | None = None


@dataclass(frozen=True)
class Invalid:
    """A terminal usage block that can't be priced: the request never settles automatically."""
    reason: str


@dataclass(frozen=True)
class EventReading:
    metered: Usage | Invalid | None = None  # None: the event carries no usage
    served: str | None = None  # the model the event says served the request
    complete: bool = False  # the event ends the response's protocol

    @property
    def empty(self) -> bool:
        return self.metered is None and self.served is None and not self.complete


class Settler:
    def billing_id(self, event: dict) -> str | None:
        """The billing id carried by one parsed response event (or a whole JSON body), if any."""
        raise NotImplementedError

    def read(self, event: dict) -> EventReading:
        """What one parsed event says about the request's charge; a billed settler reads nothing."""
        return EventReading()

    def settle(self, pending: envelope.PendingSettlement) -> Outcome:
        raise NotImplementedError

    def key_usage(self) -> float:
        """The scope's cumulative usage reading in USD (signed for a balance); raises when it can't be read."""
        raise NotImplementedError


def _id_at_fixed_paths(event: dict, pattern: re.Pattern) -> str | None:
    message, response = event.get("message"), event.get("response")
    for candidate in (event.get("id"), message.get("id") if isinstance(message, dict) else None,
                      response.get("id") if isinstance(response, dict) else None):
        if isinstance(candidate, str) and pattern.match(candidate):
            return candidate
    return None


def _get_json(url: str, key: str) -> dict:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


class OpenRouterSettler(Settler):
    """OpenRouter: the `gen-…` id is the response's own id (Responses: `response.id`, Messages
    stream: `message.id` of `message_start`); `GET /generation?id=` reports `total_cost` and the
    `provider_name` that served it."""

    _GEN = re.compile(r"^gen-[0-9A-Za-z-]+$")

    def __init__(self, api_base: str, key: str):
        self.api_base, self._key = api_base.rstrip("/"), key

    def billing_id(self, event: dict) -> str | None:
        """Also a Messages error envelope's top-level `request_id`: a refusal OpenRouter may have billed."""
        found = _id_at_fixed_paths(event, self._GEN)
        if found is None and event.get("type") == "error":
            candidate = event.get("request_id")
            found = candidate if isinstance(candidate, str) and self._GEN.match(candidate) else None
        return found

    def settle(self, pending: envelope.PendingSettlement) -> Outcome:
        try:
            data = _get_json(f"{self.api_base}/generation?id={urllib.parse.quote(pending.billing_id)}", self._key)["data"]
        except urllib.error.HTTPError as exc:  # a 404 proves nothing
            exc.close()
            return Pending(f"lookup answered {exc.code}")
        except (OSError, ValueError, KeyError, TypeError) as exc:  # nor does an outage or a garbled answer
            return Pending(f"lookup failed: {type(exc).__name__}")
        cost = data.get("total_cost") if isinstance(data, dict) else None
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and 0 <= cost < float("inf"):
            served = data.get("provider_name")
            return Billed(float(cost), served if isinstance(served, str) and served else None, _native_counts(data))
        return Pending("no total_cost yet")

    def key_usage(self) -> float:
        try:
            usage = _get_json(f"{self.api_base}/key", self._key)["data"]["usage"]
        except urllib.error.HTTPError as exc:
            exc.close()
            raise UsageUnavailable(f"key lookup answered {exc.code}") from None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise UsageUnavailable(f"key lookup failed: {type(exc).__name__}") from None
        if isinstance(usage, bool) or not isinstance(usage, (int, float)) or not math.isfinite(usage) or usage < 0:
            raise UsageUnavailable(f"usage is {usage!r}")
        return float(usage)


def _count(counters, key: str, *, required: bool = True) -> int:
    value = counters.get(key) if isinstance(counters, dict) else None
    if value is None and not required:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{key} is {value!r}")
    return value


class DeepSeekSettler(Settler):
    """DeepSeek's own API: metered. Anthropic stream: `message_start` names the model, the last
    `message_delta` carries the usage, `message_stop` completes it. Responses: `response.created`
    names the model, `response.completed` or `response.incomplete` carries the usage and completes
    it. The balance (`GET /user/balance`) is the reconciliation reading: −(USD total balance)."""

    _ID = re.compile(r"^[0-9A-Za-z_-]{1,128}$")

    def __init__(self, api_base: str, key: str, price: prices.Price | None = None):
        self.api_base, self._key, self.price = api_base.rstrip("/"), key, price

    def billing_id(self, event: dict) -> str | None:
        """Only the events that carry the response's own id: its ids have no prefix to tell them apart."""
        kind = event.get("type")
        if kind == "message_start":
            node = event.get("message")
        elif isinstance(kind, str) and kind.startswith("response."):
            node = event.get("response")
        elif kind == "message" or event.get("object") == "response":
            node = event  # an unstreamed body
        else:
            return None
        candidate = node.get("id") if isinstance(node, dict) else None
        return candidate if isinstance(candidate, str) and self._ID.match(candidate) else None

    def read(self, event: dict) -> EventReading:
        kind = event.get("type")
        if kind == "message_start":
            return EventReading(served=_model(event.get("message")))
        if kind == "message_delta" and "usage" in event:
            return EventReading(metered=self._anthropic(event["usage"]))
        if kind == "message_stop":
            return EventReading(complete=True)
        if kind == "message":  # a whole, unstreamed Messages body
            return EventReading(self._anthropic(event.get("usage")), _model(event), True)
        if kind == "response.created":
            return EventReading(served=_model(event.get("response")))
        if kind in ("response.completed", "response.incomplete"):
            response = event.get("response")
            usage = response.get("usage") if isinstance(response, dict) else None
            return EventReading(self._responses(usage), _model(response), True)
        if event.get("object") == "response" and event.get("status") in ("completed", "incomplete"):
            return EventReading(self._responses(event.get("usage")), _model(event), True)  # unstreamed
        return EventReading()

    def _anthropic(self, usage) -> Usage | Invalid:
        """`input_tokens` excludes cache hits: total input is it plus the cache reads and writes, so every
        counter is required (an absent one could hide input)."""
        try:
            _no_server_tools(usage)
            fresh = _count(usage, "input_tokens") + _count(usage, "cache_creation_input_tokens")
            return self._priced(fresh, _count(usage, "cache_read_input_tokens"), _count(usage, "output_tokens"))
        except (ValueError, OverflowError) as exc:
            return Invalid(str(exc))

    def _responses(self, usage) -> Usage | Invalid:
        """`input_tokens` includes the cached ones (an absent cached count prices them all as fresh, an
        overstatement); `output_tokens` includes reasoning."""
        try:
            _no_server_tools(usage)
            total = _count(usage, "input_tokens")
            cached = _count(usage.get("input_tokens_details"), "cached_tokens", required=False)
            if cached > total:
                raise ValueError("cached tokens exceed input tokens")
            return self._priced(total - cached, cached, _count(usage, "output_tokens"))
        except (ValueError, AttributeError, OverflowError) as exc:
            return Invalid(str(exc))

    def _priced(self, fresh: int, cached: int, output: int) -> Usage:
        if self.price is None:
            raise ValueError("no price for this route's model")
        r = self.price.rates
        return Usage((fresh * r.input + cached * r.cache_read + output * r.output) / 1e6,
                     {"fresh": fresh, "cached": cached, "output": output})

    def settle(self, pending: envelope.PendingSettlement) -> Outcome:
        """The metered figure, once the response is complete and its usage valid and unambiguous."""
        if pending.metered_invalid:
            return Pending(f"usage can't be priced: {pending.metered_invalid}")
        if not pending.protocol_complete or pending.metered is None:
            return Pending("the response never completed with its usage")
        return Billed(pending.metered)

    def key_usage(self) -> float:
        return self.balance_reading()[0]

    def balance_reading(self) -> tuple[float, str]:
        """The usage reading, −(USD balance), with the balance exactly as DeepSeek wrote it."""
        try:
            infos = _get_json(f"{self.api_base}/user/balance", self._key)["balance_infos"]
        except urllib.error.HTTPError as exc:
            exc.close()
            raise UsageUnavailable(f"balance lookup answered {exc.code}") from None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise UsageUnavailable(f"balance lookup failed: {type(exc).__name__}") from None
        balances = _balances(infos)
        if any(currency != "USD" and amount != 0 for currency, amount in balances.items()):
            raise UsageUnavailable("a non-USD balance can't be reconciled in dollars")
        if "USD" not in balances:
            raise UsageUnavailable("no USD balance")
        raw = next(str(info["total_balance"]) for info in infos if info.get("currency") == "USD")
        return -balances["USD"], raw


def _model(node) -> str | None:
    model = node.get("model") if isinstance(node, dict) else None
    return model if isinstance(model, str) and model else None


def _native_counts(data: dict) -> dict | None:
    """OpenRouter's own token counts for a generation, when all three are present and sane."""
    counts = {"prompt": data.get("native_tokens_prompt"), "completion": data.get("native_tokens_completion"),
              "cached": data.get("native_tokens_cached") if data.get("native_tokens_cached") is not None else 0}
    ok = all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in counts.values())
    return counts if ok and counts["cached"] <= counts["prompt"] else None


def _balances(infos) -> dict[str, float]:
    """Each row strictly: a currency named once, its total a finite number (DeepSeek sends a string)."""
    if not isinstance(infos, list):
        raise UsageUnavailable("balance_infos is not a list")
    balances = {}
    for info in infos:
        currency = info.get("currency") if isinstance(info, dict) else None
        total = info.get("total_balance") if isinstance(info, dict) else None
        if not isinstance(currency, str) or currency in balances:
            raise UsageUnavailable(f"balance row {info!r} names no currency, or one already read")
        if isinstance(total, bool) or not isinstance(total, (str, int, float)):
            raise UsageUnavailable(f"{currency} balance is {total!r}")
        try:
            amount = float(total)
        except (ValueError, OverflowError):
            raise UsageUnavailable(f"{currency} balance is {total!r}") from None
        if not math.isfinite(amount):
            raise UsageUnavailable(f"{currency} balance is {total!r}")
        balances[currency] = amount
    return balances


def _no_server_tools(usage) -> None:
    """Server tools bill per call, outside token prices: a request that used one can't be metered. An
    absent block means none; a present one (null included) must be counters, all zero."""
    if not isinstance(usage, dict) or "server_tool_use" not in usage:
        return
    tools = usage["server_tool_use"]
    if not isinstance(tools, dict) or any(isinstance(v, bool) or not isinstance(v, int) or v < 0
                                          for v in tools.values()):
        raise ValueError(f"server_tool_use is {tools!r}")
    if any(tools.values()):
        raise ValueError("a server tool was used")


def from_upstream(upstream: Upstream, fingerprint: str) -> Settler:
    """The settler an upstream names, for the scope whose key has `fingerprint`."""
    if upstream.settler not in NAMES:
        raise NoSettler(f"no settler {upstream.settler!r}")
    try:
        key = providers.api_key(upstream.settler)
    except (OSError, ValueError) as exc:
        raise NoSettler(f"the key file can't be read: {type(exc).__name__}") from None
    if envelope.fingerprint(key) != fingerprint:
        raise NoSettler("the key file holds a different key from the scope's")
    if upstream.settler == "openrouter":
        return OpenRouterSettler(upstream.base_url + "/v1", key)
    try:
        price = prices.lookup(prices.DEEPSEEK, upstream.model or "")
    except prices.UnpricedModel as exc:
        raise NoSettler(str(exc)) from None
    return DeepSeekSettler(upstream.base_url, key, price)


def opening_usage(provider: str, key: str) -> float:
    """A scope's opening usage reading, through the provider's own settler."""
    if provider == "openrouter":
        return OpenRouterSettler(providers.get(provider).upstream + "/v1", key).key_usage()
    return DeepSeekSettler(providers.get(provider).upstream, key).key_usage()
