"""Hidden review tests: every form of a board URL names one board, a posting's own escaped text survives however
many times its content was escaped, and a malformed job fails its board."""
import json
import tempfile
import unittest
from pathlib import Path

from jobscan import fetch, greenhouse
from jobscan.records import Board, BoardError

BOARD = Board("greenhouse", "acme", "Acme", "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true")


def greenhouse_job(job_id, title, location="London", content=None, offices=()):
    """A Greenhouse Job Board API job as `?content=true` returns it: markup entity-escaped once."""
    markup = content if content is not None else f"&lt;p&gt;{title} role. We sell software.&lt;/p&gt;"
    return {"id": job_id, "title": title, "location": {"name": location},
            "offices": [{"name": o} for o in offices], "content": markup,
            "absolute_url": f"https://job-boards.greenhouse.io/acme/jobs/{job_id}"}


def escape(markup):
    return markup.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def description(content):
    (posting,) = greenhouse.parse(BOARD, {"jobs": [greenhouse_job(1, "AE", content=content)]})
    return posting.description


def board_for(url):
    """The board a boards file entry names, as `run` reads it."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "boards.json"
        path.write_text(json.dumps({"companies": [{"company": "", "board_url": url}]}))
        boards, _ = fetch.load_boards(path)
    return boards[0] if boards else None


class BoardUrlTest(unittest.TestCase):
    def test_a_semicolon_stays_in_the_token_written_or_encoded(self):
        for url in ["https://job-boards.greenhouse.io/x;y", "https://job-boards.greenhouse.io/x%3By",
                    "https://job-boards.greenhouse.io/x;y/jobs/1"]:
            self.assertEqual(getattr(board_for(url), "id", None), "greenhouse:x;y", url)

    def test_host_case_and_the_default_port_are_one_board(self):
        for url in ["https://Job-Boards.Greenhouse.io/acme", "https://job-boards.greenhouse.io:443/acme"]:
            self.assertEqual(getattr(board_for(url), "id", None), "greenhouse:acme", url)


class LiteralTextTest(unittest.TestCase):
    def test_escaped_text_in_the_posting_survives_any_number_of_escapings(self):
        markup = "<p>Use &lt;customer&gt; APIs.</p>"
        for content in (markup, escape(markup), escape(escape(markup))):
            self.assertEqual(description(content), "Use <customer> APIs.", content)


class MalformedJobTest(unittest.TestCase):
    def test_an_office_that_is_not_an_object_fails_the_board(self):
        with self.assertRaises(BoardError):
            greenhouse.parse(BOARD, {"jobs": [{**greenhouse_job(1, "AE"), "offices": [None]}]})

    def test_an_id_with_a_lone_surrogate_fails_the_board(self):
        with self.assertRaises(BoardError):
            greenhouse.parse(BOARD, {"jobs": [{**greenhouse_job(1, "AE"), "id": "a\ud800"}]})


if __name__ == "__main__":
    unittest.main()
