import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval.harness import agents, procs

SECRET_PROMPT = "PROMPT-CONTENT-7f3a do the task"


class ArgvTest(unittest.TestCase):
    """Process arguments are visible to every process, so they must carry no prompt content."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.calls = []

    def fake_run(self, argv, **kwargs):
        self.calls.append((argv, kwargs.get("stdin_path")))
        kwargs["stdout_path"].write_text("")
        return procs.ProcResult(0, False, 0.1)

    def check(self, run):
        with mock.patch.object(procs, "run", self.fake_run):
            run("claude-haiku-5-5" if run is agents.run_claude else "gpt-6-luna", SECRET_PROMPT,
                role=agents.REVIEWER, cwd=self.tmp, out_dir=self.tmp / "out")
        argv, stdin_path = self.calls[0]
        self.assertFalse(any("PROMPT-CONTENT" in arg for arg in argv))
        return stdin_path

    def test_claude_prompt_on_stdin(self):
        stdin_path = self.check(agents.run_claude)
        self.assertEqual(stdin_path.read_text(), SECRET_PROMPT)

    def test_codex_prompt_on_stdin(self):
        self.assertIsNotNone(self.check(agents.run_codex))



class OfflineTest(unittest.TestCase):
    """Offline sessions: Claude's Bash sandbox allows no domain; Codex gets localhost-only Seatbelt, a live
    egress proxy in its environment for the whole session, and web search off. Online ones are unchanged."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_claude_offline_sandbox_allows_localhost_only(self):
        online = agents.claude_settings(agents.WRITER, self.tmp)
        offline = agents.claude_settings(agents.WRITER, self.tmp, offline=True)
        self.assertNotIn("network", online["sandbox"])
        self.assertEqual(offline["sandbox"]["network"], {"allowedDomains": [], "allowLocalBinding": True})
        del offline["sandbox"]["network"]
        self.assertEqual(offline, online)

    def test_codex_offline_profile_denies_outbound_but_localhost_last(self):
        online = agents.codex_seatbelt_profile(self.tmp, self.tmp / "home", writable=True)
        offline = agents.codex_seatbelt_profile(self.tmp, self.tmp / "home", writable=True, offline=True)
        self.assertNotIn("network", online)
        self.assertTrue(offline.startswith(online))
        self.assertEqual(offline[len(online):],
                         '(deny network-outbound)\n(allow network-outbound (remote ip "localhost:*"))\n')

    def run_codex(self, offline: bool) -> dict:
        seen = {}

        def fake_run(argv, **kwargs):
            env = kwargs["env"]
            seen["env"] = env
            seen["config"] = (Path(env["CODEX_HOME"]) / "config.toml").read_text()
            seen["profile"] = Path(argv[2]).read_text()
            if "HTTPS_PROXY" in env and env["HTTPS_PROXY"].startswith("http://127.0.0.1:"):
                port = int(env["HTTPS_PROXY"].rsplit(":", 1)[1])
                with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                    sock.sendall(b"CONNECT github.com:443 HTTP/1.1\r\n\r\n")
                    seen["reply"] = sock.recv(64)
            kwargs["stdout_path"].write_text("")
            return procs.ProcResult(0, False, 0.1)

        with mock.patch.object(procs, "run", fake_run):
            agents.run_codex("gpt-6.1-sol", "p", role=agents.REVIEWER, cwd=self.tmp, out_dir=self.tmp / "out",
                             offline=offline)
        return seen

    def test_codex_offline_session_runs_behind_a_live_proxy_with_search_off(self):
        seen = self.run_codex(offline=True)
        self.assertTrue(seen["reply"].startswith(b"HTTP/1.1 403"))
        self.assertEqual(seen["env"]["NO_PROXY"], "localhost,127.0.0.1")
        self.assertIn('web_search = "disabled"', seen["config"])
        self.assertIn("(deny network-outbound)", seen["profile"])
        self.assertEqual((self.tmp / "out" / "egress.log").read_text(), "DENY CONNECT github.com:443\n")
        self.assertEqual((self.tmp / "out" / "codex-config.toml").read_text(), seen["config"])

    def test_codex_online_session_has_no_proxy_search_switch_or_network_rule(self):
        seen = self.run_codex(offline=False)
        self.assertNotIn("reply", seen)
        self.assertNotIn("web_search", seen["config"])
        self.assertNotIn("network", seen["profile"])
        self.assertFalse((self.tmp / "out" / "egress.log").exists())


if __name__ == "__main__":
    unittest.main()
