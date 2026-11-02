"""The spend envelope: one authoritative ledger per budget, bounding final billed liability.

Every experiment and preparation step drawing on a budget names its envelope, so probes,
calibration and retries sit inside the same cumulative bound. The guarantee, in parts:
- envelope: funded credit + purchase fees + taxes + Σ_routes(slots × max_charge) ≤ envelope;
- per request, per billing scope (a provider key): settled + Σ(unresolved, each at the bound it
  was admitted under) + the candidate's bound ≤ the scope's allocation, the allocations together
  never exceeding funded credit; so provider usage stays within funded credit whatever the
  provider's own balance says;
- reconciliation, one-sided: a key billed beyond settled + unresolved bounds refuses everything;
  usage below settled is reporting lag, already counted in full by admission. A usage reading is
  signed (a metered scope reads −balance) and cumulative, so one that falls (a top-up, a refund, an
  expired grant) faults its scope, refusing everything until the operator clears it.

One process owns an envelope at a time (an exclusive flock held for its lifetime); every
transition is check, modify a copy, persist with an atomic replace, then swap in, under one mutex,
so a crash or a failed write leaves the last persisted state and never a half-recorded charge.
Lock order is the envelope's own flock, then the registry lock, then the in-process mutex. State that can't be read or doesn't
check out refuses everything.
"""

import copy
import fcntl
import hashlib
import json
import math
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "data" / "envelopes"
SNAPSHOT_SECONDS = 600
PROBE = "probe"  # the job id of a probe reservation
STAGES = ("probe", "validated")
_EPSILON = 1e-9


def fingerprint(credential: str) -> str:
    """How a scope names its key: never the key itself."""
    return hashlib.sha256(credential.encode()).hexdigest()[:16]


def _version(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Route:
    name: str
    scope: str
    max_charge: float
    slots: int
    policy: dict
    bootstrap_build_usd: float
    stage: str
    upstream: dict | None
    key_fingerprint: str
    version: str  # admission checks it: the ledger records exactly the route the proxy checked


@dataclass(frozen=True)
class PendingSettlement:
    request: str
    billing_id: str
    upstream: dict | None
    scope: str
    key_fingerprint: str  # the scope's key: settlement is looked up with that key only
    metered: float | None = None  # the usage the response reported, priced (a metered route)
    metered_invalid: str | None = None  # why its usage can't be priced: it never settles automatically
    protocol_complete: bool = False  # the response's protocol ended (not merely its forwarding)


@dataclass(frozen=True)
class ScopeAccount:
    """What the ledger holds for a scope: its opening usage reading, the spend it has settled, its
    unresolved requests, and when each request was admitted."""
    opening_usage: float
    settled: float
    unresolved: int
    admitted_at: tuple[float, ...]


class Refused(Exception):
    """A request, job or funding entry the bound does not admit."""


class EnvelopeError(Exception):
    """An envelope that can't be owned, read or trusted; nothing is admitted."""


def _money(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _reading(value) -> bool:
    """A usage reading: finite, and signed (a metered scope reads −balance)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class Envelope:
    """The owner's handle: `with Envelope.owned(name) as env:`; every method is one transition."""

    def __init__(self, path: Path, state: dict, lock_file, now):
        self.path, self._state, self._lock_file, self._now = path, state, lock_file, now
        self._mutex = threading.Lock()
        self._closed = False  # set before the flock is released: a straggling thread can no longer write

    # ---- ownership and persistence

    @classmethod
    @contextmanager
    def owned(cls, name: str, *, root: Path | None = None, create_usd: float | None = None, now=time.time):
        root = DEFAULT_ROOT if root is None else root  # resolved per call, so a test can point it elsewhere
        root.mkdir(parents=True, exist_ok=True)
        lock_file = open(root / f"{name}.lock", "a")
        try:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise EnvelopeError(f"envelope {name} is owned by another process") from None
            path = root / f"{name}.json"
            if not path.exists():
                if create_usd is None:
                    raise EnvelopeError(f"no envelope {name}")
                if not _money(create_usd) or create_usd <= 0:
                    raise EnvelopeError(f"envelope must be a positive amount, not {create_usd!r}")
                _write(path, _empty(name, create_usd))
            envelope = cls(path, load(path), lock_file, now)
            try:
                yield envelope
            finally:
                with envelope._mutex:
                    envelope._closed = True
        finally:
            lock_file.close()  # releases the flock

    def _transition(self, change) -> object:
        """Apply `change` to a copy, persist it, then swap it in: the persisted file is always whole."""
        with self._mutex:
            if self._closed:
                raise EnvelopeError("this envelope is no longer owned here")
            draft = copy.deepcopy(self._state)
            result = change(draft)
            _check(draft)
            try:
                _write(self.path, draft)
            except OSError as exc:
                # The file may hold either state (a failure after the replace has committed the new one):
                # this owner can no longer tell, so it writes nothing more; a new owner reloads the file.
                self._closed = True
                raise EnvelopeError(f"the ledger write failed ({type(exc).__name__}); reopen the envelope") from None
            self._state = draft
            return result

    @property
    def state(self) -> dict:
        with self._mutex:
            return copy.deepcopy(self._state)

    # ---- funding, scopes and routes

    def fund(self, credit: float = 0.0, fees: float = 0.0, taxes: float = 0.0) -> None:
        if not all(_money(v) for v in (credit, fees, taxes)):
            raise Refused("funding amounts must be non-negative numbers")

        def change(s):
            s["funding"].append({"credit": credit, "fees": fees, "taxes": taxes, "at": self._now()})
            _require_bound(s)
        self._transition(change)

    def fund_probe(self, usd: float) -> None:
        """Extend the probe reservation, which probe-stage requests draw on."""
        if not _money(usd) or usd <= 0:
            raise Refused("a probe reservation must be positive")

        def change(s):
            held = s["reservations"].setdefault(PROBE, {"amount": 0.0, "open": True, "terminated": False,
                                                        "baseline": 0.0})  # cumulative: probe funding vs spend
            held["amount"] += usd
            held["open"], held["terminated"] = True, False
            _require_reservations_fit(s)
        self._transition(change)

    def register_scope(self, scope: str, *, provider: str, fingerprint: str, opening_usage: float,
                       allocation: float, provider_limit: float | None, account: str | None = None) -> None:
        """`account` names the provider account the key belongs to (the operator's label): account-level
        evidence, like an exhaustion run, carries over between keys of the same account."""
        if not (_reading(opening_usage) and _money(allocation)) or not fingerprint:
            raise Refused("a scope needs a fingerprint, an opening usage and an allocation")
        registry = open(self.path.parent / "registry.lock", "a")
        try:
            fcntl.flock(registry, fcntl.LOCK_EX)  # serialises the scan with the commit, across envelopes
            for other in sorted(self.path.parent.glob("*.json")):
                if other == self.path:
                    continue
                if any(s["fingerprint"] == fingerprint for s in load(other)["scopes"].values()):
                    raise Refused(f"that key belongs to envelope {other.stem}")

            def change(s):
                if scope in s["scopes"]:
                    raise Refused(f"scope {scope} exists: a scope's key and opening usage never change")
                if any(v["fingerprint"] == fingerprint for v in s["scopes"].values()):
                    raise Refused("that key already has a scope in this envelope")
                s["scopes"][scope] = {"provider": provider, "fingerprint": fingerprint, "opening_usage": opening_usage,
                                      "allocation": allocation, "provider_limit": provider_limit,
                                      "snapshot": None, "excess": 0.0, "fault": None, "account": account}
                _require_allocations(s)
            self._transition(change)
        finally:
            registry.close()

    def raise_allocation(self, scope: str, allocation: float) -> None:
        """Give a scope more of the funded credit; a scope's key and opening usage never change."""
        if not _money(allocation):
            raise Refused("an allocation is a non-negative amount")

        def change(s):
            if scope not in s["scopes"]:
                raise Refused(f"no scope {scope}")
            if allocation < s["scopes"][scope]["allocation"]:
                raise Refused("an allocation only rises")
            s["scopes"][scope]["allocation"] = allocation
            _require_allocations(s)
        self._transition(change)

    def set_route(self, route: str, *, scope: str, max_charge: float, slots: int, policy: dict,
                  bootstrap_build_usd: float, upstream: dict | None = None) -> None:
        """`upstream` is where the proxy forwards: {"base_url": ..., "settler": "openrouter"}."""
        if not (_money(max_charge) and max_charge > 0 and isinstance(slots, int) and not isinstance(slots, bool)
                and slots >= 1 and _money(bootstrap_build_usd) and isinstance(policy, dict)
                and (upstream is None or isinstance(upstream, dict))):
            raise Refused("a route needs a positive max charge, at least one slot, a policy and a bootstrap cost")

        config = {"scope": scope, "max_charge": max_charge, "slots": slots, "policy": copy.deepcopy(policy),
                  "bootstrap_build_usd": bootstrap_build_usd, "upstream": copy.deepcopy(upstream)}

        def change(s):
            if scope not in s["scopes"]:
                raise Refused(f"no scope {scope}")
            old = s["routes"].get(route)
            unchanged = old is not None and {k: old[k] for k in config} == config
            # A validated route stays validated only while nothing about it changes.
            s["routes"][route] = {**config, "stage": old["stage"] if unchanged else "probe"}
            _require_bound(s)
        self._transition(change)

    def validate_route(self, route: str) -> None:
        def change(s):
            if route not in s["routes"]:
                raise Refused(f"no route {route}")
            s["routes"][route]["stage"] = "validated"
        self._transition(change)

    # ---- reconciliation

    def reconcile(self, scope: str, key_usage: float) -> float:
        """Record a successful reading of the key's usage; returns the excess (0 when it reconciles).
        A reading below the last one (or, first, below the opening usage) faults the scope."""
        if not _reading(key_usage):
            raise Refused(f"key usage {key_usage!r} is not a reading")

        def change(s):
            if scope not in s["scopes"]:
                raise Refused(f"no scope {scope}")
            entry = s["scopes"][scope]
            previous = entry["opening_usage"] if entry["snapshot"] is None else entry["snapshot"]["usage"]
            if key_usage < previous - _EPSILON and entry["fault"] is None:
                entry["fault"] = (f"usage fell from {previous:.4f} to {key_usage:.4f} (a top-up, refund or "
                                  "expired grant): a top-up can mask an overrun")
            accounted = _settled(s, scope) + _unresolved(s, scope)
            excess = max(0.0, key_usage - entry["opening_usage"] - accounted)
            entry["snapshot"] = {"usage": key_usage, "at": self._now()}
            entry["excess"] = excess if excess > _EPSILON else 0.0
            return entry["excess"]
        return self._transition(change)

    def clear_fault(self, scope: str) -> None:
        """The operator's release of a faulted scope, after checking the account's billing."""
        def change(s):
            if scope not in s["scopes"]:
                raise Refused(f"no scope {scope}")
            s["scopes"][scope]["fault"] = None
        self._transition(change)

    # ---- jobs

    def reserve(self, job: str, amount: float) -> None:
        if not _money(amount) or amount <= 0 or job == PROBE:
            raise Refused("a reservation is a positive amount for a job")

        def change(s):
            _require_trusted(s, self._now())
            held = s["reservations"].get(job)
            if held is not None and held["open"]:
                held["terminated"] = False  # a resumed job keeps the reservation it holds
                return
            # A job id reopened for a fresh execution starts a fresh hold: what it spent before (all settled,
            # since its reservation closed) counts as other spend, not inside the new amount.
            s["reservations"][job] = {"amount": amount, "open": True, "terminated": False,
                                      "baseline": _job_spend(s, job) if held is not None else 0.0}
            _require_reservations_fit(s)
        self._transition(change)

    def finish(self, job: str) -> bool:
        """Record that a job has terminated. Its reservation closes now if every request it made has
        settled, else when its last one settles. True when it closed now."""
        def change(s):
            held = s["reservations"].get(job)
            if held is None or not held["open"]:
                return True
            held["terminated"] = True
            return _close_if_done(s, job)
        return self._transition(change)

    # ---- requests

    def admit(self, request: str, route: str, *, job: str, attempt: str, version: str | None = None) -> float:
        """Admit one request, or record a refusal against its attempt and raise Refused. With `version`,
        the route must still be exactly the one the caller checked."""
        def change(s):
            if request in s["requests"]:
                raise Refused(f"request {request} was already admitted")
            reason = _admission_problem(s, route, job, attempt, self._now())
            if not reason and version is not None and _version(_config(s["routes"][route])) != version:
                reason = f"route {route} changed while the request was checked"
            if not reason:
                r = s["routes"][route]
                s["requests"][request] = {"route": route, "scope": r["scope"], "bound": r["max_charge"], "job": job,
                                          "attempt": attempt, "state": "unresolved", "billed": None,
                                          "billing_id": None, "ambiguous": False, "captured": False,
                                          "metered": None, "metered_invalid": None, "protocol_complete": False,
                                          "served": [], "metered_tokens": None, "billed_tokens": None,
                                          "refused_by_provider": None,
                                          "upstream": copy.deepcopy(r["upstream"]), "admitted_at": self._now()}
                try:
                    _require_reservations_fit(s)  # the request must not eat into another job's hold
                except Refused as exc:
                    del s["requests"][request]
                    reason = str(exc)
            if reason:
                s["refusals"].setdefault(attempt, reason)  # the first reason stands: a retrying CLI can't grow it
                return reason
            return None
        reason = self._transition(change)
        if reason:
            raise Refused(reason)
        return self._state["requests"][request]["bound"]

    def record_billing_id(self, request: str, billing_id: str) -> None:
        """The response's billing id; a second, different one makes it ambiguous, never re-pointed."""
        def change(s):
            entry = s["requests"][request]
            if entry["billing_id"] is None and not entry["ambiguous"]:
                entry["billing_id"] = billing_id
            elif entry["billing_id"] != billing_id:
                entry["billing_id"], entry["ambiguous"] = None, True
        self._transition(change)

    def record_reading(self, request: str, *, metered: float | None = None, invalid: str | None = None,
                       served: str | None = None, complete: bool = False, tokens: dict | None = None) -> None:
        """What one response event said: a priced usage figure (a second, different one makes the usage
        unusable), an unusable usage block (sticky), the model that served it, the protocol's end."""
        if metered is not None and not _money(metered):
            raise Refused(f"metered {metered!r} is not a charge")

        def change(s):
            entry = s["requests"][request]
            if invalid is not None and entry["metered_invalid"] is None:
                entry["metered_invalid"] = invalid
            if metered is not None:
                if entry["metered"] is None:
                    entry["metered"], entry["metered_tokens"] = metered, copy.deepcopy(tokens)
                elif abs(entry["metered"] - metered) > _EPSILON and entry["metered_invalid"] is None:
                    entry["metered_invalid"] = "two different usage figures"
            if served is not None and served not in entry["served"]:
                entry["served"].append(served)
            if complete:
                entry["protocol_complete"] = True
        self._transition(change)

    def capture_complete(self, request: str) -> None:
        """The response has ended: its billing id, if any, is final, and settlement may look it up."""
        def change(s):
            s["requests"][request]["captured"] = True
        self._transition(change)

    def settle_looked_up(self, request: str, billing_id: str, billed: float | None,
                         served: str | None = None, tokens: dict | None = None) -> bool:
        """An automatic settlement: committed only if the request is still unresolved, its capture has
        ended, and `billing_id` is still its one unambiguous id. False (and nothing changed) otherwise."""
        def change(s):
            entry = s["requests"].get(request)
            if (entry is None or entry["state"] != "unresolved" or not entry["captured"] or entry["ambiguous"]
                    or entry["billing_id"] != billing_id):
                return False
            if served is not None and served not in entry["served"]:
                entry["served"].append(served)
            entry["billed_tokens"] = copy.deepcopy(tokens)
            return _settle(s, entry, billed)
        if billed is not None and not _money(billed):
            raise Refused(f"billed {billed!r} is not a charge")
        return self._transition(change)

    def settle_refused(self, request: str, evidence: dict) -> bool:
        """The provider refused it before generating (a payment or rate refusal with no billing id): not
        billed, keeping the refusal as evidence. Never applies to a request with a billing id."""
        def change(s):
            entry = s["requests"].get(request)
            if (entry is None or entry["state"] != "unresolved" or not entry["captured"] or entry["ambiguous"]
                    or entry["billing_id"] is not None or entry["metered"] is not None):
                return False
            entry["refused_by_provider"] = copy.deepcopy(evidence)
            return _settle(s, entry, None)
        return self._transition(change)

    def scope_label(self, scope: str) -> str | None:
        """The provider account a scope's key belongs to, as the operator named it."""
        with self._mutex:
            return self._state["scopes"][scope]["account"]

    def scope_account(self, scope: str) -> ScopeAccount:
        with self._mutex:
            requests = [r for r in self._state["requests"].values() if r["scope"] == scope]
            return ScopeAccount(self._state["scopes"][scope]["opening_usage"],
                                sum(r["billed"] for r in requests if r["state"] != "unresolved"),
                                sum(r["state"] == "unresolved" for r in requests),
                                tuple(r["admitted_at"] for r in requests))

    def requests_of(self, attempt: str) -> list[dict]:
        """An attempt's requests, as recorded (copies): the admission checks' evidence."""
        with self._mutex:
            return [{"request": rid, **copy.deepcopy(r)} for rid, r in self._state["requests"].items()
                    if r["attempt"] == attempt]

    def settle(self, request: str, billed: float | None) -> bool:
        """The operator's resolution, after checking the provider's billing (None: not billed).
        Idempotent: a settled request stays as it was."""
        if billed is not None and not _money(billed):
            raise Refused(f"billed {billed!r} is not a charge")

        def change(s):
            entry = s["requests"].get(request)
            if entry is None:
                raise Refused(f"no request {request}")
            if entry["state"] != "unresolved":
                return False
            return _settle(s, entry, billed)
        return self._transition(change)

    # ---- typed views

    def route(self, name: str) -> "Route | None":
        with self._mutex:
            return route_view(self._state, name)

    def route_names(self) -> list[str]:
        with self._mutex:
            return sorted(self._state["routes"])

    def scopes(self) -> list[str]:
        with self._mutex:
            return sorted(self._state["scopes"])

    def pending_settlements(self) -> list["PendingSettlement"]:
        """Unresolved requests with a billing id to look up; each keeps the upstream it was admitted under."""
        with self._mutex:
            return [PendingSettlement(rid, r["billing_id"], copy.deepcopy(r["upstream"]), r["scope"],
                                      self._state["scopes"][r["scope"]]["fingerprint"], r["metered"],
                                      r["metered_invalid"], r["protocol_complete"])
                    for rid, r in self._state["requests"].items()
                    if r["state"] == "unresolved" and r["captured"] and r["billing_id"] and not r["ambiguous"]]

    def answered(self, attempt: str) -> int:
        """How many of an attempt's admitted requests came back with a billing id: the routed model answered."""
        with self._mutex:
            return sum(r["attempt"] == attempt and r["billing_id"] is not None for r in self._state["requests"].values())

    def refusal(self, attempt: str) -> str | None:
        with self._mutex:
            return self._state["refusals"].get(attempt)


# ---- reading and checking state


def load(path: Path) -> dict:
    """An envelope's state, read without ownership (status); unreadable or inconsistent state raises."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EnvelopeError(f"{path.name} unreadable: {exc!r}") from None
    _upgrade(state)
    _check(state)
    return state


# Fields schema 2 added, and their value for a schema-1 ledger: not recorded, and conservative where
# it matters (a schema-1 request never settles by metering: its usage was never captured).
_SCOPE_DEFAULTS = {"fault": None, "account": None}
_REQUEST_DEFAULTS = {"metered": None, "metered_invalid": "recorded before metering existed",
                     "protocol_complete": False, "served": [], "metered_tokens": None, "billed_tokens": None,
                     "refused_by_provider": None}


def _upgrade(state) -> None:
    """A schema-1 (unversioned) ledger is upgraded: every charge and unresolved liability stays as
    written. A current ledger is never filled in: a field missing from it is damage, and `_check`
    refuses it. Malformed containers are left for `_check` to refuse."""
    if not isinstance(state, dict) or "schema" in state:
        return
    tables = [state.get(k) for k in ("scopes", "requests", "reservations")]
    if not all(isinstance(t, dict) for t in tables):
        return
    scopes, requests, reservations = tables
    for scope in scopes.values():
        if isinstance(scope, dict):
            for key, value in _SCOPE_DEFAULTS.items():
                scope.setdefault(key, value)
    for request in requests.values():
        if isinstance(request, dict):
            for key, value in _REQUEST_DEFAULTS.items():
                request.setdefault(key, copy.deepcopy(value))
    for held in reservations.values():
        if isinstance(held, dict):
            held.setdefault("baseline", 0.0)
    state["schema"] = SCHEMA


def route_view(state: dict, name: str) -> "Route | None":
    """A route as typed view of a state (an owner's, or a read-only `load`)."""
    r = state["routes"].get(name)
    if r is None:
        return None
    return Route(name, r["scope"], r["max_charge"], r["slots"], copy.deepcopy(r["policy"]), r["bootstrap_build_usd"],
                 r["stage"], copy.deepcopy(r["upstream"]), state["scopes"][r["scope"]]["fingerprint"],
                 _version(_config(r)))


def summary(state: dict, now: float) -> dict:
    return {
        "envelope": state["envelope_usd"], "funded_credit": _funded(state), "fees_and_taxes": _purchase_costs(state),
        "overdraw": _overdraw(state), "settled": _settled(state), "unresolved": _unresolved(state),
        "unresolved_requests": sum(r["state"] == "unresolved" for r in state["requests"].values()),
        "open_reservations": {j: r["amount"] for j, r in state["reservations"].items() if r["open"]},
        "scopes": {k: {"allocation": v["allocation"], "settled": _settled(state, k), "excess": v["excess"],
                       "fault": v["fault"],
                       "snapshot_age": None if v["snapshot"] is None else now - v["snapshot"]["at"]}
                   for k, v in state["scopes"].items()},
        "refusals": dict(state["refusals"]),
        "served": {route: sorted({m for r in state["requests"].values() if r["route"] == route for m in r["served"]})
                   for route in state["routes"]},
    }


SCHEMA = 2  # 1 (unversioned): before the metering, refusal and account fields


def _empty(name: str, envelope_usd: float) -> dict:
    return {"schema": SCHEMA, "name": name, "envelope_usd": envelope_usd, "funding": [], "scopes": {}, "routes": {},
            "requests": {}, "reservations": {}, "refusals": {}}


def _write(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".json.partial")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(state, indent=1, sort_keys=True))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    directory = os.open(path.parent, os.O_RDONLY)  # the rename is durable only once its directory is
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _config(route: dict) -> dict:
    return {k: route[k] for k in ("scope", "max_charge", "slots", "policy", "bootstrap_build_usd", "upstream")}


def _settle(s: dict, entry: dict, billed: float | None) -> bool:
    entry["state"], entry["billed"] = ("billed", billed) if billed is not None else ("not_billed", 0.0)
    held = s["reservations"].get(entry["job"])
    if held is not None and held["open"] and held["terminated"]:
        _close_if_done(s, entry["job"])
    return True


def _close_if_done(s: dict, job: str) -> bool:
    if any(r["job"] == job and r["state"] == "unresolved" for r in s["requests"].values()):
        return False
    s["reservations"][job]["open"] = False
    return True


def _check(s: dict) -> None:
    """Explicit checks, not asserts: `python -O` would strip those and trust a corrupt ledger."""
    def ensure(ok, what: str) -> None:
        if not ok:
            raise EnvelopeError(f"envelope state does not check out: {what}")
    try:
        ensure(isinstance(s, dict) and set(s) == {"schema", "name", "envelope_usd", "funding", "scopes", "routes",
                                                  "requests", "reservations", "refusals"}, "top-level fields")
        ensure(s["schema"] == SCHEMA, f"schema {s['schema']!r}")
        ensure(_money(s["envelope_usd"]) and s["envelope_usd"] > 0, "envelope amount")
        ensure(all(all(_money(f[k]) for k in ("credit", "fees", "taxes")) for f in s["funding"]), "funding")
        for name, scope in s["scopes"].items():
            ensure(_money(scope["allocation"]) and _reading(scope["opening_usage"]) and _money(scope["excess"])
                   and isinstance(scope["fingerprint"], str) and scope["fingerprint"]
                   and (scope["fault"] is None or isinstance(scope["fault"], str))
                   and (scope["account"] is None or isinstance(scope["account"], str)), f"scope {name}")
            snap = scope["snapshot"]
            ensure(snap is None or (_reading(snap["usage"]) and _money(snap["at"])), f"scope {name} snapshot")
        for name, route in s["routes"].items():
            ensure(route["scope"] in s["scopes"] and route["stage"] in STAGES and _money(route["max_charge"])
                   and route["max_charge"] > 0
                   and isinstance(route["slots"], int) and not isinstance(route["slots"], bool) and route["slots"] >= 1
                   and isinstance(route["policy"], dict) and _money(route["bootstrap_build_usd"])
                   and (route["upstream"] is None or isinstance(route["upstream"], dict)), f"route {name}")
        for name, r in s["requests"].items():
            ensure(r["route"] in s["routes"] and r["scope"] in s["scopes"] and _money(r["bound"]) and r["bound"] > 0
                   and isinstance(r["captured"], bool) and _money(r["admitted_at"])
                   and r["state"] in ("unresolved", "billed", "not_billed")
                   and (r["billed"] is None) == (r["state"] == "unresolved")
                   and (r["billed"] is None or _money(r["billed"]))
                   and isinstance(r["job"], str) and isinstance(r["attempt"], str)
                   and isinstance(r["ambiguous"], bool) and (r["billing_id"] is None or isinstance(r["billing_id"], str))
                   and (r["upstream"] is None or isinstance(r["upstream"], dict))
                   and (r["metered"] is None or _money(r["metered"]))
                   and (r["metered_invalid"] is None or isinstance(r["metered_invalid"], str))
                   and isinstance(r["protocol_complete"], bool)
                   and isinstance(r["served"], list) and all(isinstance(v, str) for v in r["served"])
                   and _counts(r["metered_tokens"], ("fresh", "cached", "output"))
                   and _counts(r["billed_tokens"], ("prompt", "completion", "cached"))
                   and (r["billed_tokens"] is None or r["billed_tokens"]["cached"] <= r["billed_tokens"]["prompt"])
                   and (r["refused_by_provider"] is None or isinstance(r["refused_by_provider"], dict)),
                   f"request {name}")
        ensure(all(_money(r["amount"]) and isinstance(r["open"], bool) and isinstance(r["terminated"], bool)
                   and _money(r["baseline"]) for r in s["reservations"].values()), "reservations")
        ensure(all(isinstance(v, str) for v in s["refusals"].values()), "refusals")
        # The financial invariants, not only the shapes: a ledger that breaks them is not trusted.
        ensure(_funded(s) + _purchase_costs(s) + _overdraw(s) <= s["envelope_usd"] + _EPSILON,
               "funding + fees + taxes + maximum overdraw exceed the envelope")
        ensure(sum(v["allocation"] for v in s["scopes"].values()) <= _funded(s) + _EPSILON,
               "scope allocations exceed funded credit")
    except (KeyError, TypeError, AttributeError) as exc:
        raise EnvelopeError(f"envelope state does not check out: {exc!r}") from None


def _counts(value, names: tuple[str, ...]) -> bool:
    """Persisted token evidence: absent, or exactly these non-negative integer counters."""
    return value is None or (isinstance(value, dict) and set(value) == set(names) and all(
        isinstance(value[n], int) and not isinstance(value[n], bool) and value[n] >= 0 for n in names))


def _funded(s: dict) -> float:
    return sum(f["credit"] for f in s["funding"])


def _purchase_costs(s: dict) -> float:
    return sum(f["fees"] + f["taxes"] for f in s["funding"])


def _overdraw(s: dict) -> float:
    return sum(r["slots"] * r["max_charge"] for r in s["routes"].values())


def _settled(s: dict, scope: str | None = None) -> float:
    return sum(r["billed"] for r in s["requests"].values()
               if r["state"] != "unresolved" and (scope is None or r["scope"] == scope))


def _unresolved(s: dict, scope: str | None = None) -> float:
    return sum(r["bound"] for r in s["requests"].values()
               if r["state"] == "unresolved" and (scope is None or r["scope"] == scope))


def _require_bound(s: dict) -> None:
    total = _funded(s) + _purchase_costs(s) + _overdraw(s)
    if total > s["envelope_usd"] + _EPSILON:
        raise Refused(f"funded credit + fees + taxes + maximum overdraw ${total:.2f} exceeds the "
                      f"${s['envelope_usd']:.2f} envelope")


def _require_allocations(s: dict) -> None:
    allocated = sum(v["allocation"] for v in s["scopes"].values())
    if allocated > _funded(s) + _EPSILON:
        raise Refused(f"scope allocations ${allocated:.2f} exceed funded credit ${_funded(s):.2f}")


def _job_spend(s: dict, job: str) -> float:
    return sum(r["bound"] if r["state"] == "unresolved" else r["billed"] for r in s["requests"].values()
               if r["job"] == job)


def _require_reservations_fit(s: dict) -> None:
    """Open reservations are full holds: each counts at the larger of its amount and its job's spend
    since the hold opened (its baseline, spent under an earlier hold, counts as other spend)."""
    open_jobs = {j for j, r in s["reservations"].items() if r["open"]}
    spent_by = {j: 0.0 for j in open_jobs}
    outside = 0.0
    for r in s["requests"].values():
        charge = r["bound"] if r["state"] == "unresolved" else r["billed"]
        if r["job"] in spent_by:
            spent_by[r["job"]] += charge
        else:
            outside += charge
    for j in open_jobs:
        baseline = s["reservations"][j]["baseline"]
        spent_by[j] -= baseline
        outside += baseline
    held = sum(max(s["reservations"][j]["amount"], spent_by[j]) for j in open_jobs)
    if outside + held > _funded(s) + _EPSILON:
        raise Refused(f"reservations ${held:.2f} and other spend ${outside:.2f} exceed funded credit "
                      f"${_funded(s):.2f}")


def _require_trusted(s: dict, now: float) -> None:
    for name, scope in s["scopes"].items():
        if scope["fault"]:
            raise Refused(f"scope {name} is faulted: {scope['fault']}")
        if scope["excess"] > 0:
            raise Refused(f"scope {name}: the provider billed ${scope['excess']:.4f} the ledger does not account for")
        if scope["snapshot"] is None or now - scope["snapshot"]["at"] > SNAPSHOT_SECONDS:
            raise Refused(f"scope {name}: no reconciliation in the last {SNAPSHOT_SECONDS} s")


def _admission_problem(s: dict, route: str, job: str, attempt: str, now: float) -> str | None:
    if attempt in s["refusals"]:
        return f"attempt already refused: {s['refusals'][attempt]}"
    r = s["routes"].get(route)
    if r is None:
        return f"no route {route}"
    held = s["reservations"].get(job)
    if held is None or not held["open"]:
        return f"job {job} holds no open reservation"
    if r["stage"] == "probe" and job != PROBE:
        return f"route {route} is not validated; only probes may use it"
    if job == PROBE:
        spent = sum(q["bound"] if q["state"] == "unresolved" else q["billed"]
                    for q in s["requests"].values() if q["job"] == PROBE)
        if spent + r["max_charge"] > held["amount"] + _EPSILON:
            return f"the ${held['amount']:.2f} probe reservation is spent"
    scope, entry = r["scope"], s["scopes"][r["scope"]]
    for name, other in s["scopes"].items():  # an anomaly on any key stops all spending
        if other["fault"]:
            return f"scope {name} is faulted: {other['fault']}"
        if other["excess"] > 0:
            return f"scope {name}: the provider billed ${other['excess']:.4f} the ledger does not account for"
    if entry["snapshot"] is None or now - entry["snapshot"]["at"] > SNAPSHOT_SECONDS:
        return f"scope {scope}: no reconciliation in the last {SNAPSHOT_SECONDS} s"
    if sum(q["route"] == route and q["state"] == "unresolved" for q in s["requests"].values()) >= r["slots"]:
        return f"route {route}: all {r['slots']} slots hold unresolved requests"
    exposure = _settled(s, scope) + _unresolved(s, scope) + r["max_charge"]
    if exposure > entry["allocation"] + _EPSILON:
        return f"scope {scope}: ${exposure:.4f} with this request exceeds its ${entry['allocation']:.2f} allocation"
    if _settled(s) + _unresolved(s) + r["max_charge"] > _funded(s) + _EPSILON:
        return "the request would exceed funded credit"
    return None
