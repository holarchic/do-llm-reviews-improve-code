"""Run a child process in its own process group under a wall-clock budget.

A budget that only signals the child is not a bound: a TERM-ignoring or
forking child survives it. The whole group gets TERM, then KILL after a grace.
"""

import os
import signal
import subprocess
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

GRACE_SECONDS = 10


@dataclass(frozen=True)
class ProcResult:
    exit_code: int | None
    timed_out: bool
    seconds: float


def run(
    argv: list[str],
    *,
    cwd: Path,
    timeout: float,
    stdout_path: Path,
    stderr_path: Path | None = None,
    env: dict[str, str] | None = None,
    stdin_path: Path | None = None,
) -> ProcResult:
    """Run argv with stdin from a file (closed if none), stdout (and stderr) to files."""
    started = time.monotonic()
    with open(stdout_path, "wb") as out, (open(stdin_path, "rb") if stdin_path else nullcontext()) as stdin:
        err = open(stderr_path, "wb") if stderr_path else subprocess.STDOUT
        try:
            child = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=stdin if stdin_path else subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            try:
                code = child.wait(timeout=timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                _kill_group(child)
                code = child.returncode
                timed_out = True
        finally:
            if stderr_path:
                err.close()
    return ProcResult(exit_code=code, timed_out=timed_out, seconds=time.monotonic() - started)


def _kill_group(child: subprocess.Popen) -> None:
    _signal_group(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    # KILL even when the leader exited on TERM: a member that ignored it
    # still holds the group open.
    _signal_group(child.pid, signal.SIGKILL)
    child.wait()


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        # macOS answers EPERM for a group whose members have all exited.
        pass
