import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval.harness import manifest, pilot, report

TASKS = [task.id for task in pilot.pilot_tasks()]
STATIC = {"ruff_violations": 1, "source_loc": 50, "test_loc": 40, "max_function_branches": 4}


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.root)
        patcher = mock.patch.object(manifest, "verify", return_value="d")
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, path: Path, data: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def populate(self, primary: dict, judged: dict[str, dict[str, str]], calibration=None):
        """primary: arm -> pass fraction (one for every task, or task -> fraction);
        judged: judge -> pair -> result for every task."""
        for arm in pilot.BUILD_ARMS:
            for task in TASKS:
                exec_dir = self.root / "runs" / f"{task}--{arm}" / "exec-1"
                identity = {"run_id": f"{task}--{arm}", "task": task, "arm": arm}
                (exec_dir / "artifact").mkdir(parents=True, exist_ok=True)
                (exec_dir / "artifact" / "m.py").write_text(f"# {task} {arm}\n")
                self.write(exec_dir / "record.json", {
                    **identity, "status": "complete", "manifest": "d", "steps": [], "rounds": {}, "flags": [],
                    "scores": {"primary": primary[arm][task] if isinstance(primary[arm], dict) else primary[arm],
                               "static": STATIC, "hidden_outcomes": {}}})
                if arm in pilot.FOLLOWUP_ARMS:
                    self.write(exec_dir / "followup" / "record.json", {
                        **identity, "status": "complete", "manifest": "d",
                        "scores": {"new_fraction": 1.0, "regressions": [], "persistent_failures": [], "repaired": []}})
        calibration = calibration or {}
        for name in pilot.judge.JUDGES:
            for task in TASKS:
                for pair in (None, *pilot.PAIRS):
                    if pair is None:
                        result, builds = ("a" if calibration.get(name, True) else "tie"), None
                    else:
                        result = judged.get(name, {}).get("-".join(pair), "tie")
                        builds = {side: pilot.build_identity(self.root / "runs" / f"{task}--{arm}" / "exec-1")
                                  for side, arm in zip(("a", "b"), pair)}
                    self.write(pilot.judging_dir(self.root, name, task, pair) / "judgement.json",
                               {"manifest": "d", "result": result, "builds": builds,
                                "leak_words": {"a": 0, "b": 2}})

    def decide(self):
        results = report.collect(self.root)
        decision = report.decide(results)
        report.render(results, decision)
        return decision

    def test_primary_gain_is_go(self):
        self.populate({"A1": 0.7, "A1r": 0.7, "A2": 0.8, "A3": 0.7}, {})
        decision = self.decide()
        self.assertEqual(decision.verdict, "GO")
        self.assertIn("A2 beats A1 on the primary by +10.0 points", decision.reasons)

    def test_small_gain_is_no_go(self):
        self.populate({"A1": 0.7, "A1r": 0.7, "A2": 0.79, "A3": 0.7}, {})
        self.assertEqual(self.decide().verdict, "NO-GO")

    def test_judge_agreed_wins_are_go(self):
        both = {"A1-A3": "b"}
        self.populate({"A1": 0.8, "A1r": 0.8, "A2": 0.8, "A3": 0.76}, {"fable": both, "astra": both})
        decision = self.decide()
        self.assertEqual(decision.verdict, "GO")
        self.assertEqual(report.agreed_wins(report.collect(self.root), "A3"), 5)

    def test_judge_branch_off_when_a_judge_fails_calibration(self):
        both = {"A1-A3": "b"}
        self.populate({"A1": 0.8, "A1r": 0.8, "A2": 0.8, "A3": 0.8}, {"fable": both, "astra": both},
                      calibration={"astra": False})
        self.assertEqual(self.decide().verdict, "NO-GO")

    def test_judge_branch_needs_primary_within_limit(self):
        both = {"A1-A2": "b"}
        self.populate({"A1": 0.8, "A1r": 0.8, "A2": 0.74, "A3": 0.8}, {"fable": both, "astra": both})
        self.assertEqual(self.decide().verdict, "NO-GO")

    def test_judges_disagreeing_is_not_a_win(self):
        self.populate({"A1": 0.8, "A1r": 0.8, "A2": 0.8, "A3": 0.8},
                      {"fable": {"A1-A2": "b"}, "astra": {"A1-A2": "a"}})
        self.assertEqual(self.decide().verdict, "NO-GO")

    def test_missing_record_is_incomplete(self):
        self.populate({"A1": 0.7, "A1r": 0.7, "A2": 0.9, "A3": 0.7}, {})
        shutil.rmtree(self.root / "runs" / f"{TASKS[0]}--A2")
        self.assertEqual(self.decide().verdict, "INCOMPLETE")

    def test_sensitivity_reports_flip(self):
        self.populate({"A1": 0.6, "A1r": 0.75, "A2": 0.7, "A3": 0.6}, {})
        decision = self.decide()
        self.assertEqual(decision.verdict, "GO")
        self.assertEqual(decision.primary_sensitivity, "changes with A1r in place of A1")


    def test_exact_threshold_with_real_denominators_is_go(self):
        # +7/14 on bill-split is exactly +10 arm-mean points; with these real denominators
        # the float difference of means is 9.999999999999998.
        sizes = {task.id: len(task.expected()["hidden"]) for task in pilot.pilot_tasks()}
        a1 = {task: 0.0 for task in sizes}
        a1["token-bucket"] = 9 / sizes["token-bucket"]
        a2 = {**a1, "bill-split": 7 / sizes["bill-split"]}
        self.populate({"A1": a1, "A1r": a1, "A2": a2, "A3": a1}, {})
        self.assertEqual(self.decide().verdict, "GO")

    def test_sensitivity_compares_the_whole_trigger(self):
        self.populate({"A1": 0.6, "A1r": 0.65, "A2": 0.7, "A3": 0.8}, {})
        self.assertEqual(self.decide().primary_sensitivity, "unchanged with A1r in place of A1")

    def test_judgement_of_a_replaced_build_is_not_reused(self):
        self.populate({"A1": 0.7, "A1r": 0.7, "A2": 0.7, "A3": 0.7}, {"fable": {"A1-A2": "b"}, "astra": {"A1-A2": "b"}})
        self.assertEqual(self.decide().verdict, "GO")
        (self.root / "runs" / f"{TASKS[0]}--A2" / "exec-1" / "artifact" / "m.py").write_text("# rebuilt\n")
        decision = self.decide()
        self.assertEqual(decision.verdict, "INCOMPLETE")

    def test_malformed_nested_fields_are_incomplete(self):
        self.populate({"A1": 0.7, "A1r": 0.7, "A2": 0.9, "A3": 0.7}, {})
        attempt = {"seconds": 1.0, "usage": {"input_tokens": None}}
        for patch in ({"steps": [{}]}, {"flags": [None]}, {"rounds": {"plan": "x"}},
                      {"steps": [{"attempts": [attempt]}]},
                      {"steps": [{"attempts": [{"seconds": float("nan"), "usage": {}}]}]},
                      {"run_id": None}):
            with self.subTest(patch=patch):
                record = self.root / "runs" / f"{TASKS[0]}--A3" / "exec-1" / "record.json"
                data = json.loads(record.read_text())
                self.write(record, {**data, **patch})
                self.assertEqual(self.decide().verdict, "INCOMPLETE")
                self.assertIsNone(pilot.complete_build(self.root / "runs", TASKS[0], "A3", "d"))
                self.write(record, data)

    def test_malformed_record_is_incomplete(self):
        self.populate({"A1": 0.7, "A1r": 0.7, "A2": 0.9, "A3": 0.7}, {})
        record = self.root / "runs" / f"{TASKS[0]}--A3" / "exec-1" / "record.json"
        self.write(record, {"status": "complete", "manifest": "d"})
        decision = self.decide()
        self.assertEqual(decision.verdict, "INCOMPLETE")


class ScheduleTest(unittest.TestCase):
    def test_schedules_are_complete_and_seeded(self):
        builds = pilot.build_schedule()
        self.assertEqual(len(builds), len(TASKS) * 4)
        self.assertEqual(builds, pilot.build_schedule())
        self.assertNotEqual(builds, sorted(builds))
        self.assertEqual(len(pilot.judge_schedule()), 2 * len(TASKS) * 4)


if __name__ == "__main__":
    unittest.main()
