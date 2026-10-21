import shutil
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


if __name__ == "__main__":
    unittest.main()
