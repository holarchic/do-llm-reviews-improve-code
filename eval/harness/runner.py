"""Standalone hidden-suite runner, executed as a script next to candidate code.

    python3 -I runner.py <suite_dir> <results.jsonl> [--list] [--pattern GLOB] [--top-level DIR]
                         [--only IDS_FILE] [--detail]

The candidate's directory is the working directory and goes first on sys.path.
Each test's outcome is appended and flushed as it finishes, so a run killed by
the caller's wall clock still leaves the tests that completed. A test that
outlives PER_TEST_SECONDS is interrupted by SIGALRM and recorded as an error.
--list records collected test IDs without running them. --pattern is the
discovery pattern (default test_*.py; a build's own tests use unittest's
default, test*.py). --top-level is the import root when the suite is a package
inside the code (default: the suite dir itself). --only runs just the test IDs
listed one per line in IDS_FILE. --detail adds each failure's exception type,
stage (import, setup, call, teardown or timeout) and traceback, the evidence a replay anchor needs.

This file runs in the candidate's process, so it imports nothing from the
harness.
"""

import json
import os
import signal
import sys
import traceback
import unittest

PER_TEST_SECONDS = 10


class _Alarm(Exception):
    pass


def _on_alarm(signum, frame):
    raise _Alarm(f"test exceeded {PER_TEST_SECONDS}s")


_SETUP_FRAMES = {"setUp", "setUpClass", "setUpModule", "asyncSetUp"}
_TEARDOWN_FRAMES = {"tearDown", "tearDownClass", "tearDownModule", "asyncTearDown", "doCleanups", "doClassCleanups",
                    "_callCleanup"}


def _stage(test, err) -> str:
    """Where the failure arose: only "call", the test body, is evidence about the code under test."""
    if type(test).__name__ == "_FailedTest":
        return "import"
    if isinstance(test, unittest.suite._ErrorHolder):
        return "setup"
    if issubclass(err[0], _Alarm):
        return "timeout"
    frames = {frame.name for frame in traceback.extract_tb(err[2])}
    if frames & _SETUP_FRAMES:
        return "setup"
    return "teardown" if frames & _TEARDOWN_FRAMES else "call"


class _StreamingResult(unittest.TestResult):
    def __init__(self, sink, detail=False):
        super().__init__()
        self._sink = sink
        self._detail = detail

    def startTest(self, test):
        super().startTest(test)
        signal.alarm(PER_TEST_SECONDS)

    def stopTest(self, test):
        signal.alarm(0)
        super().stopTest(test)

    def _write(self, test, outcome, err=None):
        record = {"id": test.id(), "outcome": outcome}
        if self._detail and err is not None:
            record.update(exc=err[0].__name__, stage=_stage(test, err),
                          trace="".join(traceback.format_exception(*err))[-4000:])
        self._sink.write(json.dumps(record) + "\n")
        self._sink.flush()

    def addSuccess(self, test):
        super().addSuccess(test)
        self._write(test, "pass")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._write(test, "fail", err)

    def addError(self, test, err):
        super().addError(test, err)
        self._write(test, "error", err)

    def addSubTest(self, test, subtest, err):
        # A failed subtest is the parent test's outcome: unittest then never reports the parent a success.
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._write(test, "fail" if issubclass(err[0], test.failureException) else "error", err)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._write(test, "skip")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._write(test, "fail")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._write(test, "fail")


def _iter_tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_tests(item)
        else:
            yield item


def _option(options, name, default=None):
    return options[options.index(name) + 1] if name in options else default


def main(argv):
    suite_dir, results_path = os.path.abspath(argv[0]), argv[1]
    options = argv[2:]
    list_only = "--list" in options
    pattern = _option(options, "--pattern", "test_*.py")
    top_level = os.path.abspath(_option(options, "--top-level", suite_dir))
    sys.path.insert(0, os.getcwd())
    signal.signal(signal.SIGALRM, _on_alarm)
    suite = unittest.TestLoader().discover(suite_dir, pattern=pattern, top_level_dir=top_level)
    only = _option(options, "--only")
    if only is not None:
        with open(only, encoding="utf-8") as ids:
            wanted = {line.strip() for line in ids if line.strip()}
        suite = unittest.TestSuite(test for test in _iter_tests(suite) if test.id() in wanted)
    with open(results_path, "a", encoding="utf-8") as sink:
        if list_only:
            for test in _iter_tests(suite):
                sink.write(json.dumps({"id": test.id(), "outcome": "listed"}) + "\n")
            return 0
        suite.run(_StreamingResult(sink, detail="--detail" in options))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
