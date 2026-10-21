"""Operator CLI for the build-loop eval: python3 -m eval <command>."""

import argparse
import sys
import time
from pathlib import Path

DATA_ROOT = Path(__file__).resolve().parent / "data"

COMMANDS = {
    "validate": "check every task's hidden suites against its reference and wrong variants",
    "probe": "prove isolation of headless Claude and Codex sessions",
    "freeze": "write MANIFEST.json over the frozen inputs",
    "run": "execute arm runs from the schedule, resuming finished ones",
    "judge": "run judge calibration and pairwise judging",
    "report": "regenerate the pilot report from raw run records",
}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m eval", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    parsers = {name: sub.add_parser(name, help=help_text) for name, help_text in COMMANDS.items()}
    parsers["run"].add_argument("--task", help="run one task (a dev task, or a pilot task after freeze)")
    parsers["run"].add_argument("--arm", choices=["A1", "A1r", "A2", "A3"], help="with --task: the arm")
    parsers["judge"].add_argument("--task", help="dev task: calibrate, or judge --pair, on one task")
    parsers["judge"].add_argument("--judge", choices=["fable", "astra"], required=False)
    parsers["judge"].add_argument("--pair", nargs=2, metavar="ARM", help="with --task: judge two arms' builds")
    args = parser.parse_args(argv)
    handler = HANDLERS.get(args.command)
    if handler is None:
        # TODO(build): each command is wired as its chunk lands.
        print(f"{args.command}: not built yet", file=sys.stderr)
        return 2
    return handler(args)


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

    problems = probe.run_probes(DATA_ROOT / "probes")
    for problem in problems:
        print(problem, file=sys.stderr)
    print("probe: FAIL" if problems else "probe: ok")
    return 1 if problems else 0


def _run(args) -> int:
    import json

    from .harness import loop, tasks

    if not (args.task and args.arm):
        # TODO(build): the pilot schedule lands with the freeze.
        print("run: pass --task and --arm", file=sys.stderr)
        return 2
    task = tasks.Task(tasks.TASKS_ROOT / args.task)
    if not task.is_dev:
        print("run: pilot tasks run only from the frozen schedule", file=sys.stderr)
        return 2
    record = loop.execute_run(task, args.arm, DATA_ROOT / "runs", manifest_hash=None)
    print(json.dumps({k: record.get(k) for k in ("run_id", "exec", "status", "flags", "rounds", "scores")}))
    if record["status"] != "complete":
        return 1
    if args.arm == "A1r":
        return 0
    exec_dir = DATA_ROOT / "runs" / record["run_id"] / record["exec"]
    followup = loop.execute_followup(task, exec_dir, manifest_hash=None)
    print(json.dumps({k: followup.get(k) for k in ("status", "flags", "scores")}))
    return 0 if followup["status"] == "complete" else 1


def _judge(args) -> int:
    import json

    from .harness import judge, loop, tasks

    if not (args.task and args.judge):
        # TODO(build): the pilot's full judging lands with the freeze.
        print("judge: pass --task and --judge", file=sys.stderr)
        return 2
    task = tasks.Task(tasks.TASKS_ROOT / args.task)
    if not task.is_dev:
        print("judge: pilot tasks are judged only from the frozen schedule", file=sys.stderr)
        return 2
    stamp = time.strftime("%Y%m%dT%H%M%S")
    if args.pair:
        a, b = (loop.latest_complete_exec(DATA_ROOT / "runs", f"{task.id}--{arm}") / "repo" for arm in args.pair)
        result = judge.judge_pair(args.judge, task, a, b, DATA_ROOT / "dev-judgements" / f"{stamp}-{args.judge}")
    else:
        result = judge.calibrate(args.judge, task, DATA_ROOT / "dev-calibration" / f"{stamp}-{args.judge}")
    print(json.dumps({"result": result["result"], "picks": [o["pick"] for o in result["orders"]],
                      "leak_words": result["leak_words"]}))
    return 0


HANDLERS = {"validate": _validate, "probe": _probe, "run": _run, "judge": _judge}


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
