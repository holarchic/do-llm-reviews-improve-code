import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval.harness import agents, loop, tasks

DEV = tasks.Task(tasks.TASKS_ROOT / "dev-intervals")
REFERENCE = (DEV.path("reference") / "intervals.py").read_text()


def session(out: Path, terminal=agents.COMPLETED, text="done") -> agents.Session:
    out.mkdir(parents=True, exist_ok=True)
    transcript = out / "transcript.jsonl"
    transcript.touch()
    return agents.Session(terminal, "fake", text, transcript, 0.1)


class FakeAgents:
    """Writer writes the reference on any build/solo step; reviewer answers from a script."""

    def __init__(self, review_text="VERDICT: YELLOW\nISSUES:\n1. [HIGH] intervals.py:1 — x\n"):
        self.review_text = review_text
        self.prompts: list[tuple[str, str]] = []
        self.writer_infra_left = 0

    def run_claude(self, model, prompt, *, role, cwd, out_dir):
        self.prompts.append((role, prompt))
        if role == agents.REVIEWER:
            return session(out_dir, text=self.review_text)
        if self.writer_infra_left:
            self.writer_infra_left -= 1
            (cwd / "junk.txt").write_text("partial")
            return session(out_dir, terminal=agents.INFRA, text="")
        if "Build the plan" in prompt or "Implement TASK.md" in prompt:
            (cwd / "intervals.py").write_text(REFERENCE)
        if "planning only" in prompt:
            (cwd / "plans").mkdir(exist_ok=True)
            (cwd / "plans" / "plan.md").write_text("# plan\n")
        return session(out_dir)

    def run_codex(self, model, prompt, *, role, cwd, out_dir):
        self.prompts.append((role, prompt))
        return session(out_dir, text=self.review_text)


class LoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)

    def run_arm(self, arm, fake):
        with mock.patch.object(agents, "run_claude", fake.run_claude), \
             mock.patch.object(agents, "run_codex", fake.run_codex):
            return loop.execute_run(DEV, arm, self.tmp, manifest_hash=None)

    def test_reviewer_never_green_hits_both_caps_and_head_is_scored(self):
        record = self.run_arm("A2", FakeAgents())
        self.assertEqual(record["status"], "complete")
        self.assertIn("plan_capped", record["flags"])
        self.assertIn("review_capped", record["flags"])
        self.assertEqual(record["rounds"], {"plan": 5, "phase": 5})
        names = [step["name"] for step in record["steps"]]
        self.assertEqual(sum(n.startswith("plan-review-") for n in names), 5)
        self.assertEqual(sum(n.startswith("plan-revise-") for n in names), 4)
        self.assertEqual(sum(n.startswith(("correctness-review-", "architecture-review-")) for n in names), 10)
        self.assertEqual(sum(n.startswith("fix-") for n in names), 4)
        self.assertEqual(record["scores"]["primary"], 1.0)

    def test_green_reviewer_ends_each_loop_after_one_round(self):
        record = self.run_arm("A3", FakeAgents(review_text="VERDICT: GREEN\nISSUES:\nNone\n"))
        self.assertEqual(record["rounds"], {"plan": 1, "phase": 1})
        self.assertEqual(record["flags"], [])

    def test_low_only_yellow_does_not_block(self):
        record = self.run_arm("A3", FakeAgents(review_text="VERDICT: YELLOW\n1. [LOW] a.py:1 — nit\n"))
        self.assertEqual(record["rounds"], {"plan": 1, "phase": 1})

    def test_malformed_verdict_counts_as_yellow(self):
        record = self.run_arm("A2", FakeAgents(review_text="Looks good to me."))
        self.assertIn("plan_capped", record["flags"])
        self.assertTrue(any(f.startswith("malformed_verdict:") for f in record["flags"]))

    def test_infra_retry_resets_to_last_commit(self):
        fake = FakeAgents()
        fake.writer_infra_left = 1
        record = self.run_arm("A1", fake)
        self.assertEqual(record["status"], "complete")
        self.assertEqual(len(record["steps"][0]["attempts"]), 2)
        repo = self.tmp / "dev-intervals--A1" / "exec-1" / "repo"
        self.assertFalse((repo / "junk.txt").exists())
        self.assertEqual(record["scores"]["primary"], 1.0)

    def test_infra_exhausted_makes_run_invalid(self):
        fake = FakeAgents()
        fake.writer_infra_left = 3
        record = self.run_arm("A1", fake)
        self.assertEqual(record["status"], "invalid")
        self.assertNotIn("scores", record)
        self.assertEqual(len(record["steps"][0]["attempts"]), 3)

    def test_solo_writer_never_sees_rules(self):
        fake = FakeAgents()
        self.run_arm("A1", fake)
        self.assertEqual(len(fake.prompts), 1)
        self.assertNotIn("Full rubric", fake.prompts[0][1])


class VerdictTest(unittest.TestCase):
    def test_labels(self):
        self.assertFalse(loop.parse_verdict("VERDICT: GREEN\n").blocking)
        self.assertTrue(loop.parse_verdict("**VERDICT: RED**\n1. [MEDIUM] x").blocking)
        self.assertTrue(loop.parse_verdict("VERDICT: YELLOW\nno tags").blocking)
        self.assertTrue(loop.parse_verdict("").malformed)


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)

    def transcript(self, *events):
        path = self.tmp / "t.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in events))
        return path

    def claude(self, events, exit_code=0, timed_out=False, role=agents.WRITER):
        proc = agents.procs.ProcResult(exit_code, timed_out, 1.0)
        return agents.classify_claude(self.transcript(*events), proc, role).terminal

    def test_claude_classes(self):
        ok = {"type": "result", "subtype": "success", "is_error": False, "result": "hi", "usage": {}}
        partial = {"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}}
        self.assertEqual(self.claude([ok]), agents.COMPLETED)
        self.assertEqual(self.claude([partial], exit_code=1), agents.INFRA)
        self.assertEqual(self.claude([{**ok, "is_error": True}]), agents.INFRA)
        self.assertEqual(self.claude([{**ok, "subtype": "error_during_execution"}]), agents.INFRA)
        self.assertEqual(self.claude([ok], exit_code=1), agents.INFRA)
        self.assertEqual(self.claude([partial], exit_code=None, timed_out=True), agents.TIMEOUT)
        self.assertEqual(self.claude([partial], exit_code=None, timed_out=True, role=agents.JUDGE), agents.INFRA)

    def codex(self, events, final="VERDICT: GREEN", exit_code=0):
        final_path = self.tmp / "final.txt"
        final_path.write_text(final)
        proc = agents.procs.ProcResult(exit_code, False, 1.0)
        return agents.classify_codex(self.transcript(*events), final_path, proc, agents.REVIEWER, "m").terminal

    def test_codex_classes(self):
        done = {"type": "turn.completed", "usage": {"input_tokens": 1}}
        self.assertEqual(self.codex([done]), agents.COMPLETED)
        self.assertEqual(self.codex([{"type": "error", "message": "usage limit"}, done]), agents.INFRA)
        self.assertEqual(self.codex([{"type": "turn.started"}], exit_code=1), agents.INFRA)
        self.assertEqual(self.codex([done], final=""), agents.INFRA)


if __name__ == "__main__":
    unittest.main()
