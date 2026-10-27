import dataclasses
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval.harness import agents, experiments, loop, manifest, pilot, report, tasks
from eval.harness.loop import Arm
from eval.tests import pilot_fixture

FIXTURES = Path(__file__).with_name("fixtures")
DEV = tasks.Task(tasks.TASKS_ROOT / "dev-intervals")
PILOT = experiments.PILOT


class PilotUnchangedTest(unittest.TestCase):
    """The pilot experiment reproduces the frozen pilot (commit 071f3d0) exactly."""

    def test_arm_table(self):
        haiku, luna = agents.Agent("claude", "claude-haiku-5-5"), agents.Agent("codex", "gpt-6-luna")
        self.assertEqual(PILOT.arms, (Arm("A1", haiku), Arm("A1r", haiku), Arm("A2", haiku, haiku),
                                      Arm("A3", haiku, luna)))
        self.assertEqual((PILOT.baseline, PILOT.replicate, PILOT.followup_arms, PILOT.pairs, PILOT.seed),
                         ("A1", "A1r", ("A1", "A2", "A3"), (("A1", "A2"), ("A1", "A3"), ("A2", "A3")), 20261021))
        self.assertEqual((PILOT.data_dir, PILOT.prereg, manifest.manifest_path(PILOT)),
                         ("", "PREREG.md", manifest.EVAL_ROOT / "MANIFEST.json"))

    def test_schedules_match_the_frozen_pilot(self):
        golden = json.loads((FIXTURES / "pilot_schedules_golden.json").read_text())
        builds = pilot.build_schedule(PILOT)
        self.assertTrue(all(rep is None for _, _, rep in builds))
        self.assertEqual([[task, arm] for task, arm, _ in builds], golden["builds"])
        judging = pilot.judge_schedule(PILOT)
        self.assertTrue(all(rep is None for *_, rep in judging))
        self.assertEqual([[n, t, list(p) if p else None] for n, t, p, _ in judging], golden["judging"])

    def test_report_matches_the_frozen_pilot(self):
        root = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, root)
        pilot_fixture.populate(root, [t.id for t in pilot.scored_tasks(PILOT)], pilot.judging_dir, pilot.build_identity)
        with mock.patch.object(manifest, "verify", return_value="d"):
            results = report.collect(root, PILOT)
            decision = report.decide(results, PILOT)
            text = report.render(results, decision, PILOT)
        self.assertEqual(text, (FIXTURES / "pilot_report_golden.md").read_text())


class RegisteredExperimentsTest(unittest.TestCase):
    def test_every_experiment_is_consistent(self):
        for name, experiment in experiments.EXPERIMENTS.items():
            with self.subTest(experiment=name):
                names = experiment.arm_names
                self.assertEqual(name, experiment.name)
                self.assertEqual(len(names), len(set(names)))
                self.assertIn(experiment.baseline, names)
                self.assertTrue(experiment.replicate is None or experiment.replicate in names)
                self.assertTrue(set(experiment.followup_arms) <= set(names))
                self.assertTrue(all(a in names and b in names for a, b in experiment.pairs))
                self.assertIsNone(experiment.arm(experiment.baseline).reviewer)
                if name != "pilot":
                    self.assertEqual((experiment.data_dir, experiment.prereg), (name, f"prereg/{name}.md"))
                    self.assertIn(experiment.decision_rule, (None, name))
                for task_id in experiment.tasks:
                    self.assertTrue((tasks.TASKS_ROOT / task_id).is_dir(), task_id)

    def test_reverse_has_gpt_writing_and_claude_reviewing_cross(self):
        reverse = experiments.EXPERIMENTS["reverse-sonnet"]
        self.assertEqual(reverse.arm("A1").writer.cli, "codex")
        self.assertEqual((reverse.arm("A2").reviewer.cli, reverse.arm("A3").reviewer.cli), ("codex", "claude"))

    def test_seeds_differ(self):
        seeds = [e.seed for e in experiments.EXPERIMENTS.values()]
        self.assertEqual(len(seeds), len(set(seeds)))


class ExperimentFreezeTest(unittest.TestCase):
    """Per-experiment freezes over a temporary repo, with the real verify."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.root)
        eval_root = self.root / "eval"
        for relative in ("__init__.py", "__main__.py", "harness/loop.py", "tasks/t/TASK.md", "tasks/t/expected.json",
                         "tasks/t/FOLLOWUP.md", "tasks/t/degraded/m.py", "PREREG.md"):
            (eval_root / relative).parent.mkdir(parents=True, exist_ok=True)
            (eval_root / relative).write_text(f"{relative}\n")
        skills = {}
        for name in ("CODEX_REVIEW_SKILL", "BUILD_LOOP_SKILL", "RUBRIC"):
            skills[name] = self.root / "skills" / f"{name}.md"
            skills[name].parent.mkdir(parents=True, exist_ok=True)
            skills[name].write_text(name)
        targets = {"REPO_ROOT": self.root, "EVAL_ROOT": eval_root, "MANIFEST": eval_root / "MANIFEST.json",
                   "MANIFESTS": eval_root / "manifests", **skills}
        for target, value in targets.items():
            patcher = mock.patch.object(manifest, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.eval_root = eval_root
        self.sonnet, self.opus = (dataclasses.replace(experiments.EXPERIMENTS[name], tasks=("t",))
                                  for name in ("sonnet", "opus"))
        self.pilot = dataclasses.replace(PILOT, tasks=("t",))

    def prereg(self, experiment, text="rule\n"):
        path = self.eval_root / experiment.prereg
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_freeze_needs_a_prereg(self):
        with self.assertRaisesRegex(manifest.ManifestMismatch, "no pre-registration"):
            manifest.freeze(self.sonnet)

    def test_run_refuses_without_a_freeze(self):
        with mock.patch.object(loop, "execute_run", side_effect=AssertionError("must not run")):
            with self.assertRaisesRegex(manifest.ManifestMismatch, "no manifest for sonnet"):
                pilot.run_builds(self.root / "data", self.sonnet)

    def test_changed_prereg_refuses(self):
        self.prereg(self.sonnet)
        manifest.freeze(self.sonnet)
        self.assertTrue(manifest.verify(self.sonnet))
        self.prereg(self.sonnet, "rule, edited after the freeze\n")
        with self.assertRaisesRegex(manifest.ManifestMismatch, "prereg/sonnet.md"):
            manifest.verify(self.sonnet)

    def test_one_experiments_manifest_cannot_verify_another(self):
        self.prereg(self.sonnet)
        self.prereg(self.opus)
        manifest.freeze(self.sonnet)
        manifest.manifest_path(self.opus).write_text(manifest.manifest_path(self.sonnet).read_text())
        with self.assertRaisesRegex(manifest.ManifestMismatch, "freezes sonnet, not opus"):
            manifest.verify(self.opus)

    def test_freezing_another_experiment_leaves_the_pilot_frozen(self):
        pilot_digest = manifest.freeze(self.pilot)
        self.prereg(self.sonnet)
        manifest.freeze(self.sonnet)
        self.assertEqual(manifest.verify(self.pilot), pilot_digest)

    def test_freeze_refuses_tasks_without_the_parts_the_experiment_uses(self):
        replicated = dataclasses.replace(self.sonnet, report="replicated", followup_arms=(), pairs=())
        self.prereg(replicated)
        with self.assertRaisesRegex(manifest.ManifestMismatch, "t: no mutants/"):
            manifest.freeze(replicated)
        (self.eval_root / "tasks" / "t" / "FOLLOWUP.md").unlink()
        with self.assertRaisesRegex(manifest.ManifestMismatch, "t: no follow-up group"):
            manifest.freeze(self.sonnet)

    def test_deleted_prereg_refuses_cleanly(self):
        self.prereg(self.sonnet)
        manifest.freeze(self.sonnet)
        (self.eval_root / self.sonnet.prereg).unlink()
        with self.assertRaisesRegex(manifest.ManifestMismatch, "frozen input missing: eval/prereg/sonnet.md"):
            manifest.verify(self.sonnet)

    def test_changed_experiment_table_refuses(self):
        self.prereg(self.sonnet)
        manifest.freeze(self.sonnet)
        (self.eval_root / "harness" / "experiments.py").write_text("arms changed\n")
        with self.assertRaisesRegex(manifest.ManifestMismatch, "experiments.py"):
            manifest.verify(self.sonnet)


class SyntheticExperimentRunTest(unittest.TestCase):
    """A non-pilot experiment end to end through the real schedule, loop, judging and report,
    with fake model sessions on the dev task."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.calls = []

    def fake(self, cli):
        def session(model, prompt, *, role, cwd, out_dir, provider=agents.NATIVE, offline=False):
            self.calls.append({"cli": cli, "model": model, "role": role, "followup": "FOLLOWUP.md" in prompt})
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "transcript.jsonl").touch()
            text = "done"
            if role == agents.WRITER:
                source = "reference_followup" if "FOLLOWUP.md" in prompt else "reference"
                if "Build the plan" in prompt or "Implement TASK.md" in prompt or "FOLLOWUP.md" in prompt:
                    shutil.copy(DEV.path(source) / "intervals.py", cwd / "intervals.py")
            elif role == agents.REVIEWER:
                text = "VERDICT: GREEN\nISSUES:\nNone\n"
            else:
                text = '{"winner": "X", "reasons": ["clearer"]}'
            return agents.Session(agents.COMPLETED, "fake", text, out_dir / "transcript.jsonl", 0.1)
        return session

    def run_experiment(self, experiment):
        data_root = self.root / experiment.data_dir
        with mock.patch.object(pilot, "scored_tasks", lambda experiment: [DEV]), \
             mock.patch.object(manifest, "verify", return_value="d"), \
             mock.patch.object(agents, "run_claude", self.fake("claude")), \
             mock.patch.object(agents, "run_codex", self.fake("codex")):
            self.assertEqual(pilot.run_builds(data_root, experiment), 0)
            self.assertEqual(pilot.run_judging(data_root, experiment), 0)
            results = report.collect(data_root, experiment)
            decision = report.decide(results, experiment)
            text = report.render(results, decision, experiment)
        return data_root, results, decision, text

    def check(self, experiment, followup_cli):
        data_root, results, decision, text = self.run_experiment(experiment)
        self.assertEqual(results.missing, [])
        self.assertEqual(decision.verdict, "DESCRIPTIVE")
        self.assertNotIn("GO", text.replace("DESCRIPTIVE", ""))
        self.assertIn(f"# Eval report: {experiment.name}", text)
        for arm in experiment.arm_names:
            self.assertTrue((data_root / "runs" / f"dev-intervals--{arm}").is_dir())
            self.assertIn(f"| {arm} |", text)
        for a, b in experiment.pairs:
            self.assertIn(f"{a} vs {b} (winner per task):", text)
            self.assertTrue((data_root / "judging" / "fable" / "dev-intervals" / f"{a}-vs-{b}").is_dir())
        followups = {c["cli"] for c in self.calls if c["followup"]}
        self.assertEqual(followups, {followup_cli})
        self.assertEqual(sum(1 for c in self.calls if c["followup"]), len(experiment.followup_arms))
        return text

    def test_reverse_experiment(self):
        experiment = experiments.EXPERIMENTS["reverse-sonnet"]
        text = self.check(experiment, followup_cli="codex")
        self.assertIn("A1 − A1r spread", text)
        writers = {(c["cli"], c["model"]) for c in self.calls if c["role"] == agents.WRITER}
        self.assertEqual(writers, {("codex", "gpt-5.6-terra")})

    def test_ablation_experiment_without_replicate(self):
        experiment = experiments.EXPERIMENTS["ablation-sonnet"]
        text = self.check(experiment, followup_cli="claude")
        self.assertNotIn("spread", text)
        self.assertIn("removes the rules and rubric from the writer only", text)
        self.assertIn("L-rubric − S", text)


if __name__ == "__main__":
    unittest.main()
