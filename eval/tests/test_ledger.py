import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from eval.harness import agents, ledger, prices
from eval.harness.experiments import EXPERIMENTS
from eval.harness.ledger import Tokens

REPO = Path(__file__).resolve().parents[2]
REPLAY_RUNS = REPO / "eval" / "data" / "replay" / "runs"
OPUS, HAIKU = agents.Agent("claude", "claude-opus-5-5"), agents.Agent("claude", "claude-haiku-5-5")
SOL = agents.Agent("codex", "gpt-6.1-sol")
ARM = SimpleNamespace(writer=OPUS, reviewer=SOL)


def model_usage(**counters) -> dict:
    base = {"inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
            "cacheCreationEphemeral5mInputTokens": 0, "cacheCreationEphemeral1hInputTokens": 0, "thinkingTokens": 0}
    return {**base, **counters}


def codex_usage(prompt=0, cached=0, written=0, output=0, reasoning=0) -> dict:
    return {"input_tokens": prompt, "cached_input_tokens": cached, "cache_write_input_tokens": written,
            "output_tokens": output, "reasoning_output_tokens": reasoning}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-ledger-"))
        self.addCleanup(shutil.rmtree, self.tmp)

    def attempt_dir(self, *results: dict) -> Path:
        """An attempt directory whose transcript holds these result events, in order."""
        path = self.tmp / f"attempt-{len(list(self.tmp.iterdir()))}"
        path.mkdir()
        lines = [json.dumps({"type": "assistant"})] + [json.dumps({"type": "result", **r}) for r in results]
        (path / "transcript.jsonl").write_text("\n".join(lines) + "\n{cut mid-line", encoding="utf-8")
        return path


class ClaudeTest(Fixture):
    def test_a_resumed_session_prices_from_the_last_cumulative_snapshot(self):
        first = model_usage(inputTokens=10, outputTokens=1_000)
        last = model_usage(inputTokens=20, outputTokens=50_000, cacheReadInputTokens=1_000_000,
                           cacheCreationInputTokens=10_000, cacheCreationEphemeral1hInputTokens=10_000)
        attempt = self.attempt_dir({"modelUsage": {"claude-opus-5-5": first}},
                                   {"modelUsage": {"claude-opus-5-5": last}})
        turn = {"input_tokens": 5, "output_tokens": 100}  # what the record keeps: the last turn alone
        cost = ledger.attempt_cost("build", {"usage": turn, "seconds": 3}, OPUS, attempt)
        expected = (20 * 4 + 50_000 * 20 + 1_000_000 * 0.20 + 10_000 * 8) / 1e6
        self.assertEqual(cost.basis, ledger.SESSION)
        self.assertAlmostEqual(cost.cost.low, expected, places=12)
        self.assertEqual(cost.cost.low, cost.cost.high)

    def test_a_recorded_by_model_wins_over_the_transcript_and_prices_each_model(self):
        by_model = {"claude-opus-5-5": model_usage(outputTokens=1_000_000),
                    "claude-haiku-5-5": model_usage(inputTokens=1_000_000)}
        attempt = self.attempt_dir({"modelUsage": {"claude-opus-5-5": model_usage(outputTokens=1)}})
        cost = ledger.attempt_cost("build", {"usage": {"by_model": by_model}}, OPUS, attempt)
        self.assertEqual(set(cost.tokens), {"claude-opus-5-5", "claude-haiku-5-5"})
        # Haiku's 1M-token prompt crosses its 100K tier in an unknown mix of requests: a range.
        self.assertAlmostEqual(cost.cost.low, 20.0 + 0.10, places=9)
        self.assertAlmostEqual(cost.cost.high, 20.0 + 0.50, places=9)

    def test_without_counters_the_last_turn_is_priced_and_marked(self):
        missing = self.tmp / "no-transcript"
        missing.mkdir()
        usage = {"input_tokens": 1_000, "output_tokens": 1_000, "cache_creation_input_tokens": 300,
                 "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 100}}
        cost = ledger.attempt_cost("build", {"usage": usage}, OPUS, missing)
        self.assertEqual(cost.basis, ledger.TURN_ONLY)
        # 100 undated writes span the 5m and 1h prices.
        fixed = (1_000 * 4 + 1_000 * 20 + 100 * 5 + 100 * 8) / 1e6
        self.assertAlmostEqual(cost.cost.low, fixed + 100 * 5 / 1e6, places=12)
        self.assertAlmostEqual(cost.cost.high, fixed + 100 * 8 / 1e6, places=12)


class CodexTest(Fixture):
    def test_session_totals_under_the_tier_are_exact(self):
        cost = ledger.attempt_cost("review", {"usage": codex_usage(200_000, 150_000, 0, 4_000, 2_000)}, SOL, self.tmp)
        expected = (50_000 * 2 + 150_000 * 0.10 + 4_000 * 10) / 1e6  # $0.155
        self.assertEqual(cost.basis, ledger.SESSION)
        self.assertAlmostEqual(cost.cost.low, expected, places=12)
        self.assertEqual(cost.cost.low, cost.cost.high)

    def test_session_totals_over_the_tier_are_a_range(self):
        cost = ledger.attempt_cost("review", {"usage": codex_usage(1_000_000, 800_000, 0, 10_000)}, SOL, self.tmp)
        self.assertAlmostEqual(cost.cost.low, (200_000 * 2 + 800_000 * 0.10 + 10_000 * 10) / 1e6, places=12)  # $0.58
        self.assertAlmostEqual(cost.cost.high, (200_000 * 4 + 800_000 * 0.20 + 10_000 * 15) / 1e6, places=12)  # $1.11

    def test_reconciling_requests_price_each_on_its_own_tier(self):
        small, big = codex_usage(100_000, 50_000, 0, 1_000), codex_usage(300_000, 200_000, 0, 1_000)
        total = codex_usage(400_000, 250_000, 0, 2_000)
        cost = ledger.attempt_cost("review", {"usage": {**total, "requests": [small, big]}}, SOL, self.tmp)
        expected = (50_000 * 2 + 50_000 * 0.10 + 1_000 * 10 + 100_000 * 4 + 200_000 * 0.20 + 1_000 * 15) / 1e6
        self.assertEqual(cost.basis, ledger.PER_REQUEST)
        self.assertAlmostEqual(cost.cost.low, expected, places=12)
        self.assertEqual(cost.cost.low, cost.cost.high)

    def test_requests_that_do_not_reconcile_fall_back_to_the_totals(self):
        total = codex_usage(400_000, 250_000, 0, 2_000)
        cost = ledger.attempt_cost("review", {"usage": {**total, "requests": [codex_usage(100_000)]}}, SOL, self.tmp)
        self.assertEqual(cost.basis, ledger.SESSION)
        self.assertIn("reconcile", cost.reason)
        self.assertLess(cost.cost.low, cost.cost.high)

    def test_cache_writes_are_their_own_bucket_and_a_range(self):
        cost = ledger.attempt_cost("review", {"usage": codex_usage(100_000, 0, 100_000)}, SOL, self.tmp)
        self.assertEqual(cost.tokens["gpt-6.1-sol"].input, 0)
        self.assertAlmostEqual(cost.cost.low, 100_000 * 2.50 / 1e6, places=12)
        self.assertAlmostEqual(cost.cost.high, 100_000 * 5.0 / 1e6, places=12)


class UnusableTest(Fixture):
    def test_malformed_or_inconsistent_usage_is_unknown_with_a_reason(self):
        cases = {
            "negative": codex_usage(-1),
            "nan": {**codex_usage(10), "output_tokens": math.nan},
            "float": {**codex_usage(10), "output_tokens": 1.5},
            "bool": {**codex_usage(10), "output_tokens": True},
            "cached over input": codex_usage(10, 11),
            "reasoning over output": codex_usage(10, 0, 0, 1, 2),
            "empty requests": {**codex_usage(10), "requests": []},
            "empty": {},
            "priority tier": {**codex_usage(10), "service_tier": "priority"},
            "fast mode": {**codex_usage(10), "speed": "fast"},
            "us-only inference": {**codex_usage(10), "inference_geo": "us"},
        }
        for name, usage in cases.items():
            with self.subTest(name):
                cost = ledger.attempt_cost("review", {"usage": usage}, SOL, self.tmp)
                self.assertEqual(cost.basis, ledger.UNKNOWN)
                self.assertIsNone(cost.cost)
                self.assertTrue(cost.reason)

    def test_an_unpriced_model_raises(self):
        with self.assertRaises(prices.UnpricedModel):
            ledger.attempt_cost("review", {"usage": codex_usage(10)}, agents.Agent("codex", "gpt-0"), self.tmp)


class ExecutionTest(Fixture):
    def write_exec(self, run: Path, number: int, steps, status="complete") -> Path:
        exec_dir = run / f"exec-{number}"
        exec_dir.mkdir(parents=True)
        (exec_dir / "record.json").write_text(json.dumps({"status": status, "steps": steps}), encoding="utf-8")
        return exec_dir

    def test_every_execution_counts_and_only_the_scored_one_is_scored(self):
        run = self.tmp / "run"
        review = {"name": "correctness-review-1", "attempts": [
            {"usage": codex_usage(100_000, 0, 0, 1_000), "seconds": 10},  # an infra retry is billed too
            {"usage": codex_usage(100_000, 0, 0, 1_000), "seconds": 20}]}
        scored = self.write_exec(run, 1, [review])
        self.write_exec(run, 2, [review, {"name": "fix-1", "attempts": [{"usage": {}, "seconds": 5}]}], "invalid")
        cost = ledger.run_cost(run, scored, ARM)
        self.assertEqual(cost.scored.exec_dir, scored)
        totals = cost.scored.totals()
        self.assertEqual((totals.attempts, totals.seconds, totals.unknown), (2, 30, 0))
        self.assertAlmostEqual(totals.cost.low, 2 * (100_000 * 2 + 1_000 * 10) / 1e6, places=12)
        self.assertEqual(set(cost.scored.by_step_kind()), {"correctness-review"})
        unscored = cost.unscored[0].totals()
        self.assertEqual((unscored.attempts, unscored.unknown), (3, 1))

    def test_a_lone_invalid_execution_is_unscored_spend(self):
        run = self.tmp / "run"
        self.write_exec(run, 1, [{"name": "plan-review-1", "attempts": [{"usage": codex_usage(10), "seconds": 1}]}],
                        "invalid")
        cost = ledger.run_cost(run, None, ARM)
        self.assertIsNone(cost.scored)
        self.assertEqual(cost.unscored[0].totals().attempts, 1)

    def test_an_unreadable_record_and_a_malformed_attempt_are_unknowns(self):
        run = self.tmp / "run"
        broken = run / "exec-1"
        broken.mkdir(parents=True)
        (broken / "record.json").write_text("{not json", encoding="utf-8")
        mixed = self.write_exec(run, 2, [{"name": "correctness-review-1", "attempts": [
            {"usage": codex_usage(10, 20)}, {"usage": codex_usage(10)}]}])
        cost = ledger.run_cost(run, mixed, ARM)
        self.assertEqual(cost.unscored[0].totals().unknown, 1)
        totals = cost.scored.totals()
        self.assertEqual((totals.attempts, totals.unknown), (2, 1))
        self.assertGreater(totals.cost.low, 0)

    def test_step_kinds_strip_the_round(self):
        self.assertEqual([ledger.step_kind(n) for n in ("plan", "plan-review-3", "plan-revise-1", "fix-4", "build")],
                         ["plan", "plan-review", "plan-revise", "fix", "build"])


@unittest.skipUnless(REPLAY_RUNS.is_dir(), "v1's replay records are not on this machine")
class V1Test(unittest.TestCase):
    def test_every_v1_claude_session_matches_claude_codes_own_list_price(self):
        experiment, compared, codex = EXPERIMENTS["replay"], 0, {"exact": 0, "ranged": 0, "unknown": 0}
        for record_path in sorted(REPLAY_RUNS.glob("*/exec-*/record.json")):
            exec_dir = record_path.parent
            record = json.loads(record_path.read_text(encoding="utf-8"))
            costs = iter(ledger.execution_cost(exec_dir, experiment.arm(record["arm"])).attempts)
            for index, step in enumerate(record["steps"]):
                for number, _ in enumerate(step["attempts"], start=1):
                    cost = next(costs)
                    if "review" in step["name"]:
                        codex["unknown" if cost.cost is None else
                              "ranged" if cost.cost.high > cost.cost.low else "exact"] += 1
                        continue
                    transcript = exec_dir / "steps" / f"{index:02d}-{step['name']}" / f"attempt-{number}" / \
                        "transcript.jsonl"
                    totals = [event["total_cost_usd"] for event in _events(transcript) if event.get("type") == "result"]
                    if not totals:
                        self.assertIsNone(cost.cost, f"{exec_dir} {step['name']}")
                        continue
                    self.assertAlmostEqual(cost.cost.low, totals[-1], delta=1e-6, msg=f"{exec_dir} {step['name']}")
                    self.assertEqual(cost.cost.low, cost.cost.high)
                    compared += 1
        self.assertEqual(compared, 105)
        self.assertEqual(codex, {"exact": 27, "ranged": 117, "unknown": 1})


def _events(transcript: Path) -> list[dict]:
    events = []
    for line in transcript.read_text(encoding="utf-8").splitlines() if transcript.exists() else []:
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


if __name__ == "__main__":
    unittest.main()
