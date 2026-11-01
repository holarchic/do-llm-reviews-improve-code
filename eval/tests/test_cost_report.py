import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from eval.harness import agents, cost_report, pilot, prices

ARM = SimpleNamespace(name="R", writer=agents.Agent("claude", "claude-opus-5-5"),
                      reviewer=agents.Agent("codex", "gpt-6.1-sol"))


def codex_usage(prompt, cached=0, output=0) -> dict:
    return {"input_tokens": prompt, "cached_input_tokens": cached, "cache_write_input_tokens": 0,
            "output_tokens": output, "reasoning_output_tokens": 0}


class CostReportTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="eval-cost-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.experiment = SimpleNamespace(name="stub", tasks=("t1", "t2"), rep_ids=(1,), arm_names=("R",),
                                          arm=lambda name: ARM)

    def write_exec(self, run_id: str, number: int, steps: list) -> Path:
        exec_dir = self.root / "runs" / run_id / f"exec-{number}"
        exec_dir.mkdir(parents=True)
        (exec_dir / "record.json").write_text(json.dumps({"steps": steps}), encoding="utf-8")
        return exec_dir

    def test_scored_and_unscored_spend_ranges_and_unknowns_reach_the_page(self):
        review = {"name": "correctness-review-1", "attempts": [{"usage": codex_usage(1_000_000, 800_000, 10_000),
                                                                "seconds": 60}]}
        scored = self.write_exec("t1--R--r1", 2, [review, {"name": "fix-1", "attempts": [{"usage": {}, "seconds": 6}]}])
        self.write_exec("t1--R--r1", 1, [review])  # an older execution: spend that was not scored
        self.write_exec("t2--R--r1", 1, [review])  # never completed: no scored execution
        complete = {("t1", 1): scored}
        with mock.patch.object(cost_report, "frozen_digest", lambda experiment: "d"), \
                mock.patch.object(pilot, "complete_build",
                                  lambda runs, task, arm, digest, rep: complete.get((task, rep))):
            report = cost_report.collect(self.root, self.experiment)
        cost = report.arms["R"]
        self.assertEqual((cost.builds, cost.missing), (1, ["t2--R--r1"]))
        self.assertEqual((cost.total.attempts, cost.total.unknown, cost.total.ranged), (2, 1, 1))
        self.assertEqual(cost.unscored.attempts, 2)
        self.assertEqual(set(cost.by_step), {"correctness-review", "fix"})
        text = cost_report.render(report)
        self.assertIn("$0.58–1.11", text)  # one Sol session over the 272K tier, priced at both tiers
        self.assertIn("| R | 1 | $0.58–1.11 |", text)
        self.assertIn("1 / 1 / 0", text)
        self.assertIn(prices.AS_OF, text)
        self.assertIn(prices.TABLE_SHA256[:12], text)
        self.assertIn("No scored execution: t2--R--r1", text)

    def test_without_a_manifest_everything_is_unscored(self):
        self.write_exec("t1--R--r1", 1, [{"name": "plan-review-1", "attempts": [{"usage": codex_usage(10)}]}])
        with mock.patch.object(cost_report, "frozen_digest", lambda experiment: None):
            report = cost_report.collect(self.root, self.experiment)
        self.assertEqual((report.arms["R"].builds, report.arms["R"].unscored.attempts), (0, 1))
        self.assertIn("No manifest", cost_report.render(report))

    def test_an_unreadable_execution_and_a_never_built_arm_are_on_the_page(self):
        broken = self.root / "runs" / "t1--R--r1" / "exec-1"
        broken.mkdir(parents=True)
        (broken / "record.json").write_text("{not json", encoding="utf-8")
        with mock.patch.object(cost_report, "frozen_digest", lambda experiment: "d"), \
                mock.patch.object(pilot, "complete_build", lambda *args: None):
            report = cost_report.collect(self.root, self.experiment)
        self.assertEqual((report.arms["R"].unscored.attempts, report.arms["R"].unscored.unknown), (0, 1))
        text = cost_report.render(report)
        self.assertIn("(0 attempts, 1 unknown)", text)
        self.assertIn("No scored execution: t1--R--r1, t2--R--r1", text)

    def test_the_cli_refuses_an_unknown_arm(self):
        from eval.__main__ import main
        with mock.patch("sys.stderr"):
            self.assertEqual(main(["cost", "--experiment", "replay", "--arm", "Z9"]), 2)


if __name__ == "__main__":
    unittest.main()
