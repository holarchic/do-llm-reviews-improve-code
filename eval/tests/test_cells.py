import json
import unittest
from pathlib import Path
from unittest import mock

from eval.harness import agents, experiments, loop, prompts, records, tasks
from eval.tests.test_source import PRE_REVIEW_CALC, REFERENCE_CALC, FixtureCase, git

OPUS, SOL = experiments.OPUS, experiments.SOL
NONE = loop.Arm("none", OPUS)
SAME = loop.Arm("same", OPUS, OPUS, review=loop.PLAIN)
OTHER = loop.Arm("other", OPUS, SOL, review=loop.PLAIN)
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
        refusals = {
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


class ArmTest(unittest.TestCase):
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
