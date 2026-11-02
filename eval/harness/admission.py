"""Admission checks: the observed evidence that admits a routed path into B.

A route (provider, model) is admitted by the operator (`spend validate-route`) only after its checks
pass, per CLI where a check is per CLI. Each check returns a `Result`: pass, fail or inconclusive,
with its evidence, and inconclusive never admits. Records bind each result to what it admits (route
version, key, endpoint, CLI version, price table). Every paid request goes through an envelope's proxy.

- `cache_and_served`: one session; cache hits are billed at the cache rate (the saving the provider
  gave, against the saving its rates promise), and every request was served by whom the route says.
- `metering` (a metered route): over a workload with cached input and reasoning, in a DeepSeek peak
  window, the account balance's fall agrees with the metered total within the balance's precision.
  It shows agreement for the observed workload, nothing more; reconciliation keeps checking.
- `exhaustion` (once per provider account, in its own envelope): the provider, not the envelope,
  stops spending at the account's balance, and in-flight requests overdraw it by at most the
  reserved overdraw (Σ slots × max charge).

Each check takes its own typed dependencies (the clock, sessions, balances, raw requests), so tests
drive them.
"""

import json
import math
import secrets
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .envelope import ScopeAccount

PASS, FAIL, INCONCLUSIVE = "pass", "fail", "inconclusive"
CHECKS = ("cache", "served", "metering", "exhaustion")

# DeepSeek's peak hours, UTC, Monday to Friday, except Chinese public holidays (off-peak by its terms).
PEAK_HOURS = ((1, 4), (6, 10))
# Chinese public holidays from 2026-11-01 to the end of the coverage: none (the next is 2027-01-01,
# whose year's schedule the State Council has not yet published). A date past coverage is refused.
HOLIDAYS: frozenset[date] = frozenset()
COVERED_UNTIL = date(2026, 12, 31)
MIN_WINDOW_LEFT = timedelta(minutes=30)

POLL_SECONDS = 300
QUIET_SECONDS = 1800  # a balance unchanged this long has its debits visible
SETTLE_LIMIT_SECONDS = 5400
TOLERANCE_FLOOR = 0.01
WORKLOAD_FACTOR, COMPONENT_FACTOR = 50, 3
MAX_SESSIONS = 20
CACHE_SAVING = (0.80, 1.05)  # the billed saving's share of the saving the route's rates promise
REASONING_CHARS_PER_TOKEN = 6  # conservative: Claude Code's transcript keeps thinking text, not its count
SERVED = {"openrouter": "Z.AI"}  # a metered route's served value must be its own model
RATE_LIMIT_BACKOFF = 30
CONFIRM_PAUSE = 120  # before confirming: a transient refusal (spend still settling) has cleared by then


@dataclass(frozen=True)
class RouteRef:
    """A route as the checks see it: its name in the envelope, and the provider and model it serves."""
    name: str
    provider: str
    model: str


@dataclass(frozen=True)
class Binding:
    """What a result admits: evidence for one configuration, stale once any of it changes."""
    route: str
    route_version: str
    key_fingerprint: str
    endpoint: str
    cli: str
    cli_version: str
    prices_sha: str
    account: str | None = None


@dataclass(frozen=True)
class Result:
    check: str
    route: str
    cli: str
    attempt: str
    verdict: str
    reason: str
    evidence: dict = field(default_factory=dict)


class Refused(Exception):
    """The check would not start: nothing was spent."""


class Stopped(Exception):
    """The check stopped after spending began (a session's files held a key): its record stays running."""


# ---- the peak window


def peak_window(utc: datetime) -> bool | None:
    """Whether DeepSeek charges peak rates at this moment; None past the holiday list's coverage."""
    if utc.date() > COVERED_UNTIL:
        return None
    if utc.weekday() >= 5 or utc.date() in HOLIDAYS:
        return False
    return any(start <= utc.hour < end for start, end in PEAK_HOURS)


def window_left(utc: datetime) -> timedelta:
    """Time left in the current peak window (zero outside one)."""
    if not peak_window(utc):
        return timedelta(0)
    end = next(end for start, end in PEAK_HOURS if start <= utc.hour < end)
    return utc.replace(hour=end, minute=0, second=0, microsecond=0) - utc


def in_window(utc: datetime) -> bool:
    """Inside a peak window with at least MIN_WINDOW_LEFT of it to go."""
    return bool(peak_window(utc)) and window_left(utc) >= MIN_WINDOW_LEFT


def require_window(utc: datetime) -> None:
    if peak_window(utc) is None:
        raise Refused(f"{utc.date()} is past the holiday list's coverage ({COVERED_UNTIL})")
    if not in_window(utc):
        raise Refused(f"{utc:%Y-%m-%d %H:%M} UTC is not inside a DeepSeek peak window with {MIN_WINDOW_LEFT} left")


# ---- the request views a check reads


@dataclass(frozen=True)
class Request:
    """One of an attempt's requests, as the envelope recorded it."""
    id: str
    state: str  # unresolved | billed | not_billed
    billed: float | None
    metered: float | None
    metered_tokens: dict | None
    billed_tokens: dict | None
    served: tuple[str, ...]
    admitted_at: float
    refused: dict | None

    @classmethod
    def of(cls, record: dict) -> "Request":
        return cls(record["request"], record["state"], record["billed"], record["metered"], record["metered_tokens"],
                   record["billed_tokens"], tuple(record["served"]), record["admitted_at"],
                   record["refused_by_provider"])


@dataclass(frozen=True)
class SessionEvidence:
    """What a check needs of one CLI session: how it ended, and the reasoning it evidenced (Codex's
    reported reasoning tokens; Claude Code's thinking text, counted conservatively)."""
    terminal: str
    reason: str
    reasoning_tokens: int


@dataclass(frozen=True)
class BalanceReading:
    """A metered scope's usage reading (−balance) and the balance exactly as the provider wrote it."""
    usage: float
    raw: str


@dataclass(frozen=True)
class SessionDeps:
    """Run one CLI session into a dir; an attempt's requests by its key; one settlement round; sleep."""
    session: Callable[[Path], SessionEvidence]
    requests: Callable[[str], list[Request]]
    settle: Callable[[], None]
    sleep: Callable[[float], None]


@dataclass(frozen=True)
class MeteringDeps(SessionDeps):
    """Also: the scope's balance reading; the ledger's account of the scope; the clock."""
    balance: Callable[[], BalanceReading]
    scope: Callable[[], ScopeAccount]
    now: Callable[[], datetime]


@dataclass(frozen=True)
class ExhaustionDeps:
    """Send n requests at once through the proxy; the account's remaining credit; an attempt's requests;
    one settlement round; the clock; whether the run may go on (the DeepSeek window, where it applies)."""
    send: Callable[[int], list["Reply"]]
    remaining: Callable[[], float]
    requests: Callable[[str], list[Request]]
    settle: Callable[[], None]
    sleep: Callable[[float], None]
    now: Callable[[], datetime]
    may_continue: Callable[[datetime], bool]


def new_attempt() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)


def attempt_key(attempt_dir: Path) -> str:
    """How the routing context names a session's attempt: its resolved out dir."""
    return str(attempt_dir.resolve())


def _settle_all(settle, requests, sleep, attempt: str, limit: float) -> tuple[list[Request], bool]:
    """Settlement rounds until the attempt has nothing unresolved, or the limit; and whether it got there."""
    waited = 0.0
    while True:
        settle()
        found = requests(attempt)
        if all(r.state != "unresolved" for r in found):
            return found, True
        if waited >= limit:
            return found, False
        sleep(30)
        waited += 30


# ---- cache and served


def cache_and_served(route: RouteRef, cli: str, price, attempt: str, attempt_dir: Path,
                     deps: SessionDeps) -> list[Result]:
    """One session; then the two checks over its own requests."""
    session = deps.session(attempt_dir)
    requests, _ = _settle_all(deps.settle, deps.requests, deps.sleep, attempt_key(attempt_dir), 600)
    base = {"requests": [r.id for r in requests], "session": session.terminal}
    problem = _incomplete(session, requests)
    if problem:
        return [Result(name, route.name, cli, attempt, INCONCLUSIVE, problem, base) for name in ("cache", "served")]
    return [_cache(route, cli, price, attempt, requests, base), _served(route, cli, attempt, requests, base)]


def _incomplete(session: SessionEvidence, requests: list[Request]) -> str | None:
    if session.terminal != "completed":
        return f"the session did not complete: {session.reason}"
    if not requests:
        return "no request of this attempt reached the proxy"
    unresolved = [r.id for r in requests if r.state == "unresolved"]
    return f"unresolved requests: {unresolved}" if unresolved else None


def _tokens(request: Request) -> dict | None:
    """The provider's own counts: prompt (cached included), completion, cached."""
    if request.billed_tokens is not None:
        return request.billed_tokens
    if request.metered_tokens is not None:
        m = request.metered_tokens
        return {"prompt": m["fresh"] + m["cached"], "completion": m["output"], "cached": m["cached"]}
    return None


def _cache(route, cli, price, attempt, requests: list[Request], base) -> Result:
    billed_requests = [r for r in requests if r.state == "billed"]
    counts = [_tokens(r) for r in billed_requests]
    if not counts or any(c is None for c in counts):
        return Result("cache", route.name, cli, attempt, INCONCLUSIVE, "a request has no provider token counts", base)
    rates = price.rates
    prompt, completion, cached = (sum(c[k] for c in counts) for k in ("prompt", "completion", "cached"))
    billed = sum(r.billed for r in billed_requests)
    uncached = (prompt * rates.input + completion * rates.output) / 1e6
    expected = cached * (rates.input - rates.cache_read) / 1e6
    evidence = {**base, "prompt": prompt, "completion": completion, "cached": cached, "billed": billed,
                "uncached": uncached, "expected_saving": expected, "billed_saving": uncached - billed}
    if cached == 0 or expected <= 0:
        return Result("cache", route.name, cli, attempt, FAIL, "no cache hits", evidence)
    share = (uncached - billed) / expected
    low, high = CACHE_SAVING
    verdict = PASS if low <= share <= high else FAIL
    return Result("cache", route.name, cli, attempt, verdict, f"billed saving {share:.0%} of the rates' saving",
                  {**evidence, "share": share})


def _served(route, cli, attempt, requests: list[Request], base) -> Result:
    expected = SERVED.get(route.provider, route.model)
    wrong = [r.id for r in requests if r.served != (expected,)]
    verdict = FAIL if wrong else PASS
    reason = f"served by {expected}" if not wrong else f"requests not served by exactly {expected}: {wrong}"
    return Result("served", route.name, cli, attempt, verdict, reason,
                  {**base, "served": [list(r.served) for r in requests], "expected": expected})


# ---- metering


def tolerance(raw_balance: str) -> float:
    """Two units of the balance's last decimal place, never below a cent."""
    decimals = len(raw_balance.split(".", 1)[1]) if "." in raw_balance else 0
    return max(2 * 10 ** -decimals, TOLERANCE_FLOOR)


def _quiet(reader, now, sleep, readings: list, key=lambda r: r):
    """Poll `reader` until its value has held for QUIET_SECONDS; None past SETTLE_LIMIT_SECONDS."""
    waited = unchanged = 0.0
    last = reader()
    readings.append((now().isoformat(), key(last)))
    while unchanged < QUIET_SECONDS:
        if waited >= SETTLE_LIMIT_SECONDS:
            return None
        sleep(POLL_SECONDS)
        waited += POLL_SECONDS
        current = reader()
        readings.append((now().isoformat(), key(current)))
        unchanged = unchanged + POLL_SECONDS if key(current) == key(last) else 0.0
        last = current
    return last


def metering(route: RouteRef, cli: str, price, attempt: str, attempt_root: Path, deps: MeteringDeps) -> Result:
    """Refuses (nothing spent) outside a peak window or with earlier requests unresolved."""
    require_window(deps.now())
    account = deps.scope()
    if account.unresolved:
        raise Refused("the scope has unresolved requests: their debits could land in this attempt's measure")
    if any(not peak_window(datetime.fromtimestamp(t, timezone.utc)) for t in account.admitted_at):
        raise Refused("the scope has requests admitted off-peak: they settled at peak prices above what DeepSeek "
                      "billed, so no baseline can match; run metering first on a fresh scope, and keep every "
                      "request before it in a peak window")
    readings: list = []

    def result(verdict, reason, **evidence):
        return Result("metering", route.name, cli, attempt, verdict, reason, {"balances": readings, **evidence})

    opening = _quiet(deps.balance, deps.now, deps.sleep, readings, key=lambda r: r.raw)
    if opening is None:
        return result(INCONCLUSIVE, "the balance never held still before the run: the account isn't isolated")
    tol = tolerance(opening.raw)
    # Earlier billing is accounted for: the account's fall since the scope opened is what the ledger settled.
    account = deps.scope()
    earlier_gap = (opening.usage - account.opening_usage) - account.settled
    if abs(earlier_gap) > tol:
        return result(INCONCLUSIVE, f"earlier billing isn't accounted for (gap ${earlier_gap:.4f}): the baseline "
                      "can't be trusted", earlier_gap=earlier_gap, tolerance=tol)
    rates = price.rates
    metered = cached_usd = reasoning_usd = 0.0
    keys: list[str] = []

    def workload_done() -> bool:
        return (metered >= WORKLOAD_FACTOR * tol and cached_usd >= COMPONENT_FACTOR * tol
                and reasoning_usd >= COMPONENT_FACTOR * tol)

    while not workload_done():
        if len(keys) >= MAX_SESSIONS:
            return result(INCONCLUSIVE, f"{MAX_SESSIONS} sessions without the workload's components",
                          metered=metered, cached_usd=cached_usd, reasoning_usd=reasoning_usd)
        if not in_window(deps.now()):
            return result(INCONCLUSIVE, "the peak window closed before the workload was complete", sessions=len(keys))
        attempt_dir = attempt_root / f"session-{len(keys)}"
        session = deps.session(attempt_dir)
        keys.append(attempt_key(attempt_dir))
        for request in _settle_all(deps.settle, deps.requests, deps.sleep, keys[-1], 600)[0]:
            if request.metered is not None:
                metered += request.metered
                cached_usd += (request.metered_tokens or {}).get("cached", 0) * rates.cache_read / 1e6
        reasoning_usd += session.reasoning_tokens * rates.output / 1e6
    requests = [r for key in keys for r in deps.requests(key)]
    outside = [r.id for r in requests if not peak_window(datetime.fromtimestamp(r.admitted_at, timezone.utc))]
    unresolved = [r.id for r in requests if r.state == "unresolved"]
    evidence = {"requests": [r.id for r in requests], "metered": metered, "tolerance": tol,
                "cached_usd": cached_usd, "reasoning_usd": reasoning_usd, "earlier_gap": earlier_gap}
    if outside:
        return result(INCONCLUSIVE, f"requests admitted outside the peak window: {outside}", **evidence)
    if unresolved:
        return result(INCONCLUSIVE, f"unresolved requests: {unresolved}", **evidence)
    closing = _quiet(deps.balance, deps.now, deps.sleep, readings, key=lambda r: r.raw)
    if closing is None:
        return result(INCONCLUSIVE, "the balance was still moving 90 minutes after the run", **evidence)
    fall = closing.usage - opening.usage  # a reading is −balance: it rises as the account is billed
    difference = fall - metered
    evidence.update(fall=fall, difference=difference)
    if abs(difference) <= tol:
        return result(PASS, "agreement for the observed workload within tolerance", **evidence)
    if difference > tol:
        return result(FAIL, "the account was billed more than metered", **evidence)
    return result(INCONCLUSIVE, "billed less than metered: debits may still be landing", **evidence)


# ---- exhaustion


@dataclass(frozen=True)
class Reply:
    status: int | None
    body: dict | None

    @property
    def from_proxy(self) -> bool:
        error = self.body.get("error") if isinstance(self.body, dict) else None
        return isinstance(error, dict) and error.get("type") == "proxy"

    @property
    def rate_limited(self) -> bool:
        return self.status == 429 and not self.from_proxy

    @property
    def request_size(self) -> bool:
        """OpenRouter refusing one request as too dear for what is left, while credit remains."""
        return "weight_exceeds_budget" in json.dumps(self.body or {})

    def insufficient_balance(self, provider: str) -> bool:
        """The one refusal that evidences an empty account, by provider; anything else, or an unknown
        attribution, counts for nothing. DeepSeek: a 402 with a JSON error (its only 402 is insufficient
        balance). OpenRouter: a 402 whose `error.metadata.limit_source` is the account's credits, and not
        a request-size refusal; key limits and in-flight budgets name other sources."""
        error = self.body.get("error") if isinstance(self.body, dict) else None
        if self.status != 402 or self.from_proxy or not isinstance(error, (dict, str)):
            return False
        if provider == "deepseek":
            return True
        metadata = error.get("metadata") if isinstance(error, dict) else None
        source = metadata.get("limit_source") if isinstance(metadata, dict) else None
        return provider == "openrouter" and source == "openrouter_credits" and not self.request_size


def exhaustion(route: RouteRef, attempt: str, *, funding: float, overdraw_bound: float, deps: ExhaustionDeps,
               max_requests: int, deadline: datetime, batch: int = 4) -> Result:
    """Drain the account through the proxy until the provider refuses for insufficient balance, then
    confirm it with every request settled and the account's final balance read once it holds still.
    Refuses (nothing spent) when the account isn't funded to start with."""
    opening = deps.remaining()
    if opening <= 0:
        raise Refused(f"the account holds ${opening:.4f}: nothing to drain, so nothing would be shown")
    replies: list[Reply] = []
    empty = lambda reply: reply.insufficient_balance(route.provider)  # noqa: E731

    def result(verdict, reason, **evidence):
        return Result("exhaustion", route.name, "any", attempt, verdict, reason,
                      {"funding": funding, "opening_remaining": opening, "overdraw_bound": overdraw_bound,
                       "statuses": [r.status for r in replies], **evidence})

    def blocked(reserve: int) -> str | None:
        """Why no further request may start: one guard before every send, the confirmation included."""
        now = deps.now()
        if len(replies) + reserve > max_requests:
            return f"the {max_requests}-request cap"
        if now >= deadline:
            return "the deadline"
        if not deps.may_continue(now):
            return "the run's window closing"
        return None

    while not any(empty(r) for r in replies):
        reason = blocked(1 + 1)  # one request, and the confirmation's
        if reason:
            return result(INCONCLUSIVE, f"stopped by {reason} before the provider refused for balance")
        sent = deps.send(min(batch, max_requests - len(replies) - 1))
        replies.extend(sent)
        if any(r.from_proxy for r in sent):
            return result(INCONCLUSIVE, "the envelope refused before the provider did")
        if any(r.rate_limited for r in sent):
            deps.sleep(RATE_LIMIT_BACKOFF)  # a rate limit says nothing of the balance: back off and go on
        _settle_all(deps.settle, deps.requests, deps.sleep, attempt, 600)  # free the slots for the next batch
    _settle_all(deps.settle, deps.requests, deps.sleep, attempt, 600)
    deps.sleep(CONFIRM_PAUSE)  # let a refusal that only reflects spend still settling clear
    reason = blocked(1)
    if reason:
        return result(INCONCLUSIVE, f"no confirmation: stopped by {reason}")
    confirm = deps.send(1)[0]
    replies.append(confirm)
    requests, settled = _settle_all(deps.settle, deps.requests, deps.sleep, attempt, SETTLE_LIMIT_SECONDS)
    evidence = {"confirmation": confirm.status,
                "refusals": [r.body for r in replies if r.status in (402, 429) and not r.from_proxy][:5]}
    if not settled:
        return result(INCONCLUSIVE, "requests still unresolved: the final spend isn't known", **evidence)
    if not empty(confirm):
        return result(INCONCLUSIVE, "the refusal didn't hold: the confirmation was not refused for balance",
                      **evidence)
    charges = [r.billed for r in requests if r.state == "billed" and r.billed]
    if not charges:
        return result(INCONCLUSIVE, "no request of this attempt was billed: it drained nothing", **evidence)
    readings: list = []
    final = _quiet(deps.remaining, deps.now, deps.sleep, readings)
    evidence["remaining_readings"] = readings
    if final is None:
        return result(INCONCLUSIVE, "the account's balance never held still after the run", **evidence)
    smallest = min(charges)
    evidence.update(remaining=final, smallest_charge=smallest, billed=sum(charges))
    if final >= smallest:
        return result(INCONCLUSIVE, "refused with credit left for a request", **evidence)
    overdraw = max(0.0, -final)
    evidence["overdraw"] = overdraw
    if overdraw <= overdraw_bound:
        return result(PASS, f"the provider stopped at its balance; overdraw ${overdraw:.4f}", **evidence)
    return result(FAIL, f"overdraw ${overdraw:.4f} exceeds the reserved ${overdraw_bound:.4f}", **evidence)


# ---- records


def record_path(root: Path, result: Result) -> Path:
    return root / result.route / f"{result.check}-{result.cli}-{result.attempt}.json"


def write(root: Path, result: Result, binding: Binding, status: str) -> Path:
    """An attempt's record: `running` before any spend, its verdict at the end; never overwritten
    by another attempt."""
    path = record_path(root, result)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.partial")
    tmp.write_text(json.dumps({"status": status, "result": asdict(result), "binding": asdict(binding)}, indent=1,
                              sort_keys=True, default=str), encoding="utf-8")
    tmp.replace(path)
    return path


@dataclass(frozen=True)
class Record:
    path: Path
    check: str
    route: str
    cli: str
    attempt: str
    status: str | None = None
    verdict: str | None = None
    binding: dict | None = None
    evidence: dict | None = None
    invalid: str | None = None  # why the file couldn't be read: it blocks reliance on any earlier attempt


_LEGAL = {"running": {"running"}, "done": {PASS, FAIL, INCONCLUSIVE}}
_EMPTY_BINDING = Binding("", "", "", "", "", "", "", None)


def _records(root: Path) -> list[Record]:
    """Every record, named by its path, parsed whole: a file that can't be read, or whose result,
    binding, route, status and verdict don't agree, is kept as invalid evidence."""
    found = []
    for path in sorted(root.glob("*/*.json")):
        check, _, rest = path.stem.partition("-")
        cli, _, attempt = rest.partition("-")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            result, raw_binding, state = record["result"], record["binding"], record["status"]
            if not isinstance(raw_binding, dict) or set(raw_binding) != set(asdict(_EMPTY_BINDING)):
                raise ValueError("the binding doesn't carry exactly its fields")
            binding = asdict(Binding(**raw_binding))
            if any(not isinstance(v, str) for k, v in binding.items() if k != "account") or \
                    not (binding["account"] is None or isinstance(binding["account"], str)):
                raise ValueError("a binding field has the wrong type")
            if (result["check"], result["cli"], result["attempt"], result["route"]) != (check, cli, attempt,
                                                                                       path.parent.name):
                raise ValueError("the record doesn't match its name")
            if binding["route"] != path.parent.name:
                raise ValueError("the binding names another route")
            if result["verdict"] not in _LEGAL.get(state, ()):
                raise ValueError(f"status {state!r} with verdict {result['verdict']!r}")
            if not isinstance(result.get("evidence"), dict):
                raise ValueError("the evidence is not a table")
            found.append(Record(path, check, path.parent.name, cli, attempt, state, result["verdict"], binding,
                                result["evidence"]))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            found.append(Record(path, check, path.parent.name, cli, attempt, invalid=f"{type(exc).__name__}: {exc}"))
    return found


def status(root: Path, bindings: list[Binding]) -> list[dict]:
    """For each B route and CLI as configured now (`bindings`), the latest attempt of each check and
    whether it still applies. A route's own checks apply only to exactly its current binding; an
    exhaustion result applies to every route of the same provider account. A damaged latest record
    reads as invalid, never falling back to an earlier attempt."""
    records = _records(root)
    rows = []
    for binding in bindings:
        now = asdict(binding)
        for check in CHECKS:
            if check == "exhaustion":
                mine = [r for r in records if r.check == check and (r.invalid or _same_account(r.binding, now))]
            else:
                mine = [r for r in records if r.check == check and (r.route, r.cli) == (binding.route, binding.cli)]
            if not mine:
                rows.append({"route": binding.route, "cli": binding.cli, "check": check, "verdict": None})
                continue
            latest = max(mine, key=lambda r: r.attempt)
            row = {"route": binding.route, "cli": binding.cli, "check": check, "attempt": latest.attempt}
            if latest.invalid:
                rows.append({**row, "verdict": "invalid", "reason": latest.invalid, "stale": True})
                continue
            current = check == "exhaustion" or latest.binding == now
            rows.append({**row, "status": latest.status, "verdict": latest.verdict, "stale": not current})
    return rows


def _origin(endpoint: str) -> str:
    from urllib.parse import urlsplit
    parts = urlsplit(endpoint)
    return f"{parts.scheme}://{parts.netloc}"


def _same_account(recorded: dict | None, now: dict) -> bool:
    """Exhaustion is the account's: it holds across keys, models and CLIs of the same named provider
    account at the same upstream, never at another."""
    return (recorded is not None and recorded["account"] is not None and recorded["account"] == now["account"]
            and recorded["route"].split("--", 1)[0] == now["route"].split("--", 1)[0]
            and _origin(recorded["endpoint"]) == _origin(now["endpoint"]))


def purchases(funding, fees, taxes) -> dict:
    """An exhaustion run's purchases, checked: written before any spend, so an interrupted run still counts."""
    amounts = {"funding": funding, "fees": fees, "taxes": taxes}
    for name, value in amounts.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise Refused(f"{name} must be a non-negative amount, not {value!r}")
    if funding <= 0:
        raise Refused("an exhaustion run needs the account's funding")
    return amounts


def budget(root: Path, total: float = 150.0) -> float:
    """What is left of the total for B's envelope after the exhaustion runs' purchases, fees and taxes.
    Every exhaustion record counts, running or done; one without its purchases refuses the sum."""
    spent = 0.0
    for record in _records(root):
        if record.check != "exhaustion":
            continue
        if record.invalid:
            raise Refused(f"{record.path} can't be read ({record.invalid}): the budget can't be summed")
        try:
            spent += sum(purchases(*(record.evidence[k] for k in ("funding", "fees", "taxes"))).values())
        except (KeyError, Refused) as exc:
            raise Refused(f"{record.path} has no valid purchases ({exc}): the budget can't be summed") from None
    return total - spent
