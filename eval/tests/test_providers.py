import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from eval.harness import agents, procs, providers, routing

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
        patcher = mock.patch.dict(providers.KEY_PATHS, {"openrouter": key_path})
        patcher.start()
        self.addCleanup(patcher.stop)


class StubProxy:
    """Stands in for proxy.Proxy: every routed session needs an active routing context."""
    BASE = "http://127.0.0.1:9/s/token"

    def __init__(self, *models, provider=agents.OPENROUTER):
        self.routes = frozenset(routing.route_name(provider, m) for m in models)
        self.env = mock.Mock(refusal=lambda attempt: None)

    def session(self, job, attempt):
        return {route: f"{self.BASE}/{route}" for route in self.routes}


class RoutedSessionTest(_KeyFile):
    """The key reaches a routed session only through its own process: never argv, files or a tool shell."""

    def capture(self, run, model, provider=agents.OPENROUTER):
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
                mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "parent", "CLAUDE_EFFORT": "max"}), \
                routing.active(StubProxy(model, provider=provider)), routing.job("j"):
            run(model, "do it", role=agents.WRITER, cwd=self.tmp, out_dir=self.tmp / "out", provider=provider)
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
        for name in providers.CREDENTIAL_VARIABLES:
            self.assertNotIn(name, call["env"])
        self.assertNotIn("CLAUDE_EFFORT", call["env"])
        self.assertEqual(call["env"]["ANTHROPIC_BASE_URL"],
                         f"{StubProxy.BASE}/{routing.route_name(agents.OPENROUTER, MODEL)}")  # the proxy, never the provider
        for name in providers._CLAUDE_MODEL_VARIABLES:
            self.assertEqual(call["env"][name], MODEL)
        settings = json.loads(call["files"]["settings.json"])
        self.assertEqual(settings["apiKeyHelper"], f"/bin/cat {providers.key_path('openrouter')}")
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
        self.assertEqual(config["model_providers"]["openrouter"]["base_url"],
                         f"{StubProxy.BASE}/{routing.route_name(agents.OPENROUTER, 'z-ai/glm-4.7-flash')}/v1")
        self.assertEqual(config["model_providers"]["openrouter"]["env_key"], "OPENROUTER_API_KEY")
        self.assertEqual(config["model_reasoning_effort"], agents.CODEX_EFFORT)
        self.assertEqual(set(config["shell_environment_policy"]["exclude"]), set(providers.CREDENTIAL_VARIABLES))


class DeepSeekSessionTest(RoutedSessionTest):
    """A DeepSeek session reaches DeepSeek's two endpoints through the proxy, with DeepSeek's own key."""
    KEY = "sk-ds-SYNTHETIC0123456789abcdef"

    def setUp(self):
        super().setUp()
        key_dir = Path(tempfile.mkdtemp(prefix="eval-test-dskey-"))
        self.addCleanup(shutil.rmtree, key_dir)
        (key_dir / "key").write_text(self.KEY + "\n")
        patcher = mock.patch.dict(providers.KEY_PATHS, {"deepseek": key_dir / "key"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_claude_reaches_the_anthropic_path_with_the_deepseek_helper(self):
        call = self.capture(agents.run_claude, "deepseek-v4-pro", agents.DEEPSEEK)
        route = routing.route_name(agents.DEEPSEEK, "deepseek-v4-pro")
        self.assertEqual(call["env"]["ANTHROPIC_BASE_URL"], f"{StubProxy.BASE}/{route}/anthropic")
        settings = json.loads(call["files"]["settings.json"])
        self.assertEqual(settings["apiKeyHelper"], f"/bin/cat {providers.key_path('deepseek')}")
        self.assertNotIn(self.KEY, call["env"].values())
        for name in providers.CREDENTIAL_VARIABLES:
            self.assertNotIn(name, call["env"])

    def test_codex_reaches_the_root_with_the_deepseek_variable(self):
        call = self.capture(agents.run_codex, "deepseek-flash", agents.DEEPSEEK)
        self.assertFalse(any(self.KEY in arg for arg in call["argv"]))
        self.assertEqual(call["env"]["DEEPSEEK_API_KEY"], self.KEY)
        self.assertNotIn("OPENROUTER_API_KEY", call["env"])
        config = tomllib.loads(call["files"]["config.toml"])
        block = config["model_providers"]["deepseek"]
        self.assertEqual(config["model_provider"], "deepseek")
        self.assertEqual(block["base_url"], f"{StubProxy.BASE}/{routing.route_name(agents.DEEPSEEK, 'deepseek-flash')}")
        self.assertEqual(block["env_key"], "DEEPSEEK_API_KEY")
        self.assertEqual(set(config["shell_environment_policy"]["exclude"]), set(providers.CREDENTIAL_VARIABLES))
        self.assertNotIn(self.KEY, config.__repr__())


class ProviderTableTest(unittest.TestCase):
    def test_paths_compose_to_each_providers_documented_endpoints(self):
        def upstream(name, cli):
            provider = providers.get(name)
            path = provider.claude_path + "/v1/messages" if cli == "claude" else provider.codex_path + "/responses"
            return provider.upstream + path
        self.assertEqual(upstream("deepseek", "claude"), "https://api.deepseek.com/anthropic/v1/messages")
        self.assertEqual(upstream("deepseek", "codex"), "https://api.deepseek.com/responses")
        self.assertEqual(upstream("openrouter", "claude"), "https://openrouter.ai/api/v1/messages")
        self.assertEqual(upstream("openrouter", "codex"), "https://openrouter.ai/api/v1/responses")

    def test_no_provider_host_is_admitted_by_the_offline_egress_proxy(self):
        """Routed CLIs reach their provider only through the routing proxy on loopback, never egress."""
        import urllib.parse
        from eval.harness import egress
        hosts = {target.rsplit(":", 1)[0] for target in egress.ALLOWED_TARGETS}
        for provider in providers.PROVIDERS.values():
            self.assertNotIn(urllib.parse.urlsplit(provider.upstream).hostname, hosts, provider.name)

    def test_an_unknown_provider_is_refused(self):
        with self.assertRaises(ValueError):
            providers.get("fireworks")
        with self.assertRaises(ValueError):
            agents.Agent("codex", "m", "fireworks")

    def test_every_providers_key_is_a_private_root_and_its_variable_is_excluded(self):
        from eval.harness import audit
        denied = set(audit.sandbox_deny_roots())
        for name in providers.PROVIDERS:
            self.assertIn(providers.KEY_ROOT / name, audit.PRIVATE_ROOTS)
            self.assertTrue(any(str(root).endswith(f".config/{name}") for root in denied), name)
            self.assertIn(providers.get(name).codex_variable, providers.CREDENTIAL_VARIABLES)


class MultiKeyScrubTest(unittest.TestCase):
    """A DeepSeek-only arm on a machine with no OpenRouter key: its run is still scrubbed of its key."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        (self.tmp / "keys").mkdir()
        (self.tmp / "keys" / "ds").write_text("sk-ds-SYNTHETIC-scrub\n")
        patcher = mock.patch.dict(providers.KEY_PATHS, {"openrouter": self.tmp / "keys" / "missing",
                                                        "deepseek": self.tmp / "keys" / "ds"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_deepseek_only_arm_scrubs_its_record_transcript_and_history(self):
        from eval.harness import loop
        exec_dir = self.tmp / "exec-1"
        (exec_dir / "steps").mkdir(parents=True)
        (exec_dir / "steps" / "transcript.jsonl").write_text('{"out": "sk-ds-SYNTHETIC-scrub"}\n')
        repo = exec_dir / "repo"
        repo.mkdir()
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
        subprocess.run(git + ["init", "-q"], cwd=repo, check=True)
        (repo / "leak.py").write_text("KEY = 'sk-ds-SYNTHETIC-scrub'\n")
        subprocess.run(git + ["add", "-A"], cwd=repo, check=True)
        subprocess.run(git + ["commit", "-qm", "x"], cwd=repo, check=True)
        arm = loop.Arm("D", agents.Agent("codex", "deepseek-flash", agents.DEEPSEEK))
        record = {"steps": [], "note": "sk-ds-SYNTHETIC-scrub"}
        loop._write_record(exec_dir, record, arm)
        saved = (exec_dir / "record.json").read_text()
        self.assertNotIn("sk-ds-SYNTHETIC-scrub", saved)
        self.assertIn("[deepseek-key]", saved)
        self.assertIn("key_exposed:record.json", record["flags"])
        self.assertIn("key_exposed:repo/.git", record["flags"])
        self.assertNotIn("sk-ds-SYNTHETIC-scrub", (exec_dir / "steps" / "transcript.jsonl").read_text())
        self.assertFalse((repo / ".git").exists())
        self.assertEqual(providers.files_holding_keys(exec_dir), [])

    def test_a_key_that_exists_but_cant_be_read_or_a_required_missing_one_refuses(self):
        unreadable = self.tmp / "keys" / "locked"
        unreadable.mkdir()  # reading a directory raises, as a locked file would
        with mock.patch.dict(providers.KEY_PATHS, {"openrouter": unreadable}):
            with self.assertRaises(ValueError):
                providers.scrub(self.tmp)
        with self.assertRaises(ValueError):  # the run used OpenRouter, whose key is missing here
            providers.scrub(self.tmp, required={"openrouter"})
        (self.tmp / "clean").mkdir()
        self.assertEqual(providers.scrub(self.tmp / "clean", required={"deepseek"}), [])  # present: fine

    def test_a_scrub_with_no_key_installed_refuses(self):
        with mock.patch.dict(providers.KEY_PATHS, {"deepseek": self.tmp / "keys" / "missing"}):
            with self.assertRaises(ValueError):
                providers.scrub(self.tmp)


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

        hits = providers.scrub(root)

        self.assertEqual(sorted(hits), ["repo/.git", "steps/transcript.jsonl"])
        self.assertNotIn(KEY, (root / "steps" / "transcript.jsonl").read_text())
        self.assertIn("[openrouter-key]", (root / "steps" / "transcript.jsonl").read_text())
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
        self.assertEqual(sorted(providers.scrub(self.tmp)), ["broken/.git", "config/.git"])

    def test_a_record_that_held_the_key_stays_scrubbed_once_flagged(self):
        from eval.harness import loop
        arm = loop.Arm("A1", agents.Agent("claude", "m", agents.OPENROUTER))
        record = {"status": "invalid", "invalid_reason": f"provider said: bad key {KEY}", "flags": []}
        loop._write_record(self.tmp, record, arm)
        saved = (self.tmp / "record.json").read_text()
        self.assertNotIn(KEY, saved)
        self.assertNotIn(KEY, json.dumps(record))
        self.assertEqual(json.loads(saved)["flags"], ["key_exposed:record.json"])

    def test_a_file_that_cant_be_read_is_removed_and_reported(self):
        (self.tmp / "locked.txt").write_text(KEY)
        (self.tmp / "later.txt").write_text(KEY)
        real = Path.read_bytes

        def read_bytes(path):
            if path.name == "locked.txt":
                raise PermissionError(13, "denied")
            return real(path)

        with mock.patch.object(Path, "read_bytes", read_bytes):
            hits = providers.scrub(self.tmp)
        self.assertEqual(sorted(hits), ["later.txt", "locked.txt"])
        self.assertFalse((self.tmp / "locked.txt").exists())
        self.assertNotIn(KEY, (self.tmp / "later.txt").read_text())

    def test_a_file_that_cant_be_redacted_is_removed_and_the_scan_goes_on(self):
        (self.tmp / "a-readonly.txt").write_text(KEY)
        (self.tmp / "b-later.txt").write_text(KEY)
        real = Path.write_bytes

        def write_bytes(path, data):
            if path.name == "a-readonly.txt":
                raise PermissionError(13, "denied")
            return real(path, data)

        with mock.patch.object(Path, "write_bytes", write_bytes):
            hits = providers.scrub(self.tmp)
        self.assertEqual(sorted(hits), ["a-readonly.txt", "b-later.txt"])
        self.assertFalse((self.tmp / "a-readonly.txt").exists())
        self.assertNotIn(KEY, (self.tmp / "b-later.txt").read_text())

    def test_a_clean_tree_is_untouched(self):
        (self.tmp / "a.txt").write_text("plain")
        self.assertEqual(providers.scrub(self.tmp), [])
        self.assertEqual((self.tmp / "a.txt").read_text(), "plain")


if __name__ == "__main__":
    unittest.main()
