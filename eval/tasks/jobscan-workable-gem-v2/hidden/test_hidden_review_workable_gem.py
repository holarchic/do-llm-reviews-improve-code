"""Hidden review tests: board links are judged after decoding and folding, a place is listed once, and a remote
job's Remote marker isn't lost to a name that merely contains the word."""
import unittest

from jobscan import fetch, gem, prefilter, workable
from jobscan.config import Profile


def workable_job(shortcode, title, city="London", country="United Kingdom", description=None, **extra):
    """A Workable widget-API job as `?details=true` returns it: one entry per location."""
    return {"shortcode": shortcode, "title": title, "url": f"https://apply.workable.com/j/{shortcode}",
            "description": description if description is not None else f"<p>{title} role. We sell software.</p>",
            "telecommuting": False, "city": city, "state": "", "country": country,
            "locations": [{"city": city, "region": None, "country": country, "countryCode": "GB", "hidden": False}],
            **extra}


def gem_job(job_id, title, location="London", offices=(), content_plain=None, **extra):
    """A Gem job board API job: plain-text and HTML descriptions, a location and offices with their own."""
    text = content_plain if content_plain is not None else f"{title} role. We sell software."
    return {"id": job_id, "title": title, "absolute_url": f"https://jobs.gem.com/acme/{job_id}",
            "content_plain": text, "content": f"<p>{text}</p>", "location": {"name": location},
            "offices": [{"name": name, "location": {"name": place}} for name, place in offices],
            "location_type": "in_office", **extra}


class DecodedLinkTest(unittest.TestCase):
    def test_an_encoded_job_link_is_not_a_board(self):
        self.assertIsNone(fetch.board_for("Acme", "https://apply.workable.com/%6a/599813E1DD"))

    def test_a_greenhouse_token_is_folded_before_it_is_checked(self):
        board = fetch.board_for("", "https://job-boards.greenhouse.io/%E2%84%AAlarna")
        self.assertEqual(getattr(board, "id", None), "greenhouse:klarna")


class WorkablePlacesTest(unittest.TestCase):
    board = workable.page_board("Acme", "https://apply.workable.com/acme")

    def test_a_hidden_record_repeating_a_place_lists_it_once(self):
        job = workable_job("AB12", "AE", telecommuting=True, locations=[
            {"city": "London", "region": "England", "country": "United Kingdom", "hidden": False},
            {"city": "", "region": None, "country": "Ireland", "hidden": True},
            {"city": "London", "region": "England", "country": "United Kingdom", "hidden": True}])
        self.assertEqual(workable.parse(self.board, {"jobs": [job]})[0].location,
                         "London, England, United Kingdom / Ireland / Remote")


class GemRemoteTest(unittest.TestCase):
    board = gem.page_board("Acme", "https://jobs.gem.com/acme")

    def test_remote_is_added_unless_a_name_says_remote_as_a_word(self):
        remote_only = Profile(name="r", titles=("AE",), exclude_titles=(), locations=("Remote",), eligible_where="",
                              tags=(), custom_tags=())
        for location, offices in [("Work remotely", ()), ("United States", [("RemoteHQ", "")])]:
            (p,) = gem.parse(self.board, [gem_job("1", "AE", location, offices=offices, location_type="remote")])
            self.assertTrue(p.location.endswith(" / Remote"), p.location)
            self.assertTrue(prefilter.matches(p, remote_only), p.location)


if __name__ == "__main__":
    unittest.main()
