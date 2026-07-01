"""Hermetic tests for Workday public-board support (no network — _post_json mocked).

Workday is the one ATS reached via POST (its /wday/cxs/ board endpoint 405s a GET),
and its slug "<tenant>/<site>" intentionally drops the data-center (wd503) that the
endpoint needs — so count_from_url re-parses the FULL url for tenant+dc+site. These
tests pin (a) detection collapsing locale segments and (b) the JSON parse, with the
network call stubbed out so nothing leaves the box.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import ats_api


class WorkdayDetectTests(unittest.TestCase):
    def test_detect_collapses_locale_segment(self):
        # Both the bare and the locale-prefixed board URL must yield the same slug.
        self.assertEqual(
            ats_api.detect_ats("https://k2services.wd503.myworkdayjobs.com/Opensity"),
            ("workday", "k2services/Opensity"),
        )
        self.assertEqual(
            ats_api.detect_ats("https://k2services.wd503.myworkdayjobs.com/en-US/Opensity"),
            ("workday", "k2services/Opensity"),
        )

    def test_detect_other_data_centers(self):
        self.assertEqual(
            ats_api.detect_ats("https://acme.wd1.myworkdayjobs.com/fr-FR/Careers"),
            ("workday", "acme/Careers"),
        )
        self.assertEqual(
            ats_api.detect_ats("https://acme.wd5.myworkdayjobs.com/External"),
            ("workday", "acme/External"),
        )

    def test_non_workday_unaffected(self):
        self.assertIsNone(ats_api.detect_ats("https://example.myworkday.com/Opensity"))
        self.assertIsNone(ats_api.detect_ats("https://k2services.myworkdayjobs.com/Opensity"))
        # Existing detection still works (no regression in the shared function).
        self.assertEqual(ats_api.detect_ats("https://jobs.lever.co/acme"), ("lever", "acme"))


class WorkdayExtractTests(unittest.TestCase):
    def test_extract_total_and_titles(self):
        data = {"total": 3, "jobPostings": [{"title": "A"}, {"title": "B"}, {"title": "C"}]}
        count, titles, depts = ats_api._extract("workday", data)
        self.assertEqual(count, 3)
        self.assertEqual(titles, ["A", "B", "C"])
        self.assertEqual(depts, [])

    def test_extract_prefers_server_total_over_page_len(self):
        # The board has 218 roles; jobPostings is only one page of 2 — trust "total".
        data = {"total": 218, "jobPostings": [{"title": "A"}, {"title": "B"}]}
        self.assertEqual(ats_api._extract("workday", data)[0], 218)

    def test_extract_bad_shape_returns_none(self):
        self.assertIsNone(ats_api._extract("workday", "not a dict"))


class WorkdayCountFromUrlTests(unittest.TestCase):
    def setUp(self):
        self._orig = ats_api._post_json

    def tearDown(self):
        ats_api._post_json = self._orig

    def test_count_from_url_posts_to_cxs_endpoint(self):
        seen = {}

        def fake_post(url, body, timeout):
            seen["url"] = url
            seen["body"] = body
            return {"total": 3, "jobPostings": [{"title": "A"}, {"title": "B"}, {"title": "C"}]}

        ats_api._post_json = fake_post
        r = ats_api.count_from_url("https://k2services.wd503.myworkdayjobs.com/en-US/Opensity")
        self.assertIsNotNone(r)
        self.assertEqual(r["ats"], "workday")
        self.assertEqual(r["count"], 3)
        self.assertEqual(len(r["titles"]), 3)
        self.assertEqual(r["slug"], "k2services/Opensity")
        # The data-center (wd503) the slug drops must reappear in the POST url.
        self.assertEqual(
            seen["url"],
            "https://k2services.wd503.myworkdayjobs.com/wday/cxs/k2services/Opensity/jobs",
        )
        self.assertEqual(seen["body"], {"limit": 20, "offset": 0, "searchText": ""})

    def test_count_from_url_none_on_fetch_failure(self):
        ats_api._post_json = lambda url, body, timeout: None
        self.assertIsNone(
            ats_api.count_from_url("https://k2services.wd503.myworkdayjobs.com/Opensity")
        )


if __name__ == "__main__":
    unittest.main()


class WorkdayDetailUrlTests(unittest.TestCase):
    """Live 2026-06-10: job-DETAIL URLs took the last segment as the site and
    manufactured a phantom per-job 'board' for every posting (stem.com made 9
    failed boards out of its own postings). Detail URLs must resolve to the
    BOARD site."""

    def test_detail_url_resolves_to_board_site(self):
        self.assertEqual(
            ats_api.detect_ats(
                "https://stem.wd1.myworkdayjobs.com/Stem_Careers/job/Remote/"
                "Data-Scientist--Forecasting_R1008"),
            ("workday", "stem/Stem_Careers"),
        )
        self.assertEqual(
            ats_api.detect_ats(
                "https://stem.wd1.myworkdayjobs.com/en-US/Stem_Careers/job/X/Y_R1"),
            ("workday", "stem/Stem_Careers"),
        )

    def test_workday_parts_detail_url(self):
        self.assertEqual(
            ats_api._workday_parts(
                "https://stem.wd1.myworkdayjobs.com/Stem_Careers/job/Remote/Title_R1"),
            ("stem", "wd1", "Stem_Careers"),
        )

    def test_board_urls_unchanged(self):
        self.assertEqual(
            ats_api.detect_ats("https://k2services.wd503.myworkdayjobs.com/en-US/Opensity"),
            ("workday", "k2services/Opensity"),
        )
