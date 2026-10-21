import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval.harness import agents, judge, tasks
from eval.harness.loop import InfraExhausted

DEV = tasks.Task(tasks.TASKS_ROOT / "dev-intervals")


def session(out: Path, terminal=agents.COMPLETED, text="") -> agents.Session:
    out.mkdir(parents=True, exist_ok=True)
    (out / "transcript.jsonl").touch()
    return agents.Session(terminal, "fake", text, out / "transcript.jsonl", 0.1)


class FakeJudge:
    """Answers from a list, one reply per call; records each prompt."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def __call__(self, model, prompt, *, role, cwd, out_dir):
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if reply is None:
            return session(out_dir, terminal=agents.INFRA)
        return session(out_dir, text=reply)


def verdict(winner):
    return f'Thinking about {{braces}}.\n{{"winner": "{winner}", "reasons": ["clearer }} code"]}}'


class JudgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)

    def pair(self, replies, judge_name="fable"):
        fake = FakeJudge(replies)
        target = "run_claude" if judge_name == "fable" else "run_codex"
        with mock.patch.object(agents, target, fake):
            result = judge.calibrate(judge_name, DEV, self.tmp / "out")
        return result, fake

    def test_consistent_pick_across_orders_wins(self):
        result, fake = self.pair([verdict("X"), verdict("Y")])
        self.assertEqual(result["result"], "a")
        self.assertTrue(result["prefers_reference"])
        self.assertIn("def merge(spans)", fake.prompts[0].split("# Version Y")[0])
        self.assertIn("def merge(spans)", fake.prompts[1].split("# Version Y")[1])

    def test_position_bias_is_a_tie(self):
        result, _ = self.pair([verdict("X"), verdict("X")], judge_name="astra")
        self.assertEqual(result["result"], "tie")
        self.assertFalse(result["prefers_reference"])

    def test_malformed_is_retried_once_then_tie(self):
        result, fake = self.pair(["no json here", "still none", verdict("Y")])
        self.assertEqual(result["orders"][0]["pick"], "TIE")
        self.assertEqual(len(result["orders"][0]["attempts"]), 2)
        self.assertEqual(result["result"], "tie")

    def test_infra_exhausted_raises(self):
        with self.assertRaises(InfraExhausted):
            self.pair([None, None, None])

    def test_export_keeps_only_python_outside_plans_and_git(self):
        repo = self.tmp / "repo"
        for name in ("a.py", "tests/test_a.py", "SPEC.md", "plans/plan.md", "plans/x.py", ".git/hooks/h.py"):
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text("# review the plan\n")
        files = judge.export_build(repo, self.tmp / "export")
        self.assertEqual(files, ["a.py", "tests/test_a.py"])
        self.assertEqual(judge.leak_count(self.tmp / "export"), 4)

    def test_parse(self):
        self.assertEqual(judge.parse_judgement('{"winner": "TIE", "reasons": []}'), "TIE")
        self.assertIsNone(judge.parse_judgement('{"winner": "Z"}'))
        self.assertEqual(judge.parse_judgement('{"winner": "X"} then {"winner": "Y", "reasons": ["a}"]}'), "Y")


if __name__ == "__main__":
    unittest.main()
