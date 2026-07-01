"""Hermetic tests for fetch_postings (no network — _get_json/_post_json mocked).

Fixtures are trimmed recordings of the live responses verified 2026-06-10
(convene/greenhouse, deepl/ashby, carbon-health/rippling, livenation/workday,
etc.) — structurally complete, content shortened.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import ats_api

LONG = ("We are looking for an engineer to own our endpoint platform. "
        "You will design, build, and operate fleet tooling. " * 5)


class HtmlToTextTests(unittest.TestCase):
    def test_blocks_become_newlines_scripts_dropped(self):
        html = ("<script>var x=1;</script><h2>About</h2><p>First.</p>"
                "<ul><li>One</li><li>Two</li></ul>")
        text = ats_api.html_to_text(html)
        self.assertNotIn("var x", text)
        self.assertIn("About", text)
        self.assertIn("One", text)
        self.assertIn("\n", text)

    def test_entities_unescaped_and_plain_passthrough(self):
        self.assertEqual(ats_api.html_to_text("Tools &amp; Data"), "Tools & Data")
        self.assertEqual(ats_api.html_to_text("plain text"), "plain text")


def _mock_get(routes):
    def fake(url, timeout):
        for frag, data in routes.items():
            if frag in url:
                return data
        return None
    return fake


class GreenhousePostingsTests(unittest.TestCase):
    def test_entity_escaped_content_unescaped(self):
        routes = {"boards-api.greenhouse.io/v1/boards/acme/jobs?content=true": {
            "jobs": [{"id": 1, "title": "Engineer",
                      "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                      "location": {"name": "Remote"},
                      "departments": [{"name": "Eng"}],
                      "content": "&lt;p&gt;%s&lt;/p&gt;" % LONG}],
            "meta": {"total": 1},
        }}
        orig = ats_api._get_json
        ats_api._get_json = _mock_get(routes)
        try:
            res = ats_api.fetch_postings("greenhouse", "acme")
        finally:
            ats_api._get_json = orig
        self.assertEqual(res["count"], 1)
        p = res["postings"][0]
        self.assertEqual(p["url"], "https://boards.greenhouse.io/acme/jobs/1")
        self.assertNotIn("&lt;", p["description_text"])
        self.assertIn("endpoint platform", p["description_text"])
        self.assertEqual(p["department"], "Eng")
        self.assertFalse(res["truncated"])


class LeverPostingsTests(unittest.TestCase):
    def test_plain_fields_and_lists_concatenated(self):
        routes = {"api.lever.co/v0/postings/acme": [
            {"id": "u1", "text": "Designer", "hostedUrl": "https://jobs.lever.co/acme/u1",
             "categories": {"location": "NYC", "team": "Design"},
             "descriptionPlain": LONG,
             "lists": [{"text": "Requirements", "content": "<li>Figma</li>"}],
             "additionalPlain": "Benefits included."},
        ]}
        orig = ats_api._get_json
        ats_api._get_json = _mock_get(routes)
        try:
            res = ats_api.fetch_postings("lever", "acme")
        finally:
            ats_api._get_json = orig
        p = res["postings"][0]
        self.assertIn("Figma", p["description_text"])
        self.assertIn("Benefits included", p["description_text"])
        self.assertEqual(p["location"], "NYC")


class AshbyPostingsTests(unittest.TestCase):
    def test_unlisted_filtered_and_plain_preferred(self):
        routes = {"api.ashbyhq.com/posting-api/job-board/acme": {"jobs": [
            {"id": "a1", "title": "PM", "isListed": True,
             "jobUrl": "https://jobs.ashbyhq.com/acme/a1",
             "location": "Berlin", "descriptionPlain": LONG},
            {"id": "a2", "title": "Hidden", "isListed": False,
             "jobUrl": "https://jobs.ashbyhq.com/acme/a2"},
        ]}}
        orig = ats_api._get_json
        ats_api._get_json = _mock_get(routes)
        try:
            res = ats_api.fetch_postings("ashby", "acme")
        finally:
            ats_api._get_json = orig
        self.assertEqual(res["count"], 1)
        self.assertEqual(len(res["postings"]), 1)
        self.assertIn("endpoint platform", res["postings"][0]["description_text"])


class SmartRecruitersPostingsTests(unittest.TestCase):
    def test_paginates_and_merges_detail_sections(self):
        list_p1 = {"totalFound": 2, "content": [{"id": "1"}, {"id": "2"}]}
        detail = {"name": "Analyst", "postingUrl": "https://jobs.smartrecruiters.com/Acme/1-analyst",
                  "location": {"city": "Austin", "country": "us"},
                  "jobAd": {"sections": {
                      "jobDescription": {"title": "Job Description", "text": f"<p>{LONG}</p>"},
                      "qualifications": {"title": "Qualifications", "text": "<p>SQL</p>"}}}}

        def fake(url, timeout):
            if "postings?limit" in url:
                return list_p1 if "offset=0" in url else {"totalFound": 2, "content": []}
            if "/postings/" in url:
                return detail
            return None

        orig = ats_api._get_json
        ats_api._get_json = fake
        try:
            res = ats_api.fetch_postings("smartrecruiters", "acme")
        finally:
            ats_api._get_json = orig
        self.assertEqual(res["count"], 2)
        self.assertEqual(len(res["postings"]), 2)
        self.assertIn("SQL", res["postings"][0]["description_text"])
        self.assertEqual(res["postings"][0]["location"], "Austin, us")


class RipplingPostingsTests(unittest.TestCase):
    def test_uuid_dedupe_and_detail_descriptions(self):
        listing = [
            {"uuid": "x1", "name": "Nurse", "url": "https://ats.rippling.com/acme/jobs/x1",
             "workLocation": {"label": "SF"}},
            {"uuid": "x1", "name": "Nurse", "url": "https://ats.rippling.com/acme/jobs/x1",
             "workLocation": {"label": "LA"}},  # location-exploded duplicate
            {"uuid": "x2", "name": "Doctor", "url": "https://ats.rippling.com/acme/jobs/x2",
             "workLocation": {"label": "NY"}},
        ]

        def fake(url, timeout):
            if url.endswith("/jobs"):
                return listing
            if "/jobs/x1" in url:
                # live shape: description is a DICT of HTML sections
                return {"description": {"company": f"<p>{LONG}</p>", "role": "<p>Do the work.</p>"}}
            if "/jobs/x2" in url:
                return {"description": f"<p>{LONG}</p>"}  # plain-string variant stays supported
            return None

        orig = ats_api._get_json
        ats_api._get_json = fake
        try:
            res = ats_api.fetch_postings("rippling", "acme")
        finally:
            ats_api._get_json = orig
        self.assertEqual(res["count"], 2)  # deduped by uuid, not 3 rows
        self.assertEqual(len(res["postings"]), 2)
        self.assertTrue(all(p["description_text"] for p in res["postings"]))


class WorkdayPostingsTests(unittest.TestCase):
    BOARD = "https://acme.wd5.myworkdayjobs.com/External"

    def test_pages_list_and_fetches_details(self):
        page0 = {"total": 3, "jobPostings": [
            {"externalPath": "/job/loc/eng-1_R1", "title": "Eng 1", "postedOn": "Posted Today"},
            {"externalPath": "/job/loc/eng-2_R2", "title": "Eng 2"},
        ]}
        page1 = {"total": 3, "jobPostings": [
            {"externalPath": "/job/loc/eng-3_R3", "title": "Eng 3"},
        ]}
        details = {"jobPostingInfo": {
            "title": "Eng", "jobDescription": f"<p>{LONG}</p>",
            "location": "Toronto", "jobReqId": "R1",
            "externalUrl": "https://acme.wd5.myworkdayjobs.com/External/job/eng"}}

        def fake_post(url, body, timeout):
            return page0 if body.get("offset", 0) == 0 else page1

        orig_post, orig_get = ats_api._post_json, ats_api._get_json
        ats_api._post_json = fake_post
        ats_api._get_json = lambda url, timeout: details if "/wday/cxs/" in url else None
        try:
            res = ats_api.fetch_postings_from_url(self.BOARD)
        finally:
            ats_api._post_json, ats_api._get_json = orig_post, orig_get
        self.assertEqual(res["ats"], "workday")
        self.assertEqual(res["count"], 3)
        self.assertEqual(len(res["postings"]), 3)
        self.assertIn("endpoint platform", res["postings"][0]["description_text"])

    def test_workday_without_board_url_is_none(self):
        self.assertIsNone(ats_api.fetch_postings("workday", "acme/External"))


class CapAndContractTests(unittest.TestCase):
    def test_truncated_flag_when_capped(self):
        jobs = [{"id": i, "title": f"T{i}",
                 "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{i}",
                 "content": "&lt;p&gt;%s&lt;/p&gt;" % LONG} for i in range(5)]
        routes = {"boards-api.greenhouse.io": {"jobs": jobs, "meta": {"total": 5}}}
        orig = ats_api._get_json
        ats_api._get_json = _mock_get(routes)
        try:
            res = ats_api.fetch_postings("greenhouse", "acme", max_postings=2)
        finally:
            ats_api._get_json = orig
        self.assertEqual(res["count"], 5)
        self.assertEqual(len(res["postings"]), 2)
        self.assertTrue(res["truncated"])

    def test_unknown_ats_and_total_failure_are_none(self):
        self.assertIsNone(ats_api.fetch_postings("icims", "cobank"))  # counts only
        orig = ats_api._get_json
        ats_api._get_json = lambda url, timeout: None
        try:
            self.assertIsNone(ats_api.fetch_postings("greenhouse", "gone"))
        finally:
            ats_api._get_json = orig

    def test_from_url_dispatch(self):
        orig = ats_api._get_json
        ats_api._get_json = _mock_get({"recruitee.com/api/offers": {"offers": [
            {"id": 9, "title": "Ops", "status": "published",
             "careers_url": "https://acme.recruitee.com/o/ops",
             "description": f"<p>{LONG}</p>"}]}})
        try:
            res = ats_api.fetch_postings_from_url("https://acme.recruitee.com/o/ops")
        finally:
            ats_api._get_json = orig
        self.assertEqual(res["ats"], "recruitee")
        self.assertEqual(res["postings"][0]["url"], "https://acme.recruitee.com/o/ops")


if __name__ == "__main__":
    unittest.main()
