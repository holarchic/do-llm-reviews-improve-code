import dataclasses
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval.harness import agents, experiments, loop, manifest, openrouter, pilot, tasks

DEV = tasks.Task(tasks.TASKS_ROOT / "dev-intervals")
PILOT2 = experiments.PILOT2
KEY = "sk-or-v1-SYNTHETIC-pilot2-test"


class ShapeTest(unittest.TestCase):
    def test_run_ids_carry_the_replicate_only_when_there_is_one(self):
        self.assertEqual(loop.run_id("t", "A1"), "t--A1")
        self.assertEqual(loop.run_id("t", "A1", 2), "t--A1--r2")

    def test_pilot2_schedule_covers_every_cell_once(self):
        jobs = pilot.build_schedule(PILOT2)
        self.assertEqual(len(jobs), len(PILOT2.tasks) * len(PILOT2.arms) * PILOT2.reps)
        self.assertEqual(len(set(jobs)), len(jobs))
        self.assertEqual({rep for _, _, rep in jobs}, {1, 2, 3})
        self.assertEqual({task for task, _, _ in jobs}, set(PILOT2.tasks))

    def test_each_experiment_freezes_only_its_own_tasks(self):
        def task_ids(experiment):
            return {path.relative_to(manifest.EVAL_ROOT / "tasks").parts[0]
                    for path in manifest.frozen_files(experiment) if "tasks" in path.parts}
        self.assertEqual(task_ids(experiments.PILOT), set(experiments.PILOT_TASKS))
        self.assertEqual(task_ids(PILOT2), set(PILOT2.tasks))

    def test_pilot2_is_routed_and_reviewers_differ_only_in_model(self):
        self.assertTrue(PILOT2.routed)
        self.assertFalse(experiments.PILOT.routed)
        a2, a3 = PILOT2.arm("A2").reviewer, PILOT2.arm("A3").reviewer
        self.assertEqual((a2.cli, a2.provider), (a3.cli, a3.provider))
        self.assertNotEqual(a2.model, a3.model)
        self.assertEqual({arm.writer for arm in PILOT2.arms}, {experiments.QWEN_WRITER})


class SpendCapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.ledger = self.tmp / "spend.json"

    def admit(self, usage, cap=1.0, phase="scored"):
        side_effect = openrouter.CreditUnavailable("down") if usage is None else None
        with mock.patch.object(openrouter, "credit_used", return_value=usage, side_effect=side_effect):
            return openrouter.SpendCap(self.ledger, phase, cap).admit()

    def test_baseline_survives_a_resume_and_counts_cumulative_spend(self):
        self.assertTrue(self.admit(5.0))
        self.assertTrue(self.admit(5.9))  # a new SpendCap: the resumed run keeps the first baseline
        self.assertFalse(self.admit(6.0))
        self.assertAlmostEqual(openrouter.spent(self.ledger, "scored"), 1.0)

    def test_settle_records_the_last_jobs_spend(self):
        self.assertTrue(self.admit(5.0))
        with mock.patch.object(openrouter, "credit_used", return_value=5.4):
            openrouter.SpendCap(self.ledger, "scored", 1.0).settle()
        self.assertAlmostEqual(openrouter.spent(self.ledger, "scored"), 0.4)

    def test_unreadable_usage_refuses(self):
        self.assertFalse(self.admit(None))

    def test_phases_have_separate_baselines(self):
        self.assertTrue(self.admit(5.0, phase="calibration"))
        self.assertTrue(self.admit(8.0, phase="scored"))
        self.assertFalse(self.admit(9.0, phase="calibration", cap=3.0))
        self.assertTrue(self.admit(8.5, phase="scored"))
        self.assertIsNone(openrouter.spent(self.tmp / "missing.json", "scored"))


class RunBuildsCapTest(unittest.TestCase):
    def test_no_job_is_admitted_past_the_cap(self):
        root = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, root)
        experiment = dataclasses.replace(PILOT2, tasks=("dev-intervals",), reps=2, parallel=1, budget_usd=1.0)
        usage = iter([0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0])
        started = []

        def fake_execute(task, arm, runs_root, *, rep=None, manifest_hash, offline=False):
            started.append((arm.name, rep))
            return {"run_id": loop.run_id(task.id, arm.name, rep), "exec": "exec-1", "status": "invalid"}

        with mock.patch.object(manifest, "verify", return_value="d"), \
                mock.patch.object(openrouter, "credit_used", side_effect=lambda: next(usage)), \
                mock.patch.object(pilot.loop, "execute_run", fake_execute):
            self.assertEqual(pilot.run_builds(root, experiment), 1)
        self.assertEqual(len(started), 2)  # admitted at 0.0 and 0.5; refused from 1.0 on
        ledger = json.loads((root / pilot.SPEND_LEDGER).read_text())
        self.assertEqual(ledger["scored"]["baseline"], 0.0)


class RoutedRunTest(unittest.TestCase):
    """A routed run flags a session that reported a model it was not routed to, and scrubs the key."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        key_dir = Path(tempfile.mkdtemp(prefix="eval-test-key-"))
        self.addCleanup(shutil.rmtree, key_dir)
        (key_dir / "key").write_text(KEY)
        patcher = mock.patch.object(openrouter, "KEY_PATH", key_dir / "key")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_unrouted_model_on_a_retried_attempt_is_flagged(self):
        calls = []

        def writer(model, prompt, *, role, cwd, out_dir, provider=agents.NATIVE, offline=False):
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "transcript.jsonl").touch()
            calls.append(model)
            if len(calls) == 1:  # an infrastructure failure that still reported a stray model
                return agents.Session(agents.INFRA, "overloaded", "", out_dir / "transcript.jsonl", 0.1,
                                      reported_models=["claude-haiku-5-5"])
            shutil.copy(DEV.path("reference") / "intervals.py", cwd / "intervals.py")
            return agents.Session(agents.COMPLETED, "ok", "done", out_dir / "transcript.jsonl", 0.1,
                                  reported_models=[model])

        with mock.patch.object(agents, "run_claude", writer):
            record = loop.execute_run(DEV, PILOT2.arm("A1"), self.tmp, rep=1, manifest_hash=None)
        self.assertIn("unrouted_model:build:claude-haiku-5-5", record["flags"])

    def test_unrouted_model_and_leaked_key_are_flagged(self):
        def writer(model, prompt, *, role, cwd, out_dir, provider=agents.NATIVE, offline=False):
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "transcript.jsonl").write_text(json.dumps({"leak": KEY}) + "\n")
            shutil.copy(DEV.path("reference") / "intervals.py", cwd / "intervals.py")
            return agents.Session(agents.COMPLETED, "ok", "done", out_dir / "transcript.jsonl", 0.1,
                                  reported_models=[model, "claude-haiku-5-5"])

        with mock.patch.object(agents, "run_claude", writer):
            record = loop.execute_run(DEV, PILOT2.arm("A1"), self.tmp, rep=1, manifest_hash=None)
        self.assertEqual((record["run_id"], record["rep"]), ("dev-intervals--A1--r1", 1))
        self.assertIn("unrouted_model:build:claude-haiku-5-5", record["flags"])
        self.assertTrue(any(flag.startswith("key_exposed:steps/") for flag in record["flags"]))
        exec_dir = self.tmp / "dev-intervals--A1--r1" / "exec-1"
        saved = json.loads((exec_dir / "record.json").read_text())
        self.assertEqual(saved["flags"], record["flags"])
        for path in exec_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(KEY.encode(), path.read_bytes(), path)


if __name__ == "__main__":
    unittest.main()
