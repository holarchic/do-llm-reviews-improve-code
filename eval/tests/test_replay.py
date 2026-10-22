import json
import shutil
import unittest
from unittest import mock

from eval.harness import agents, experiments, loop, prompts, records, tasks
from eval.tests.test_source import PRE_REVIEW_CALC, REFERENCE_CALC, FixtureCase

OPUS = experiments.OPUS
SOLO = loop.Arm("A1", OPUS)
LOOP = loop.Arm("A3", OPUS, experiments.SOL)


class PromptTest(unittest.TestCase):
    def test_every_replay_prompt_says_the_repository_exists(self):
        replay = [
            prompts.solo_writer(brownfield=True),
            prompts.loop_plan_writer(brownfield=True),
            prompts.loop_plan_revision_writer("V", brownfield=True),
            prompts.loop_build_writer(brownfield=True),
            prompts.loop_fix_writer({"correctness": "x"}, brownfield=True),
            prompts.plan_review(brownfield=True),
            prompts.code_review("correctness", "abc", commits="c", final_state="f", brownfield=True),
            prompts.code_review("architecture", "abc", commits="c", final_state="f", brownfield=True),
        ]
        for text in replay:
            self.assertIn("existing repository", text)
            self.assertNotIn("empty repository", text)

    def test_the_replay_writer_differs_from_the_pilot_writer_only_in_its_preamble(self):
        swapped = prompts.loop_build_writer(brownfield=True).replace(
            prompts._BROWNFIELD_PREAMBLE, prompts._WRITER_PREAMBLE)
        self.assertEqual(swapped, prompts.loop_build_writer())


class RunTest(FixtureCase):
    """A stub-agent replay run on the fixture: seeded from base, built, scored on the replay groups, saved and
    read back by the records loader every consumer uses."""

    def setUp(self):
        super().setUp()
        self.assertEqual(tasks.validate(self.task), [])
        self.seen_task_md = []
        self.calc = REFERENCE_CALC

    def agent(self, model, prompt, *, role, cwd, out_dir, provider=agents.NATIVE):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "transcript.jsonl").touch()
        if role == agents.REVIEWER:
            return agents.Session(agents.COMPLETED, "ok", "VERDICT: GREEN\nISSUES:\nNone\n",
                                  out_dir / "transcript.jsonl", 0.1, reported_models=[model])
        self.seen_task_md.append((cwd / "TASK.md").read_text())
        if "existing repository" in prompt and ("Implement TASK.md" in prompt or "Build the plan" in prompt):
            (cwd / "calc.py").write_text(self.calc)
            shutil.rmtree(cwd / "tests")  # a build that drops the repo's tests gains nothing
        return agents.Session(agents.COMPLETED, "ok", "done", out_dir / "transcript.jsonl", 0.1,
                              reported_models=[model])

    def execute(self, arm: loop.Arm) -> dict:
        with mock.patch.object(agents, "run_claude", self.agent), mock.patch.object(agents, "run_codex", self.agent):
            return loop.execute_run(self.task, arm, self.tmp / "runs", rep=1, manifest_hash="d")

    def test_a_solo_replay_run_is_seeded_scored_and_reloadable(self):
        record = self.execute(SOLO)
        self.assertEqual(record["status"], "complete", record.get("invalid_reason"))
        self.assertTrue(self.seen_task_md)
        replay = record["scores"]["replay"]
        self.assertEqual((replay["review"], replay["feature"], replay["regressions"]), (1.0, 1.0, 0.0))
        saved = self.tmp / "runs" / record["run_id"] / record["exec"] / "record.json"
        self.assertEqual(records.build(saved, "d")["scores"]["replay"]["defects"], [{"id": "zero", "avoided": True}])

    def test_a_loop_replay_run_ships_the_historical_defect_when_the_writer_does(self):
        self.calc = PRE_REVIEW_CALC
        record = self.execute(LOOP)
        self.assertEqual(record["status"], "complete", record.get("invalid_reason"))
        self.assertEqual(record["scores"]["replay"]["review"], 0.0)
        self.assertIn("plan-review-1", [step["name"] for step in record["steps"]])

    def test_malformed_and_control_replay_records(self):
        record = self.execute(SOLO)
        saved = self.tmp / "runs" / record["run_id"] / record["exec"] / "record.json"
        data = json.loads(saved.read_text())
        data["scores"]["replay"]["review"] = None  # defects present, so review must be a fraction
        saved.write_text(json.dumps(data))
        self.assertIsNone(records.build(saved, "d"))
        data["scores"]["replay"]["defects"] = []  # a control: no defects, review None
        saved.write_text(json.dumps(data))
        self.assertIsNotNone(records.build(saved, "d"))


if __name__ == "__main__":
    unittest.main()
