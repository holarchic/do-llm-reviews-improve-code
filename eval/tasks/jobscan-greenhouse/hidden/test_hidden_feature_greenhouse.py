"""Hidden feature tests. Greenhouse adapter: board URLs → boards (through a boards file), and Job Board API
payloads → postings."""
import json
import tempfile
import unittest
from pathlib import Path

from jobscan import fetch, greenhouse, scan, store, tag
from jobscan.records import Board, BoardError
from tests.fakes import model_reply, tags_quoting_the_posting
from tests.test_run import RunFixture

API = "https://boards-api.greenhouse.io/v1/boards/"


def greenhouse_job(job_id, title, location="London", content=None, offices=()):
    """A Greenhouse Job Board API job as `?content=true` returns it: markup entity-escaped once."""
    markup = content if content is not None else f"&lt;p&gt;{title} role. We sell software.&lt;/p&gt;"
    return {"id": job_id, "title": title, "location": {"name": location},
            "offices": [{"name": o} for o in offices], "content": markup,
            "absolute_url": f"https://job-boards.greenhouse.io/acme/jobs/{job_id}"}


def board_for(company, url):
    """The board a boards file entry names, as `run` reads it."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "boards.json"
        path.write_text(json.dumps({"companies": [{"company": company, "board_url": url}]}))
        boards, _ = fetch.load_boards(path)
    return boards[0] if boards else None


class BoardTest(unittest.TestCase):
    def test_every_url_form_is_one_board(self):
        for url in ["https://job-boards.greenhouse.io/acme", "https://job-boards.greenhouse.io/ACME/jobs/123",
                    "https://job-boards.eu.greenhouse.io/acme", "https://boards.greenhouse.io/acme",
                    "https://boards.eu.greenhouse.io/acme", "https://boards.greenhouse.io/embed/job_board?for=Acme",
                    "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
                    "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"]:
            b = board_for("Acme", url)
            self.assertEqual((b.id, b.api_url), ("greenhouse:acme", API + "acme/jobs?content=true"), url)

    def test_encoded_tokens_are_one_board_and_encoded_again_for_the_api(self):
        for url in ["https://job-boards.greenhouse.io/ep%2Bco", "https://job-boards.greenhouse.io/EP+Co",
                    "https://boards.greenhouse.io/embed/job_board?for=ep%2Bco"]:
            b = board_for("", url)
            self.assertEqual((b.id, b.company, b.api_url),
                             ("greenhouse:ep+co", "ep+co", API + "ep%2Bco/jobs?content=true"), url)
        self.assertEqual(board_for("", "https://job-boards.greenhouse.io/harrison%26star").api_url,
                         API + "harrison%26star/jobs?content=true")

    def test_api_url_on_another_host_is_kept(self):
        b = board_for("Acme", "http://127.0.0.1:8000/v1/boards/acme/jobs")
        self.assertEqual(b.api_url, "http://127.0.0.1:8000/v1/boards/acme/jobs?content=true")

    def test_other_urls_are_not_greenhouse(self):
        for url in ["https://jobs.ashbyhq.com/acme", "https://jobs.lever.co/acme", "https://job-boards.greenhouse.io/",
                    "https://boards.greenhouse.io/embed/job_board", "https://job-boards.greenhouse.io/a%3Ab",
                    "https://job-boards.greenhouse.io/a%2Fb", "https://job-boards.greenhouse.io/a%20b",
                    "https://www.example.com/acme", "ftp://job-boards.greenhouse.io/acme", "not a url"]:
            found = board_for("", url)
            self.assertTrue(found is None or found.ats != "greenhouse", url)


class ParseTest(unittest.TestCase):
    board = Board("greenhouse", "acme", "Acme", API + "acme/jobs?content=true")

    def test_posting_fields(self):
        (p,) = greenhouse.parse(self.board, {"jobs": [greenhouse_job(42, "Account Executive", "London, UK",
                                                                     offices=["London", "London, UK", "Remote (UK)"])]})
        self.assertEqual((p.key, p.ats, p.board, p.company, p.title, p.location, p.url),
                         ("greenhouse:acme:42", "greenhouse", "greenhouse:acme", "Acme", "Account Executive",
                          "London, UK / London / Remote (UK)", "https://job-boards.greenhouse.io/acme/jobs/42"))

    def test_escaped_once_or_twice_gives_the_same_text(self):
        markup = ("<div><p>You'll be our <strong>first sales hire</strong> &amp; more.</p>"
                  "<ul><li>Own EMEA</li></ul></div>")
        once = markup.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        twice = once.replace("&", "&amp;")
        for content in (once, twice, markup):
            (p,) = greenhouse.parse(self.board, {"jobs": [greenhouse_job(1, "AE", content=content)]})
            self.assertEqual(p.description, "You'll be our first sales hire & more.\nOwn EMEA", content[:20])
            self.assertTrue(tag.verify_quote("our first sales hire & more", p))

    def test_ids_may_be_numbers_or_strings(self):
        postings = greenhouse.parse(self.board, {"jobs": [greenhouse_job(1, "A"), greenhouse_job("x2", "B")]})
        self.assertEqual([p.key for p in postings], ["greenhouse:acme:1", "greenhouse:acme:x2"])

    def test_missing_location_and_offices_are_empty(self):
        job = greenhouse_job(1, "AE")
        job["location"], job["offices"] = None, None
        self.assertEqual(greenhouse.parse(self.board, {"jobs": [job]})[0].location, "")

    def test_a_malformed_job_fails_the_board(self):
        bad = []
        for change in [{"id": True}, {"id": None}, {"id": " "}, {"title": None}, {"title": 3}, {"location": "London"},
                       {"offices": "London"}, {"offices": [3]}, {"content": 5}, {"absolute_url": 1},
                       {"title": "\ud800"}]:
            bad.append({**greenhouse_job(1, "AE"), **change})
        for job in [*bad, "not a job"]:
            with self.assertRaises(BoardError, msg=job):
                greenhouse.parse(self.board, {"jobs": [job]})
        for data in [{}, {"jobs": "x"}, [], {"jobs": [greenhouse_job(1, "A"), greenhouse_job(1, "B")]}]:
            with self.assertRaises(BoardError, msg=data):
                greenhouse.parse(self.board, data)



class GreenhouseRunTest(RunFixture):
    def setUp(self):
        super().setUp()
        # A dead sentinel per adapter, so the discovered list covers every ATS and `run` doesn't rediscover.
        sentinels = [Board(ats, "sentinel", "sentinel", "http://127.0.0.1:9/never") for ats in fetch.ADAPTERS]
        conn = store.open_db(self.home)
        try:
            store.replace_discovered(conn, sentinels, scan._now())
            with conn:
                conn.execute("UPDATE boards SET not_found = ? WHERE slug = 'sentinel'", (store.DEAD_AFTER,))
        finally:
            conn.close()

    def test_ashby_and_greenhouse_boards_in_one_digest(self):
        (self.home / "boards.json").write_text(json.dumps({"companies": [
            {"company": "Acme", "board_url": f"{self.ats.url}/posting-api/job-board/acme"},
            {"company": "Globex", "board_url": f"{self.ats.url}/v1/boards/globex/jobs"},
        ]}))
        content = "&lt;p&gt;You'll be our &lt;strong&gt;first&lt;/strong&gt; Account Executive.&lt;/p&gt;"
        self.ats.routes["/v1/boards/globex/jobs?content=true"] = (200, {"jobs": [
            greenhouse_job(7, "Account Executive", "London, UK", content=content), greenhouse_job(8, "Engineer")]})

        def quoting_the_description(payload):
            tags = tags_quoting_the_posting(payload)
            if "first Account Executive" in payload["messages"][0]["content"]:
                tags["founding"] = {"value": "yes", "quote": "our first Account Executive"}
            return model_reply(tags)

        self.model.routes["/chat/completions"] = (200, quoting_the_description)
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "3"))
        text = Path(path).read_text()
        self.assertIn("## Globex — Account Executive · London, UK\n"
                      "https://job-boards.greenhouse.io/acme/jobs/7 · Greenhouse · first seen", text)
        self.assertIn('- founding: **yes** — "our first Account Executive"\n', text)  # verified: no ⚠
        self.assertIn("· 2 open roles", text)


if __name__ == "__main__":
    unittest.main()
