"""Hidden feature tests: company notes in the profile, their matcher, and a run that shows them."""
import json
import unittest
from pathlib import Path

from jobscan.config import CompanyNote, ConfigError, Profile, load_profile, profile_toml
from jobscan.watch import noted
from tests.fakes import ashby_job
from tests.test_profile_write import PROFILE as WRITTEN_PROFILE, Home
from tests.test_run import PROFILE, RunFixture


class NotedTest(unittest.TestCase):
    def test_every_note_naming_the_company_in_file_order(self):
        notes = (CompanyNote("hooli-eu", "talking to Sam"), CompanyNote("Globex", "rejected"),
                 CompanyNote("Hooli", "applied 29 Sep"))
        self.assertEqual(noted(notes, "Hooli Europe", "ashby:hooli-eu"), ("talking to Sam", "applied 29 Sep"))
        self.assertEqual(noted(notes, "Acme", "ashby:acme"), ())
        self.assertEqual(noted((), "Hooli", "ashby:hooli"), ())


class CompanyNotesProfileTest(Home):
    def test_company_notes_are_a_table_written_only_when_set_and_read_back(self):
        self.assertNotIn("company_notes", profile_toml(WRITTEN_PROFILE))
        noting = Profile(**{**vars(WRITTEN_PROFILE), "watch": ("Acme",), "company_notes": (
            CompanyNote("Acme AI", 'applied 29 Sep, "PM"'), CompanyNote('Q "x" Ltd', "line one\nline two"),
            CompanyNote("hooli-eu", "talking to Sam"), CompanyNote("Zürich Ltd", "rejected"))})
        text = profile_toml(noting)
        self.assertIn('\n[company_notes]\n"Acme AI" = "applied 29 Sep, \\"PM\\""\n', text)
        self.assertLess(text.index("[company_notes]"), text.index("[custom_tags."))
        (self.home / "profiles" / "ae.toml").write_text(text)
        self.assertEqual(load_profile(self.home), noting)

    def test_company_notes_that_arent_names_to_notes_or_name_one_twice_are_refused(self):
        for table, message in [('company_notes = "Acme"', "must be a table of company names to your notes"),
                               ("[company_notes]\nAcme = 3", "must be a table"),
                               ('[company_notes]\nAcme = " "', "must be a table"),
                               ('[company_notes]\n" " = "x"', "must be a table"),
                               ('[company_notes]\nAcme = "a"\nACME = "b"', "names a company twice"),
                               ('[company_notes]\nAcme = "a"\n" acme " = "b"', "names a company twice")]:
            top, _, tables = profile_toml(WRITTEN_PROFILE).partition("\n\n[custom_tags")
            text = (table + "\n" + top if table.startswith("company_notes =") else top + "\n\n" + table) + \
                "\n\n[custom_tags" + tables
            (self.home / "profiles" / "ae.toml").write_text(text)
            with self.assertRaisesRegex(ConfigError, message, msg=table):
                load_profile(self.home)


class CompanyNotesRunTest(RunFixture):
    def test_a_note_shows_on_each_entry_from_the_company_it_names(self):
        (self.home / "profiles" / "ae.toml").write_text(
            PROFILE + '\n[company_notes]\n"Initech" = "applied 29 Sep"\n"hooli-eu" = "talking to Sam"\n')
        (self.home / "listed.json").write_text(json.dumps({"companies": [
            {"company": "Acme", "board_url": f"{self.ats.url}/posting-api/job-board/acme"},
            {"company": "Initech Ltd", "board_url": f"{self.ats.url}/posting-api/job-board/initech"},
            {"company": "Hooli", "board_url": f"{self.ats.url}/posting-api/job-board/hooli-eu"},
        ]}))
        self.ats.routes["/posting-api/job-board/initech"] = (200, {"jobs": [ashby_job("i1", "Account Executive")]})
        self.ats.routes["/posting-api/job-board/hooli-eu"] = (200, {"jobs": [ashby_job("h1", "Account Executive")]})
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "4"))
        blocks = {b.split("\n", 1)[0]: b for b in Path(path).read_text().split("\n\n")}
        self.assertEqual(blocks["## Initech Ltd — Account Executive · London"].splitlines()[2],
                         "Your note: applied 29 Sep")
        self.assertEqual(blocks["## Hooli — Account Executive · London"].splitlines()[2], "Your note: talking to Sam")
        self.assertNotIn("Your note", blocks["## Acme — Account Executive · London"])


if __name__ == "__main__":
    unittest.main()
