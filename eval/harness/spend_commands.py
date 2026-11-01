"""The operator's interface to a spend envelope (`python3 -m eval spend ACTION --envelope NAME ...`).

Each action's flags are parsed into a typed command and checked before the envelope is touched,
so a malformed configuration is never persisted. `status` reads without ownership and works while
a run owns the envelope; every other action takes ownership and is refused while one does.
"""

import json
import time
from dataclasses import dataclass

from . import envelope, openrouter, proxy

ACTIONS = ("create", "fund", "scope", "allocate", "route", "validate-route", "status", "resolve")


class Usage(Exception):
    """Flags that don't make a valid command; nothing was changed."""


@dataclass(frozen=True)
class RouteCommand:
    route: str
    scope: str
    max_charge: float
    slots: int
    policy: dict
    bootstrap: float
    upstream: dict


def add_arguments(parser) -> None:
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--envelope", required=True, help="the budget's name, e.g. B")
    for flag in ("--usd", "--credit", "--fees", "--taxes", "--probe", "--allocation", "--provider-limit",
                 "--max-charge", "--bootstrap", "--billed"):
        parser.add_argument(flag, type=float)
    for flag in ("--scope", "--provider", "--route", "--policy", "--upstream-base", "--settler", "--request"):
        parser.add_argument(flag)
    parser.add_argument("--slots", type=int)
    parser.add_argument("--not-billed", action="store_true")


def run(args, out) -> int:
    """0 done, 2 bad flags (nothing changed), 3 refused by the envelope or its provider."""
    try:
        command = _parse(args)
        if args.action == "status":
            state = envelope.load(envelope.DEFAULT_ROOT / f"{args.envelope}.json")
            out(json.dumps(envelope.summary(state, time.time()), indent=2))
            return 0
        with envelope.Envelope.owned(args.envelope, create_usd=command if args.action == "create" else None) as env:
            _apply(env, args, command)
            out(json.dumps(envelope.summary(env.state, time.time()), indent=2))
        return 0
    except Usage as exc:
        out(f"spend {args.action}: {exc}", error=True)
        return 2
    except (envelope.Refused, envelope.EnvelopeError, openrouter.CreditUnavailable) as exc:
        out(f"spend {args.action}: refused: {exc}", error=True)
        return 3


def _need(args, *names):
    missing = [f"--{n.replace('_', '-')}" for n in names if getattr(args, n) is None]
    if missing:
        raise Usage(f"needs {', '.join(missing)}")


def _parse(args):
    action = args.action
    if action == "create":
        _need(args, "usd")
        return args.usd
    if action == "fund" and args.probe is None and args.credit is None:
        raise Usage("needs --credit (with --fees and --taxes) or --probe")
    if action == "scope":
        _need(args, "scope", "provider", "allocation")
        if args.provider != "openrouter":
            raise Usage("only an openrouter scope can read its key's usage so far")
    if action == "allocate":
        _need(args, "scope", "allocation")
    if action == "route":
        _need(args, "route", "scope", "max_charge", "slots", "policy", "bootstrap", "upstream_base", "settler")
        try:
            policy = json.loads(args.policy)
        except ValueError as exc:
            raise Usage(f"--policy is not JSON: {exc}") from None
        try:
            proxy.parse_policy(policy)  # the proxy's own parser: a policy it would refuse is never stored
        except ValueError as exc:
            raise Usage(f"--policy: {exc}") from None
        return RouteCommand(args.route, args.scope, args.max_charge, args.slots, policy, args.bootstrap,
                            {"base_url": args.upstream_base, "settler": args.settler})
    if action == "validate-route":
        _need(args, "route")
    if action == "resolve":
        _need(args, "request")
        if args.not_billed == (args.billed is not None):
            raise Usage("exactly one of --billed USD or --not-billed")
    return None


def _apply(env: envelope.Envelope, args, command) -> None:
    action = args.action
    if action == "fund":
        if args.probe is not None:
            env.fund_probe(args.probe)
        else:
            env.fund(credit=args.credit, fees=args.fees or 0.0, taxes=args.taxes or 0.0)
    elif action == "scope":
        key = openrouter.api_key()
        env.register_scope(args.scope, provider=args.provider, fingerprint=envelope.fingerprint(key),
                           opening_usage=openrouter.credit_used(), allocation=args.allocation,
                           provider_limit=args.provider_limit)
    elif action == "allocate":
        env.raise_allocation(args.scope, args.allocation)
    elif action == "route":
        env.set_route(command.route, scope=command.scope, max_charge=command.max_charge, slots=command.slots,
                      policy=command.policy, bootstrap_build_usd=command.bootstrap, upstream=command.upstream)
    elif action == "validate-route":
        env.validate_route(args.route)
    elif action == "resolve":
        if not env.settle(args.request, None if args.not_billed else args.billed):
            raise envelope.Refused(f"{args.request} was already settled")
