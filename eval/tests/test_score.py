import os
import shutil
import tempfile
import unittest
from pathlib import Path

from eval.harness import score, tasks

DEV = tasks.Task(tasks.TASKS_ROOT / "dev-intervals")


class ScoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.expected = DEV.expected()["hidden"]
        self._n = 0

    def work(self) -> Path:
        self._n += 1
        return self.tmp / f"w{self._n}"

    def candidate(self, source: str) -> Path:
        code = self.work()
        code.mkdir()
        (code / "intervals.py").write_text(source)
        return code

    def fraction(self, code: Path, timeout: float = score.SUITE_SECONDS) -> float:
        outcome = score.run_suite(code, DEV.path("hidden"), self.work(), timeout=timeout)
        return score.pass_fraction(outcome, self.expected)

    def test_a_test_with_failed_subtests_is_its_most_severe_failure(self):
        records = [{"id": "t", "outcome": "fail", "exc": "AssertionError"},
                   {"id": "t", "outcome": "error", "exc": "AttributeError"},
                   {"id": "t", "outcome": "fail", "exc": "AssertionError"},
                   {"id": "u", "outcome": "pass"}]
        chosen = score._by_test(records)
        self.assertEqual((chosen["t"]["outcome"], chosen["t"]["exc"], chosen["u"]["outcome"]),
                         ("error", "AttributeError", "pass"))

    def test_reference_scores_full(self):
        self.assertEqual(self.fraction(DEV.path("reference")), 1.0)

    def test_wrong_variant_scores_below_full(self):
        self.assertLess(self.fraction(DEV.path("wrong") / "adjacent-not-merged"), 1.0)

    def test_missing_module_scores_zero(self):
        code = self.work()
        code.mkdir()
        self.assertEqual(self.fraction(code), 0.0)

    def test_import_time_exception_scores_zero(self):
        self.assertEqual(self.fraction(self.candidate("raise RuntimeError('boom')\n")), 0.0)

    def test_import_time_hang_scores_zero(self):
        outcome = score.run_suite(self.candidate("import time\ntime.sleep(600)\n"), DEV.path("hidden"),
                                  self.work(), timeout=3)
        self.assertTrue(outcome.timed_out)
        self.assertEqual(score.pass_fraction(outcome, self.expected), 0.0)

    def test_hanging_test_fails_while_others_score(self):
        reference = (DEV.path("reference") / "intervals.py").read_text()
        hang_on_adjacent = reference.replace(
            "    merged = []\n",
            "    if spans == [(1, 2), (3, 4)]:\n        while True:\n            pass\n    merged = []\n")
        self.assertNotEqual(hang_on_adjacent, reference)
        outcome = score.run_suite(self.candidate(hang_on_adjacent), DEV.path("hidden"), self.work())
        self.assertFalse(outcome.timed_out)
        self.assertEqual(outcome.outcomes["test_merge.MergeTest.test_adjacent_merge"], "error")
        self.assertEqual(score.pass_fraction(outcome, self.expected), 7 / 8)

    def test_suite_killed_mid_run_counts_unfinished_as_failed(self):
        reference = (DEV.path("reference") / "intervals.py").read_text()
        uninterruptible_on_overlap = reference.replace(
            "    merged = []\n",
            "    if spans == [(1, 5), (3, 8)]:\n        import signal\n"
            "        signal.signal(signal.SIGALRM, signal.SIG_IGN)\n        while True:\n            pass\n"
            "    merged = []\n")
        self.assertNotEqual(uninterruptible_on_overlap, reference)
        outcome = score.run_suite(self.candidate(uninterruptible_on_overlap), DEV.path("hidden"),
                                  self.work(), timeout=15)
        self.assertTrue(outcome.timed_out)
        self.assertEqual(len(outcome.passed()), 5)
        self.assertEqual(score.pass_fraction(outcome, self.expected), 5 / 8)

    def test_relative_work_dir(self):
        # A path relative to the caller's cwd; it means nothing from the runner's cwd.
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.tmp)
        outcome = score.run_suite(DEV.path("reference"), DEV.path("hidden"), Path("relative-work"))
        self.assertEqual(score.pass_fraction(outcome, self.expected), 1.0)

    def test_nested_function_branches_counted_separately(self):
        source = ("def outer(x):\n    if x:\n        pass\n\n    def inner(y):\n"
                  "        if y:\n            return 1\n        if not y:\n            return 2\n\n    return inner\n")
        self.assertEqual(score.static_metrics(self.candidate(source)).max_function_branches, 2)

    def test_lambda_branches_measured(self):
        source = "def factory():\n    return lambda x: 1 if x > 0 else (2 if x < 0 else 0)\n"
        self.assertEqual(score.static_metrics(self.candidate(source)).max_function_branches, 2)

    def test_static_metrics(self):
        code = self.candidate("def f(x):\n    if x and x > 1:\n        return [i for i in x if i]\n    return None\n")
        (code / "test_f.py").write_text("import f\n\n\nx = 1\n")
        metrics = score.static_metrics(code)
        self.assertEqual(metrics.source_loc, 4)
        self.assertEqual(metrics.test_loc, 2)
        self.assertEqual(metrics.max_function_branches, 3)
        self.assertEqual(metrics.ruff_violations, 1)  # F401: unused import in test_f.py


if __name__ == "__main__":
    unittest.main()
