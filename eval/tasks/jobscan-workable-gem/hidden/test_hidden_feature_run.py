"""Hidden feature tests: Workable and Gem boards are routed, fetched and reach the digest through `jobscan run`."""
import json
import unittest
from pathlib import Path

from jobscan import fetch
from tests.test_run import RunFixture


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


class RoutingTest(unittest.TestCase):
    def test_api_paths_and_board_pages_route_to_their_ats_on_any_host(self):
        for url, ats in [("https://apply.workable.com/acme", "workable"),
                         ("https://apply.workable.com/api/v1/widget/accounts/acme", "workable"),
                         ("https://jobs.ashbyhq.com/api/v1/widget/accounts/acme", "workable"),
                         ("https://apply.workable.com/v1/boards/acme/jobs", "greenhouse"),
                         ("https://jobs.gem.com/acme", "gem"),
                         ("https://api.gem.com/job_board/v0/acme/job_posts/", "gem"),
                         ("https://jobs.gem.com/posting-api/job-board/acme", "ashby"),
                         ("https://job-boards.greenhouse.io/job_board/v0/acme", "gem")]:
            self.assertEqual(fetch.board_for("", url).ats, ats, url)


class WorkableGemRunTest(RunFixture):
    def test_workable_and_gem_boards_reach_the_digest(self):
        (self.home / "boards.json").write_text(json.dumps({"companies": [
            {"company": "Acme", "board_url": f"{self.ats.url}/posting-api/job-board/acme"},
            {"company": "Initech", "board_url": f"{self.ats.url}/api/v1/widget/accounts/initech"},
            {"company": "Hooli", "board_url": f"{self.ats.url}/job_board/v0/hooli/job_posts/"},
        ]}))
        self.ats.routes["/api/v1/widget/accounts/initech?details=true"] = (200, {"jobs": [
            workable_job("AB12", "Account Executive", "London"), workable_job("CD34", "Engineer"),
            workable_job("AB12", "Account Executive", "Manchester")]})
        self.ats.routes["/job_board/v0/hooli/job_posts/"] = (200, [
            gem_job("g1", "Account Executive", "London, UK"), gem_job("g2", "Engineer")])
        code, (_, new, path) = self.run_cli()
        self.assertEqual((code, new), (0, "4"))
        text = Path(path).read_text()
        self.assertIn("## Initech — Account Executive · London, United Kingdom / Manchester, United Kingdom\n"
                      "https://apply.workable.com/j/AB12 · Workable · first seen", text)
        self.assertIn("## Hooli — Account Executive · London, UK\nhttps://jobs.gem.com/acme/g1 · Gem · first seen",
                      text)
        self.assertEqual(text.count("· 2 open roles"), 2)  # one per new board: jobs, not Workable's entries
        self.assertEqual(self.run_cli()[1][1:], ["0", "-"])


if __name__ == "__main__":
    unittest.main()
