import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval.harness import agents, loop, openrouter, procs, routing
from eval.tests.test_replay import StubAgentCase

MODEL = "qwen/qwen3-coder-30b-a3b-instruct"


class StubProxy:
    def __init__(self, refused: str | None = None):
        self.routes = frozenset({routing.route_name(agents.OPENROUTER, MODEL)})
        self.env = mock.Mock(refusal=lambda attempt: refused)

    def session(self, job, attempt):
        return {route: f"http://127.0.0.1:9/s/t/{route}" for route in self.routes}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-wiring-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        key = self.tmp / "key"
        key.write_text("sk-or-v1-SYNTHETIC\n")
        patcher = mock.patch.object(openrouter, "KEY_PATH", key)
        patcher.start()
        self.addCleanup(patcher.stop)


class CoverageTest(Fixture):
    def test_a_routed_session_outside_a_routing_context_never_launches(self):
        for run in (agents.run_claude, agents.run_codex):
            with self.subTest(run=run.__name__), mock.patch.object(procs, "run") as launched:
                with self.assertRaises(routing.RoutingRequired):
                    run(MODEL, "hi", role=agents.WRITER, cwd=self.tmp, out_dir=self.tmp / run.__name__,
                        provider=agents.OPENROUTER)
                launched.assert_not_called()
                self.assertFalse((self.tmp / run.__name__).exists())

    def test_a_context_without_a_job_refuses_too(self):
        with routing.active(StubProxy()), mock.patch.object(procs, "run") as launched:
            with self.assertRaises(routing.RoutingRequired):
                agents.run_claude(MODEL, "hi", role=agents.WRITER, cwd=self.tmp, out_dir=self.tmp / "o",
                                  provider=agents.OPENROUTER)
            launched.assert_not_called()

    def test_native_sessions_need_no_context(self):
        self.assertEqual(routing.refusal("anything"), None)


class RefusalTest(Fixture):
    def run_claude_succeeding(self, refused):
        def fake_run(argv, **kwargs):
            result = {"type": "result", "subtype": "success", "is_error": False, "result": "done", "usage": {}}
            kwargs["stdout_path"].write_text(json.dumps(result) + "\n")
            return procs.ProcResult(0, False, 0.1)
        with routing.active(StubProxy(refused)), routing.job("j"), mock.patch.object(procs, "run", fake_run):
            return agents.run_claude(MODEL, "hi", role=agents.WRITER, cwd=self.tmp, out_dir=self.tmp / "o",
                                     provider=agents.OPENROUTER)

    def test_a_refused_session_ends_infra_even_when_its_cli_got_through(self):
        session = self.run_claude_succeeding("scope or: allocation spent")
        self.assertTrue(session.spend_refused)
        self.assertEqual(session.terminal, agents.INFRA)
        self.assertTrue(session.reason.startswith(agents.SPEND_REFUSED))

    def test_an_unrefused_routed_session_completes(self):
        self.assertEqual(self.run_claude_succeeding(None).terminal, agents.COMPLETED)


class LoopTest(StubAgentCase):
    def test_the_loop_never_retries_a_spend_refusal(self):
        calls = []

        def refused(model, prompt, *, role, cwd, out_dir, provider=agents.NATIVE, offline=False):
            calls.append(role)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "transcript.jsonl").touch()
            return agents.Session(agents.INFRA, f"{agents.SPEND_REFUSED}: no room", "", out_dir / "transcript.jsonl", 0.1,
                                  spend_refused=True)

        with mock.patch.object(agents, "run_claude", refused), mock.patch.object(agents, "run_codex", refused):
            record = loop.execute_run(self.task, loop.Arm("A1", agents.Agent("claude", "m")), self.tmp / "runs", rep=1,
                                      manifest_hash="d", offline=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(record["status"], "invalid")
        self.assertEqual(len(record["steps"][0]["attempts"]), 1)


if __name__ == "__main__":
    unittest.main()
