import contextlib
import dataclasses
import hashlib
import io
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

from eval.__main__ import main
from eval.harness import agents, cells, experiments, loop, manifest, pilot, prompts, records, tasks
from eval.harness.pilot import BuildJob
from eval.tests.test_source import PRE_REVIEW_CALC, REFERENCE_CALC, FixtureCase, git

OPUS, SOL = experiments.OPUS, experiments.SOL
PLAIN_REVIEW = experiments.PLAIN_REVIEW
NONE, SAME, OTHER = (PLAIN_REVIEW.arm(name) for name in ("none", "same", "other"))
REVIEW_TEXT = "1. calc.py:5: div(1, 0) raises ZeroDivisionError where TASK.md asks for a ValueError.\n"
FIX_TEXT = "Guarded the divisor.\n\n1: fixed, a zero divisor now raises ValueError\n"
BUILD_PROMPT = "Implement TASK.md"


class CellCase(FixtureCase):
    """The fixture replay task with stub sessions: the build ships `self.build_calc`, the reviewer answers
    `self.review_text`, and the fixer writes `self.fix_calc` and answers `self.fix_text`. Every session is
    logged with the files and the calc.py it found."""

    def setUp(self):
        super().setUp()
        self.assertEqual(tasks.validate(self.task), [])
        self.runs = self.tmp / "runs"
        self.experiment = dataclasses.replace(PLAIN_REVIEW, tasks=("fixture",))
        self.calls: list[dict] = []
        self.build_calc, self.fix_calc = PRE_REVIEW_CALC, REFERENCE_CALC
        self.review_text, self.fix_text = REVIEW_TEXT, FIX_TEXT
        self.reviewer_writes = False
        self.times_out = None  # a role whose review or fix session runs out of wall clock

    def agent(self, model, prompt, *, role, cwd, out_dir, provider=agents.NATIVE, offline=False):
        out_dir.mkdir(parents=True, exist_ok=True)
        transcript = out_dir / "transcript.jsonl"
        transcript.touch()
        building = role == agents.WRITER and BUILD_PROMPT in prompt
        self.calls.append({"role": role, "model": model, "prompt": prompt, "offline": offline, "building": building,
                           "files": sorted(p.name for p in cwd.iterdir()), "calc": (cwd / "calc.py").read_text()})
        if building:
            (cwd / "calc.py").write_text(self.build_calc)
            return agents.Session(agents.COMPLETED, "ok", "built", transcript, 0.1)
        if role == self.times_out:
            return agents.Session(agents.TIMEOUT, "wall clock", "", transcript, 1.0)
        if role == agents.REVIEWER:
            if self.reviewer_writes:
                (cwd / "calc.py").write_text(REFERENCE_CALC)
            return agents.Session(agents.COMPLETED, "ok", self.review_text, transcript, 0.1)
        (cwd / "calc.py").write_text(self.fix_calc)
        return agents.Session(agents.COMPLETED, "ok", self.fix_text, transcript, 0.1)

    def sessions(self):
        return mock.patch.multiple(agents, run_claude=self.agent, run_codex=self.agent)

    def build(self) -> Path:
        with self.sessions():
            record = loop.execute_run(self.task, NONE, self.runs, manifest_hash="d", offline=True)
        self.assertEqual(record["status"], "complete", record.get("invalid_reason"))
        return self.runs / record["run_id"] / record["exec"]

    def cell(self, build_exec: Path, arm: loop.Arm = SAME, rep: int = 1) -> dict:
        with self.sessions():
            return loop.execute_cell(self.task, build_exec, arm, self.runs, rep=rep, manifest_hash="d", offline=True)

    def replay(self, record: dict) -> tuple:
        scores = record["scores"]["replay"]
        return scores["review"], scores["feature"], scores["regressions"]

    def schedule_patches(self) -> tuple:
        """The fixture task as the experiment's one task, stub sessions, and the schedule's log kept quiet."""
        return (self.sessions(), mock.patch.object(pilot, "scored_tasks", lambda experiment: [self.task]),
                contextlib.redirect_stdout(io.StringIO()))

    def frozen(self) -> contextlib.ExitStack:
        """`schedule_patches` with the freeze taken as verified, under manifest digest "d"."""
        stack = contextlib.ExitStack()
        for patch in (*self.schedule_patches(), mock.patch.object(manifest, "verify", return_value="d")):
            stack.enter_context(patch)
        return stack

    def run_cells(self, only_arm=None, experiment=None) -> int:
        with self.frozen():
            return cells.run(self.tmp, experiment or self.experiment, only_arm=only_arm)

    def run_with_one_failed_review(self) -> str:
        """A whole run in which the first review session cannot start and is not retried: that cell's first
        execution is invalid and the run fails. Returns the cell's run id."""
        failing = iter([True])

        def flaky(model, prompt, *, role, cwd, out_dir, provider=agents.NATIVE, offline=False):
            if role == agents.REVIEWER and next(failing, False):
                raise OSError("spawn failed")
            return self.agent(model, prompt, role=role, cwd=cwd, out_dir=out_dir, provider=provider, offline=offline)

        with self.frozen(), mock.patch.object(loop, "MAX_ATTEMPTS", 1), \
             mock.patch.multiple(agents, run_claude=flaky, run_codex=flaky):
            self.assertEqual(cells.run(self.tmp, self.experiment), 1)
        invalid = [path.parent.parent.name for path in self.runs.glob("fixture--*--r*/exec-1/record.json")
                   if json.loads(path.read_text())["status"] == "invalid"]
        self.assertEqual(len(invalid), 1)
        return invalid[0]


class ExecuteCellTest(CellCase):
    def test_a_cell_reviews_the_build_once_and_scores_the_fix(self):
        build_exec = self.build()
        build = json.loads((build_exec / "record.json").read_text())
        self.assertEqual(self.replay(build), (0.0, 1.0, 0.0))
        base = git(build_exec / "repo", "rev-list", "--max-parents=0", "HEAD").strip()
        record = self.cell(build_exec, OTHER, rep=2)
        self.assertEqual(record["status"], "complete", record.get("invalid_reason"))
        self.assertEqual([step["name"] for step in record["steps"]], ["review", "fix"])
        self.assertEqual(self.replay(record), (1.0, 1.0, 0.0))
        review, fix = self.calls[1:]
        self.assertEqual((review["role"], review["model"], review["prompt"]),
                         (agents.REVIEWER, SOL.model, prompts.plain_review(base)))
        self.assertEqual((fix["role"], fix["model"], fix["prompt"]),
                         (agents.WRITER, OPUS.model, prompts.plain_fix(REVIEW_TEXT)))
        self.assertTrue(all(call["offline"] for call in self.calls))
        self.assertEqual((record["run_id"], record["rep"], record["arm"]), ("fixture--other--r2", 2, "other"))
        saved = records.cell(self.runs / "fixture--other--r2" / "exec-1" / "record.json", "d")
        self.assertEqual((saved["build_run"], saved["build_exec"], saved["build_artifact"]),
                         ("fixture--none", "exec-1", loop.artifact_digest(build_exec / "artifact")))
        self.assertEqual((saved["review_text"], saved["fix_text"]), (REVIEW_TEXT, FIX_TEXT))

    def test_it_starts_from_the_scored_tree_without_what_the_stored_repo_left_untracked(self):
        build_exec = self.build()
        (build_exec / "repo" / "junk.txt").write_text("left by the build's session\n")
        self.cell(build_exec)
        review = self.calls[1]
        self.assertNotIn("junk.txt", review["files"])
        self.assertEqual(review["calc"], PRE_REVIEW_CALC)

    def test_a_stored_repo_past_the_scored_tree_is_refused_before_any_session(self):
        build_exec = self.build()
        repo = build_exec / "repo"
        (repo / "calc.py").write_text(REFERENCE_CALC)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "after the score")
        with self.assertRaisesRegex(ValueError, "not the tree that was scored"):
            self.cell(build_exec)
        self.assertEqual(len(self.calls), 1)  # the build's own session
        self.assertFalse((self.runs / "fixture--same--r1").exists())

    def test_a_reviewer_that_writes_is_reset_and_flagged(self):
        self.reviewer_writes, self.fix_calc = True, PRE_REVIEW_CALC
        record = self.cell(self.build())
        self.assertEqual(record["flags"], ["reviewer_wrote:review"])
        self.assertEqual(self.calls[2]["calc"], PRE_REVIEW_CALC)  # the fixer never saw the reviewer's edit
        self.assertEqual(self.replay(record)[0], 0.0)

    def test_a_reviewer_timeout_scores_the_builds_own_tree(self):
        build_exec = self.build()
        build = json.loads((build_exec / "record.json").read_text())
        self.times_out = agents.REVIEWER
        record = self.cell(build_exec)
        self.assertEqual((record["status"], record["flags"]), ("complete", ["timeout:review"]))
        self.assertEqual([step["name"] for step in record["steps"]], ["review"])
        self.assertEqual(self.replay(record), self.replay(build))
        self.assertEqual((record["review_text"], record["fix_text"]), ("", ""))
        self.assertIsNotNone(records.cell(self.runs / "fixture--same--r1" / "exec-1" / "record.json", "d"))

    def test_a_fix_timeout_is_scored_as_it_stands_and_keeps_the_review(self):
        build_exec = self.build()
        self.times_out = agents.WRITER
        record = self.cell(build_exec)
        self.assertEqual((record["status"], record["flags"]), ("complete", ["timeout:fix"]))
        self.assertEqual((record["review_text"], record["fix_text"]), (REVIEW_TEXT, ""))
        self.assertEqual(self.replay(record)[0], 0.0)

    def test_what_a_cell_refuses(self):
        build_exec = self.build()
        dev = tasks.Task(tasks.TASKS_ROOT / "dev-intervals")
        shutil.copytree(self.task.root, self.tmp / "tasks" / "another")
        another = tasks.Task(self.tmp / "tasks" / "another")
        refusals = {
            "is of fixture, not another": lambda: loop.execute_cell(another, build_exec, SAME, self.runs, rep=1,
                                                                    manifest_hash="d", offline=True),
            "not a review cell": lambda: loop.execute_cell(self.task, build_exec, NONE, self.runs, rep=1,
                                                           manifest_hash="d", offline=True),
            "offline": lambda: loop.execute_cell(self.task, build_exec, SAME, self.runs, rep=1, manifest_hash="d"),
            "replayed source repository": lambda: loop.execute_cell(dev, build_exec, SAME, self.runs, rep=1,
                                                                    manifest_hash="d", offline=True),
            "no complete build record": lambda: loop.execute_cell(self.task, build_exec, SAME, self.runs, rep=1,
                                                                  manifest_hash="another manifest", offline=True),
        }
        for message, call in refusals.items():
            with self.subTest(message), self.sessions(), self.assertRaisesRegex(ValueError, message):
                call()
        self.assertEqual(len(self.calls), 1)
        self.assertFalse((self.runs / "fixture--same--r1").exists())

    def test_the_loop_runner_refuses_a_plain_arm(self):
        with self.sessions(), self.assertRaisesRegex(ValueError, "review cell"):
            loop.execute_run(self.task, SAME, self.runs, manifest_hash="d", offline=True)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.runs.exists())

    def test_a_build_record_is_not_a_cell_record_and_a_cell_needs_all_its_fields(self):
        build_exec = self.build()
        self.assertIsNone(records.cell(build_exec / "record.json", "d"))
        self.cell(build_exec)
        saved = self.runs / "fixture--same--r1" / "exec-1" / "record.json"
        self.assertIsNotNone(records.build(saved, "d"))  # resume and cost read a cell as a build
        for key in ("build_run", "build_exec", "build_artifact", "review_text", "fix_text", "rep"):
            with self.subTest(key):
                data = json.loads(saved.read_text())
                del data[key]
                broken = self.tmp / "broken.json"
                broken.write_text(json.dumps(data))
                self.assertIsNone(records.cell(broken, "d"))


class ScheduleTest(unittest.TestCase):
    def test_one_build_per_task_then_every_cell_of_every_reviewer(self):
        plan = cells.schedule(PLAIN_REVIEW)
        task_ids = PLAIN_REVIEW.tasks
        self.assertEqual(sorted(plan.builds), sorted(BuildJob(task, "none", None) for task in task_ids))
        self.assertEqual(sorted(plan.cells), sorted(BuildJob(task, arm, rep) for task in task_ids
                                                    for arm in ("same", "other") for rep in (1, 2)))

    def test_the_order_of_a_three_task_experiment(self):
        three = dataclasses.replace(PLAIN_REVIEW, tasks=PLAIN_REVIEW.tasks[:3])
        plan = cells.schedule(three)
        self.assertEqual([job.task for job in plan.builds],
                         ["jobscan-email-v2", "jobscan-workable-gem-v2", "jobscan-greenhouse-v2"])
        short = {"jobscan-email-v2": "email", "jobscan-greenhouse-v2": "greenhouse", "jobscan-workable-gem-v2": "gem"}
        self.assertEqual([(short[job.task], job.arm, job.rep) for job in plan.cells], [
            ("email", "other", 1), ("greenhouse", "same", 2), ("gem", "other", 1), ("greenhouse", "other", 2),
            ("email", "same", 2), ("email", "same", 1), ("greenhouse", "same", 1), ("gem", "same", 2),
            ("greenhouse", "other", 1), ("gem", "other", 2), ("gem", "same", 1), ("email", "other", 2)])

    def test_the_orders_depend_only_on_the_seed(self):
        self.assertEqual(cells.schedule(PLAIN_REVIEW), cells.schedule(PLAIN_REVIEW))
        reseeded = cells.schedule(dataclasses.replace(PLAIN_REVIEW, seed=PLAIN_REVIEW.seed + 7))
        self.assertNotEqual(reseeded.cells, cells.schedule(PLAIN_REVIEW).cells)

    def test_cell_runs_are_numbered_from_one_even_when_there_is_one(self):
        single = dataclasses.replace(PLAIN_REVIEW, reps=1)
        self.assertEqual({job.rep for job in cells.schedule(single).cells}, {1})

    def test_the_replay_keeps_its_schedule(self):
        jobs = [[job.task, job.arm, job.rep] for job in pilot.build_schedule(experiments.REPLAY)]
        self.assertEqual(hashlib.sha256(json.dumps(jobs).encode()).hexdigest(),
                         "f8fd051472ceb25854c44b041a6556a3a99f42b74f27d2f9b29957e52bed6be6")


class RunTest(CellCase):
    """The paired schedule over the fixture task, with stub sessions and the freeze taken as verified."""

    def run_dirs(self) -> list[str]:
        return sorted(path.name for path in self.runs.iterdir())

    def test_a_run_builds_once_then_runs_every_cell_and_a_rerun_calls_no_model(self):
        self.assertEqual(self.run_cells(), 0)
        self.assertEqual([call["building"] for call in self.calls], [True] + [False] * 8)
        self.assertEqual(self.run_dirs(), ["fixture--none", "fixture--other--r1", "fixture--other--r2",
                                           "fixture--same--r1", "fixture--same--r2"])
        build = cells.pairing(self.runs / "fixture--none" / "exec-1")
        for name in self.run_dirs()[1:]:
            record = records.cell(self.runs / name / "exec-1" / "record.json", "d")
            self.assertEqual(cells.pairing_of(record), build, name)
        reviewers = [call["model"] for call in self.calls if call["role"] == agents.REVIEWER]
        self.assertEqual(sorted(reviewers), sorted([OPUS.model] * 2 + [SOL.model] * 2))
        self.calls.clear()
        self.assertEqual(self.run_cells(), 0)
        self.assertEqual(self.calls, [])

    def test_a_task_whose_build_changed_is_refused_even_after_every_cell_finished(self):
        self.assertEqual(self.run_cells(), 0)
        self.build_calc = REFERENCE_CALC
        self.build()  # a newer complete build of the task, with another artifact
        self.calls.clear()
        self.assertEqual(self.run_cells(), 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.run_cells(only_arm="same"), 1)

    def test_only_arm_runs_that_arms_jobs(self):
        self.assertEqual(self.run_cells(only_arm="same"), 1)  # no build yet, and none is made for it
        self.assertEqual(self.calls, [])
        self.assertEqual(self.run_cells(only_arm="none"), 0)
        self.assertEqual([call["building"] for call in self.calls], [True])
        self.assertEqual(self.run_cells(only_arm="other"), 0)
        self.assertEqual(self.run_dirs(), ["fixture--none", "fixture--other--r1", "fixture--other--r2"])
        self.assertEqual({call["model"] for call in self.calls if call["role"] == agents.REVIEWER}, {SOL.model})

    def test_a_failed_cell_fails_the_run_and_is_run_again(self):
        failed = self.run_with_one_failed_review()
        self.assertEqual(self.run_cells(), 0)
        self.assertTrue((self.runs / failed / "exec-2" / "record.json").exists())

    def test_a_routed_experiment_is_refused_before_anything_runs(self):
        routed = dataclasses.replace(self.experiment, arms=(
            NONE, loop.Arm("same", OPUS, experiments.GLM_REVIEWER, review=loop.PLAIN)))
        with self.assertRaisesRegex(ValueError, "native sessions only"):
            self.run_cells(experiment=routed)
        self.assertEqual(self.calls, [])

    def test_an_unfrozen_experiment_runs_nothing(self):
        with contextlib.ExitStack() as stack:
            for patch in self.schedule_patches():
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(manifest, "MANIFESTS", self.tmp / "no-manifests"))
            with self.assertRaises(manifest.ManifestMismatch):
                cells.run(self.tmp, self.experiment)
        self.assertEqual(self.calls, [])

    def test_finished_means_the_review_and_the_fix_both_ran_to_the_end(self):
        build_exec = self.build()
        self.assertTrue(cells.finished(self.cell(build_exec, rep=1)))
        self.times_out = agents.REVIEWER
        self.assertFalse(cells.finished(self.cell(build_exec, rep=2)))
        self.times_out = agents.WRITER
        self.assertFalse(cells.finished(self.cell(build_exec, rep=3)))
        self.times_out, self.fix_text = None, " \n"
        silent = self.cell(build_exec, rep=4)
        self.assertEqual(silent["status"], "complete")
        self.assertFalse(cells.finished(silent))

    def test_calibration_builds_afresh_and_runs_its_cells_unscored(self):
        with contextlib.ExitStack() as stack:
            for patch in self.schedule_patches():
                stack.enter_context(patch)
            built = cells.calibrate(self.tmp, self.experiment, ["fixture"], "other", reps=2)
        self.assertEqual([record["run_id"] for record in built], ["fixture--other--r1", "fixture--other--r2"])
        self.assertTrue(all(record["manifest"] is None and cells.finished(record) for record in built))
        self.assertTrue((self.tmp / "calibration" / "runs" / "fixture--none" / "exec-1" / "artifact").is_dir())
        self.assertFalse(self.runs.exists())


def cell_record(**changes) -> dict:
    step = {"attempts": [{"terminal": agents.COMPLETED, "seconds": 1.0, "usage": {}}]}
    return {"run_id": "t--same--r1", "exec": "exec-1", "status": "complete", "flags": [], "scores": {},
            "steps": [{"name": "review", **step}, {"name": "fix", **step}], "review_text": "1. x", "fix_text": "1: fixed",
            **changes}


class FinishedTest(unittest.TestCase):
    """`finished` on records as they are read back: each condition on its own."""

    def attempt(self, terminal: str) -> dict:
        return {"terminal": terminal, "seconds": 1.0, "usage": {}}

    def steps(self, review: list[str], fix: list[str]) -> list[dict]:
        return [{"name": "review", "attempts": [self.attempt(t) for t in review]},
                {"name": "fix", "attempts": [self.attempt(t) for t in fix]}]

    def test_a_cell_whose_two_steps_completed_and_spoke_is_finished(self):
        self.assertTrue(cells.finished(cell_record()))
        retried = cell_record(steps=self.steps([agents.INFRA, agents.COMPLETED], [agents.COMPLETED]))
        self.assertTrue(cells.finished(retried))  # the last attempt of a step is the one that counts

    def test_each_way_of_not_finishing(self):
        unfinished = {
            "a fix whose last session timed out": cell_record(steps=self.steps([agents.COMPLETED], [agents.TIMEOUT])),
            "a review whose last session timed out": cell_record(steps=self.steps([agents.TIMEOUT], [agents.COMPLETED])),
            "no fix step": cell_record(steps=self.steps([agents.COMPLETED], [agents.COMPLETED])[:1]),
            "an invalid record": cell_record(status="invalid"),
            "a reviewer that said nothing": cell_record(review_text="\n"),
            "a fixer that said nothing": cell_record(fix_text=""),
        }
        for name, record in unfinished.items():
            with self.subTest(name):
                self.assertFalse(cells.finished(record))


class CliTest(unittest.TestCase):
    def cli(self, *argv) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main(list(argv))

    def test_run_sends_a_paired_experiment_to_its_own_schedule(self):
        with mock.patch.object(cells, "run", return_value=0) as run, \
             mock.patch.object(pilot, "run_builds", side_effect=AssertionError("the pilot's schedule")):
            self.assertEqual(self.cli("run", "--experiment", "plain-review", "--arm", "same"), 0)
        self.assertEqual((run.call_args.args[1], run.call_args.kwargs), (PLAIN_REVIEW, {"only_arm": "same"}))

    def test_other_experiments_keep_the_pilots_schedule(self):
        with mock.patch.object(pilot, "run_builds", return_value=0) as run_builds, \
             mock.patch.object(cells, "run", side_effect=AssertionError("the paired schedule")):
            self.assertEqual(self.cli("run", "--experiment", "replay", "--arm", "A1"), 0)
        self.assertEqual(run_builds.call_args.kwargs, {"only_arm": "A1"})

    def test_a_paired_experiment_has_no_dev_build_and_no_cost_command(self):
        with mock.patch.object(cells, "run", side_effect=AssertionError("must not run")):
            self.assertEqual(self.cli("run", "--experiment", "plain-review", "--task", "dev-intervals", "--arm",
                                      "same"), 2)
            self.assertEqual(self.cli("run", "--experiment", "plain-review", "--arm", "A3"), 2)
        self.assertEqual(self.cli("cost", "--experiment", "plain-review"), 2)

    def test_calibrate_sends_a_cell_arm_to_cells_and_succeeds_only_on_finished_cells(self):
        argv = ("calibrate", "--experiment", "plain-review", "--task", "jobscan-watchlist-v2", "--arm", "other",
                "--reps", "1")
        timed_out = cell_record(flags=["timeout:review"], steps=cell_record()["steps"][:1], review_text="")
        for record, code in ((cell_record(), 0), (timed_out, 1)):
            with self.subTest(code=code), mock.patch.object(cells, "calibrate", return_value=[record]) as calibrate, \
                 mock.patch.object(pilot, "run_calibration", side_effect=AssertionError("a build calibration")):
                self.assertEqual(self.cli(*argv), code)
                self.assertEqual(calibrate.call_args.args[2:], (["jobscan-watchlist-v2"], "other", 1))

    def test_calibrating_the_baseline_stays_a_build_calibration(self):
        with mock.patch.object(pilot, "run_calibration", return_value=[]) as run_calibration, \
             mock.patch.object(cells, "calibrate", side_effect=AssertionError("a cell calibration")):
            self.assertEqual(self.cli("calibrate", "--experiment", "plain-review", "--task", "jobscan-watchlist-v2",
                                      "--reps", "1"), 1)
        self.assertEqual(run_calibration.call_args.args[2:], (["jobscan-watchlist-v2"], "none", 1))


class ArmTest(unittest.TestCase):
    def test_the_plain_review_experiment_declares_two_cells_on_one_baseline(self):
        self.assertEqual((PLAIN_REVIEW.baseline, PLAIN_REVIEW.cell_arms, PLAIN_REVIEW.reps, PLAIN_REVIEW.report),
                         ("none", ("same", "other"), 2, "paired"))
        self.assertEqual((NONE.reviewer, SAME.reviewer, OTHER.reviewer), (None, OPUS, SOL))
        self.assertTrue(PLAIN_REVIEW.offline and not PLAIN_REVIEW.routed)
        self.assertTrue(all(not experiment.cell_arms for name, experiment in experiments.EXPERIMENTS.items()
                            if name != "plain-review"))

    def test_an_arm_is_a_loop_arm_unless_it_says_plain(self):
        self.assertEqual(loop.Arm("A3", OPUS, SOL).review, loop.LOOP)
        self.assertEqual(loop.Arm("A3", OPUS, SOL), loop.Arm("A3", OPUS, SOL, review=loop.LOOP))

    def test_a_plain_arm_needs_a_reviewer_and_a_known_kind(self):
        with self.assertRaisesRegex(ValueError, "needs a reviewer"):
            loop.Arm("solo", OPUS, review=loop.PLAIN)
        with self.assertRaisesRegex(ValueError, "unknown review kind"):
            loop.Arm("x", OPUS, SOL, review="rules")


class PlainPromptTest(unittest.TestCase):
    def test_neither_prompt_reads_the_skill_files(self):
        missing = Path("/nonexistent/skill.md")
        with mock.patch.multiple(prompts, CODEX_REVIEW_SKILL=missing, BUILD_LOOP_SKILL=missing, RUBRIC=missing):
            review, fix = prompts.plain_review("abc123"), prompts.plain_fix(REVIEW_TEXT)
        for text in (review, fix):
            self.assertNotIn(prompts.rubric()[:300], text)
            self.assertNotIn("VERDICT", text)
            self.assertNotIn("[HIGH", text)

    def test_the_review_names_the_base_and_asks_for_numbered_findings(self):
        review = prompts.plain_review("abc123")
        self.assertIn("`git diff abc123..HEAD`", review)
        self.assertIn("Number your findings.", review)
        self.assertIn("Do not modify anything.", review)

    def test_the_fix_carries_the_review_and_asks_for_a_closing_line_per_finding(self):
        fix = prompts.plain_fix("  " + REVIEW_TEXT + "\n")
        self.assertIn(f"<review>\n{REVIEW_TEXT.strip()}\n</review>", fix)
        self.assertIn("then `fixed` or `left`", fix)
        self.assertIn("end with the line `no findings`", fix)
        self.assertTrue(fix.startswith(prompts.solo_writer(brownfield=True).split("\nImplement")[0]))


if __name__ == "__main__":
    unittest.main()
