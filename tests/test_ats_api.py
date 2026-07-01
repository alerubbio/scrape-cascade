"""Hermetic tests for the direct ATS-API tier (no network — _get_json is mocked)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import ats_api


class DetectAtsTests(unittest.TestCase):
    def test_greenhouse_us_eu_unified(self):
        self.assertEqual(ats_api.detect_ats("https://boards.greenhouse.io/embed/job_board?for=acme"), ("greenhouse", "acme"))
        self.assertEqual(ats_api.detect_ats("https://job-boards.eu.greenhouse.io/embed/job_board?for=acme"), ("greenhouse", "acme"))
        self.assertEqual(ats_api.detect_ats("https://boards.greenhouse.io/acme"), ("greenhouse", "acme"))

    def test_lever_board_only_not_api(self):
        self.assertEqual(ats_api.detect_ats("https://jobs.lever.co/acme"), ("lever", "acme"))
        self.assertEqual(ats_api.detect_ats("https://jobs.eu.lever.co/acme"), ("lever", "acme"))
        # api.lever.co must NOT be mistaken for a board (was a bug)
        self.assertIsNone(ats_api.detect_ats("https://api.lever.co/v0/postings/acme"))

    def test_other_platforms(self):
        self.assertEqual(ats_api.detect_ats("https://jobs.ashbyhq.com/Acme"), ("ashby", "Acme"))
        self.assertEqual(ats_api.detect_ats("https://apply.workable.com/acme/"), ("workable", "acme"))
        self.assertEqual(ats_api.detect_ats("https://acme.recruitee.com/"), ("recruitee", "acme"))
        self.assertEqual(ats_api.detect_ats("https://acme.pinpointhq.com/"), ("pinpoint", "acme"))

    def test_rippling_locale_handling(self):
        self.assertIsNone(ats_api.detect_ats("https://ats.rippling.com/de-DE/jobs"))  # locale only, no slug
        self.assertEqual(ats_api.detect_ats("https://ats.rippling.com/acme/jobs"), ("rippling", "acme"))
        self.assertEqual(ats_api.detect_ats("https://ats.rippling.com/en-US/acme/jobs"), ("rippling", "acme"))

    def test_smartrecruiters_exact_host_only(self):
        self.assertEqual(ats_api.detect_ats("https://jobs.smartrecruiters.com/Acme"), ("smartrecruiters", "Acme"))
        # the vendor's own marketing site must NOT match with a junk slug
        self.assertIsNone(ats_api.detect_ats("https://www.smartrecruiters.com/blog/post"))
        self.assertIsNone(ats_api.detect_ats("https://smartrecruiters.com/resources"))

    def test_non_ats_returns_none(self):
        self.assertIsNone(ats_api.detect_ats("https://example.com/careers"))
        self.assertIsNone(ats_api.detect_ats(""))



    def test_breezy_subdomain_and_region(self):
        self.assertEqual(ats_api.detect_ats("https://acme.breezy.hr/"), ("breezy", "acme"))
        self.assertEqual(ats_api.detect_ats("https://75f-apac.breezy.hr/p/123-engineer"), ("breezy", "75f-apac"))
        self.assertIsNone(ats_api.detect_ats("https://www.breezy.hr/"))
        self.assertIsNone(ats_api.detect_ats("https://app.breezy.hr/signin"))

    def test_ultipro_jobboard_detect_only(self):
        self.assertEqual(
            ats_api.detect_ats("https://recruiting.ultipro.com/ACM1000ACME/JobBoard/11111111-2222-3333-4444-555555555555/"),
            ("ultipro", "ACM1000ACME/11111111-2222-3333-4444-555555555555"))
        self.assertEqual(
            ats_api.detect_ats("https://recruiting2.ultipro.com/ORG/JobBoard/uuid-here/OpportunityDetail?x=1"),
            ("ultipro", "ORG/uuid-here"))
        self.assertIsNone(ats_api.detect_ats("https://recruiting.ultipro.com/ACME"))
        self.assertNotIn("ultipro", ats_api.HARVESTABLE_ATS)
        self.assertEqual(ats_api._api_urls("ultipro", "ORG/uuid"), [])


class ExtractCountTests(unittest.TestCase):
    def test_greenhouse_meta_total_then_len_fallback(self):
        self.assertEqual(ats_api._extract("greenhouse", {"jobs": [{"title": "A"}, {"title": "B"}], "meta": {"total": 2}})[0], 2)
        self.assertEqual(ats_api._extract("greenhouse", {"jobs": [{"title": "A"}, {"title": "B"}]})[0], 2)

    def test_lever_list_len(self):
        self.assertEqual(ats_api._extract("lever", [{"text": "A"}, {"text": "B"}, {"text": "C"}])[0], 3)

    def test_ashby_counts_listed_only(self):
        data = {"jobs": [{"title": "A", "isListed": True}, {"title": "B", "isListed": False}, {"title": "C"}]}
        self.assertEqual(ats_api._extract("ashby", data)[0], 2)  # B excluded; C defaults listed

    def test_smartrecruiters_totalfound(self):
        self.assertEqual(ats_api._extract("smartrecruiters", {"totalFound": 9, "content": [{"name": "A"}]})[0], 9)

    def test_recruitee_published_only(self):
        data = {"offers": [{"title": "A", "status": "published"}, {"title": "B", "status": "draft"}, {"title": "C"}]}
        self.assertEqual(ats_api._extract("recruitee", data)[0], 2)

    def test_workable_pinpoint_rippling(self):
        self.assertEqual(ats_api._extract("workable", {"jobs": [{"title": "A"}]})[0], 1)
        self.assertEqual(ats_api._extract("pinpoint", {"data": [{"attributes": {"title": "A"}}, {"attributes": {"title": "B"}}]})[0], 2)
        self.assertEqual(ats_api._extract("rippling", [{"name": "A"}])[0], 1)

    def test_breezy_list_array(self):
        data = [
            {"id": "a", "name": "Engineer", "url": "https://acme.breezy.hr/p/a",
             "department": "Eng", "location": {"name": "NYC"}},
            {"id": "b", "name": "PM", "url": "https://acme.breezy.hr/p/b",
             "department": "Product", "location": {}},
        ]
        count, titles, depts = ats_api._extract("breezy", data)
        self.assertEqual(count, 2)
        self.assertIn("Engineer", titles)
        self.assertIn("Product", depts)
        self.assertEqual(ats_api._extract("breezy", [])[0], 0)
        self.assertIn("breezy.hr/json", ats_api._api_urls("breezy", "acme")[0])

    def test_extract_returns_full_titles_and_departments(self):
        gh = {"jobs": [
            {"title": "IT Manager", "departments": [{"name": "IT"}]},
            {"title": "Account Executive", "departments": [{"name": "Sales"}]},
            {"title": "Endpoint Administrator", "departments": [{"name": "IT"}]},
        ]}
        count, titles, depts = ats_api._extract("greenhouse", gh)
        self.assertEqual(count, 3)
        self.assertEqual(len(titles), 3)  # FULL list, not capped at 5
        self.assertIn("IT Manager", titles)
        self.assertIn("IT", depts)
        # lever pulls department from categories.team
        c, t, d = ats_api._extract("lever", [{"text": "Security Engineer", "categories": {"team": "Security"}}])
        self.assertEqual((c, t, d), (1, ["Security Engineer"], ["Security"]))

    def test_bad_shape_returns_none(self):
        self.assertIsNone(ats_api._extract("greenhouse", "not a dict"))


class ApiUrlsTests(unittest.TestCase):
    def test_endpoint_lists(self):
        self.assertEqual(len(ats_api._api_urls("greenhouse", "x")), 1)
        self.assertEqual(len(ats_api._api_urls("lever", "x")), 2)  # US + EU fallback
        self.assertIn("boards-api.greenhouse.io", ats_api._api_urls("greenhouse", "x")[0])
        self.assertEqual(ats_api._api_urls("nonsense", "x"), [])


class CountOpenRolesTests(unittest.TestCase):
    def setUp(self):
        self._orig = ats_api._get_json

    def tearDown(self):
        ats_api._get_json = self._orig

    def test_real_zero_distinct_from_none(self):
        ats_api._get_json = lambda url, timeout: {"jobs": [], "meta": {"total": 0}}
        r = ats_api.count_open_roles("greenhouse", "acme")
        self.assertIsNotNone(r)
        self.assertEqual(r["count"], 0)  # a real "no open roles" signal
        ats_api._get_json = lambda url, timeout: None
        self.assertIsNone(ats_api.count_open_roles("greenhouse", "acme"))  # fetch fail → fall through

    def test_lever_eu_fallback_when_us_empty(self):
        calls = []

        def fake(url, timeout):
            calls.append(url)
            return None if "api.lever.co" in url else [{"text": "A"}, {"text": "B"}]

        ats_api._get_json = fake
        r = ats_api.count_open_roles("lever", "acme")
        self.assertEqual(r["count"], 2)
        self.assertIn("api.eu.lever.co", r["api_url"])
        self.assertEqual(len(calls), 2)  # tried US first, then EU

    def test_lever_eu_fallback_when_us_empty_board(self):
        # US returns a 200 empty board (count 0) -- must still try EU and prefer its roles
        ats_api._get_json = lambda url, timeout: [] if "api.lever.co" in url else [{"text": "A"}]
        r = ats_api.count_open_roles("lever", "acme")
        self.assertEqual(r["count"], 1)
        self.assertIn("api.eu.lever.co", r["api_url"])


if __name__ == "__main__":
    unittest.main()
