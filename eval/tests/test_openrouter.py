import io
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from eval.harness import agents, openrouter, procs

KEY = "sk-or-v1-SYNTHETIC0123456789abcdef"
MODEL = "qwen/qwen3-coder-30b-a3b-instruct"


class _KeyFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        key_dir = Path(tempfile.mkdtemp(prefix="eval-test-key-"))
        self.addCleanup(shutil.rmtree, key_dir)
        key_path = key_dir / "key"
        key_path.write_text(KEY + "\n")
        patcher = mock.patch.object(openrouter, "KEY_PATH", key_path)
        patcher.start()
        self.addCleanup(patcher.stop)


class RoutedSessionTest(_KeyFile):
    """The key reaches a routed session only through its own process: never argv, files or a tool shell."""

    def capture(self, run, model):
        calls = []

        def fake_run(argv, **kwargs):
            env = kwargs.get("env") or {}
            files = {path.name: path.read_text() for root in (kwargs["stdout_path"].parent,
                                                              Path(env.get("CODEX_HOME", self.tmp / "none")))
                     if root.is_dir() for path in root.iterdir() if path.is_file()}
            calls.append({"argv": argv, "env": env, "files": files})
            kwargs["stdout_path"].write_text("")
            return procs.ProcResult(0, False, 0.1)

        with mock.patch.object(procs, "run", fake_run), \
                mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "parent", "CLAUDE_EFFORT": "max"}):
            run(model, "do it", role=agents.WRITER, cwd=self.tmp, out_dir=self.tmp / "out", provider=agents.OPENROUTER)
        return calls[0]

    def assert_key_nowhere_saved(self, call):
        self.assertFalse(any(KEY in arg for arg in call["argv"]))
        for name, text in call["files"].items():
            self.assertNotIn(KEY, text, name)
        for path in (self.tmp / "out").rglob("*"):
            if path.is_file():
                self.assertNotIn(KEY, path.read_text(errors="replace"), path.name)

    def test_claude_gets_the_key_from_a_helper_not_the_environment(self):
        call = self.capture(agents.run_claude, MODEL)
        self.assert_key_nowhere_saved(call)
        self.assertNotIn(KEY, call["env"].values())
        for name in openrouter.KEY_VARIABLES:
            self.assertNotIn(name, call["env"])
        self.assertNotIn("CLAUDE_EFFORT", call["env"])
        self.assertEqual(call["env"]["ANTHROPIC_BASE_URL"], openrouter.ANTHROPIC_BASE_URL)
        for name in openrouter._CLAUDE_MODEL_VARIABLES:
            self.assertEqual(call["env"][name], MODEL)
        settings = json.loads(call["files"]["settings.json"])
        self.assertEqual(settings["apiKeyHelper"], f"/bin/cat {openrouter.KEY_PATH}")
        helper = subprocess.run(settings["apiKeyHelper"].split(), capture_output=True, text=True, check=True)
        self.assertEqual(helper.stdout.strip(), KEY)

    def test_native_claude_has_no_helper(self):
        self.assertNotIn("apiKeyHelper", agents.claude_settings(agents.WRITER, self.tmp))

    def test_codex_holds_the_key_in_its_process_and_excludes_it_from_shells(self):
        call = self.capture(agents.run_codex, "z-ai/glm-4.7-flash")
        self.assert_key_nowhere_saved(call)
        self.assertEqual(call["env"]["OPENROUTER_API_KEY"], KEY)
        self.assertNotIn("ANTHROPIC_API_KEY", call["env"])
        self.assertNotIn("auth.json", call["files"])
        config = tomllib.loads(call["files"]["config.toml"])
        self.assertEqual(config["model_provider"], "openrouter")
        self.assertEqual(config["model_providers"]["openrouter"]["env_key"], "OPENROUTER_API_KEY")
        self.assertEqual(config["model_reasoning_effort"], agents.CODEX_EFFORT)
        self.assertEqual(set(config["shell_environment_policy"]["exclude"]), set(openrouter.KEY_VARIABLES))


class ScrubTest(_KeyFile):
    def test_redacts_files_and_drops_a_repo_whose_history_holds_the_key(self):
        root = self.tmp / "exec"
        (root / "steps").mkdir(parents=True)
        (root / "steps" / "transcript.jsonl").write_text(f'{{"out": "{KEY}"}}\n')
        (root / "clean.txt").write_text("nothing here\n")
        repo = root / "repo"
        repo.mkdir()
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
        subprocess.run(git + ["init", "-q"], cwd=repo, check=True)
        (repo / "leak.py").write_text(f"KEY = '{KEY}'\n")
        subprocess.run(git + ["add", "-A"], cwd=repo, check=True)
        subprocess.run(git + ["commit", "-qm", "x"], cwd=repo, check=True)
        (repo / "leak.py").write_text("KEY = None\n")

        hits = openrouter.scrub(root)

        self.assertEqual(sorted(hits), ["repo/.git", "steps/transcript.jsonl"])
        self.assertNotIn(KEY, (root / "steps" / "transcript.jsonl").read_text())
        self.assertIn(openrouter.REDACTED.decode(), (root / "steps" / "transcript.jsonl").read_text())
        self.assertFalse((repo / ".git").exists())
        self.assertEqual((root / "clean.txt").read_text(), "nothing here\n")

    def test_drops_a_repo_holding_the_key_in_its_metadata_or_that_git_cant_read(self):
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
        for name in ("config", "broken"):
            repo = self.tmp / name
            repo.mkdir()
            subprocess.run(git + ["init", "-q"], cwd=repo, check=True)
        with (self.tmp / "config" / ".git" / "config").open("a") as config:
            config.write(f'[remote "o"]\n\turl = https://x:{KEY}@example.com/r\n')
        (self.tmp / "broken" / ".git" / "HEAD").write_text("not a ref\n")
        self.assertEqual(sorted(openrouter.scrub(self.tmp)), ["broken/.git", "config/.git"])

    def test_a_record_that_held_the_key_stays_scrubbed_once_flagged(self):
        from eval.harness import loop
        arm = loop.Arm("A1", agents.Agent("claude", "m", agents.OPENROUTER))
        record = {"status": "invalid", "invalid_reason": f"provider said: bad key {KEY}", "flags": []}
        loop._write_record(self.tmp, record, arm)
        saved = (self.tmp / "record.json").read_text()
        self.assertNotIn(KEY, saved)
        self.assertNotIn(KEY, json.dumps(record))
        self.assertEqual(json.loads(saved)["flags"], ["key_exposed:record.json"])

    def test_a_clean_tree_is_untouched(self):
        (self.tmp / "a.txt").write_text("plain")
        self.assertEqual(openrouter.scrub(self.tmp), [])
        self.assertEqual((self.tmp / "a.txt").read_text(), "plain")


class CreditTest(_KeyFile):
    def respond(self, body):
        return mock.patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(body).encode()))

    def test_reads_usage(self):
        with self.respond({"data": {"usage": 1.25}}):
            self.assertEqual(openrouter.credit_used(), 1.25)

    def test_unreadable_usage_raises(self):
        for body in ({"data": {}}, {"data": {"usage": True}}, {"data": {"usage": "1"}}):
            with self.respond(body), self.assertRaises(openrouter.CreditUnavailable):
                openrouter.credit_used()
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")), \
                self.assertRaises(openrouter.CreditUnavailable) as caught:
            openrouter.credit_used()
        self.assertNotIn(KEY, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
