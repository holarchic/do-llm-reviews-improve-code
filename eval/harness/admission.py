"""Admission checks: the observed evidence that admits a routed path into B.

A route (provider, model) is admitted by the operator (`spend validate-route`) only after its checks
pass, per CLI where a check is per CLI. Each check returns a `Result`: pass, fail or inconclusive,
with its evidence and what it is bound to (route version, key, endpoint, CLI version, price table),
and inconclusive never admits. Every paid request goes through the active envelope's proxy.

- `cache_and_served`: one session; cache hits are billed at the cache rate (the saving the provider
  gave, against the saving its rates promise), and every request was served by whom the route says.
- `metering` (a metered route): over a workload with cached input and reasoning, in a DeepSeek peak
  window, the account balance's fall agrees with the metered total within the balance's precision.
  It shows agreement for the observed workload, nothing more; reconciliation keeps checking.
- `exhaustion` (once per provider account, in its own envelope): the provider, not the envelope,
  stops spending at the account's balance, and in-flight requests overdraw it by at most the
  reserved overdraw (Σ slots × max charge).

The clock, sleeping, sessions, balance readings and raw requests are injected, so tests drive them.
"""

import json
import math
import secrets
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PASS, FAIL, INCONCLUSIVE = "pass", "fail", "inconclusive"

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


def _require_window(utc: datetime) -> None:
    open_ = peak_window(utc)
    if open_ is None:
        raise Refused(f"{utc.date()} is past the holiday list's coverage ({COVERED_UNTIL})")
    if not open_ or window_left(utc) < MIN_WINDOW_LEFT:
        raise Refused(f"{utc:%Y-%m-%d %H:%M} UTC is not inside a DeepSeek peak window with "
                      f"{MIN_WINDOW_LEFT} left")


# ---- deps


@dataclass
class Deps:
    """What a check talks to. `session(attempt_dir)` runs one CLI session and returns it; `requests(attempt)`
    the envelope's requests for an attempt; `settle()` one settlement round; `balance()` the scope's
    reading and its raw string; `now()` and `sleep(seconds)` the clock."""
    session: object = None
    requests: object = None
    settle: object = None
    balance: object = None
    scope_unresolved: object = None
    now: object = None
    sleep: object = None
    send: object = None
    remaining: object = None


def new_attempt() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)


def _settled(deps: Deps, attempt: str, limit: float = 600) -> list[dict]:
    """The attempt's requests once settlement has had its chance (bounded)."""
    waited = 0.0
    while True:
        deps.settle()
        requests = deps.requests(attempt)
        if all(r["state"] != "unresolved" for r in requests) or waited >= limit:
            return requests
        deps.sleep(30)
        waited += 30


# ---- cache and served


def cache_and_served(route, cli: str, price, attempt: str, attempt_dir: Path, deps: Deps) -> list[Result]:
    """One session; then the two checks over its own requests."""
    session = deps.session(attempt_dir)
    requests = _settled(deps, _attempt_key(attempt_dir))
    base = {"requests": [r["request"] for r in requests], "session": session.terminal}
    problem = _incomplete(session, requests)
    if problem:
        return [Result(name, route.name, cli, attempt, INCONCLUSIVE, problem, base) for name in ("cache", "served")]
    return [_cache(route, cli, price, attempt, requests, base), _served(route, cli, attempt, requests, base)]


def _attempt_key(attempt_dir: Path) -> str:
    return str(attempt_dir.resolve())  # the routing context names an attempt by its resolved out dir


def _incomplete(session, requests: list[dict]) -> str | None:
    if session.terminal != "completed":
        return f"the session did not complete: {session.reason}"
    if not requests:
        return "no request of this attempt reached the proxy"
    unresolved = [r["request"] for r in requests if r["state"] == "unresolved"]
    return f"unresolved requests: {unresolved}" if unresolved else None


def _tokens(request: dict) -> dict | None:
    """The provider's own counts: prompt (cached included), completion, cached."""
    if request["billed_tokens"] is not None:
        return request["billed_tokens"]
    metered = request["metered_tokens"]
    if metered is not None:
        return {"prompt": metered["fresh"] + metered["cached"], "completion": metered["output"],
                "cached": metered["cached"]}
    return None


def _cache(route, cli, price, attempt, requests, base) -> Result:
    counts = [_tokens(r) for r in requests if r["state"] == "billed"]
    if not counts or any(c is None for c in counts):
        return Result("cache", route.name, cli, attempt, INCONCLUSIVE, "a request has no provider token counts", base)
    rates = price.rates
    prompt, completion, cached = (sum(c[k] for c in counts) for k in ("prompt", "completion", "cached"))
    billed = sum(r["billed"] for r in requests)
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


def _served(route, cli, attempt, requests, base) -> Result:
    expected = SERVED.get(route.provider, route.model)
    seen = [r["served"] for r in requests]
    wrong = [r["request"] for r in requests if r["served"] != [expected]]
    verdict = FAIL if wrong else PASS
    reason = f"served by {expected}" if not wrong else f"requests not served by exactly {expected}: {wrong}"
    return Result("served", route.name, cli, attempt, verdict, reason, {**base, "served": seen, "expected": expected})


# ---- metering


def tolerance(raw_balance: str) -> float:
    """Two units of the balance's last decimal place, never below a cent."""
    decimals = len(raw_balance.split(".", 1)[1]) if "." in raw_balance else 0
    return max(2 * 10 ** -decimals, TOLERANCE_FLOOR)


def _quiet_balance(deps: Deps, readings: list) -> tuple[float, str] | None:
    """Poll until the balance has been unchanged for QUIET_SECONDS; None past SETTLE_LIMIT_SECONDS."""
    waited = 0.0
    last = deps.balance()
    readings.append((deps.now().isoformat(), last[1]))
    unchanged = 0.0
    while unchanged < QUIET_SECONDS:
        if waited >= SETTLE_LIMIT_SECONDS:
            return None
        deps.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
        current = deps.balance()
        readings.append((deps.now().isoformat(), current[1]))
        unchanged = unchanged + POLL_SECONDS if current[1] == last[1] else 0.0
        last = current
    return last


def reasoning_tokens(cli: str, session) -> int:
    """Codex reports its reasoning tokens; Claude Code's transcript keeps the thinking text."""
    if cli == "codex":
        value = session.usage.get("reasoning_output_tokens") if isinstance(session.usage, dict) else None
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
    chars = 0
    try:
        lines = session.transcript.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = event.get("message") if isinstance(event, dict) else None
        for block in (message.get("content") or []) if isinstance(message, dict) else []:
            if isinstance(block, dict) and block.get("type") == "thinking" and isinstance(block.get("thinking"), str):
                chars += len(block["thinking"])
    return chars // REASONING_CHARS_PER_TOKEN


def metering(route, cli: str, price, attempt: str, attempt_root: Path, deps: Deps) -> Result:
    """Refuses (nothing spent) outside a peak window or with earlier requests unresolved."""
    _require_window(deps.now())
    if deps.scope_unresolved():
        raise Refused("the scope has unresolved requests: their debits could land in this attempt's measure")
    readings: list = []

    def result(verdict, reason, **evidence):
        return Result("metering", route.name, cli, attempt, verdict, reason, {"balances": readings, **evidence})

    opening = _quiet_balance(deps, readings)
    if opening is None:
        return result(INCONCLUSIVE, "the balance never held still before the run: the account isn't isolated")
    tol = tolerance(opening[1])
    rates = price.rates
    metered = cached_usd = reasoning_usd = 0.0
    keys = []

    def workload_done() -> bool:
        return (metered >= WORKLOAD_FACTOR * tol and cached_usd >= COMPONENT_FACTOR * tol
                and reasoning_usd >= COMPONENT_FACTOR * tol)

    while not workload_done():
        if len(keys) >= MAX_SESSIONS:
            return result(INCONCLUSIVE, f"{MAX_SESSIONS} sessions without the workload's components",
                          metered=metered, cached_usd=cached_usd, reasoning_usd=reasoning_usd)
        now = deps.now()
        if not peak_window(now) or window_left(now) < MIN_WINDOW_LEFT:
            return result(INCONCLUSIVE, "the peak window closed before the workload was complete", sessions=len(keys))
        attempt_dir = attempt_root / f"session-{len(keys)}"
        session = deps.session(attempt_dir)
        keys.append(_attempt_key(attempt_dir))
        for request in _settled(deps, keys[-1]):
            if request["metered"] is not None:
                metered += request["metered"]
                cached_usd += (request["metered_tokens"] or {}).get("cached", 0) * rates.cache_read / 1e6
        reasoning_usd += reasoning_tokens(cli, session) * rates.output / 1e6
    requests = [r for key in keys for r in deps.requests(key)]
    outside = [r["request"] for r in requests
               if not peak_window(datetime.fromtimestamp(r["admitted_at"], timezone.utc))]
    unresolved = [r["request"] for r in requests if r["state"] == "unresolved"]
    evidence = {"requests": [r["request"] for r in requests], "metered": metered, "tolerance": tol,
                "cached_usd": cached_usd, "reasoning_usd": reasoning_usd}
    if outside:
        return result(INCONCLUSIVE, f"requests admitted outside the peak window: {outside}", **evidence)
    if unresolved:
        return result(INCONCLUSIVE, f"unresolved requests: {unresolved}", **evidence)
    closing = _quiet_balance(deps, readings)
    if closing is None:
        return result(INCONCLUSIVE, "the balance was still moving 90 minutes after the run", **evidence)
    fall = closing[0] - opening[0]  # a reading is −balance: it rises as the account is billed
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
    def provider_refusal(self) -> bool:
        return self.status in (402, 429) and not self.from_proxy

    @property
    def request_size(self) -> bool:
        """OpenRouter refusing one request as too dear for what is left, while credit remains."""
        return "weight_exceeds_budget" in json.dumps(self.body or {})


def exhaustion(route, attempt: str, *, funding: float, request_cost: float, overdraw_bound: float,
               deps: Deps, fees: float = 0.0, taxes: float = 0.0, max_requests: int = 300, batch: int = 4) -> Result:
    """Drain the account through the proxy until the provider refuses, then attribute the refusal."""
    replies: list[Reply] = []

    def result(verdict, reason, **evidence):
        return Result("exhaustion", route.name, "-", attempt, verdict, reason,
                      {"funding": funding, "fees": fees, "taxes": taxes, "request_cost": request_cost,
                       "overdraw_bound": overdraw_bound,
                       "statuses": [r.status for r in replies], **evidence})

    while not any(r.provider_refusal for r in replies):
        if len(replies) >= max_requests:
            return result(INCONCLUSIVE, f"{max_requests} requests without the provider refusing")
        replies.extend(deps.send(batch))
        deps.settle()  # settled requests free their slots for the next batch
        if any(r.from_proxy for r in replies):
            return result(INCONCLUSIVE, "the envelope refused before the provider did")
    _settled(deps, attempt, limit=SETTLE_LIMIT_SECONDS)
    remaining = deps.remaining()
    confirm = deps.send(1)[0]
    replies.append(confirm)
    refusals = [r.body for r in replies if r.provider_refusal]
    evidence = {"remaining": remaining, "confirmation": confirm.status, "refusals": refusals[:5]}
    if not confirm.provider_refusal:
        return result(INCONCLUSIVE, "the refusal cleared: a later request was served", **evidence)
    if remaining >= request_cost:
        reason = "refused per request with credit left" if confirm.request_size else "refused with credit left"
        return result(INCONCLUSIVE, reason, **evidence)
    overdraw = max(0.0, -remaining)
    if overdraw <= overdraw_bound:
        return result(PASS, f"the provider stopped at its balance; overdraw ${overdraw:.4f}", overdraw=overdraw,
                      **evidence)
    return result(FAIL, f"overdraw ${overdraw:.4f} exceeds the reserved ${overdraw_bound:.4f}", overdraw=overdraw,
                  **evidence)


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


def status(root: Path, bindings: list[Binding]) -> list[dict]:
    """For each B route and CLI as configured now (`bindings`), the latest attempt of each check and
    whether it still applies. A route's own checks apply only to exactly its current binding; an
    exhaustion result applies to every route of the same provider account."""
    records = []
    for path in sorted(root.glob("*/*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            records.append((record["result"], record["binding"], record["status"]))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    rows = []
    for binding in bindings:
        now = asdict(binding)
        for check in ("cache", "served", "metering", "exhaustion"):
            mine = [(r, b, st) for r, b, st in records if r["check"] == check
                    and (_same_account(b, now) if check == "exhaustion" else (r["route"], r["cli"]) == (binding.route, binding.cli))]
            if not mine:
                rows.append({"route": binding.route, "cli": binding.cli, "check": check, "verdict": None})
                continue
            result, recorded, state = max(mine, key=lambda m: m[0]["attempt"])
            current = check == "exhaustion" or recorded == now
            rows.append({"route": binding.route, "cli": binding.cli, "check": check, "attempt": result["attempt"],
                         "status": state, "verdict": result["verdict"], "stale": not current})
    return rows


def _same_account(recorded: dict, now: dict) -> bool:
    """Exhaustion is the account's: it holds across keys and routes of the same named provider account."""
    return (recorded.get("account") is not None and recorded["account"] == now["account"]
            and recorded["route"].split("--", 1)[0] == now["route"].split("--", 1)[0])


def budget(root: Path, total: float = 150.0) -> float:
    """What is left of the total for B's envelope after the exhaustion runs' purchases, fees and taxes."""
    spent = 0.0
    for path in root.glob("*/exhaustion-*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        evidence = record["result"]["evidence"]
        spent += sum(float(evidence.get(k, 0.0)) for k in ("funding", "fees", "taxes"))
    if not math.isfinite(spent) or spent < 0:
        raise ValueError(f"exhaustion spend {spent!r} is not a sum of charges")
    return total - spent
