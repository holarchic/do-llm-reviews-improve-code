"""Hidden review tests: `preview` counts only what the next run reads, a flag given empty is still a flag given,
and a boards file holding text UTF-8 can't encode is not crashed on."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobscan import cli, init, store
from jobscan.records import Board, Posting
from tests.fakes import patch_smtp

PROFILE = """titles = ["Account Executive"]
locations = ["London"]
eligible_where = "the UK"
tags = ["founding", "location_eligible"]
"""


def posting(board, key, title, location="London, UK"):
    return Posting(key=f"{board}:{key}", ats=board.split(":")[0], board=board, company=board.split(":")[1],
                   title=title, location=location, url=f"https://example.com/{key}", description="")


class Home(unittest.TestCase):
    """A temporary $JOBSCAN_HOME and a CLI caller; `terminal` makes stdin claim to be one."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        environ = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
        patcher = mock.patch.dict(os.environ, {**environ, "JOBSCAN_HOME": str(self.home)}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cli(self, *argv, stdin="", terminal=False):
        out, err, typed = io.StringIO(), io.StringIO(), io.StringIO(stdin)
        typed.isatty = lambda: terminal
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch("sys.stdin", typed):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def install(self):
        (self.home / "profiles").mkdir(parents=True, exist_ok=True)
        (self.home / "profiles" / "ae.toml").write_text(PROFILE)
        (self.home / "config.toml").write_text(init.CONFIG)
        (self.home / "openrouter.key").write_text("sk-or-v1-abc\n")
        (self.home / "openrouter.key").chmod(0o600)


class PreviewTest(Home):
    def test_preview_counts_only_boards_the_next_run_reads(self):
        self.install()
        conn = store.open_db(self.home)
        try:
            store.apply_fetch(conn, "ashby:acme", (posting("ashby:acme", "1", "Account Executive"),),
                              "2026-10-01T00:00:00+00:00")
            store.apply_fetch(conn, "greenhouse:beta", (posting("greenhouse:beta", "9", "Senior Account Executive"),),
                              "2026-10-03T00:00:00+00:00")
            # Stored by an earlier run, but on neither the discovered list nor the boards file now.
            store.apply_fetch(conn, "greenhouse:gone", (posting("greenhouse:gone", "7", "Account Executive"),),
                              "2026-10-04T00:00:00+00:00")
            store.replace_discovered(conn, [Board("ashby", "acme", "acme", ""), Board("greenhouse", "beta", "beta", "")],
                                     "2026-10-01T00:00:00+00:00")
        finally:
            conn.close()
        (self.home / "boards.json").write_text(json.dumps({"companies": []}))
        code, out, err = self.cli("preview")
        self.assertEqual(code, 0, err)
        self.assertIn("matching your titles and locations: 2 postings", out)
        self.assertNotIn("gone", out)


class EmptyFlagTest(Home):
    def test_init_with_a_flag_given_empty_asks_nothing_and_refuses(self):
        asked = []

        def ask(*args):
            asked.append(args)
            raise EOFError

        with mock.patch("builtins.input", side_effect=ask), mock.patch("getpass.getpass", side_effect=ask):
            code, _, _ = self.cli("init", "--name", "", terminal=True)
        self.assertEqual((asked, code), ([], 2))
        self.assertFalse((self.home / "config.toml").exists())
        # The control: with the flag filled in, the same command sets up.
        code, _, err = self.cli("init", "--name", "ae", "--title", "Account Executive", "--location", "London",
                                "--eligible-where", "the UK", "--key-stdin", stdin="sk-or-v1-abc\n")
        self.assertEqual(code, 0, err)

    def test_email_with_an_empty_sender_is_refused_sending_and_saving_nothing(self):
        calls = []
        for patcher in patch_smtp(calls, {}):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.install()
        code, _, err = self.cli("email", "--to", "you@gmail.com", "--from", "", "--password-stdin",
                                stdin="abcd efgh ijkl mnop\n")
        self.assertEqual(code, 2, err)
        self.assertEqual(calls, [])
        self.assertFalse((self.home / "email.toml").exists())
        # The control: without the empty --from, the sender is the address and the setting is saved.
        code, _, err = self.cli("email", "--to", "you@gmail.com", "--password-stdin", stdin="abcd efgh ijkl mnop\n")
        self.assertEqual(code, 0, err)
        self.assertTrue((self.home / "email.toml").exists())


class BoardsFileTest(Home):
    def test_a_boards_file_holding_a_lone_surrogate_is_kept_or_refused_never_crashed_on(self):
        """JSON can hold a lone surrogate escape that UTF-8 can't encode. Writing it back escaped and refusing
        the file are both right; an exception is not."""
        self.install()
        path = self.home / "boards.json"
        original = '{"note": "\\ud800", "companies": []}'
        path.write_text(original)
        code, _, err = self.cli("boards", "add", "https://job-boards.greenhouse.io/acme")
        if code == 2:
            self.assertEqual(path.read_text(), original)
        else:
            self.assertEqual(code, 0, err)
            data = json.loads(path.read_text())
            self.assertEqual((data["note"], [c["board_url"] for c in data["companies"]]),
                             ("\ud800", ["https://job-boards.greenhouse.io/acme"]))
        # The control: a boards file that can be written back takes the board.
        path.write_text('{"companies": []}')
        code, _, err = self.cli("boards", "add", "https://job-boards.greenhouse.io/acme")
        self.assertEqual(code, 0, err)
        self.assertIn("https://job-boards.greenhouse.io/acme", path.read_text())


if __name__ == "__main__":
    unittest.main()
