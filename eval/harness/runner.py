"""Standalone hidden-suite runner, executed as a script next to candidate code.

    python3 -I runner.py <suite_dir> <results.jsonl> [--list]

The candidate's directory is the working directory and goes first on sys.path.
Each test's outcome is appended and flushed as it finishes, so a run killed by
the caller's wall clock still leaves the tests that completed. A test that
outlives PER_TEST_SECONDS is interrupted by SIGALRM and recorded as an error.
--list records collected test IDs without running them.

This file runs in the candidate's process, so it imports nothing from the
harness.
"""

import json
import os
import signal
import sys
import unittest

PER_TEST_SECONDS = 10


class _Alarm(Exception):
    pass


def _on_alarm(signum, frame):
    raise _Alarm(f"test exceeded {PER_TEST_SECONDS}s")


class _StreamingResult(unittest.TestResult):
    def __init__(self, sink):
        super().__init__()
        self._sink = sink

    def startTest(self, test):
        super().startTest(test)
        signal.alarm(PER_TEST_SECONDS)

    def stopTest(self, test):
        signal.alarm(0)
        super().stopTest(test)

    def _write(self, test, outcome):
        self._sink.write(json.dumps({"id": test.id(), "outcome": outcome}) + "\n")
        self._sink.flush()

    def addSuccess(self, test):
        super().addSuccess(test)
        self._write(test, "pass")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._write(test, "fail")

    def addError(self, test, err):
        super().addError(test, err)
        self._write(test, "error")

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


def main(argv):
    suite_dir, results_path = os.path.abspath(argv[0]), argv[1]
    list_only = "--list" in argv[2:]
    sys.path.insert(0, os.getcwd())
    signal.signal(signal.SIGALRM, _on_alarm)
    suite = unittest.TestLoader().discover(suite_dir, pattern="test_*.py", top_level_dir=suite_dir)
    with open(results_path, "a", encoding="utf-8") as sink:
        if list_only:
            for test in _iter_tests(suite):
                sink.write(json.dumps({"id": test.id(), "outcome": "listed"}) + "\n")
            return 0
        suite.run(_StreamingResult(sink))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
