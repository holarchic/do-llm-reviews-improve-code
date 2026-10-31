import dataclasses
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval.harness import agents, experiments, loop, pilot, rounds, source
from eval.tests.test_replay import LOOP, StubAgentCase
from eval.tests.test_source import PRE_REVIEW_CALC, REFERENCE_CALC, git


def build(run_id: str, task: str, review: list[float], *, caught: bool = True, stop: str = rounds.CAPPED) -> rounds.Build:
    """A scored build whose round k avoids review[k] of its defects (a fraction)."""
    scores = {k: {"review": r if caught else None, "feature": 1.0, "regressions": 0.0, "defects": [], "outcomes": {}}
              for k, r in enumerate(review)}
    steps = [rounds.Round(k, "build" if k == 0 else f"fix-{k}", f"c{k}") for k in range(len(review))]
    record = {"scores": {"replay": scores[len(review) - 1]}}
    return rounds.Build(run_id, task, caught, stop, Path("/x"), steps, record, scores)


def results(*builds: rounds.Build, problems=()) -> rounds.Results:
    return rounds.Results("d", "A3", "prereg/replay-rounds.md", "s", list(builds), list(problems))


class TriggerTest(unittest.TestCase):
    def test_met_at_exactly_three_quarters_over_three_tasks(self):
        built = results(*(build(f"{t}--r1", t, [0, 0.75, 0.75, 1, 1]) for t in "abc"))
        trigger = rounds.decide(built)
        self.assertEqual(trigger.verdict, "met")
        self.assertAlmostEqual(trigger.mean, 0.75)

    def test_two_contributing_tasks_are_not_enough(self):
        built = results(build("a--r1", "a", [0, 1, 1, 1, 1]), build("b--r1", "b", [0, 1, 1, 1, 1]),
                        build("c--r1", "c", [0.5, 0.5, 0.5, 0.5, 0.5]))
        trigger = rounds.decide(built)
        self.assertEqual((trigger.verdict, list(trigger.task_means)), ("not met", ["a", "b"]))

    def test_an_empty_eligible_set_is_not_met(self):
        trigger = rounds.decide(results(build("a--r1", "a", [1, 1, 1, 1, 1])))
        self.assertEqual((trigger.verdict, trigger.eligible, trigger.mean), ("not met", [], None))

    def test_a_non_positive_final_gain_controls_and_truncated_builds_are_excluded(self):
        built = results(build("a--r1", "a", [0.5, 1, 1, 1, 0.25]),  # final below round 0
                        build("b--r1", "b", [0, 1], stop=rounds.TRUNCATED),
                        build("k--r1", "k", [0, 0, 0, 0, 0], caught=False),
                        build("c--r1", "c", [0, 0.5, 1, 1, 1]))
        self.assertEqual([e.run_id for e in rounds.decide(built).eligible], ["c--r1"])

    def test_ratios_above_one_and_below_zero_are_kept(self):
        built = results(build("a--r1", "a", [0, 1, 0.5, 0.5, 0.5]), build("b--r1", "b", [0.5, 0.25, 1, 1, 1]))
        ratios = {e.run_id: e.ratio for e in rounds.decide(built).eligible}
        self.assertEqual(ratios, {"a--r1": 2.0, "b--r1": -0.5})

    def test_ratios_are_averaged_within_tasks_then_across_them(self):
        # Pooled over builds: (0 + 1 + 1 + 1 + 1) / 5 = 0.8, met. Task-balanced: (0 + 1 + 1) / 3, not met.
        built = results(build("a--r1", "a", [0, 0, 1, 1, 1]), build("a--r2", "a", [1, 1, 1, 1, 1]),
                        *(build(f"{t}--r{r}", t, [0, 1, 1, 1, 1]) for t in "bc" for r in (1, 2)))
        trigger = rounds.decide(built)
        self.assertEqual(trigger.task_means, {"a": 0.0, "b": 1.0, "c": 1.0})
        self.assertEqual((trigger.verdict, trigger.median), ("not met", 1.0))

    def test_any_problem_withholds_the_trigger(self):
        built = results(*(build(f"{t}--r1", t, [0, 1, 1, 1, 1]) for t in "abc"), problems=["c--r2: no complete build"])
        trigger = rounds.decide(built)
        self.assertEqual((trigger.verdict, trigger.problems), ("INCOMPLETE", ["c--r2: no complete build"]))
        self.assertIn("INCOMPLETE, withheld", rounds.render(built, trigger, experiments.REPLAY))


class RoundValueTest(unittest.TestCase):
    def test_a_passed_build_carries_forward_and_a_truncated_one_stops(self):
        passed = build("a--r1", "a", [0, 0.5], stop=rounds.PASSED)
        truncated = build("b--r1", "b", [0, 0.5], stop=rounds.TRUNCATED)
        self.assertEqual([rounds.value(passed, k, rounds.REVIEW) for k in range(5)], [0, 50, 50, 50, 50])
        self.assertEqual([rounds.value(truncated, k, rounds.REVIEW) for k in range(5)], [0, 50, None, None, None])

    def test_stop_kinds_come_from_the_flags(self):
        self.assertEqual(rounds.stop_kind({"flags": ["review_capped"]}), rounds.CAPPED)
        self.assertEqual(rounds.stop_kind({"flags": ["plan_capped", "timeout:build"]}), rounds.TRUNCATED)
        # A non-blocking last round, GREEN or LOW-only YELLOW, leaves no flag.
        self.assertEqual(rounds.stop_kind({"flags": ["plan_capped", "reviewer_wrote:plan-review-1"]}), rounds.PASSED)

    def test_only_allowlisted_frozen_files_may_change(self):
        frozen = {"eval/tasks/t/TASK.md": "1", "eval/harness/tasks.py": "2", "eval/harness/source.py": "3",
                  "eval/harness/loop.py": "4", "eval/__main__.py": "5"}
        current = {**frozen, "eval/harness/loop.py": "x", "eval/__main__.py": "y", "eval/harness/rounds.py": "z"}
        self.assertEqual(rounds.changed_inputs(frozen, current), [])
        current.update({"eval/harness/tasks.py": "x", "eval/harness/source.py": "x"})
        del current["eval/tasks/t/TASK.md"]
        self.assertEqual(rounds.changed_inputs(frozen, current),
                         ["eval/harness/source.py", "eval/harness/tasks.py", "eval/tasks/t/TASK.md"])

    def test_reproduction_compares_tests_not_only_aggregates(self):
        recorded = {"review": 1.0, "defects": [{"id": "d", "avoided": True}], "outcomes": {"t1": "fail", "t2": "pass"}}
        rescored = {**recorded, "outcomes": {"t1": "pass", "t2": "fail"}}
        b = build("a--r1", "a", [1.0])
        b.record, b.scores = {"scores": {"replay": recorded}}, {0: rescored}
        self.assertEqual(rounds.reproduction(b), ["t1: fail on record, pass now", "t2: pass on record, fail now"])
        b.scores = {0: recorded}
        self.assertEqual(rounds.reproduction(b), [])


class CodeRoundsTest(unittest.TestCase):
    def repo(self, *subjects: str) -> Path:
        repo = Path(tempfile.mkdtemp(prefix="eval-rounds-"))
        self.addCleanup(shutil.rmtree, repo)
        git(repo, "init", "-q")
        for subject in subjects:
            git(repo, "commit", "-q", "--allow-empty", "-m", subject)
        return repo

    def record(self, *steps: str, flags=()) -> dict:
        return {"steps": [{"name": name} for name in steps], "flags": list(flags)}

    def test_rounds_are_matched_by_step_name_and_plan_steps_are_not_rounds(self):
        repo = self.repo("task", "plan", "plan-revise-1", "build", "fix-1", "fix-2")
        steps = ("plan", "plan-review-1", "plan-revise-1", "plan-review-2", "build", "correctness-review-1", "fix-1",
                 "correctness-review-2", "fix-2", "correctness-review-3")
        found = rounds.code_rounds(self.record(*steps), repo)
        head = git(repo, "rev-parse", "HEAD").strip()
        self.assertEqual([(r.number, r.step) for r in found], [(0, "build"), (1, "fix-1"), (2, "fix-2")])
        self.assertEqual(found[-1].commit, head)

    def test_a_timeout_at_build_leaves_round_zero_only(self):
        found = rounds.code_rounds(self.record("plan", "plan-review-1", "build"), self.repo("task", "plan", "build"))
        self.assertEqual([r.step for r in found], ["build"])

    def test_a_duplicate_or_missing_commit_is_refused(self):
        with self.assertRaisesRegex(rounds.RoundError, "2 commits named fix-1"):
            rounds.code_rounds(self.record("build", "fix-1"), self.repo("task", "build", "fix-1", "fix-1"))
        with self.assertRaisesRegex(rounds.RoundError, "0 commits named fix-1"):
            rounds.code_rounds(self.record("build", "fix-1"), self.repo("task", "build"))

    def test_a_capped_build_must_reach_the_last_round_and_none_may_pass_it(self):
        with self.assertRaisesRegex(rounds.RoundError, "capped, but its last round is 1"):
            rounds.code_rounds(self.record("build", "fix-1", flags=["review_capped"]), self.repo("build", "fix-1"))
        steps = ["build", *(f"fix-{k}" for k in range(1, 6))]
        with self.assertRaisesRegex(rounds.RoundError, "past round 4"):
            rounds.code_rounds(self.record(*steps), self.repo(*steps))


class EndToEndTest(StubAgentCase):
    """Stub agents build the fixture task's historical defect, the first code review blocks, fix-1 repairs it and
    the second review passes; the real scorer then scores each round from the build's repo."""

    def setUp(self):
        super().setUp()
        self.calc = PRE_REVIEW_CALC
        self.built = self.blocked = False
        self.experiment = dataclasses.replace(experiments.REPLAY, tasks=("fixture",), parallel=2)
        self.data = self.tmp / "data"
        patcher = mock.patch.object(pilot, "scored_tasks", lambda e: [self.task])
        patcher.start()
        self.addCleanup(patcher.stop)

    def agent(self, model, prompt, *, role, cwd, out_dir, provider=agents.NATIVE, offline=False):
        if role == agents.REVIEWER and self.built and not self.blocked:
            self.blocked = True
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "transcript.jsonl").touch()
            return agents.Session(agents.COMPLETED, "ok", "VERDICT: RED\nISSUES:\n", out_dir / "transcript.jsonl",
                                  0.1, reported_models=[model])
        if role == agents.WRITER and self.built:
            (cwd / "calc.py").write_text(REFERENCE_CALC)
        session = super().agent(model, prompt, role=role, cwd=cwd, out_dir=out_dir, provider=provider,
                                offline=offline)
        self.built = self.built or (role == agents.WRITER and "Build the plan" in prompt)
        return session

    def build_rep(self, rep: int) -> dict:
        self.built = self.blocked = False
        with mock.patch.object(agents, "run_claude", self.agent), mock.patch.object(agents, "run_codex", self.agent):
            return loop.execute_run(self.task, LOOP, self.data / "runs", rep=rep, manifest_hash="d", offline=True)

    def test_each_round_is_scored_carried_forward_and_reproduces_the_record(self):
        for rep in (1, 2):
            self.assertEqual(self.build_rep(rep)["status"], "complete")
        self.assertEqual(rounds.score_rounds(self.data, self.experiment, "A3", "d"), [])
        built = rounds.collect(self.data, self.experiment, "A3", "d")
        self.assertEqual(built.problems, [])
        b = built.builds[0]
        self.assertEqual((b.stop, [r.step for r in b.rounds]), (rounds.PASSED, ["build", "fix-1"]))
        self.assertEqual([rounds.value(b, k, rounds.REVIEW) for k in range(5)], [0, 100, 100, 100, 100])
        self.assertEqual([rounds.reproduction(x) for x in built.builds], [[], []])
        trigger = rounds.decide(built)
        self.assertEqual((trigger.verdict, trigger.task_means), ("not met", {"fixture": 1.0}))
        text = rounds.render(built, trigger, self.experiment)
        self.assertIn("| fixture--A3--r1 | passed | 0.0 / 100.0 / 0.0 | 100.0 / 100.0 / 0.0 | 100.0 / 100.0 / 0.0* |",
                      text)

    def test_a_resume_rescores_only_an_interrupted_round(self):
        self.build_rep(1)
        self.build_rep(2)
        rounds.score_rounds(self.data, self.experiment, "A3", "d")
        exec_dir = self.data / "rounds" / "fixture--A3--r1" / "exec-1"
        kept = (exec_dir / "round-0" / "score.json").stat().st_mtime_ns
        (exec_dir / "round-1" / "score.json").unlink()  # interrupted: work dirs left, no score
        calls = []
        real = source.score_build
        with mock.patch.object(source, "score_build", lambda *a: calls.append(a) or real(*a)):
            self.assertEqual(rounds.score_rounds(self.data, self.experiment, "A3", "d"), [])
        self.assertEqual(len(calls), 1)
        self.assertEqual((exec_dir / "round-0" / "score.json").stat().st_mtime_ns, kept)
        self.assertEqual(rounds.collect(self.data, self.experiment, "A3", "d").problems, [])

    def test_an_invalid_stored_score_is_scored_again(self):
        self.build_rep(1)
        self.build_rep(2)
        rounds.score_rounds(self.data, self.experiment, "A3", "d")
        stored = self.data / "rounds" / "fixture--A3--r1" / "exec-1" / "round-1" / "score.json"
        data = json.loads(stored.read_text())
        data["scores"]["replay"]["feature"] = float("nan")  # the right commit, but not a fraction
        stored.write_text(json.dumps(data))
        self.assertEqual(rounds.collect(self.data, self.experiment, "A3", "d").problems,
                         ["fixture--A3--r1: round 1 unscored"])
        self.assertEqual(rounds.score_rounds(self.data, self.experiment, "A3", "d"), [])
        self.assertEqual(json.loads(stored.read_text())["scores"]["replay"]["feature"], 1.0)

    def test_a_stored_score_for_other_defects_or_tests_is_scored_again(self):
        self.build_rep(1)
        self.build_rep(2)
        rounds.score_rounds(self.data, self.experiment, "A3", "d")
        stored = self.data / "rounds" / "fixture--A3--r1" / "exec-1" / "round-1" / "score.json"
        data = json.loads(stored.read_text())
        # Valid as a control's scores, but this task is caught: its one defect is missing.
        data["scores"]["replay"].update(review=None, defects=[])
        stored.write_text(json.dumps(data))
        self.assertEqual(rounds.collect(self.data, self.experiment, "A3", "d").problems,
                         ["fixture--A3--r1: round 1 unscored"])
        data["scores"]["replay"] = json.loads(stored.read_text())["scores"]["replay"] | {"review": 1.0}
        data["scores"]["replay"]["defects"] = [{"id": "zero", "avoided": True}]
        data["scores"]["replay"]["outcomes"].popitem()  # one frozen test unaccounted for
        stored.write_text(json.dumps(data))
        self.assertEqual(rounds.collect(self.data, self.experiment, "A3", "d").problems,
                         ["fixture--A3--r1: round 1 unscored"])

    def test_the_command_writes_an_incomplete_report_into_a_missing_data_dir(self):
        from eval.__main__ import main
        experiment = dataclasses.replace(self.experiment, data_dir=str(self.tmp / "absent"))
        with mock.patch.dict(experiments.EXPERIMENTS, {"replay": experiment}), \
                mock.patch.object(rounds, "verify_freeze", return_value="d"), mock.patch("sys.stdout"):
            self.assertEqual(main(["rounds", "--experiment", "replay", "--arm", "A3"]), 1)
        self.assertIn("INCOMPLETE, withheld", (self.tmp / "absent" / "rounds.md").read_text())

    def test_a_build_without_its_repo_is_listed_not_raised(self):
        self.build_rep(1)
        self.build_rep(2)
        shutil.rmtree(self.data / "runs" / "fixture--A3--r2" / "exec-1" / "repo")
        self.assertEqual(rounds.score_rounds(self.data, self.experiment, "A3", "d"), [])
        built = rounds.collect(self.data, self.experiment, "A3", "d")
        self.assertEqual([p.split(":")[0] for p in built.problems], ["fixture--A3--r2"])

    def test_a_missing_build_or_unscored_round_is_incomplete(self):
        self.build_rep(1)
        rounds.score_rounds(self.data, self.experiment, "A3", "d")
        (self.data / "rounds" / "fixture--A3--r1" / "exec-1" / "round-1" / "score.json").unlink()
        built = rounds.collect(self.data, self.experiment, "A3", "d")
        self.assertEqual(built.problems, ["fixture--A3--r2: no complete build", "fixture--A3--r1: round 1 unscored"])
        self.assertEqual(rounds.decide(built).verdict, "INCOMPLETE")

    def test_the_command_refuses_a_changed_freeze_before_scoring(self):
        from eval.__main__ import main
        with mock.patch.object(rounds, "changed_inputs", return_value=["eval/harness/tasks.py"]), \
                mock.patch.object(rounds, "score_rounds") as scored, \
                mock.patch("sys.stderr"):
            self.assertEqual(main(["rounds", "--experiment", "replay", "--arm", "A3"]), 3)
        scored.assert_not_called()

