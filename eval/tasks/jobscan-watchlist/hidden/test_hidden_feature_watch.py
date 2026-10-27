"""Hidden feature tests: the watchlist matcher, its profile key, and a run whose digest leads with watched
companies."""
import json
import unittest
from pathlib import Path

from jobscan import digest
from jobscan.config import ConfigError, Profile, load_profile, profile_toml
from jobscan.watch import watched
from tests.fakes import ashby_job, model_reply, tags_quoting_the_posting
from tests.test_profile_write import PROFILE as WRITTEN_PROFILE, Home
from tests.test_run import PROFILE, RunFixture


class WatchedTest(unittest.TestCase):
    def test_a_name_is_whole_words_of_the_company_case_folded(self):
        for terms, company, expected in [(("Acme",), "Acme AI", True), (("acme ai",), "ACME AI Ltd", True),
                                         (("Acme",), "Acmeco", False), (("Acme",), "Big Acme", True),
                                         (("Straße",), "STRASSE GmbH", True), (("AI",), "Acme AI", True),
                                         (("Globex", "Acme"), "Acme", True), ((), "Acme", False),
                                         (("Initech",), "Initrode", False)]:
            self.assertEqual(watched(terms, company, "ashby:x"), expected, (terms, company))

    def test_a_slug_matches_the_boards_own_slug_exactly(self):
        for term, board, expected in [("hooli-eu", "ashby:hooli-eu", True), ("Hooli-EU", "lever:hooli-eu", True),
                                      ("hooli-eu", "ashby:hooli-eu-2", False), ("hooli", "ashby:hooli-eu", False),
                                      ("acme.wd3.careers", "workday:acme.wd3.careers", True)]:
            self.assertEqual(watched((term,), "Someone Else", board), expected, (term, board))


class WatchProfileTest(Home):
    def test_the_watchlist_is_written_only_when_set_and_read_back(self):
        self.assertNotIn("watch", profile_toml(WRITTEN_PROFILE))
        watching = Profile(**{**vars(WRITTEN_PROFILE), "watch": ("Acme", "Acme AI", "hooli-eu", 'Q "quoted"')})
        self.assertIn('watch = ["Acme", "Acme AI", "hooli-eu", "Q \\"quoted\\""]', profile_toml(watching))
        (self.home / "profiles" / "ae.toml").write_text(profile_toml(watching))
        self.assertEqual(load_profile(self.home), watching)
        (self.home / "profiles" / "ae.toml").write_text("watch = []\n" + profile_toml(WRITTEN_PROFILE))
        self.assertEqual(load_profile(self.home), WRITTEN_PROFILE)

    def test_a_watchlist_that_isnt_names_or_names_one_twice_is_refused(self):
        for value, message in [('[""]', "`watch` must be a list of strings"), ("[1]", "`watch` must be a list"),
                               ('"Acme"', "`watch` must be a list"),
                               ('["Acme", "ACME"]', "`watch` lists a company twice")]:
            (self.home / "profiles" / "ae.toml").write_text(f"watch = {value}\n" + profile_toml(WRITTEN_PROFILE))
            with self.assertRaisesRegex(ConfigError, message, msg=value):
                load_profile(self.home)


class WatchRunTest(RunFixture):
    def test_watched_companies_lead_the_digest_and_a_ruled_out_one_is_starred(self):
        (self.home / "profiles" / "ae.toml").write_text(
            PROFILE + 'require = { language_required = "no" }\nwatch = ["Initech", "hooli-eu"]\n')
        (self.home / "listed.json").write_text(json.dumps({"companies": [
            {"company": "Acme", "board_url": f"{self.ats.url}/posting-api/job-board/acme"},
            {"company": "Initech Ltd", "board_url": f"{self.ats.url}/posting-api/job-board/initech"},
            {"company": "Hooli", "board_url": f"{self.ats.url}/posting-api/job-board/hooli-eu"},
        ]}))
        self.ats.routes["/posting-api/job-board/initech"] = (200, {"jobs": [
            ashby_job("i1", "Account Executive"), ashby_job("i2", "Account Executive - German Speaking")]})
        self.ats.routes["/posting-api/job-board/hooli-eu"] = (200, {"jobs": [ashby_job("h1", "Account Executive")]})

        def answering(payload):
            tags = tags_quoting_the_posting(payload)
            if "German" in tags["founding"]["quote"]:
                tags["language_required"] = {"value": "German", "quote": tags["founding"]["quote"]}
            return model_reply(tags)

        self.model.routes["/chat/completions"] = (200, answering)
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "4"))
        text = Path(path).read_text()
        self.assertTrue(text.startswith(f"# ae — {Path(path).stem} · 4 new · 2 watched · 1 left out\n\n"
                                        f"{digest.WATCH_LEGEND}\n"), text[:200])
        headings = [line for line in text.splitlines() if line.startswith("## ")]
        self.assertEqual(sorted(headings[:2]), ["## ★ Hooli — Account Executive · London",
                                                "## ★ Initech Ltd — Account Executive · London"])
        self.assertEqual(headings[2:], ["## Acme — Account Executive · London",
                                        "## Acme — Enterprise Account Executive · London", "## Left out (1)"])
        self.assertIn("\n- ★ Initech Ltd — Account Executive - German Speaking · London · language_required: German",
                      text)


if __name__ == "__main__":
    unittest.main()
