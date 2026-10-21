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
    parsers["run"].add_argument("--arm", help="with --task: the arm (A1, A1r, A2, A3)")
    parsers["judge"].add_argument("--task", help="dev task: calibrate, or judge --pair, on one task")
    parsers["judge"].add_argument("--judge", choices=["fable", "astra"], required=False)
    parsers["judge"].add_argument("--pair", nargs=2, metavar="ARM", help="with --task: judge two arms' builds")
    args = parser.parse_args(argv)
    handler = HANDLERS[args.command]
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

    from .harness import loop, pilot, tasks

    if not args.task:
        return _guarded(lambda: pilot.run_builds(DATA_ROOT))
    if args.arm not in loop.ARMS:
        print(f"run: --task needs --arm, one of {', '.join(loop.ARMS)}", file=sys.stderr)
        return 2
    task = tasks.Task(tasks.TASKS_ROOT / args.task)
    if not task.is_dev:
        print("run: pilot tasks run only from the frozen schedule", file=sys.stderr)
        return 2
    record = loop.execute_run(task, loop.ARMS[args.arm], DATA_ROOT / "runs", manifest_hash=None)
    print(json.dumps({k: record.get(k) for k in ("run_id", "exec", "status", "flags", "rounds", "scores")}))
    if record["status"] != "complete":
        return 1
    if args.arm == "A1r":
        return 0
    exec_dir = DATA_ROOT / "runs" / record["run_id"] / record["exec"]
    followup = loop.execute_followup(task, exec_dir, loop.ARMS[args.arm], manifest_hash=None)
    print(json.dumps({k: followup.get(k) for k in ("status", "flags", "scores")}))
    return 0 if followup["status"] == "complete" else 1


def _judge(args) -> int:
    import json

    from .harness import judge, loop, pilot, tasks

    if not args.task:
        return _guarded(lambda: pilot.run_judging(DATA_ROOT))
    if not args.judge:
        print("judge: --task needs --judge", file=sys.stderr)
        return 2
    task = tasks.Task(tasks.TASKS_ROOT / args.task)
    if not task.is_dev:
        print("judge: pilot tasks are judged only from the frozen schedule", file=sys.stderr)
        return 2
    stamp = time.strftime("%Y%m%dT%H%M%S")
    if args.pair:
        a, b = (loop.latest_complete_exec(DATA_ROOT / "runs", f"{task.id}--{arm}") / "artifact" for arm in args.pair)
        result = judge.judge_pair(args.judge, task, a, b, DATA_ROOT / "dev-judgements" / f"{stamp}-{args.judge}")
    else:
        result = judge.calibrate(args.judge, task, DATA_ROOT / "dev-calibration" / f"{stamp}-{args.judge}")
    print(json.dumps({"result": result["result"], "picks": [o["pick"] for o in result["orders"]],
                      "leak_words": result["leak_words"]}))
    return 0


def _freeze(args) -> int:
    from .harness import manifest

    print(manifest.freeze())
    return 0


def _report(args) -> int:
    from .harness import report

    def write() -> int:
        results = report.collect(DATA_ROOT)
        decision = report.decide(results)
        text = report.render(results, decision)
        (DATA_ROOT / "report.md").write_text(text, encoding="utf-8")
        print(text)
        return 0 if decision.verdict != "INCOMPLETE" else 1

    return _guarded(write)


def _guarded(action) -> int:
    """Pilot commands refuse to start on changed frozen inputs."""
    from .harness import manifest

    try:
        return action()
    except manifest.ManifestMismatch as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3


HANDLERS = {"validate": _validate, "probe": _probe, "freeze": _freeze, "run": _run, "judge": _judge,
            "report": _report}


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
