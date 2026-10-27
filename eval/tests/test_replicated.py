import dataclasses
import json
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from eval.harness import agents, experiments, loop, manifest, openrouter, pilot, replicated, tasks

PILOT2 = experiments.PILOT2
TASKS = list(PILOT2.tasks)
STATIC = {"ruff_violations": 1, "source_loc": 50, "test_loc": 40, "max_function_branches": 4}
OWN = {"collected": 10, "pass_on_reference": 9, "fail_on_reference": 1, "load_errors": 0}


def record(task: str, arm: str, rep: int, primary: float, planted: float, flags=()) -> dict:
    return {"run_id": loop.run_id(task, arm, rep), "task": task, "arm": arm, "rep": rep, "exec": "exec-1",
            "status": "complete", "manifest": "d", "flags": list(flags), "rounds": {"plan": 1, "phase": 2},
            "steps": [{"name": "build", "attempts": [{"seconds": 60.0, "usage": {"input_tokens": 1000,
                                                                                 "output_tokens": 100}}]}],
            "scores": {"primary": primary, "hidden_outcomes": {}, "static": STATIC,
                       "planted": {"fraction": planted, "caught": [], "missed": [], "own_tests": OWN}}}


class DecisionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.root)

    def populate(self, primary: dict[str, list[float]], planted: dict[str, list[float]], flags=None, skip=()):
        """primary/planted: arm -> per-task values, the same for each replicate."""
        for arm in PILOT2.arm_names:
            for index, task in enumerate(TASKS):
                for rep in PILOT2.rep_ids:
                    if (task, arm, rep) in skip:
                        continue
                    path = self.root / "runs" / loop.run_id(task, arm, rep) / "exec-1" / "record.json"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(record(task, arm, rep, primary[arm][index], planted[arm][index],
                                                      (flags or {}).get(arm, ()))))

    def decide(self):
        with mock.patch.object(manifest, "verify", return_value="d"):
            results = replicated.collect(self.root, PILOT2)
            return results, replicated.decide(results, PILOT2)

    def test_correctness_effect_needs_ten_points_and_four_tasks(self):
        base = [0.5] * 5
        self.populate({"A1": base, "A2": [0.62] * 5, "A3": [0.62, 0.62, 0.62, 0.5, 0.5]},
                      {"A1": base, "A2": base, "A3": base})
        _, decision = self.decide()
        self.assertEqual(decision.verdict, "LOOP EFFECT")
        self.assertEqual(len(decision.reasons), 1)
        self.assertTrue(decision.reasons[0].startswith("correctness: A2 beats A1 by +12.0 points, on 5/5"))

    def test_exactly_ten_points_on_four_tasks_fires(self):
        self.populate({"A1": [0.5] * 5, "A2": [0.625, 0.625, 0.625, 0.625, 0.5], "A3": [0.5] * 5},
                      {arm: [0.5] * 5 for arm in PILOT2.arm_names})
        _, decision = self.decide()
        self.assertEqual(decision.verdict, "LOOP EFFECT")

    def test_test_quality_effect_and_its_primary_guard(self):
        planted = {"A1": [0.4] * 5, "A2": [0.6, 0.6, 0.6, 0.6, 0.4], "A3": [0.4] * 5}
        self.populate({"A1": [0.6] * 5, "A2": [0.57] * 5, "A3": [0.6] * 5}, planted)
        _, decision = self.decide()
        self.assertEqual(decision.verdict, "LOOP EFFECT")
        self.assertTrue(decision.reasons[0].startswith("test quality: A2"))
        self.populate({"A1": [0.6] * 5, "A2": [0.54] * 5, "A3": [0.6] * 5}, planted)
        self.assertEqual(self.decide()[1].verdict, "NO LOOP EFFECT")

    def test_ceiling_voids_the_correctness_branch(self):
        self.populate({"A1": [0.9] * 5, "A2": [1.0] * 5, "A3": [0.9] * 5}, {arm: [0.5] * 5 for arm in PILOT2.arm_names})
        results, decision = self.decide()
        self.assertEqual((decision.verdict, decision.ceiling), ("NO LOOP EFFECT", True))
        self.assertIn("uninformative", replicated.render(results, decision, PILOT2))

    def test_a_missing_replicate_is_incomplete(self):
        self.populate({arm: [0.5] * 5 for arm in PILOT2.arm_names}, {arm: [0.5] * 5 for arm in PILOT2.arm_names},
                      skip={(TASKS[2], "A3", 2)})
        results, decision = self.decide()
        self.assertEqual(decision.verdict, "INCOMPLETE")
        self.assertEqual(results.missing, [f"build {TASKS[2]}--A3--r2"])

    def test_a_routing_violation_withholds_even_a_winning_verdict(self):
        self.populate({"A1": [0.5] * 5, "A2": [0.9] * 5, "A3": [0.9] * 5}, {arm: [0.5] * 5 for arm in PILOT2.arm_names},
                      flags={"A2": ["unrouted_model:build:claude-haiku-5-5"]})
        _, decision = self.decide()
        self.assertEqual(decision.verdict, "INCOMPLETE")
        self.assertIn("routing violation", decision.reasons[0])

    def test_render_shows_cells_spend_and_band(self):
        self.populate({"A1": [0.9] * 5, "A2": [0.9] * 5, "A3": [0.8] * 5}, {arm: [0.5] * 5 for arm in PILOT2.arm_names})
        (self.root / pilot.SPEND_LEDGER).write_text(json.dumps({"scored": {"baseline": 1.0, "readings": [1.0, 4.5]}}))
        results, decision = self.decide()
        text = replicated.render(results, decision, PILOT2)
        self.assertIn("| A3 | 80.0 ± 0.0 |", text)
        self.assertIn("$3.50", text)
        self.assertIn("A3 − A2 (descriptive): -10.0", text)
        self.assertIn("fell outside the calibration band", text)


class CostTest(unittest.TestCase):
    def test_codex_counts_cached_inside_input_claude_apart(self):
        price = openrouter.PRICES["z-ai/glm-4.7-flash"]
        codex = replicated.session_cost("z-ai/glm-4.7-flash",
                                        {"input_tokens": 1_000_000, "cached_input_tokens": 400_000, "output_tokens": 0})
        self.assertAlmostEqual(codex, 0.6 * price.input + 0.4 * price.cache_read)
        claude = replicated.session_cost("z-ai/glm-4.7-flash", {"input_tokens": 600_000, "cache_read_input_tokens":
                                                                400_000, "output_tokens": 1_000_000})
        self.assertAlmostEqual(claude, 0.6 * price.input + 0.4 * price.cache_read + price.output)


class EndToEndTest(unittest.TestCase):
    """A stub-agent pilot2 through the real schedule, loop, planted score and replicated report — including
    a failed job that leaves a replicate missing, and the resume that fills it."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.task = tasks.Task(self.root / "tasks" / "dev-planted")
        shutil.copytree(tasks.TASKS_ROOT / "dev-intervals" / "reference", self.task.path("reference"))
        shutil.copy(tasks.TASKS_ROOT / "dev-intervals" / "expected.json", self.task.path("expected.json"))
        shutil.copy(tasks.TASKS_ROOT / "dev-intervals" / "TASK.md", self.task.path("TASK.md"))
        shutil.copytree(tasks.TASKS_ROOT / "dev-intervals" / "hidden", self.task.path("hidden"))
        reference = self.task.path("reference/intervals.py").read_text()
        for name, old, new in (("no-adjacent", "merged[-1][1] + 1", "merged[-1][1]"),
                               ("no-validation", "if start > end:", "if False:")):
            target = self.task.path(f"mutants/{name}/intervals.py")
            target.parent.mkdir(parents=True)
            target.write_text(reference.replace(old, new))
        self.experiment = dataclasses.replace(PILOT2, tasks=("dev-planted",), reps=2, parallel=1, budget_usd=100.0)
        self.fail_once = {("A3", "r2")}

    def writer(self, model, prompt, *, role, cwd, out_dir, provider=agents.NATIVE, offline=False):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "transcript.jsonl").touch()
        if role == agents.REVIEWER:
            return agents.Session(agents.COMPLETED, "ok", "VERDICT: GREEN\nISSUES:\nNone\n", out_dir / "transcript.jsonl",
                                  0.1, reported_models=[model])
        if "Implement TASK.md" in prompt or "Build the plan" in prompt:
            shutil.copy(self.task.path("reference/intervals.py"), cwd / "intervals.py")
            # Catches the adjacency bug only.
            (cwd / "test_intervals.py").write_text(textwrap.dedent("""
                import unittest
                from intervals import merge
                class T(unittest.TestCase):
                    def test_adjacent(self):
                        self.assertEqual(merge([(1, 2), (3, 4)]), [(1, 4)])
            """))
        return agents.Session(agents.COMPLETED, "ok", "done", out_dir / "transcript.jsonl", 0.1,
                              reported_models=[model])

    def run_builds(self):
        real_execute = loop.execute_run

        def execute(task, arm, runs_root, *, rep=None, manifest_hash, offline=False):
            if (arm.name, f"r{rep}") in self.fail_once:
                self.fail_once.discard((arm.name, f"r{rep}"))
                return {"run_id": loop.run_id(task.id, arm.name, rep), "exec": "exec-0", "status": "invalid"}
            return real_execute(task, arm, runs_root, rep=rep, manifest_hash=manifest_hash, offline=offline)

        with mock.patch.object(pilot, "scored_tasks", lambda experiment: [self.task]), \
                mock.patch.object(manifest, "verify", return_value="d"), \
                mock.patch.object(openrouter, "credit_used", return_value=0.0), \
                mock.patch.object(openrouter, "scrub", return_value=[]), \
                mock.patch.object(pilot.loop, "execute_run", execute), \
                mock.patch.object(agents, "run_claude", self.writer), \
                mock.patch.object(agents, "run_codex", self.writer):
            return pilot.run_builds(self.root / "data", self.experiment)

    def report(self):
        with mock.patch.object(manifest, "verify", return_value="d"):
            results = replicated.collect(self.root / "data", self.experiment)
            decision = replicated.decide(results, self.experiment)
            return results, decision, replicated.render(results, decision, self.experiment)

    def test_resume_fills_the_missing_replicate(self):
        self.assertEqual(self.run_builds(), 1)
        results, decision, _ = self.report()
        self.assertEqual((decision.verdict, results.missing), ("INCOMPLETE", ["build dev-planted--A3--r2"]))

        self.assertEqual(self.run_builds(), 0)
        results, decision, text = self.report()
        self.assertEqual(results.missing, [])
        self.assertEqual(decision.verdict, "NO LOOP EFFECT")
        for arm in self.experiment.arm_names:
            for record in results.builds[arm]["dev-planted"]:
                self.assertEqual(record["scores"]["primary"], 1.0)
                self.assertEqual(record["scores"]["planted"]["caught"], ["no-adjacent"])
                self.assertEqual(record["scores"]["planted"]["missed"], ["no-validation"])
        self.assertIn("| A1 | 50.0 ± 0.0 | 50.0 |", text)


if __name__ == "__main__":
    unittest.main()
