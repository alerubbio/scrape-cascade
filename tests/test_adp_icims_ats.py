"""Hermetic tests for ADP WorkforceNow + iCIMS board support (no network).

Both are URL-keyed like Workday: ADP needs the cid GUID from the discovered
recruitment.html embed URL (public requisitions JSON, live-verified 2026-06-10:
missing cid -> 500, unknown cid -> 404, walled tenant -> 401/403); iCIMS needs
the careers-<tenant>.icims.com host and walks server-rendered in_iframe=1 pages
(pr=N, ~20/page, no total in the HTML). Neither joins SLUG_GUESS_ATS_ORDER —
there is no safe blind-guess path for either.

The iCIMS HTML fragments mirror the recorded careers-cobank fixtures; the ADP
200 shape is the inferred jobRequisitions/meta.totalNumber structure (parsed
defensively — a wrong shape returns None, never a wrong count).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import ats_api

ADP_CID = "4a3f1c2e-9d8b-4e5f-a6c7-112233445566"
ADP_URL = ("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/"
           f"recruitment.html?cid={ADP_CID}&ccId=19000101_000001&lang=en_US")

ADP_LIST_RESPONSE = {
    "jobRequisitions": [
        {"jobRequisitionReference": {"requisitionID": "REQ-1", "clientRequisitionID": "101"},
         "requisitionTitle": "IT Systems Administrator"},
        {"jobRequisitionReference": {"requisitionID": "REQ-2", "clientRequisitionID": "102"},
         "requisitionTitle": "Endpoint Engineer"},
    ],
    "meta": {"totalNumber": 17},
}


def _icims_card(jid, slug, title):
    return (
        '<li class="iCIMS_JobCardItem"><div class="col-xs-12 title">'
        f'<a href="https://careers-cobank.icims.com/jobs/{jid}/{slug}/job?in_iframe=1" '
        f'class="iCIMS_Anchor" title="{jid} - {title}"><h3>{title}</h3></a>'
        "</div></li>"
    )


ICIMS_PAGE_0 = ('<ul class="container-fluid iCIMS_JobsTable">'
                + _icims_card(7836, "tech-risk-director", "Technology Risk Director")
                + _icims_card(7833, "lead-engineer", "Lead Engineer")
                + "</ul>")
ICIMS_PAGE_1 = ('<ul class="container-fluid iCIMS_JobsTable">'
                + _icims_card(7714, "credit-analyst", "Credit Analyst")
                + "</ul>")
ICIMS_EMPTY = '<ul class="container-fluid iCIMS_JobsTable"></ul>'


class AdpIcimsDetectTests(unittest.TestCase):
    def test_detect_adp_cid_from_embed_url(self):
        self.assertEqual(ats_api.detect_ats(ADP_URL), ("adp", ADP_CID))

    def test_detect_adp_requires_cid(self):
        self.assertIsNone(ats_api.detect_ats(
            "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"))

    def test_detect_myjobs_slug_but_separate_family(self):
        self.assertEqual(ats_api.detect_ats("https://myjobs.adp.com/npgexternalcareers"),
                         ("adp_myjobs", "npgexternalcareers"))

    def test_detect_icims_public_board_only(self):
        self.assertEqual(ats_api.detect_ats("https://careers-cobank.icims.com/jobs/search?ss=1"),
                         ("icims", "cobank"))
        # the bare tenant host is the auth-walled portal, NOT the public board
        self.assertIsNone(ats_api.detect_ats("https://cobank.icims.com/jobs/search"))

    def test_not_in_slug_guess_order(self):
        self.assertNotIn("adp", ats_api.SLUG_GUESS_ATS_ORDER)
        self.assertNotIn("icims", ats_api.SLUG_GUESS_ATS_ORDER)

    def test_existing_detections_unaffected(self):
        self.assertEqual(ats_api.detect_ats("https://jobs.lever.co/acme"), ("lever", "acme"))
        self.assertEqual(
            ats_api.detect_ats("https://k2services.wd503.myworkdayjobs.com/Opensity"),
            ("workday", "k2services/Opensity"),
        )


class AdpCountTests(unittest.TestCase):
    def setUp(self):
        self._orig = ats_api._get_status_json

    def tearDown(self):
        ats_api._get_status_json = self._orig

    def test_open_board_counts_via_meta_total(self):
        ats_api._get_status_json = lambda url, timeout: (200, ADP_LIST_RESPONSE)
        res = ats_api._adp_count(ADP_URL)
        self.assertEqual(res["ats"], "adp")
        self.assertEqual(res["slug"], ADP_CID)
        self.assertEqual(res["count"], 17)  # meta.totalNumber over len(list)
        self.assertIn("Endpoint Engineer", res["titles"])
        self.assertNotIn("auth_walled", res)

    def test_auth_walled_is_a_real_board_not_a_miss(self):
        ats_api._get_status_json = lambda url, timeout: (403, None)
        res = ats_api._adp_count(ADP_URL)
        self.assertTrue(res["auth_walled"])
        self.assertIsNone(res["count"])

    def test_dead_cid_is_none(self):
        ats_api._get_status_json = lambda url, timeout: (404, None)
        self.assertIsNone(ats_api._adp_count(ADP_URL))
        ats_api._get_status_json = lambda url, timeout: (500, None)
        self.assertIsNone(ats_api._adp_count(ADP_URL))

    def test_wrong_shape_is_none_never_a_wrong_count(self):
        ats_api._get_status_json = lambda url, timeout: (200, {"unexpected": True})
        res = ats_api._adp_count(ADP_URL)
        self.assertEqual(res["count"], 0)  # empty list + no meta -> honest zero
        ats_api._get_status_json = lambda url, timeout: (200, None)
        self.assertIsNone(ats_api._adp_count(ADP_URL))

    def test_non_adp_url_fast_none(self):
        called = []
        ats_api._get_status_json = lambda url, timeout: called.append(url)
        self.assertIsNone(ats_api._adp_count("https://jobs.lever.co/acme"))
        self.assertEqual(called, [])


class IcimsCountTests(unittest.TestCase):
    def setUp(self):
        self._orig = ats_api._get_text

    def tearDown(self):
        ats_api._get_text = self._orig

    def _route(self, pages):
        def fake(url, timeout):
            for frag, html in pages.items():
                if frag in url:
                    return html
            return None
        ats_api._get_text = fake

    def test_paginates_until_no_new_ids(self):
        self._route({"pr=0": ICIMS_PAGE_0, "pr=1": ICIMS_PAGE_1, "pr=2": ICIMS_EMPTY})
        res = ats_api._icims_count("https://careers-cobank.icims.com/jobs/search?ss=1")
        self.assertEqual(res["ats"], "icims")
        self.assertEqual(res["slug"], "cobank")
        self.assertEqual(res["count"], 3)
        self.assertIn("Credit Analyst", res["titles"])

    def test_repeated_page_stops_walk(self):
        # a board that serves page 0 for any pr must not loop or double-count
        self._route({"pr=": ICIMS_PAGE_0})
        res = ats_api._icims_count("https://careers-cobank.icims.com/jobs/search?ss=1")
        self.assertEqual(res["count"], 2)

    def test_dead_tenant_is_none(self):
        self._route({})
        self.assertIsNone(ats_api._icims_count("https://careers-gone.icims.com/jobs/search"))

    def test_non_icims_url_fast_none(self):
        called = []
        ats_api._get_text = lambda url, timeout: called.append(url)
        self.assertIsNone(ats_api._icims_count("https://jobs.lever.co/acme"))
        self.assertEqual(called, [])


class CountFromUrlDispatchTests(unittest.TestCase):
    def test_adp_and_icims_dispatch_before_slug_path(self):
        orig_adp, orig_icims = ats_api._adp_count, ats_api._icims_count
        try:
            ats_api._adp_count = lambda url, timeout=0: ({"ats": "adp", "count": 5}
                                                         if "adp.com" in url else None)
            ats_api._icims_count = lambda url, timeout=0: ({"ats": "icims", "count": 7}
                                                           if "icims.com" in url else None)
            self.assertEqual(ats_api.count_from_url(ADP_URL)["count"], 5)
            self.assertEqual(
                ats_api.count_from_url("https://careers-cobank.icims.com/jobs/search")["count"], 7)
        finally:
            ats_api._adp_count, ats_api._icims_count = orig_adp, orig_icims


if __name__ == "__main__":
    unittest.main()


class WorkableJunkSlugTests(unittest.TestCase):
    """Live noise 2026-06-10: workable's own portal hosts and bare posting URLs
    must not detect as company boards (they poisoned the harvest queue)."""

    def test_portal_subdomains_rejected(self):
        for host in ("jobs", "jobseekers", "careers", "help", "www"):
            self.assertIsNone(ats_api.detect_ats(f"https://{host}.workable.com/view/x"),
                              host)

    def test_bare_posting_url_rejected_but_slugged_kept(self):
        self.assertIsNone(ats_api.detect_ats("https://apply.workable.com/j/8B29504386"))
        self.assertEqual(ats_api.detect_ats("https://apply.workable.com/acme/j/123"),
                         ("workable", "acme"))
        self.assertEqual(ats_api.detect_ats("https://acme.workable.com"),
                         ("workable", "acme"))
