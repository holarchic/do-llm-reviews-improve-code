"""How each provider reports what it billed: the proxy's settlement adapters.

A settler reads a request's billing id from the response's parsed events, at fixed paths only (a
nested id elsewhere in the body is never taken), reports its billed charge, and reads the key's
usage for reconciliation. Every outcome is typed: a lookup that fails or answers without a usable
charge is `Pending`, which keeps the request's worst case counted; nothing short of the provider's
own figure settles it. A settler belongs to one billing scope: it holds that scope's key, checked
against the fingerprint the scope was registered with.
"""

import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import envelope, providers
from .route_config import Upstream

NAMES = ("openrouter",)


class UsageUnavailable(Exception):
    """The key's usage could not be read: reconciliation has no snapshot."""


class NoSettler(Exception):
    """No settler can serve this scope: an unknown adapter, or a key file holding another key."""


@dataclass(frozen=True)
class Billed:
    usd: float

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


class Settler:
    def billing_id(self, event: dict) -> str | None:
        """The billing id carried by one parsed response event (or a whole JSON body), if any."""
        raise NotImplementedError

    def settle(self, billing_id: str) -> Outcome:
        raise NotImplementedError

    def key_usage(self) -> float:
        """The key's cumulative usage in USD; raises when it can't be read."""
        raise NotImplementedError


class OpenRouterSettler(Settler):
    """OpenRouter: the `gen-…` id is the response's own id (Responses: `response.id`, Messages
    stream: `message.id` of `message_start`); `GET /generation?id=` reports `total_cost`."""

    _GEN = re.compile(r"^gen-[0-9A-Za-z-]+$")

    def __init__(self, api_base: str, key: str):
        self.api_base, self._key = api_base.rstrip("/"), key

    def billing_id(self, event: dict) -> str | None:
        for candidate in (event.get("id"), (event.get("message") or {}).get("id") if isinstance(event.get("message"), dict)
                          else None, (event.get("response") or {}).get("id") if isinstance(event.get("response"), dict)
                          else None):
            if isinstance(candidate, str) and self._GEN.match(candidate):
                return candidate
        return None

    def settle(self, billing_id: str) -> Outcome:
        try:
            data = self._get(f"/generation?id={urllib.parse.quote(billing_id)}")["data"]
        except urllib.error.HTTPError as exc:  # a 404 proves nothing
            exc.close()
            return Pending(f"lookup answered {exc.code}")
        except (OSError, ValueError, KeyError, TypeError) as exc:  # nor does an outage or a garbled answer
            return Pending(f"lookup failed: {type(exc).__name__}")
        cost = data.get("total_cost") if isinstance(data, dict) else None
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and 0 <= cost < float("inf"):
            return Billed(float(cost))
        return Pending("no total_cost yet")

    def key_usage(self) -> float:
        try:
            usage = self._get("/key")["data"]["usage"]
        except urllib.error.HTTPError as exc:
            exc.close()
            raise UsageUnavailable(f"key lookup answered {exc.code}") from None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise UsageUnavailable(f"key lookup failed: {type(exc).__name__}") from None
        if isinstance(usage, bool) or not isinstance(usage, (int, float)) or not math.isfinite(usage) or usage < 0:
            raise UsageUnavailable(f"usage is {usage!r}")
        return float(usage)

    def _get(self, path: str) -> dict:
        request = urllib.request.Request(self.api_base + path, headers={"Authorization": f"Bearer {self._key}"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)


def from_upstream(upstream: Upstream, fingerprint: str) -> Settler:
    """The settler an upstream names, for the scope whose key has `fingerprint`."""
    if upstream.settler != "openrouter":
        raise NoSettler(f"no settler {upstream.settler!r}")
    try:
        key = providers.api_key(providers.OPENROUTER)
    except (OSError, ValueError) as exc:
        raise NoSettler(f"the key file can't be read: {type(exc).__name__}") from None
    if envelope.fingerprint(key) != fingerprint:
        raise NoSettler("the key file holds a different key from the scope's")
    return OpenRouterSettler(upstream.base_url + "/v1", key)
