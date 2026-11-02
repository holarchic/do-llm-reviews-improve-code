"""The operator's interface to the admission checks (`python3 -m eval admit ACTION ...`).

`cache`, `metering` and `exhaustion` spend: each writes its attempt's record as `running` before the
first paid request and its verdict at the end, under `eval/data/admission/`. `status` and `budget`
only read. Cache and metering run inside the admission experiment's routing context (B's envelope,
its probe reservation); exhaustion runs in its own envelope.
"""

import json
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
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
EXHAUST_TEXT_WORDS = 4000  # words of filler per exhaustion request: moderate input


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
            return _status(out)
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


def _binding(env: envelope.Envelope, route: admission.RouteRef, cli: str) -> admission.Binding:
    configured = env.route(route.name)
    if configured is None:
        raise Usage(f"no route {route.name} in envelope {env.path.stem}")
    provider = providers.get(route.provider)
    endpoint = provider.upstream + (provider.claude_path + "/v1/messages" if cli == "claude"
                                    else provider.codex_path + "/responses")
    account = env.state["scopes"][configured.scope]["account"]
    return admission.Binding(route.name, configured.version, configured.key_fingerprint, endpoint, cli,
                             _cli_version(cli), prices.TABLE_SHA256, account)


def _session_check(action: str, route: admission.RouteRef, cli: str, out) -> int:
    from . import pilot
    from .experiments import ADMISSION
    price = _price(route)
    if action == "metering" and route.provider != providers.DEEPSEEK:
        raise Usage("metering applies to a metered (DeepSeek) route")
    attempt = admission.new_attempt()
    root = ROOT / route.name / f"{action}-{cli}-{attempt}"
    with pilot.routing_context(ADMISSION) as env, routing.job(envelope.PROBE):
        binding = _binding(env, route, cli)
        served = routing.active_proxy()
        deps = admission.Deps(
            session=lambda attempt_dir: _session(route, cli, attempt_dir),
            requests=env.requests_of, settle=served.settle_now, now=lambda: datetime.now(timezone.utc),
            sleep=time.sleep, balance=lambda: _balance(route),
            scope_unresolved=lambda: _scope_unresolved(env, route))
        placeholder = admission.Result(action, route.name, cli, attempt, "running", "")
        if action == "cache":
            names = ("cache", "served")
            for name in names:
                admission.write(ROOT, replace(placeholder, check=name), binding, "running")
            results = admission.cache_and_served(route, cli, price, attempt, root, deps)
        else:
            admission.write(ROOT, placeholder, binding, "running")
            results = [admission.metering(route, cli, price, attempt, root, deps)]
    for result in results:
        out(f"{admission.write(ROOT, result, binding, 'done')}: {result.verdict} — {result.reason}")
    return 0


def _session(route: admission.RouteRef, cli: str, attempt_dir: Path):
    workdir = audit.anonymous_workdir()
    for name, text in FILES.items():
        (workdir / name).write_text(text, encoding="utf-8")
    try:
        return agents.run(agents.Agent(cli, route.model, route.provider), PROMPT, role=agents.REVIEWER, cwd=workdir,
                          out_dir=attempt_dir, offline=True)
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)


def _balance(route: admission.RouteRef) -> tuple[float, str]:
    settler = settlers.DeepSeekSettler(providers.get(route.provider).upstream, providers.api_key(route.provider))
    return settler.balance_reading()


def _scope_unresolved(env: envelope.Envelope, route: admission.RouteRef) -> bool:
    scope = env.route(route.name).scope
    return any(r["scope"] == scope and r["state"] == "unresolved" for r in env.state["requests"].values())


def _status(out) -> int:
    from .experiments import ADMISSION
    from . import pilot
    with pilot.routing_context(ADMISSION) as env:
        bindings = []
        for arm in ADMISSION.arms:
            for agent in (arm.writer, arm.reviewer):
                if agent is not None and env.route(routing.route_name(agent.provider, agent.model)) is not None:
                    route = admission.RouteRef(routing.route_name(agent.provider, agent.model), agent.provider,
                                               agent.model)
                    bindings.append(_binding(env, route, agent.cli))
    out(json.dumps(admission.status(ROOT, list(dict.fromkeys(bindings))), indent=1))
    return 0


def _exhaustion(args, route: admission.RouteRef, out) -> int:
    if args.funding is None:
        raise Usage("needs --funding (the account's small first funding, USD)")
    price = _price(route)
    attempt = admission.new_attempt()
    key = providers.api_key(route.provider)
    with routing.for_envelope(args.envelope) as env:
        served = routing.active_proxy()
        configured = env.route(route.name)
        if configured is None:
            raise Usage(f"no route {route.name} in envelope {args.envelope}")
        binding = _binding(env, route, "-")
        overdraw = sum(r.slots * r.max_charge for r in map(env.route, env.route_names()) if r.scope == configured.scope)
        body = _exhaust_body(route.model)
        request_cost = prices.request_charge_bound(price, max_input_tokens=len(body) + 1000, max_output_tokens=16)
        if route.provider == providers.DEEPSEEK:
            admission._require_window(datetime.now(timezone.utc))  # metered = billed only at peak
        path = providers.get(route.provider).claude_path + "/v1/messages"

        def send(count: int) -> list[admission.Reply]:
            replies: list = [None] * count

            def one(i):
                base = served.session(envelope.PROBE, attempt)[route.name]
                replies[i] = _post(base + path, body, key)
            threads = [threading.Thread(target=one, args=(i,)) for i in range(count)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            return replies

        deps = admission.Deps(requests=env.requests_of, settle=served.settle_now, sleep=time.sleep,
                              send=send, remaining=lambda: settlers.remaining_credit(route.provider, key))
        placeholder = admission.Result("exhaustion", route.name, "-", attempt, "running", "")
        admission.write(ROOT, placeholder, binding, "running")
        result = admission.exhaustion(route, attempt, funding=args.funding, fees=args.fees or 0.0,
                                      taxes=args.taxes or 0.0, request_cost=request_cost, overdraw_bound=overdraw,
                                      deps=deps)
    out(f"{admission.write(ROOT, result, binding, 'done')}: {result.verdict} — {result.reason}")
    return 0


def _exhaust_body(model: str) -> bytes:
    words = " ".join(secrets.token_hex(3) for _ in range(EXHAUST_TEXT_WORDS))
    return json.dumps({"model": model, "max_tokens": 16,
                       "messages": [{"role": "user", "content": [{"type": "text", "text": words}]}]}).encode()


def _post(url: str, body: bytes, key: str) -> admission.Reply:
    request = urllib.request.Request(url, data=body, headers={"x-api-key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
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
