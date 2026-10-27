"""Operator CLI for the build-loop eval: python3 -m eval <command>."""

import argparse
import sys
import time
from pathlib import Path

DATA_ROOT = Path(__file__).resolve().parent / "data"

COMMANDS = {
    "validate": "check every task's hidden suites against its reference and wrong variants",
    "probe": "prove isolation of headless Claude and Codex sessions",
    "calibrate": "unscored builds of the experiment's tasks before its freeze (difficulty and cost)",
    "freeze": "write the experiment's manifest over its frozen inputs",
    "run": "execute the experiment's arm runs, resuming finished ones",
    "judge": "run judge calibration and pairwise judging",
    "report": "regenerate the experiment's report from raw run records",
}
EXPERIMENT_COMMANDS = ("probe", "calibrate", "freeze", "run", "judge", "report")


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
    from .harness import tasks

    problems = []
    for task in tasks.load_tasks():
        found = tasks.validate(task)
        print(f"{task.id}: {'FAIL' if found else 'ok'}")
        problems += found
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


def _probe(args) -> int:
    from .harness import probe

    problems = probe.run_probes(_data_root(args.experiment) / "probes", args.experiment)
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
    record = loop.execute_run(task, arm, runs_root, manifest_hash=None, offline=experiment.offline)
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
    from .harness import openrouter, pilot, replicated

    experiment = args.experiment
    task_ids = args.task or list(experiment.tasks)
    arm = args.arm or experiment.baseline
    if arm not in experiment.arm_names or any(t not in experiment.tasks for t in task_ids):
        print(f"calibrate: arm one of {experiment.arm_names}, task one of {experiment.tasks}", file=sys.stderr)
        return 2
    data_root = _data_root(experiment)
    built = pilot.run_calibration(data_root, experiment, task_ids, arm, args.reps)
    for record in built:
        cost = f" est=${replicated.build_cost(record, experiment):.3f}" if experiment.routed else ""
        print(f"{record['run_id']} {record['exec']}: {record['status']} {_score_line(record.get('scores') or {})}"
              f"{cost} flags={record.get('flags')}")
    if experiment.routed:
        print(f"calibration spend so far: {openrouter.spent(data_root / pilot.SPEND_LEDGER, 'calibration')}")
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


def _guarded(action) -> int:
    """Experiment commands refuse to start on a missing or changed freeze."""
    from .harness import manifest

    try:
        return action()
    except manifest.ManifestMismatch as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3


HANDLERS = {"validate": _validate, "probe": _probe, "calibrate": _calibrate, "freeze": _freeze, "run": _run,
            "judge": _judge, "report": _report}


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
