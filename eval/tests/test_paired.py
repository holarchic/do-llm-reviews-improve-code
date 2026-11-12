import unittest
from unittest import mock

from eval.__main__ import main
from eval.harness import experiments, ledger, loop, manifest, paired
from eval.tests.test_cells import CellCase
from eval.tests.test_source import REFERENCE_CALC

EXPERIMENT = experiments.PLAIN_REVIEW
REGRESSION = [f"tests.test_base.BaseTest.test_{n}" for n in range(155)]
CAUGHT = ["t0", "t1", "t2", "t3"]


def avoided(count: int, of: int = 20) -> dict[str, bool]:
    """`of` defects, the first `count` avoided: review is count/of."""
    return {f"d{n}": n < count for n in range(of)}


def scores(defects: dict[str, bool], feature: float = 1.0, failed: int = 0) -> dict:
    """A replay score. `defects` maps each defect to avoided and is empty for a control; the first `failed`
    of the 155 regression tests fail."""
    listed = [{"id": name, "avoided": kept} for name, kept in defects.items()]
    return {"replay": {"review": sum(d["avoided"] for d in listed) / len(listed) if listed else None,
                       "feature": feature, "regressions": failed / len(REGRESSION), "defects": listed,
                       "outcomes": {test: "fail" if n < failed else "pass" for n, test in enumerate(REGRESSION)}}}


def record(arm: str, task: str, scored: dict, rep: int | None = None) -> dict:
    return {"run_id": loop.run_id(task, arm, rep), "task": task, "arm": arm, "rep": rep, "flags": [],
            "scores": scored, "review_text": "1. calc.py:1: wrong\n", "fix_text": "1: fixed, guarded\n"}


def results(builds: dict[str, dict], cell_scores: dict[str, dict[str, list[dict]]], controls=()) -> paired.Results:
    """builds: task -> its build's score. cell_scores: cell arm -> task -> one score per run."""
    built = paired.Results(digest="d", baseline="none", cell_arms=list(cell_scores),
                           caught=[task for task in builds if task not in controls], controls=list(controls))
    built.costs["none"] = paired.RunCosts()
    for task, scored in builds.items():
        built.builds[task] = record("none", task, scored)
        built.regression_tests[task] = REGRESSION
    for arm, by_task in cell_scores.items():
        built.costs[arm] = paired.RunCosts()
        for task, runs in by_task.items():
            built.cells.setdefault(arm, {})[task] = [record(arm, task, scored, rep) for rep, scored in enumerate(runs, 1)]
    return built


def uniform(build: int, cell: int, tasks=CAUGHT) -> paired.Results:
    """Every caught task's build avoids `build` of 20 defects, and both runs of both cells avoid `cell`."""
    return results({task: scores(avoided(build)) for task in tasks},
                   {arm: {task: [scores(avoided(cell))] * 2 for task in tasks} for arm in ("same", "other")})


def same(decision: paired.Decision) -> paired.Comparison:
    return decision.comparisons[0]


def decide(built: paired.Results) -> paired.Decision:
    return paired.decide(built, EXPERIMENT)


class GainTest(unittest.TestCase):
    def test_exactly_fifteen_points_on_enough_tasks_is_met(self):
        decision = decide(uniform(8, 11))
        self.assertEqual(decision.verdict, paired.DECIDED)
        self.assertAlmostEqual(same(decision).gain, 15.0)
        self.assertEqual((same(decision).wins, same(decision).needed, same(decision).gain_met), (4, 3, True))

    def test_a_hair_under_fifteen_is_not_met(self):
        built = uniform(8, 11)
        built.cells["same"]["t3"][1] = record("same", "t3", scores(avoided(10)), 2)
        decision = decide(built)
        self.assertAlmostEqual(same(decision).gain, 14.375)
        self.assertEqual((same(decision).wins, same(decision).gain_met), (4, False))
        self.assertTrue(decision.comparisons[1].gain_met)  # the other cell is judged on its own runs

    def test_the_gain_must_show_on_two_thirds_of_the_caught_tasks(self):
        tasks = [f"t{n}" for n in range(6)]

        def cells_winning(winners: int, cell: int) -> paired.Results:
            return results({task: scores(avoided(8)) for task in tasks},
                           {"same": {task: [scores(avoided(cell if n < winners else 8))] * 2
                                     for n, task in enumerate(tasks)}})

        enough, one_short = decide(cells_winning(4, 14)), decide(cells_winning(3, 16))
        self.assertAlmostEqual(same(enough).gain, 20.0)
        self.assertAlmostEqual(same(one_short).gain, 20.0)
        self.assertEqual((same(enough).wins, same(enough).needed, same(enough).gain_met), (4, 4, True))
        self.assertEqual((same(one_short).wins, same(one_short).gain_met), (3, False))

    def test_each_cell_is_compared_with_its_own_tasks_build(self):
        built = results({"t0": scores(avoided(8)), "t1": scores(avoided(16))},
                        {"same": {"t0": [scores(avoided(18))] * 2, "t1": [scores(avoided(18))] * 2}})
        self.assertAlmostEqual(same(decide(built)).gain, 30.0)  # +50 and +10, never 18 against another task's build


class HarmTest(unittest.TestCase):
    def with_feature(self, feature: float) -> paired.Comparison:
        built = uniform(8, 11)
        for task in CAUGHT:
            built.cells["same"][task] = [record("same", task, scores(avoided(11), feature=feature), rep)
                                         for rep in (1, 2)]
        return same(decide(built))

    def test_the_feature_floor_is_five_points(self):
        at_floor, below = self.with_feature(0.95), self.with_feature(0.94)
        self.assertEqual((at_floor.feature_ok, at_floor.harm_ok, at_floor.gain_met), (True, True, True))
        self.assertEqual((below.feature_ok, below.harm_ok, below.gain_met), (False, False, True))

    def with_failures(self, runs: tuple[int, int], task: str = "t0", controls=()) -> paired.Comparison:
        """Every build fails 2 regression tests; `task`'s `same` runs fail `runs` of them, the rest stay at 2."""
        built = results({name: scores({} if name in controls else avoided(8), failed=2) for name in ("t0", "t1", *controls)},
                        {"same": {name: [scores({} if name in controls else avoided(11), failed=2)] * 2
                                  for name in ("t0", "t1", *controls)}}, controls)
        defects = {} if task in controls else avoided(11)
        built.cells["same"][task] = [record("same", task, scores(defects, failed=count), rep)
                                     for rep, count in enumerate(runs, 1)]
        return same(decide(built))

    def test_three_extra_failures_over_two_runs_pass_a_155_test_task_and_four_fail(self):
        three, four = self.with_failures((4, 3)), self.with_failures((4, 4))
        self.assertEqual((three.regressions.passed, three.harm_ok, three.regressions.over_cap), (True, True, ()))
        self.assertEqual((four.regressions.passed, four.harm_ok, four.regressions.over_cap), (False, False, ("t0",)))

    def test_the_builds_own_failures_are_the_comparator(self):
        unchanged = self.with_failures((2, 2))
        self.assertEqual(unchanged.regressions.mean, 0)
        self.assertEqual([task.comparator for task in unchanged.regressions.tasks], [(2, 2), (2, 2)])

    def test_each_tasks_regressions_are_compared_with_its_own_build(self):
        built = results({"t0": scores(avoided(8), failed=5), "t1": scores(avoided(8))},
                        {"same": {"t0": [scores(avoided(11), failed=5)] * 2, "t1": [scores(avoided(11))] * 2}})
        guard = same(decide(built)).regressions
        self.assertEqual((guard.mean, guard.passed), (0, True))
        self.assertEqual([task.comparator for task in guard.tasks], [(5, 5), (0, 0)])

    def test_a_control_with_broken_tests_fails_harm_and_leaves_gain_alone(self):
        harmed = self.with_failures((12, 12), task="k0", controls=("k0",))
        self.assertEqual((harmed.gain_met, harmed.regressions.over_cap, harmed.harm_ok), (True, ("k0",), False))
        self.assertAlmostEqual(harmed.gain, 15.0)


class HeadroomAndCompletenessTest(unittest.TestCase):
    def test_a_build_mean_of_85_leaves_room_and_one_above_does_not(self):
        at_limit = decide(uniform(17, 20))
        self.assertEqual((at_limit.verdict, at_limit.headroom.ok, same(at_limit).gain_met), (paired.DECIDED, True, True))
        built = uniform(17, 20)
        built.builds["t0"] = record("none", "t0", scores(avoided(18)))
        above = decide(built)
        self.assertEqual((above.verdict, above.headroom.ok, above.comparisons), (paired.UNINFORMATIVE, False, ()))
        self.assertEqual(len(above.estimates), len(paired.ENDPOINTS))  # still described, with no statement

    def test_a_missing_or_mispaired_cell_or_no_caught_task_is_incomplete(self):
        missing, mispaired = uniform(8, 11), uniform(8, 11)
        missing.missing.append("t0--same--r2")
        mispaired.invalid.append("t1--other--r1")
        controls_only = results({"k0": scores({})}, {"same": {"k0": [scores({})] * 2}}, controls=("k0",))
        for built, said in ((missing, "scheduled runs missing: 1"), (mispaired, "cells paired with another build: 1"),
                            (controls_only, "no caught task")):
            decision = decide(built)
            text = paired.render(built, decision, EXPERIMENT)
            self.assertEqual((decision.verdict, decision.comparisons), (paired.INCOMPLETE, ()))
            self.assertIn(f"**Verdict:** INCOMPLETE, no statement — {said}", text)
            self.assertNotIn("gain", text)
        self.assertIn("## Missing\n\n- t0--same--r2", paired.render(missing, decide(missing), EXPERIMENT))
        self.assertIn("## Paired with another build\n\n- t1--other--r1",
                      paired.render(mispaired, decide(mispaired), EXPERIMENT))


class EstimateTest(unittest.TestCase):
    def differing(self) -> paired.Results:
        """`other` minus `same` on review is +10, 0, −5 and +15 points over the four tasks."""
        other = dict(zip(CAUGHT, (12, 10, 9, 13)))
        return results({task: scores(avoided(8)) for task in CAUGHT},
                       {"same": {task: [scores(avoided(10))] * 2 for task in CAUGHT},
                        "other": {task: [scores(avoided(other[task]))] * 2 for task in CAUGHT}})

    def test_the_mean_the_range_and_a_repeatable_interval(self):
        built = self.differing()
        review = decide(built).estimates[0]
        self.assertEqual((review.endpoint, review.tasks), (paired.REVIEW, 4))
        self.assertAlmostEqual(review.mean, 5.0)
        self.assertAlmostEqual(review.leave_one_out[0], 5 / 3)
        self.assertAlmostEqual(review.leave_one_out[1], 25 / 3)
        low, high = review.interval
        self.assertTrue(-5.0 <= low < 5.0 < high <= 15.0, review.interval)
        self.assertEqual(decide(built).estimates[0].interval, review.interval)

    def test_the_spread_between_a_cells_runs(self):
        built = self.differing()
        built.cells["other"]["t0"] = [record("other", "t0", scores(avoided(count)), rep)
                                      for rep, count in enumerate((12, 14), 1)]
        spread = decide(built).run_spread
        self.assertEqual(list(spread), ["same", "other"])
        self.assertAlmostEqual(spread["same"], 0.0)
        self.assertAlmostEqual(spread["other"], 2.5)

    def test_removed_and_brought_back_are_shares_of_what_the_build_shipped_and_avoided(self):
        build = record("none", "t0", scores(avoided(8)))  # avoids d0..d7, ships d8..d19
        cell = record("same", "t0", scores({f"d{n}": 2 <= n < 13 for n in range(20)}), 1)  # avoids d2..d12
        self.assertAlmostEqual(paired.value(cell, build, paired.REMOVED), 100 * 5 / 12)
        self.assertAlmostEqual(paired.value(cell, build, paired.BROUGHT_BACK), 100 * 2 / 8)

    def test_an_endpoint_with_no_eligible_task_is_unavailable_not_zero(self):
        built = uniform(0, 3)  # no build avoided anything: nothing can be brought back
        by_endpoint = {estimate.endpoint: estimate for estimate in decide(built).estimates}
        brought_back = by_endpoint[paired.BROUGHT_BACK]
        self.assertEqual((brought_back.tasks, brought_back.mean, brought_back.interval, brought_back.leave_one_out),
                         (0, None, None, None))
        self.assertEqual((by_endpoint[paired.REMOVED].tasks, by_endpoint[paired.REMOVED].mean), (4, 0.0))
        self.assertIsNone(paired.value(built.cells["same"]["t0"][0], built.builds["t0"], paired.BROUGHT_BACK))
        text = paired.render(built, decide(built), EXPERIMENT)
        self.assertIn("| Defects brought back | 0 | unavailable | unavailable | unavailable |", text)
        self.assertIn("| same | 15.0 | 4 | unavailable | 0 |", text)

    def test_one_eligible_task_gives_a_mean_and_no_interval(self):
        built = uniform(0, 3)
        built.builds["t0"] = record("none", "t0", scores(avoided(4)))
        built.cells["other"]["t0"] = [record("other", "t0", scores(avoided(2)), rep) for rep in (1, 2)]
        brought_back = {e.endpoint: e for e in decide(built).estimates}[paired.BROUGHT_BACK]
        self.assertEqual((brought_back.tasks, brought_back.interval, brought_back.leave_one_out), (1, None, None))
        self.assertAlmostEqual(brought_back.mean, 25.0)  # other shipped 2 of the 4 the build avoided; same 1 of 4


class FixReportTest(unittest.TestCase):
    def test_closing_lines_are_counted_by_finding(self):
        cases = {
            "1: fixed, guarded\n2: left, upstream validates it\n": paired.FixReport(1, 1),
            "Done.\n\n- **1.** Fixed: the divisor\n- 2) left (not a defect)\n3 - FIXED\n": paired.FixReport(2, 1),
            "1: left, unsure\nLooked again.\n1: fixed, it was real\n": paired.FixReport(1, 0),
            "Finding 4: fixed\n": paired.FixReport(1, 0),
            "Finding #1: fixed\nFinding #2 — left, intended\n": paired.FixReport(1, 1),
            "1 fixed — punctuation now has to match too.\n": paired.FixReport(1, 0),
        }
        for text, expected in cases.items():
            with self.subTest(text):
                self.assertEqual(paired.fix_report(text), expected)

    def test_no_findings_is_said_and_silence_is_unavailable(self):
        self.assertEqual(paired.fix_report("Nothing to change.\n\nno findings\n"), paired.FixReport(0, 0))
        self.assertEqual(paired.fix_report("**No findings.**"), paired.FixReport(0, 0))
        for silent in ("", "I fixed everything the reviewer raised.\n", "There were 3 tests fixed by this.\n"):
            with self.subTest(silent):
                self.assertIsNone(paired.fix_report(silent))


class RenderTest(unittest.TestCase):
    def decided(self) -> paired.Results:
        built = uniform(8, 11)
        built.cells["same"]["t0"][0]["fix_text"] = "1: fixed, guarded\n2: left, not a defect\n"
        built.cells["other"]["t0"][0]["fix_text"] = "no findings\n"
        timed_out = built.cells["other"]["t1"][0]
        timed_out["fix_text"], timed_out["flags"] = "", ["timeout:fix"]
        built.cells["other"]["t2"][1]["flags"] = ["timeout:review", "reviewer_wrote:review"]
        built.costs["same"] = paired.RunCosts(runs=8, scored=ledger.Totals(cost=ledger.Cost(12.0, 24.0), seconds=4800.0,
                                                                           attempts=16, unknown=1, ranged=2))
        built.costs["same"].unscored = ledger.Totals(cost=ledger.Cost(0.5, 0.5), attempts=3, unknown=2)
        return built

    def test_the_statements_and_every_table_carry_the_fixtures_numbers(self):
        built = self.decided()
        text = paired.render(built, decide(built), EXPERIMENT)
        expected = [
            "# Eval plain-review report",
            "- **same vs none:** gain met — review +15.0 points (needs +15), higher on 4/4 caught tasks (needs 3); "
            "harm within tolerance — feature +0.0 points (floor −5), existing tests broken mean Δ +0.00 (limit 0.5), "
            "tasks over the 1.0 cap: none",
            "- Cells: same, other, each run 2 times on its task's one none build",
            "| t0 | 40.0 | 55.0 (55.0, 55.0) | 55.0 (55.0, 55.0) |",
            "| Mean, caught tasks | 40.0 | 55.0 | 55.0 |",
            "| Mean, caught tasks | 100.0 | 100.0 | 100.0 |",
            "| Mean, all tasks | 0.0 | 0.0 | 0.0 |",
            "| same | 25.0 | 4 | 0.0 | 4 |",
            "| t0 | d0 | avoided | 2/2 | 2/2 |",
            "| t0 | d10 | shipped | 2/2 | 2/2 |",
            "| t0 | d11 | shipped | 0/2 | 0/2 |",
            "| same | 8 | 8 | 8 | 1 | 0 | 0 |",
            "| other | 8 | 6 | 6 | 0 | 1 | 1 |",
            "| none | 0 | 0 | $0.00 | 0 / 0 / 0 | — | — |",
            "| same | 8 | 80 | $12.00–24.00 | 1 / 2 / 0 | $0.50 (3 attempts, 2 unknown) | $0.50–1.00 (24 removed) |",
            "| other | 0 | 0 | $0.00 | 0 / 0 / 0 | — | $0.00 (24 removed) |",
            "| same | 0 | 0 | 0 |",
            "| other | 1 | 1 | 1 |",
            "## other minus same (an estimate, no verdict)",
            "| Review-caught defects avoided | 4 | +0.0 | +0.0 to +0.0 | +0.0 to +0.0 |",
            "- Between a cell's runs on the same build, review differs by 0.0 points on average for other.",
        ]
        for line in expected:
            self.assertIn(line, text)

    def test_a_cell_arm_that_removed_nothing_has_no_cost_per_removal(self):
        built = results({task: scores(avoided(8)) for task in CAUGHT},
                        {"same": {task: [scores(avoided(11))] * 2 for task in CAUGHT},
                         "other": {task: [scores(avoided(8))] * 2 for task in CAUGHT}})
        text = paired.render(built, decide(built), EXPERIMENT)
        self.assertIn("| other | 0 | 0 | $0.00 | 0 / 0 / 0 | — | unavailable |", text)
        self.assertIn("**other vs none:** gain not met — review +0.0 points", text)

    def test_an_uninformative_report_describes_and_states_nothing(self):
        built = uniform(18, 20)
        text = paired.render(built, decide(built), EXPERIMENT)
        self.assertIn("**Verdict:** uninformative, no statement — the builds' review mean is 90.0 (limit 85.0) with 4 "
                      "caught tasks below 100 (3 needed): no room for a +15 gain", text)
        self.assertNotIn("vs none:**", text)
        self.assertIn("| t0 | 90.0 | 100.0 (100.0, 100.0) | 100.0 (100.0, 100.0) |", text)


class ScheduleToReportTest(CellCase):
    """The real schedule with stub sessions, then the report from what it saved."""

    def report(self) -> tuple[paired.Results, paired.Decision, str]:
        with self.frozen():
            built = paired.collect(self.tmp, self.experiment)
            decision = paired.decide(built, self.experiment)
            return built, decision, paired.render(built, decision, self.experiment)

    def test_a_finished_run_reports_its_gain_and_counts_what_it_cannot_price(self):
        failed = self.run_with_one_failed_review()
        failed_arm = failed.split("--")[1]
        built, decision, text = self.report()
        self.assertEqual((decision.verdict, built.missing), (paired.INCOMPLETE, [failed]))
        self.assertEqual(self.run_cells(), 0)
        built, decision, text = self.report()
        self.assertEqual((built.missing, built.invalid, decision.verdict), ([], [], paired.DECIDED))
        self.assertTrue(all(c.gain_met and c.harm_ok for c in decision.comparisons))
        self.assertIn("**same vs none:** gain met — review +100.0 points", text)
        self.assertIn("| fixture | 0.0 | 100.0 (100.0, 100.0) | 100.0 (100.0, 100.0) |", text)
        self.assertIn("| fixture | zero | shipped | 2/2 | 2/2 |", text)
        # Stub sessions report no usage: every attempt is unknown, and none of it reads as a known $0.
        self.assertEqual({arm: (costs.runs, costs.scored.attempts, costs.scored.unknown) for arm, costs in built.costs.items()},
                         {"none": (1, 1, 1), "same": (2, 4, 4), "other": (2, 4, 4)})
        self.assertIn("| none | 1 | 0 | $0.00 | 1 / 0 / 0 | — | — |", text)
        for arm in ("same", "other"):
            unscored = "$0.00 (1 attempts, 1 unknown)" if arm == failed_arm else "—"
            self.assertIn(f"| {arm} | 2 | 0 | $0.00 | 4 / 0 / 0 | {unscored} | $0.00 (2 removed) |", text)
        self.assertIn("| same | 2 | 2 | 2 | 0 | 0 | 0 |", text)

    def test_cells_that_ran_on_a_replaced_build_make_the_report_incomplete(self):
        self.assertEqual(self.run_cells(), 0)
        self.build_calc = REFERENCE_CALC
        self.build()  # a newer complete build of the task
        built, decision, text = self.report()
        self.assertEqual(sorted(built.invalid), ["fixture--other--r1", "fixture--other--r2", "fixture--same--r1",
                                                 "fixture--same--r2"])
        self.assertEqual((decision.verdict, built.cells), (paired.INCOMPLETE, {}))
        self.assertIn("cells paired with another build: 4", text)

    def test_the_report_command_writes_the_paired_report(self):
        self.assertEqual(self.run_cells(), 0)
        with self.frozen(), mock.patch("eval.__main__.DATA_ROOT", self.tmp / "data"):
            (self.tmp / "data").mkdir()
            (self.tmp / "data" / "plain-review").symlink_to(self.tmp)
            with mock.patch.dict(experiments.EXPERIMENTS, {"plain-review": self.experiment}):
                self.assertEqual(main(["report", "--experiment", "plain-review"]), 0)
        self.assertIn("**same vs none:** gain met", (self.tmp / "report.md").read_text())

    def test_an_unfrozen_experiment_has_no_report(self):
        with mock.patch.object(manifest, "MANIFESTS", self.tmp / "no-manifests"), \
             self.assertRaises(manifest.ManifestMismatch):
            paired.collect(self.tmp, self.experiment)


if __name__ == "__main__":
    unittest.main()
