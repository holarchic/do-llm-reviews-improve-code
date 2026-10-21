"""Operator CLI for the build-loop eval: python3 -m eval <command>."""

import argparse
import sys

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
    for name, help_text in COMMANDS.items():
        sub.add_parser(name, help=help_text)
    args = parser.parse_args(argv)
    handler = HANDLERS.get(args.command)
    if handler is None:
        # TODO(build): each command is wired as its chunk lands.
        print(f"{args.command}: not built yet", file=sys.stderr)
        return 2
    return handler()


def _validate() -> int:
    from .harness import tasks

    problems = []
    for task in tasks.load_tasks():
        found = tasks.validate(task)
        print(f"{task.id}: {'FAIL' if found else 'ok'}")
        problems += found
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


HANDLERS = {"validate": _validate}


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
