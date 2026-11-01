import hashlib
import json
import math
import unittest
from fractions import Fraction
from pathlib import Path

from eval.harness import guards, pilot, records, replay
from eval.harness.experiments import EXPERIMENTS
from eval.harness.guards import TaskRegressions

REPO = Path(__file__).resolve().parents[2]
REPLAY_DATA = REPO / "eval" / "data" / "replay"


def task(name: str, tests: int, treatment: tuple[int, ...], comparator: tuple[int, ...] = ()) -> TaskRegressions:
    return TaskRegressions(name, tests, treatment, comparator or (0,) * len(treatment))


def clean(name: str, tests: int = 200) -> TaskRegressions:
    return task(name, tests, (0, 0))


class RegressionGuardTest(unittest.TestCase):
    """The program plan's A2 numbers, as tests."""

    def test_the_worked_example_passes_the_mean_and_fails_the_task_cap(self):
        hit = task("big", 420, (25, 0))
        guard = guards.regression_guard([hit, *(clean(f"t{i}") for i in range(5))])
        self.assertAlmostEqual(float(guard.mean), 0.496, places=3)
        self.assertTrue(guard.mean_ok)
        self.assertAlmostEqual(float(hit.delta), 2.98, places=2)
        self.assertEqual(guard.over_cap, ("big",))
        self.assertFalse(guard.passed)

    def test_the_cap_allows_3_extra_failures_on_155_tests_and_8_on_420(self):
        for tests, allowed in ((155, 3), (420, 8)):
            for split in ((allowed, 0), (allowed - 1, 1)):
                with self.subTest(tests=tests, split=split):
                    self.assertFalse(guards.regression_guard([task("t", tests, split)]).over_cap)
            for split in ((allowed + 1, 0), (allowed, 1)):
                with self.subTest(tests=tests, split=split):
                    self.assertEqual(guards.regression_guard([task("t", tests, split)]).over_cap, ("t",))

    def test_a_task_exactly_at_the_cap_and_a_mean_exactly_at_the_limit_pass(self):
        at_cap, untouched = task("at-cap", 100, (2, 0)), task("untouched", 100, (0, 0))
        guard = guards.regression_guard([at_cap, untouched])
        self.assertEqual((at_cap.delta, untouched.delta, guard.mean), (1, 0, Fraction(1, 2)))
        self.assertTrue(guard.mean_ok)
        self.assertEqual(guard.over_cap, ())
        self.assertTrue(guard.passed)

    def test_a_mean_above_the_limit_fails_with_every_task_under_the_cap(self):
        guard = guards.regression_guard([task("a", 100, (1, 0)), task("b", 100, (2, 0))])  # 0.5 and 1.0
        self.assertEqual(guard.mean, Fraction(3, 4))
        self.assertEqual(guard.over_cap, ())
        self.assertFalse(guard.passed)

    def test_fewer_failures_offset_the_mean_but_never_another_tasks_cap(self):
        better, worse = task("better", 100, (0, 0), (4, 4)), task("worse", 100, (4, 0))
        guard = guards.regression_guard([better, worse])
        self.assertEqual((better.delta, worse.delta, guard.mean), (-4, 2, -1))
        self.assertTrue(guard.mean_ok)
        self.assertEqual(guard.over_cap, ("worse",))
        self.assertFalse(guard.passed)

    def test_zero_tolerance_is_the_sensitivity_verdict(self):
        one_more = guards.regression_guard([task("t", 420, (1, 0))])
        self.assertTrue(one_more.passed)
        self.assertFalse(one_more.zero_tolerance)
        same = guards.regression_guard([task("t", 420, (3, 2), (2, 3))])
        self.assertTrue(same.passed and same.zero_tolerance)

    def test_bad_inputs_are_refused(self):
        for args in (("t", 0, (0,), (0,)), ("t", 10, (1, 0), (1,)), ("t", 10, (), ()), ("t", 10, (-1,), (0,)),
                     ("t", 10, (11,), (0,))):
            with self.subTest(args=args), self.assertRaises(ValueError):
                TaskRegressions(*args)
        with self.assertRaises(ValueError):
            guards.regression_guard([])


class FailuresTest(unittest.TestCase):
    def test_every_regression_test_that_did_not_pass_counts(self):
        outcomes = {"a": "pass", "b": "fail", "c": "error", "d": "skip", "x": "fail"}
        self.assertEqual(guards.failures(outcomes, ["a", "b", "c", "d", "e"]), 4)  # e has no outcome
        self.assertEqual(guards.failures(outcomes, ["a"]), 0)


class FeatureAndHeadroomTest(unittest.TestCase):
    def test_the_feature_floor_is_five_points(self):
        self.assertTrue(guards.feature_ok(70.0, 75.0))
        self.assertTrue(guards.feature_ok(math.nextafter(70.0, 0), 75.0))  # a few ulps short
        self.assertFalse(guards.feature_ok(69.9, 75.0))

    def test_headroom_is_the_comparators_mean_and_its_open_tasks(self):
        six = {f"t{i}": 85.0 for i in range(6)}
        self.assertTrue(guards.headroom(six, 15).ok)
        self.assertFalse(guards.headroom({**six, "t0": 85.6}, 15).ok)  # mean 85.1
        self.assertTrue(guards.headroom({**six, "t0": 85.6}, 10).ok)
        self.assertFalse(guards.headroom({f"t{i}": 90.0 + (i == 0) for i in range(6)}, 10).ok)  # mean 90.17

    def test_wins_needed_is_two_thirds_rounded_up(self):
        self.assertEqual((guards.wins_needed(6), guards.wins_needed(8)), (4, 6))

    def test_too_few_caught_tasks_below_100_leave_no_headroom(self):
        three_open = {"a": 40.0, "b": 40.0, "c": 40.0, "d": 100.0, "e": 100.0, "f": 100.0}  # mean 70
        room = guards.headroom(three_open, 15)
        self.assertEqual((room.open_tasks, room.needed), (3, 4))
        self.assertFalse(room.ok)
        self.assertTrue(guards.headroom({**three_open, "d": 99.0}, 15).ok)


@unittest.skipUnless(REPLAY_DATA.is_dir(), "v1's replay records are not on this machine")
class V1EquivalenceTest(unittest.TestCase):
    """v1's records through the new guard give v1's regression change, by the frozen replay.py's own arithmetic."""

    def test_v1_records_give_its_regression_change_and_fail_zero_tolerance(self):
        experiment = EXPERIMENTS["replay"]
        frozen = json.loads((REPO / "eval" / "manifests" / "replay.json").read_text(encoding="utf-8"))
        task_list = pilot.scored_tasks(experiment)
        results = replay.Results(digest=frozen["digest"], caught=[t.id for t in task_list if not t.source.control],
                                 controls=[t.id for t in task_list if t.source.control], arms=["A1", "A3"])
        counts = {}
        for t in task_list:
            expected = t.path("expected.json")
            self.assertEqual(hashlib.sha256(expected.read_bytes()).hexdigest(),
                             frozen["files"][f"eval/tasks/{t.id}/expected.json"])
            regression = t.expected()["regression"]
            for arm in results.arms:
                for rep in experiment.rep_ids:
                    exec_dir = pilot.complete_build(REPLAY_DATA / "runs", t.id, arm, frozen["digest"], rep)
                    self.assertIsNotNone(exec_dir, f"{t.id} {arm} r{rep}")
                    record = records.build(exec_dir / "record.json", frozen["digest"])
                    scores = record["scores"]["replay"]
                    count = guards.failures(scores["outcomes"], regression)
                    self.assertAlmostEqual(count / len(regression), scores["regressions"], places=12)
                    results.builds.setdefault(arm, {}).setdefault(t.id, []).append(record)
                    counts.setdefault((t.id, arm), []).append(count)
        self.assertEqual(sum(len(by_task) for arm in results.builds.values() for by_task in arm.values()), 24)
        guard = guards.regression_guard([
            TaskRegressions(t.id, len(t.expected()["regression"]), tuple(counts[(t.id, "A3")]),
                            tuple(counts[(t.id, "A1")])) for t in task_list])
        v1_change = (replay.arm_mean(results, "A3", replay.REGRESSIONS, results.tasks)
                     - replay.arm_mean(results, "A1", replay.REGRESSIONS, results.tasks))
        self.assertAlmostEqual(float(guard.mean), v1_change, delta=1e-9)
        self.assertEqual(guard.mean, Fraction(1345, 4302))
        self.assertFalse(guard.zero_tolerance)


if __name__ == "__main__":
    unittest.main()
