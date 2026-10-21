import json
import shutil
import tempfile
import unittest
from pathlib import Path

from eval.harness import audit

PRIVATE = audit.HOME / "pg" / "projects" / "secret.txt"


class AuditTest(unittest.TestCase):
    def setUp(self):
        audit.RUNS_ROOT.mkdir(parents=True, exist_ok=True)
        self.run_dir = Path(tempfile.mkdtemp(prefix="audit-test-", dir=audit.RUNS_ROOT))
        self.addCleanup(shutil.rmtree, self.run_dir)

    def transcript(self, *events: dict) -> Path:
        path = self.run_dir.parent / f"{self.run_dir.name}.jsonl"
        self.addCleanup(path.unlink, missing_ok=True)
        path.write_text("".join(json.dumps(e) + "\n" for e in events))
        return path

    def claude_call(self, name: str, tool_input: dict) -> dict:
        return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": tool_input}]}}

    def codex_call(self, command: str) -> dict:
        return {"type": "item.completed", "item": {"type": "command_execution", "command": command}}

    def hits(self, *events: dict) -> list[str]:
        return audit.audit_transcript(self.transcript(*events), self.run_dir)

    def test_direct_read_flagged(self):
        self.assertTrue(self.hits(self.claude_call("Read", {"file_path": str(PRIVATE)})))

    def test_python_mediated_read_flagged(self):
        self.assertTrue(self.hits(self.claude_call("Bash", {"command": f"python3 -c \"open('{PRIVATE}').read()\""})))

    def test_tilde_read_flagged(self):
        self.assertTrue(self.hits(self.codex_call("cat ~/.codex/auth.json")))

    def test_relative_escape_into_sibling_run_flagged(self):
        self.assertTrue(self.hits(self.codex_call("ls ../other-run")))

    def test_symlink_planted_in_run_dir_flagged(self):
        (self.run_dir / "link").symlink_to(audit.HOME / "pg")
        self.assertTrue(self.hits(self.claude_call("Bash", {"command": "cat link/AGENTS.md"})))

    def test_multiline_command_flagged(self):
        self.assertTrue(self.hits(self.claude_call("Bash", {"command": f"echo hi\n{PRIVATE}"})))

    def test_written_content_is_not_an_access(self):
        self.assertEqual(self.hits(self.claude_call("Write", {"file_path": "notes.py", "content": f"# see {PRIVATE}\n"})), [])

    def test_eval_tree_is_private(self):
        self.assertTrue(self.hits(self.codex_call(f"cat {audit.EVAL_ROOT / 'tasks' / 'x' / 'hidden' / 't.py'}")))

    def test_instruction_file_alias_flagged(self):
        alias = audit.HOME / ".claude" / "CLAUDE.md"
        self.assertTrue(self.hits(self.codex_call(f"cat {alias}")))
        self.assertTrue(self.hits(self.codex_call("cat ~/.codex/AGENTS.md")))
        self.assertTrue(self.hits(self.codex_call("cat ~/.agents/AGENTS.md")))

    def test_link_inside_private_root_flagged_as_written(self):
        root = Path(tempfile.mkdtemp(prefix="audit-root-"))
        outside = Path(tempfile.mkdtemp(prefix="audit-outside-"))
        self.addCleanup(shutil.rmtree, root)
        self.addCleanup(shutil.rmtree, outside)
        (outside / "secret.txt").write_text("x")
        (root / "link").symlink_to(outside / "secret.txt")
        transcript = self.transcript(self.codex_call(f"cat {root / 'link'}"))
        self.assertTrue(audit.audit_transcript(transcript, self.run_dir, extra_roots=(root,)))

    def test_parent_navigation_flagged(self):
        for command in ("find .. -name '*.py' -exec cat {} +", "ls ./..", "cat ../"):
            with self.subTest(command=command):
                self.assertTrue(self.hits(self.codex_call(command)))

    def test_grep_search_text_is_not_an_access(self):
        self.assertEqual(self.hits(self.claude_call("Grep", {"pattern": str(PRIVATE), "path": "."})), [])
        self.assertTrue(self.hits(self.claude_call("Grep", {"pattern": "x", "path": str(PRIVATE.parent)})))

    def test_legitimate_session_not_flagged(self):
        events = [
            self.claude_call("Bash", {"command": "python3 -m unittest discover -s tests && git status"}),
            self.claude_call("Write", {"file_path": str(self.run_dir / "intervals.py"), "content": "x = 1/2\n"}),
            self.claude_call("Read", {"file_path": "tests/test_intervals.py"}),
            self.claude_call("Bash", {"command": "/usr/bin/env python3 -c 'import sys; print(sys.path)' > /tmp/x.txt"}),
            self.codex_call("/bin/zsh -lc \"git diff abc123..HEAD -- src/intervals.py\""),
        ]
        self.assertEqual(self.hits(*events), [])


if __name__ == "__main__":
    unittest.main()
