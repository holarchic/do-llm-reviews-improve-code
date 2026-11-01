import dataclasses
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval.harness import agents, experiments, loop, manifest, openrouter, pilot, prices, providers, tasks

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


class HistoricalSpendTest(unittest.TestCase):
    """Pilot 2's report reads the spend-cap ledger its runs wrote; no new run writes one."""

    def test_spent_reads_a_phase_and_a_missing_ledger_is_none(self):
        tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, tmp)
        ledger = tmp / "spend.json"
        ledger.write_text(json.dumps({"scored": {"baseline": 5.0, "readings": [5.0, 5.4]}}))
        self.assertAlmostEqual(openrouter.spent(ledger, "scored"), 0.4)
        self.assertIsNone(openrouter.spent(ledger, "calibration"))
        self.assertIsNone(openrouter.spent(tmp / "missing.json", "scored"))


class JobEstimateTest(unittest.TestCase):
    def test_an_estimate_is_the_bootstrap_until_calibration_then_the_dearest_build(self):
        from eval.harness import envelope, routing
        root = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, root)
        experiment = dataclasses.replace(PILOT2, envelope="T")
        with envelope.Envelope.owned("T", root=root / "envelopes", create_usd=50.0) as env:
            env.fund(credit=10.0)
            env.register_scope("or", provider="openrouter", fingerprint="fp", opening_usage=0.0, allocation=10.0,
                               provider_limit=None)
            arm = experiment.arm("A1")
            env.set_route(routing.route_name(arm.writer.provider, arm.writer.model), scope="or", max_charge=0.5,
                          slots=1, policy={}, bootstrap_build_usd=2.0)
            calibration = root / "calibration" / "runs"
            self.assertEqual(pilot.job_estimate(env, experiment, "A1", calibration), 3.0)  # 1.5 × $2 bootstrap
            exec_dir = calibration / "dev-intervals--A1--r1" / "exec-1"
            exec_dir.mkdir(parents=True)
            by_model = {arm.writer.model: {"inputTokens": 1_000_000, "outputTokens": 0, "cacheReadInputTokens": 0,
                                           "cacheCreationInputTokens": 0}}
            (exec_dir / "record.json").write_text(json.dumps({"steps": [{"name": "build", "attempts": [
                {"usage": {"by_model": by_model}, "seconds": 1}]}]}))
            with mock.patch.dict(prices.PRICES, {("openrouter", arm.writer.model): prices.Price(
                    "openrouter", arm.writer.model, prices.Rates(4.0, 0.4, 20.0), "test")}):
                self.assertAlmostEqual(pilot.job_estimate(env, experiment, "A1", calibration), 6.0)  # 1.5 × $4

    def test_a_mixed_arm_reserves_for_its_routed_seats_only(self):
        """The envelope funds no native seat: a native writer's list-price cost, known or not, reserves nothing."""
        from eval.harness import envelope, routing
        root = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, root)
        native = agents.Agent("claude", "claude-opus-5-5", agents.NATIVE)
        routed = experiments.QWEN_WRITER
        experiment = dataclasses.replace(PILOT2, envelope="T", arms=(experiments.Arm("M", native, routed),))
        with envelope.Envelope.owned("T", root=root / "envelopes", create_usd=50.0) as env:
            env.fund(credit=10.0)
            env.register_scope("or", provider="openrouter", fingerprint="fp", opening_usage=0.0, allocation=10.0,
                               provider_limit=None)
            env.set_route(routing.route_name(routed.provider, routed.model), scope="or", max_charge=0.5,
                          slots=1, policy={}, bootstrap_build_usd=2.0)
            calibration = root / "calibration" / "runs"
            exec_dir = calibration / "dev-intervals--M--r1" / "exec-1"
            exec_dir.mkdir(parents=True)

            def step(name, agent, model, usage):
                attempt = {"seconds": 1}
                if usage:
                    attempt["usage"] = {"by_model": {model: {"inputTokens": 1_000_000, "outputTokens": 0,
                                                             "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}}}
                return {"name": name, "agent": {"cli": agent.cli, "model": agent.model, "provider": agent.provider},
                        "attempts": [attempt]}
            (exec_dir / "record.json").write_text(json.dumps({"steps": [
                step("build", native, native.model, True),  # $4 at Opus list price
                step("fix-1", native, native.model, False),  # unknown, but native
                step("review-1", routed, routed.model, True)]}))  # $1 at the routed price below
            with mock.patch.dict(prices.PRICES, {("openrouter", routed.model): prices.Price(
                    "openrouter", routed.model, prices.Rates(1.0, 0.1, 5.0), "test")}):
                self.assertAlmostEqual(pilot.job_estimate(env, experiment, "M", calibration), 1.5)  # 1.5 × $1
                # A build whose native writer never reached the routed reviewer measures nothing routed.
                stopped = calibration / "dev-intervals--M--r2" / "exec-1"
                stopped.mkdir(parents=True)
                (stopped / "record.json").write_text(json.dumps({"steps": [step("build", native, native.model, True)]}))
                self.assertAlmostEqual(pilot.job_estimate(env, experiment, "M", calibration), 3.0)  # 1.5 × $2 bootstrap
                # Known routed spend above the bootstrap stands even when the same execution has an unknown attempt.
                partial = calibration / "dev-intervals--M--r3" / "exec-1"
                partial.mkdir(parents=True)
                (partial / "record.json").write_text(json.dumps({"steps": [
                    step("review-1", routed, routed.model, True), step("review-2", routed, routed.model, True),
                    step("review-3", routed, routed.model, True), step("review-4", routed, routed.model, False)]}))
                self.assertAlmostEqual(pilot.job_estimate(env, experiment, "M", calibration), 4.5)  # 1.5 × max($3, $2)

    def test_an_unpriced_native_seat_never_reaches_the_estimate(self):
        from eval.harness import envelope, routing
        root = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, root)
        native = agents.Agent("codex", "gpt-6-luna", agents.NATIVE)  # not in the price table
        routed = experiments.QWEN_WRITER
        experiment = dataclasses.replace(PILOT2, envelope="T", arms=(experiments.Arm("M", routed, native),))
        with envelope.Envelope.owned("T", root=root / "envelopes", create_usd=50.0) as env:
            env.fund(credit=10.0)
            env.register_scope("or", provider="openrouter", fingerprint="fp", opening_usage=0.0, allocation=10.0,
                               provider_limit=None)
            env.set_route(routing.route_name(routed.provider, routed.model), scope="or", max_charge=0.5,
                          slots=1, policy={}, bootstrap_build_usd=2.0)
            calibration = root / "calibration" / "runs"
            exec_dir = calibration / "dev-intervals--M--r1" / "exec-1"
            exec_dir.mkdir(parents=True)
            usage = {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 1}
            (exec_dir / "record.json").write_text(json.dumps({"steps": [
                {"name": "build", "agent": {"cli": "claude", "model": routed.model, "provider": routed.provider},
                 "attempts": [{"seconds": 1, "usage": {"by_model": {routed.model: {
                     "inputTokens": 1_000_000, "outputTokens": 0, "cacheReadInputTokens": 0,
                     "cacheCreationInputTokens": 0}}}}]},
                {"name": "review-1", "agent": {"cli": "codex", "model": native.model, "provider": native.provider},
                 "attempts": [{"seconds": 1, "usage": usage}]}]}))
            with mock.patch.dict(prices.PRICES, {("openrouter", routed.model): prices.Price(
                    "openrouter", routed.model, prices.Rates(1.0, 0.1, 5.0), "test")}):
                self.assertAlmostEqual(pilot.job_estimate(env, experiment, "M", calibration), 1.5)

    def test_experiments_sharing_an_envelope_never_share_a_jobs_hold(self):
        from eval.harness import envelope, routing
        root = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, root)
        first, second = (dataclasses.replace(PILOT2, name=name, envelope="T") for name in ("first", "second"))
        run = loop.run_id("dev-intervals", "A1", 1)  # the same task, arm and replicate in both
        with envelope.Envelope.owned("T", root=root / "envelopes", create_usd=50.0) as env:
            env.fund(credit=10.0)
            env.register_scope("or", provider="openrouter", fingerprint="fp", opening_usage=0.0, allocation=10.0,
                               provider_limit=None)
            arm = PILOT2.arm("A1")
            route = routing.route_name(arm.writer.provider, arm.writer.model)
            env.set_route(route, scope="or", max_charge=4.0, slots=4, policy={}, bootstrap_build_usd=1.0)
            env.validate_route(route)
            env.reconcile("or", 0.0)
            env.reserve(pilot.job_id(first, "build", run), 1.0)
            env.admit("q1", route, job=pilot.job_id(first, "build", run), attempt="a")
            env.settle("q1", 4.0)
            env.finish(pilot.job_id(first, "build", run))
            env.reconcile("or", 4.0)
            env.reserve(pilot.job_id(second, "build", run), 5.0)
            with self.assertRaises(envelope.Refused):  # $4 spent + two $5 holds > $10
                env.reserve(pilot.job_id(second, "calibration", run), 5.0)

    def test_a_native_only_arm_runs_without_a_reservation(self):
        ran = []
        native = dataclasses.replace(PILOT2.arm("A1"), writer=agents.Agent("claude", "claude-opus-5-5"))
        outcome = pilot.reserved(mock.Mock(reserve=mock.Mock(side_effect=AssertionError)), native, "x", lambda: 0.0,
                                 lambda: ran.append(1) or "done")
        self.assertEqual((outcome, ran), (pilot.Admitted("done"), [1]))


class RunBuildsEnvelopeTest(unittest.TestCase):
    """A routed run reserves each job's estimate in its envelope before the job starts."""

    def test_no_job_starts_past_funded_credit_and_each_releases_after(self):
        import contextlib
        from eval.harness import envelope, routing
        root = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, root)
        experiment = dataclasses.replace(PILOT2, tasks=("dev-intervals",), reps=2, parallel=1, envelope="T",
                                         arms=(PILOT2.arm("A1"),))
        owned = envelope.Envelope.owned("T", root=root / "envelopes", create_usd=50.0)
        env = owned.__enter__()
        self.addCleanup(owned.__exit__, None, None, None)
        env.fund(credit=5.0)
        env.register_scope("or", provider="openrouter", fingerprint="fp", opening_usage=0.0, allocation=5.0,
                           provider_limit=5.0)
        writer = PILOT2.arm("A1").writer
        env.set_route(routing.route_name(writer.provider, writer.model), scope="or", max_charge=0.5, slots=2,
                      policy={}, bootstrap_build_usd=2.0)  # each job reserves 1.5 × $2
        env.reconcile("or", 0.0)
        started, held = [], []

        def fake_execute(task, arm, runs_root, *, rep=None, manifest_hash, offline=False):
            started.append(rep)
            held.append(sorted(j for j, r in env.state["reservations"].items() if r["open"]))
            return {"run_id": loop.run_id(task.id, arm.name, rep), "exec": "exec-1", "status": "invalid"}

        with mock.patch.object(manifest, "verify", return_value="d"), \
                mock.patch.object(pilot, "routing_context", lambda e: contextlib.nullcontext(env)), \
                mock.patch.object(pilot.loop, "execute_run", fake_execute):
            self.assertEqual(pilot.run_builds(root, experiment), 1)
        self.assertEqual(len(started), 2)  # each $3 hold is released before the next job: both fit in turn
        self.assertEqual([len(h) for h in held], [1, 1])
        env.reserve("other", 3.0)  # nothing is left held
        self.assertFalse(any(r["open"] for j, r in env.state["reservations"].items() if j != "other"))

    def test_a_job_whose_reservation_does_not_fit_never_starts(self):
        import contextlib
        from eval.harness import envelope, routing
        root = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, root)
        experiment = dataclasses.replace(PILOT2, tasks=("dev-intervals",), reps=2, parallel=1, envelope="T",
                                         arms=(PILOT2.arm("A1"),))
        owned = envelope.Envelope.owned("T", root=root / "envelopes", create_usd=50.0)
        env = owned.__enter__()
        self.addCleanup(owned.__exit__, None, None, None)
        env.fund(credit=2.5)
        env.register_scope("or", provider="openrouter", fingerprint="fp", opening_usage=0.0, allocation=2.5,
                           provider_limit=2.5)
        writer = PILOT2.arm("A1").writer
        env.set_route(routing.route_name(writer.provider, writer.model), scope="or", max_charge=0.5, slots=2,
                      policy={}, bootstrap_build_usd=2.0)  # 1.5 × $2 = $3 > $2.50
        env.reconcile("or", 0.0)
        started = []
        with mock.patch.object(manifest, "verify", return_value="d"), \
                mock.patch.object(pilot, "routing_context", lambda e: contextlib.nullcontext(env)), \
                mock.patch.object(pilot.loop, "execute_run", lambda *a, **k: started.append(1)):
            self.assertEqual(pilot.run_builds(root, experiment), 1)
        self.assertEqual(started, [])

    def test_a_follow_up_runs_inside_its_jobs_reservation_and_routing_context(self):
        import contextlib
        from eval.harness import envelope, routing
        root = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, root)
        experiment = dataclasses.replace(PILOT2, tasks=("dev-intervals",), reps=1, parallel=1, envelope="T",
                                         arms=(PILOT2.arm("A1"),), followup_arms=("A1",))
        owned = envelope.Envelope.owned("T", root=root / "envelopes", create_usd=50.0)
        env = owned.__enter__()
        self.addCleanup(owned.__exit__, None, None, None)
        env.fund(credit=10.0)
        env.register_scope("or", provider="openrouter", fingerprint="fp", opening_usage=0.0, allocation=10.0,
                           provider_limit=10.0)
        writer = PILOT2.arm("A1").writer
        env.set_route(routing.route_name(writer.provider, writer.model), scope="or", max_charge=0.5, slots=2,
                      policy={}, bootstrap_build_usd=1.0)
        env.reconcile("or", 0.0)
        seen = []

        def fake_execute(task, arm, runs_root, *, rep=None, manifest_hash, offline=False):
            exec_dir = runs_root / loop.run_id(task.id, arm.name, rep) / "exec-1"
            exec_dir.mkdir(parents=True)
            return {"run_id": loop.run_id(task.id, arm.name, rep), "exec": "exec-1", "status": "complete"}

        def fake_followup(task, exec_dir, arm, *, manifest_hash, offline=False):
            job = getattr(routing._job, "value", None)
            seen.append((job, env.state["reservations"].get(job, {}).get("open")))
            return {"status": "complete"}

        with mock.patch.object(manifest, "verify", return_value="d"), \
                mock.patch.object(pilot, "routing_context", lambda e: contextlib.nullcontext(env)), \
                mock.patch.object(pilot.loop, "execute_run", fake_execute), \
                mock.patch.object(pilot.loop, "execute_followup", fake_followup), \
                mock.patch.object(providers, "scrub", return_value=[]):
            pilot.run_builds(root, experiment)
            # A resumed run: the build is complete, only its follow-up is still owed.
            with mock.patch.object(pilot, "complete_build", lambda *a, **k: root / "runs" / "x" / "exec-1"):
                pilot.run_builds(root, experiment)
        job = pilot.job_id(experiment, "build", loop.run_id("dev-intervals", "A1", None))
        self.assertEqual(seen, [(job, True), (job, True)])

    def test_a_routed_experiment_without_an_envelope_is_refused(self):
        from eval.harness import routing
        with mock.patch.object(manifest, "verify", return_value="d"), self.assertRaises(routing.RoutingRequired):
            pilot.run_builds(Path(tempfile.mkdtemp(prefix="eval-test-")), PILOT2)


class RoutedRunTest(unittest.TestCase):
    """A routed run flags a session that reported a model it was not routed to, and scrubs the key."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        key_dir = Path(tempfile.mkdtemp(prefix="eval-test-key-"))
        self.addCleanup(shutil.rmtree, key_dir)
        (key_dir / "key").write_text(KEY)
        patcher = mock.patch.dict(providers.KEY_PATHS, {"openrouter": key_dir / "key"})
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
