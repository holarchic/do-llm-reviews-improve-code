"""Operator CLI for the build-loop eval: python3 -m eval <command>."""

import argparse
import sys
import time
from pathlib import Path

DATA_ROOT = Path(__file__).resolve().parent / "data"

COMMANDS = {
    "validate": "check each unfrozen task's hidden suites against its reference and wrong variants",
    "probe": "prove isolation of headless Claude and Codex sessions",
    "calibrate": "unscored builds of the experiment's tasks before its freeze (difficulty and cost)",
    "freeze": "write the experiment's manifest over its frozen inputs",
    "run": "execute the experiment's arm runs, resuming finished ones",
    "judge": "run judge calibration and pairwise judging",
    "report": "regenerate the experiment's report from raw run records",
    "rounds": "score each code-review round of a replay experiment's reviewed builds; apply the round rule",
    "cost": "price every recorded step of the experiment at the dated list prices (descriptive)",
    "spend": "a spend envelope: create, fund, scope, route, validate-route, status, resolve",
}
EXPERIMENT_COMMANDS = ("probe", "calibrate", "freeze", "run", "judge", "report", "rounds", "cost")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m eval", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    parsers = {name: sub.add_parser(name, help=help_text) for name, help_text in COMMANDS.items()}
    for name in EXPERIMENT_COMMANDS:
        parsers[name].add_argument("--experiment", default="pilot", help="a name from eval/harness/experiments.py")
    parsers["run"].add_argument("--task", help="run one task (a dev task, or a pilot task after freeze)")
    parsers["run"].add_argument("--arm", help="one of the experiment's arms; without --task, that arm's builds only")
    parsers["calibrate"].add_argument("--task", action="append", help="one of the experiment's tasks, repeatable "
                                      "(default: all); one process, so one writer of the spend ledger")
    parsers["calibrate"].add_argument("--arm", help="default: the baseline arm; a loop arm gives a cost sample")
    parsers["calibrate"].add_argument("--reps", type=int, default=2)
    parsers["judge"].add_argument("--task", help="dev task: calibrate, or judge --pair, on one task")
    parsers["judge"].add_argument("--judge", choices=["fable", "astra"], required=False)
    parsers["judge"].add_argument("--pair", nargs=2, metavar="ARM", help="with --task: judge two arms' builds")
    parsers["rounds"].add_argument("--arm", required=True, help="a reviewed arm of the experiment")
    parsers["cost"].add_argument("--arm", action="append", help="one of the experiment's arms, repeatable (default: all)")
    spend = parsers["spend"]
    spend.add_argument("action", choices=["create", "fund", "scope", "route", "validate-route", "status", "resolve"])
    spend.add_argument("--envelope", required=True, help="the budget's name, e.g. B")
    for flag in ("--usd", "--credit", "--fees", "--taxes", "--probe", "--allocation", "--provider-limit",
                 "--max-charge", "--bootstrap", "--billed"):
        spend.add_argument(flag, type=float)
    for flag in ("--scope", "--provider", "--route", "--policy", "--upstream-base", "--settler", "--request"):
        spend.add_argument(flag)
    spend.add_argument("--slots", type=int)
    spend.add_argument("--not-billed", action="store_true")
    parsers["validate"].add_argument("--task", action="append", help="validate only this task, repeatable "
                                     "(default: every task no manifest freezes; a frozen task is refused)")
    args = parser.parse_args(argv)
    if args.command in EXPERIMENT_COMMANDS:
        from .harness import experiments

        if args.experiment not in experiments.EXPERIMENTS:
            print(f"unknown experiment {args.experiment!r}; one of {', '.join(experiments.EXPERIMENTS)}",
                  file=sys.stderr)
            return 2
        args.experiment = experiments.EXPERIMENTS[args.experiment]
    handler = HANDLERS[args.command]
    return handler(args)


def _data_root(experiment) -> Path:
    return DATA_ROOT / experiment.data_dir if experiment.data_dir else DATA_ROOT


def _validate(args) -> int:
    from .harness import frozen, tasks

    every = tasks.load_tasks()
    unknown = sorted(set(args.task or ()) - {task.id for task in every})
    if unknown:
        print(f"validate: no task {', '.join(unknown)}", file=sys.stderr)
        return 2

    def check() -> int:
        held = frozen.frozen_tasks()
        named = sorted(set(args.task or ()) & set(held))
        if named:
            print("validate: frozen, expected.json never rewritten: "
                  + ", ".join(f"{task_id} ({held[task_id]})" for task_id in named), file=sys.stderr)
            return 2
        problems = []
        for task in (t for t in every if args.task is None or t.id in args.task):
            if task.id in held:
                print(f"{task.id}: frozen ({held[task.id]}), skipped")
                continue
            found = tasks.validate(task)
            print(f"{task.id}: {'FAIL' if found else 'ok'}")
            problems += found
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0

    return _guarded(check)


def _probe(args) -> int:
    from .harness import envelope, pilot, probe, routing

    def run() -> list[str]:
        # A routed experiment's probes draw on its envelope's probe reservation.
        with pilot.routing_context(args.experiment), routing.job(envelope.PROBE):
            return probe.run_probes(_data_root(args.experiment) / "probes", args.experiment)
    try:
        problems = run()
    except (routing.RoutingRequired, envelope.EnvelopeError) as exc:
        print(f"probe: refused: {exc}", file=sys.stderr)
        return 3
    for problem in problems:
        print(problem, file=sys.stderr)
    print("probe: FAIL" if problems else "probe: ok")
    return 1 if problems else 0


def _run(args) -> int:
    import json

    from .harness import loop, pilot, tasks

    experiment = args.experiment
    if not args.task:
        if args.arm is not None and args.arm not in experiment.arm_names:
            print(f"run: --arm must be one of {', '.join(experiment.arm_names)}", file=sys.stderr)
            return 2
        return _guarded(lambda: pilot.run_builds(_data_root(experiment), experiment, only_arm=args.arm))
    if args.arm not in experiment.arm_names:
        print(f"run: --task needs --arm, one of {', '.join(experiment.arm_names)}", file=sys.stderr)
        return 2
    task = tasks.Task(tasks.TASKS_ROOT / args.task)
    if not task.is_dev:
        print("run: scored tasks run only from a frozen schedule", file=sys.stderr)
        return 2
    arm = experiment.arm(args.arm)
    runs_root = _data_root(experiment) / "runs"
    calibration = _data_root(experiment) / "calibration" / "runs"
    with pilot.routing_context(experiment) as env:
        record = pilot.reserved(env, f"dev:{loop.run_id(task.id, arm.name, None)}",
                                lambda: pilot.job_estimate(env, experiment, arm.name, calibration),
                                lambda: loop.execute_run(task, arm, runs_root, manifest_hash=None,
                                                         offline=experiment.offline))
    if record is None:
        print("run: not admitted by the spend envelope", file=sys.stderr)
        return 1
    print(json.dumps({k: record.get(k) for k in ("run_id", "exec", "status", "flags", "rounds", "scores")}))
    if record["status"] != "complete":
        return 1
    if arm.name not in experiment.followup_arms:
        return 0
    exec_dir = runs_root / record["run_id"] / record["exec"]
    followup = loop.execute_followup(task, exec_dir, arm, manifest_hash=None, offline=experiment.offline)
    print(json.dumps({k: followup.get(k) for k in ("status", "flags", "scores")}))
    return 0 if followup["status"] == "complete" else 1


def _calibrate(args) -> int:
    from .harness import pilot, spend

    experiment = args.experiment
    task_ids = args.task or list(experiment.tasks)
    arm = args.arm or experiment.baseline
    if arm not in experiment.arm_names or any(t not in experiment.tasks for t in task_ids):
        print(f"calibrate: arm one of {experiment.arm_names}, task one of {experiment.tasks}", file=sys.stderr)
        return 2
    if len(set(task_ids)) != len(task_ids) or args.reps < 1:
        print("calibrate: each task once, and --reps at least 1", file=sys.stderr)
        return 2
    data_root = _data_root(experiment)
    built = pilot.run_calibration(data_root, experiment, task_ids, arm, args.reps)
    for record in built:
        # Priced by the ledger: an unpriced model raises rather than counting as $0.
        priced = spend.execution_cost(data_root / "calibration" / "runs" / record["run_id"] / record["exec"],
                                      experiment.arm(arm)).totals()
        cost = f" list=${priced.cost.low:.3f}–{priced.cost.high:.3f} unknown={priced.unknown}"
        print(f"{record['run_id']} {record['exec']}: {record['status']} {_score_line(record.get('scores') or {})}"
              f"{cost} flags={record.get('flags')}")
    # A job that crashed or wasn't admitted leaves no record, so success needs every requested build.
    complete = sum(record["status"] == "complete" for record in built)
    return 0 if complete == len(task_ids) * args.reps else 1


def _percent(value) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}"


def _score_line(scores: dict) -> str:
    if "replay" in scores:
        replay = scores["replay"]
        return (f"review={_percent(replay['review'])} feature={_percent(replay['feature'])}"
                f" regressions={_percent(replay['regressions'])}")
    return (f"primary={_percent(scores.get('primary'))}"
            f" planted={_percent((scores.get('planted') or {}).get('fraction'))}")


def _judge(args) -> int:
    import json

    from .harness import judge, loop, pilot, tasks

    if not args.task:
        return _guarded(lambda: pilot.run_judging(_data_root(args.experiment), args.experiment))
    if not args.judge:
        print("judge: --task needs --judge", file=sys.stderr)
        return 2
    task = tasks.Task(tasks.TASKS_ROOT / args.task)
    if not task.is_dev:
        print("judge: scored tasks are judged only from a frozen schedule", file=sys.stderr)
        return 2
    stamp = time.strftime("%Y%m%dT%H%M%S")
    data_root = _data_root(args.experiment)
    if args.pair:
        unknown = [arm for arm in args.pair if arm not in args.experiment.arm_names]
        if unknown:
            print(f"judge: {unknown} not arms of {args.experiment.name}", file=sys.stderr)
            return 2
        a, b = (loop.latest_complete_exec(data_root / "runs", f"{task.id}--{arm}") / "artifact" for arm in args.pair)
        result = judge.judge_pair(args.judge, task, a, b, data_root / "dev-judgements" / f"{stamp}-{args.judge}",
                                  offline=args.experiment.offline)
    else:
        result = judge.calibrate(args.judge, task, data_root / "dev-calibration" / f"{stamp}-{args.judge}",
                                 offline=args.experiment.offline)
    print(json.dumps({"result": result["result"], "picks": [o["pick"] for o in result["orders"]],
                      "leak_words": result["leak_words"]}))
    return 0


def _freeze(args) -> int:
    from .harness import manifest

    return _guarded(lambda: print(manifest.freeze(args.experiment)) or 0)


def _report(args) -> int:
    from .harness import replay, replicated, report

    experiment = args.experiment
    data_root = _data_root(experiment)

    def write() -> int:
        module = {"judged": report, "replicated": replicated, "replay": replay}[experiment.report]
        results = module.collect(data_root, experiment)
        decision = module.decide(results, experiment)
        text = module.render(results, decision, experiment)
        data_root.mkdir(parents=True, exist_ok=True)
        (data_root / "report.md").write_text(text, encoding="utf-8")
        print(text)
        return 0 if decision.verdict != "INCOMPLETE" else 1

    return _guarded(write)


def _rounds(args) -> int:
    from .harness import rounds

    experiment = args.experiment
    if experiment.report != "replay" or args.arm not in experiment.treatments:
        print(f"rounds: needs a replay experiment and one of its reviewed arms, not {experiment.name} {args.arm}",
              file=sys.stderr)
        return 2
    data_root = _data_root(experiment)

    def write() -> int:
        digest = rounds.verify_freeze(experiment)
        for failure in rounds.score_rounds(data_root, experiment, args.arm, digest):
            print(failure, file=sys.stderr)
        results = rounds.collect(data_root, experiment, args.arm, digest)
        trigger = rounds.decide(results)
        text = rounds.render(results, trigger, experiment)
        data_root.mkdir(parents=True, exist_ok=True)
        (data_root / "rounds.md").write_text(text, encoding="utf-8")
        print(text)
        return 0 if trigger.verdict != "INCOMPLETE" else 1

    return _guarded(write)


def _guarded(action) -> int:
    """Run action; a missing, unreadable or changed freeze it raises becomes a refusal, exit 3."""
    from .harness import manifest

    try:
        return action()
    except manifest.ManifestMismatch as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3


def _cost(args) -> int:
    from .harness import cost_report

    unknown = sorted(set(args.arm or ()) - set(args.experiment.arm_names))
    if unknown:
        print(f"cost: no arm {', '.join(unknown)}; one of {', '.join(args.experiment.arm_names)}", file=sys.stderr)
        return 2
    data_root = _data_root(args.experiment)
    text = cost_report.render(cost_report.collect(data_root, args.experiment, args.arm))
    data_root.mkdir(parents=True, exist_ok=True)
    (data_root / "cost.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


def _spend(args) -> int:
    import json
    import time

    from .harness import envelope, openrouter, routing

    if args.action == "status":  # read-only: no ownership, so it works while a run owns the envelope
        state = envelope.load(envelope.DEFAULT_ROOT / f"{args.envelope}.json")
        print(json.dumps(envelope.summary(state, time.time()), indent=2))
        return 0
    try:
        with envelope.Envelope.owned(args.envelope, create_usd=args.usd if args.action == "create" else None) as env:
            if args.action == "fund":
                if args.probe is not None:
                    env.fund_probe(args.probe)
                else:
                    env.fund(credit=args.credit or 0.0, fees=args.fees or 0.0, taxes=args.taxes or 0.0)
            elif args.action == "scope":
                if args.provider != "openrouter":
                    print("spend scope: only an openrouter scope can read its key's usage so far", file=sys.stderr)
                    return 2
                key = openrouter.api_key()
                env.register_scope(args.scope, provider=args.provider, fingerprint=routing.fingerprint(key),
                                   opening_usage=openrouter.credit_used(), allocation=args.allocation,
                                   provider_limit=args.provider_limit)
            elif args.action == "route":
                env.set_route(args.route, scope=args.scope, max_charge=args.max_charge, slots=args.slots,
                              policy=json.loads(args.policy or "{}"), bootstrap_build_usd=args.bootstrap,
                              upstream={"base_url": args.upstream_base, "settler": args.settler})
            elif args.action == "validate-route":
                env.validate_route(args.route)
            elif args.action == "resolve":
                if args.not_billed == (args.billed is not None):
                    print("spend resolve: exactly one of --billed USD or --not-billed", file=sys.stderr)
                    return 2
                if not env.settle(args.request, None if args.not_billed else args.billed):
                    print(f"spend resolve: {args.request} was already settled", file=sys.stderr)
            print(json.dumps(envelope.summary(env.state, time.time()), indent=2))
    except (envelope.Refused, envelope.EnvelopeError, openrouter.CreditUnavailable, ValueError, TypeError) as exc:
        print(f"spend {args.action}: refused: {exc}", file=sys.stderr)
        return 3
    return 0


HANDLERS = {"validate": _validate, "probe": _probe, "calibrate": _calibrate, "freeze": _freeze, "run": _run,
            "judge": _judge, "report": _report, "rounds": _rounds, "cost": _cost,
            "spend": _spend}


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
