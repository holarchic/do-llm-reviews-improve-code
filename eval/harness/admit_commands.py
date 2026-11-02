"""The operator's interface to the admission checks (`python3 -m eval admit ACTION ...`).

`cache`, `metering` and `exhaustion` spend: each writes its attempt's record as `running` before the
first paid request (an exhaustion record carries its purchases from the start) and its verdict at
the end, under `eval/data/admission/`. Cache and metering run inside the admission experiment's
routing context (B's envelope, its probe reservation); exhaustion runs in its own envelope.
`status` and `budget` only read: status from a snapshot of B's envelope, with no proxy.
"""

import json
import math
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import admission, agents, audit, envelope, prices, providers, routing, settlers

ACTIONS = ("cache", "metering", "exhaustion", "status", "budget")
ROOT = Path(__file__).resolve().parents[1] / "data" / "admission"
# Three small files a session reads with its tools, then answers about: several requests on one
# growing prefix (cache hits), and a question that needs some reasoning.
FILES = {
    "orders.py": "def total(items):\n    return sum(price * qty for price, qty in items)\n",
    "shipping.py": "def shipping(total):\n    return 0 if total >= 50 else 4.99\n",
    "discount.py": "def discount(total, code):\n    return total * 0.9 if code == 'TEN' else total\n",
}
PROMPT = ("Read each of the three Python files in this directory with your file tools, one at a time. Then work "
          "out, showing your reasoning briefly, what a customer pays for two items at 12.50 and one at 30.00 "
          "with code TEN, and whether shipping applies before or after the discount here. Answer in one line.")
# Exhaustion: each request costs about this share of the account's funding, so the drain takes about
# fifty requests; its text is fresh randomness every time, so nothing is billed as a cache hit.
DRAIN_SHARE = 0.02
TOKENS_PER_WORD = 3  # a 6-hex-digit word: the estimate sizes the body, the provider's billing decides
DRAIN_DEADLINE = timedelta(hours=2)
DRAIN_OUTPUT = 16


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


class Usage(Exception):
    """Flags that don't make a valid command; nothing was spent."""


def add_arguments(parser) -> None:
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--route", help="provider--model, as in the envelope")
    parser.add_argument("--cli", choices=("claude", "codex"))
    parser.add_argument("--envelope", default="A3B-EXHAUST", help="exhaustion only: its own envelope")
    for flag in ("--funding", "--fees", "--taxes"):
        parser.add_argument(flag, type=float)


def run(args, out) -> int:
    """0 done, 2 bad flags, 3 refused before any spend."""
    try:
        if args.action == "budget":
            out(f"{admission.budget(ROOT):.2f}")
            return 0
        if args.action == "status":
            out(json.dumps(admission.status(ROOT, _current_bindings()), indent=1))
            return 0
        route = _route(args.route)
        if args.action == "exhaustion":
            return _exhaustion(args, route, out)
        if args.cli is None:
            raise Usage("needs --cli")
        return _session_check(args.action, route, args.cli, out)
    except Usage as exc:
        out(f"admit {args.action}: {exc}", error=True)
        return 2
    except (admission.Refused, routing.RoutingRequired, envelope.EnvelopeError, envelope.Refused,
            settlers.UsageUnavailable, settlers.NoSettler) as exc:
        out(f"admit {args.action}: refused: {exc}", error=True)
        return 3


def _route(name: str | None) -> admission.RouteRef:
    if not name or "--" not in name:
        raise Usage("needs --route provider--model")
    provider, model = name.split("--", 1)
    if provider not in providers.PROVIDERS:
        raise Usage(f"unknown provider {provider!r}")
    return admission.RouteRef(name, provider, model.replace("__", "/"))


def _price(route: admission.RouteRef) -> prices.Price:
    try:
        return prices.lookup(route.provider, route.model)
    except prices.UnpricedModel as exc:
        raise Usage(str(exc)) from None


def _cli_version(cli: str) -> str:
    try:
        return subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=30).stdout.strip() or "?"
    except (OSError, subprocess.TimeoutExpired):
        return "?"


def _endpoint(route: admission.RouteRef, cli: str) -> str:
    provider = providers.get(route.provider)
    path = provider.codex_path + "/responses" if cli == "codex" else provider.claude_path + "/v1/messages"
    return provider.upstream + path


def _binding(state: dict, route: admission.RouteRef, cli: str, endpoint_cli: str) -> admission.Binding:
    configured = envelope.route_view(state, route.name)
    if configured is None:
        raise Usage(f"no route {route.name} in envelope {state['name']}")
    return admission.Binding(route.name, configured.version, configured.key_fingerprint,
                             _endpoint(route, endpoint_cli), cli, _cli_version(endpoint_cli if cli == "any" else cli),
                             prices.TABLE_SHA256, state["scopes"][configured.scope]["account"])


def _requests(env: envelope.Envelope):
    return lambda key: [admission.Request.of(r) for r in env.requests_of(key)]


def _session_check(action: str, route: admission.RouteRef, cli: str, out) -> int:
    from . import pilot
    from .experiments import ADMISSION
    price = _price(route)
    if action == "metering" and route.provider != providers.DEEPSEEK:
        raise Usage("metering applies to a metered (DeepSeek) route")
    attempt = admission.new_attempt()
    root = ROOT / route.name / f"{action}-{cli}-{attempt}"
    with pilot.routing_context(ADMISSION) as env, routing.job(envelope.PROBE):
        binding = _binding(env.state, route, cli, cli)
        served = routing.active_proxy()
        base = dict(session=lambda attempt_dir: _session(route, cli, attempt_dir), requests=_requests(env),
                    settle=served.settle_now, sleep=lambda s: _sleep(s))
        placeholder = admission.Result(action, route.name, cli, attempt, "running", "")
        if action == "cache":
            for name in ("cache", "served"):
                admission.write(ROOT, replace(placeholder, check=name), binding, "running")
            results = admission.cache_and_served(route, cli, price, attempt, root, admission.SessionDeps(**base))
        else:
            admission.write(ROOT, placeholder, binding, "running")
            deps = admission.MeteringDeps(
                **base, balance=lambda: _balance(env, route), now=lambda: _now(),
                scope_unresolved=lambda: _scope_unresolved(env, route),
                scope_accounted=lambda: _scope_accounted(env, route))
            results = [admission.metering(route, cli, price, attempt, root, deps)]
    for result in results:
        out(f"{admission.write(ROOT, result, binding, 'done')}: {result.verdict} — {result.reason}")
    return 0


def _session(route: admission.RouteRef, cli: str, attempt_dir: Path):
    import shutil
    workdir = audit.anonymous_workdir()
    for name, text in FILES.items():
        (workdir / name).write_text(text, encoding="utf-8")
    try:
        return agents.run(agents.Agent(cli, route.model, route.provider), PROMPT, role=agents.REVIEWER, cwd=workdir,
                          out_dir=attempt_dir, offline=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _upstream_base(env: envelope.Envelope, route: admission.RouteRef) -> str:
    return str(env.route(route.name).upstream["base_url"])


def _balance(env: envelope.Envelope, route: admission.RouteRef) -> tuple[float, str]:
    settler = settlers.DeepSeekSettler(_upstream_base(env, route), providers.api_key(route.provider))
    return settler.balance_reading()


def _scope_unresolved(env: envelope.Envelope, route: admission.RouteRef) -> bool:
    scope = env.route(route.name).scope
    return any(r["scope"] == scope and r["state"] == "unresolved" for r in env.state["requests"].values())


def _scope_accounted(env: envelope.Envelope, route: admission.RouteRef) -> tuple[float, float]:
    scope = env.route(route.name).scope
    state = env.state
    settled = sum(r["billed"] for r in state["requests"].values() if r["scope"] == scope and r["state"] != "unresolved")
    return state["scopes"][scope]["opening_usage"], settled


def _current_bindings() -> list[admission.Binding]:
    """B's routes as configured now, from a read-only snapshot: no proxy, no ownership."""
    from .experiments import ADMISSION
    state = envelope.load(envelope.DEFAULT_ROOT / f"{ADMISSION.envelope}.json")
    bindings = []
    for arm in ADMISSION.arms:
        for agent in (arm.writer, arm.reviewer):
            if agent is None:
                continue
            route = admission.RouteRef(routing.route_name(agent.provider, agent.model), agent.provider, agent.model)
            if envelope.route_view(state, route.name) is not None:
                bindings.append(_binding(state, route, agent.cli, agent.cli))
    return list(dict.fromkeys(bindings))


def _exhaustion(args, route: admission.RouteRef, out) -> int:
    if args.funding is None:
        raise Usage("needs --funding (the account's small first funding, USD)")
    bought = admission.purchases(args.funding, args.fees or 0.0, args.taxes or 0.0)
    price = _price(route)
    attempt = admission.new_attempt()
    key = providers.api_key(route.provider)
    deepseek = route.provider == providers.DEEPSEEK
    if deepseek:
        admission.require_window(_now())  # metered = billed only at peak
    with routing.for_envelope(args.envelope) as env:
        served = routing.active_proxy()
        configured = env.route(route.name)
        if configured is None:
            raise Usage(f"no route {route.name} in envelope {args.envelope}")
        binding = _binding(env.state, route, "any", "claude")  # the drain posts Messages requests
        overdraw = sum(r.slots * r.max_charge for r in map(env.route, env.route_names()) if r.scope == configured.scope)
        target = DRAIN_SHARE * args.funding
        words = max(1, math.ceil(target / price.rates.input * 1e6 / TOKENS_PER_WORD))
        max_requests = math.ceil(3 * args.funding / target) + 50
        url_path = providers.get(route.provider).claude_path + "/v1/messages"
        base_url = _upstream_base(env, route)

        def send(count: int) -> list[admission.Reply]:
            replies: list = [None] * count

            def one(i):
                session = served.session(envelope.PROBE, attempt)[route.name]
                replies[i] = _post(session + url_path, _drain_body(route.model, words), key)
            threads = [threading.Thread(target=one, args=(i,)) for i in range(count)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            return replies

        now = _now()
        deps = admission.ExhaustionDeps(
            send=send, remaining=lambda: settlers.remaining_credit(route.provider, key, base_url),
            requests=_requests(env), settle=served.settle_now, sleep=lambda s: _sleep(s), now=lambda: _now(),
            may_continue=admission.in_window if deepseek else (lambda t: True))
        admission.write(ROOT, admission.Result("exhaustion", route.name, "any", attempt, "running", "", dict(bought)),
                        binding, "running")
        result = admission.exhaustion(route, attempt, funding=args.funding, overdraw_bound=overdraw, deps=deps,
                                      max_requests=max_requests, deadline=now + DRAIN_DEADLINE)
        result = replace(result, evidence={**result.evidence, **bought, "words": words, "max_requests": max_requests})
    out(f"{admission.write(ROOT, result, binding, 'done')}: {result.verdict} — {result.reason}")
    return 0


def _drain_body(model: str, words: int) -> bytes:
    text = " ".join(secrets.token_hex(3) for _ in range(words))
    return json.dumps({"model": model, "max_tokens": DRAIN_OUTPUT,
                       "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}]}).encode()


def _post(url: str, body: bytes, key: str) -> admission.Reply:
    request = urllib.request.Request(url, data=body, headers={"x-api-key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return admission.Reply(response.status, _json(response.read()))
    except urllib.error.HTTPError as exc:
        with exc:
            return admission.Reply(exc.code, _json(exc.read()))
    except (urllib.error.URLError, OSError):
        return admission.Reply(None, None)


def _json(data: bytes) -> dict | None:
    try:
        parsed = json.loads(data)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None
