import json
import shutil
import tempfile
import unittest
from pathlib import Path

from eval.harness import agents, ledger, prices, records


def token_count(last: dict | None, limits: dict | None = None) -> dict:
    payload = {"type": "token_count", "info": None if last is None else {"last_token_usage": last}}
    if limits is not None:
        payload["rate_limits"] = limits
    return {"type": "event_msg", "payload": payload}


def request(prompt: int, cached: int, output: int) -> dict:
    return {"input_tokens": prompt, "cached_input_tokens": cached, "cache_write_input_tokens": 0,
            "output_tokens": output, "reasoning_output_tokens": 0, "total_tokens": prompt + output}


class CaptureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-capture-"))
        self.addCleanup(shutil.rmtree, self.tmp)

    def write(self, path: Path, *events: dict) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        return path

    def test_a_codex_rollout_gives_each_request_and_the_rate_limit_snapshots(self):
        first = {"primary": {"used_percent": 30.0, "window_minutes": 10080, "resets_at": 1}}
        last = {"primary": {"used_percent": 31.0, "window_minutes": 10080, "resets_at": 1}}
        self.write(self.tmp / "home" / "sessions" / "2026" / "11" / "01" / "rollout-x.jsonl",
                   {"type": "session_meta", "payload": {}}, token_count(None, first),
                   token_count(request(100, 50, 10)), token_count(request(300, 200, 20), last))
        captured = agents.rollout_usage(self.tmp / "home")
        self.assertEqual(captured["requests"], [request(100, 50, 10), request(300, 200, 20)])
        self.assertEqual(captured["rate_limits"], {"first": first, "last": last})

    def test_a_malformed_or_unreadable_rollout_never_raises(self):
        rollout = self.write(self.tmp / "home" / "sessions" / "rollout-y.jsonl",
                             {"type": "event_msg", "payload": {"type": "token_count", "info": "oops"}})
        self.assertEqual(agents.rollout_usage(self.tmp / "home"), {})
        rollout.chmod(0)
        self.addCleanup(rollout.chmod, 0o600)
        self.assertIn("capture_error", agents.rollout_usage(self.tmp / "home"))

    def test_a_home_without_a_rollout_captures_nothing(self):
        self.assertEqual(agents.rollout_usage(self.tmp / "empty-home"), {})

    def test_captured_requests_reconcile_and_price_per_request(self):
        totals = request(400, 250, 30)
        totals.pop("total_tokens")
        usage = {**totals, "requests": [request(100, 50, 10), request(300, 200, 20)]}
        cost = ledger.attempt_cost("correctness-review-1", {"usage": usage},
                                   ledger.Seat("codex", prices.OPENAI, "gpt-6.1-sol"), self.tmp)
        self.assertEqual(cost.basis, ledger.PER_REQUEST)

    def test_claude_keeps_the_last_cumulative_model_usage(self):
        turn = {"input_tokens": 5, "output_tokens": 7}
        events = [{"type": "result", "subtype": "success", "is_error": False, "result": "a", "usage": turn,
                   "modelUsage": {"claude-opus-5-5": {"outputTokens": 100}}},
                  {"type": "result", "subtype": "success", "is_error": False, "result": "b", "usage": turn,
                   "modelUsage": {"claude-opus-5-5": {"outputTokens": 150}}}]
        transcript = self.write(self.tmp / "t.jsonl", *events)
        session = agents.classify_claude(transcript, agents.procs.ProcResult(0, False, 1.0), agents.WRITER)
        self.assertEqual(session.usage["by_model"], {"claude-opus-5-5": {"outputTokens": 150}})
        self.assertEqual(session.usage["output_tokens"], 7)

    def test_codex_sessions_keep_their_rollout(self):
        self.assertNotIn("--ephemeral", agents.codex_argv("gpt-6.1-sol", self.tmp / "final.txt"))

    def test_records_refuse_a_malformed_capture(self):
        def steps(usage: dict) -> list:
            return [{"name": "build", "attempts": [{"seconds": 1.0, "usage": usage}]}]
        self.assertTrue(records._valid_steps(steps({"input_tokens": 1, "requests": [request(1, 0, 1)]})))
        for bad in ({"requests": []}, {"requests": [{"input_tokens": -1}]}, {"requests": ["x"]},
                    {"requests": [{"input_tokens": 1.5}]},
                    {"by_model": {}}, {"by_model": {"m": 3}}, {"rate_limits": []}):
            with self.subTest(bad=bad):
                self.assertFalse(records._valid_steps(steps(bad)))


if __name__ == "__main__":
    unittest.main()
