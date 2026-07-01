"""Hermetic tests for the 2026-06-04 careers-discovery additions (no network).

Covers: ATS slug-guess router + variants, the Rippling v1-primary/v2-paginated
counter, the net-new sitemap/robots/structured-data/acquirer discovery helpers,
the extended ATS host regex, and the run.py canonical board-URL round-trip.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import ats_api  # noqa: E402
import cascade  # noqa: E402
import run  # noqa: E402


class SlugVariantTests(unittest.TestCase):
    def test_brand_prefix_strip(self):
        v = ats_api.slug_variants("goshippo.com")
        self.assertIn("goshippo", v)
        self.assertIn("shippo", v)  # the prefix-strip that actually wins Rippling

    def test_suffix_and_case_forms(self):
        v = ats_api.slug_variants("ivanti.com")
        self.assertIn("ivanti", v)
        self.assertIn("Ivanti1", v)  # SmartRecruiters-style Capitalized + digit suffix

    def test_dehyphen_and_prior_names(self):
        self.assertIn("carbonhealth", ats_api.slug_variants("carbon-health.com"))
        self.assertIn("logdna", ats_api.slug_variants("mezmo.com", prior_names=["LogDNA"]))

    def test_strips_subdomains_and_www_without_overstripping(self):
        # messy website values still yield the company label
        for d in ("www.acme.com", "careers.acme.com", "go.acme.com", "http://www.acme.com/jobs"):
            self.assertIn("acme", ats_api.slug_variants(d), d)
        # never over-strip a 2-label apex or a ccTLD
        self.assertIn("work", ats_api.slug_variants("work.com"))
        self.assertIn("acme", ats_api.slug_variants("acme.co.uk"))


class SlugRouterTests(unittest.TestCase):
    def setUp(self):
        self._orig = ats_api._get_json

    def tearDown(self):
        ats_api._get_json = self._orig

    def test_router_finds_board_on_stripped_slug(self):
        def fake(url, timeout):
            if "rippling" in url and "/shippo/" in url:
                return [{"name": "Eng", "department": "R&D", "url": "u", "workLocation": "Remote"}] * 3
            return None
        ats_api._get_json = fake
        r = ats_api.count_open_roles_by_slug("goshippo.com", timeout=1)
        self.assertIsNotNone(r)
        self.assertEqual((r["ats"], r["slug"], r["count"]), ("rippling", "shippo", 3))
        self.assertEqual(r["discovery"], "slug_guess")

    def test_router_none_when_nothing_resolves(self):
        ats_api._get_json = lambda url, timeout: None
        self.assertIsNone(ats_api.count_open_roles_by_slug("nonexistent-xyz.com", timeout=1))

    def test_router_rejects_zero_count_coincidental_board(self):
        # A blind slug guess that lands a 200 + count==0 board (SmartRecruiters returns
        # these for arbitrary slugs) must NOT be reported as a find — that was a real FP.
        ats_api._get_json = lambda url, timeout: (
            {"totalFound": 0, "content": []} if "smartrecruiters" in url else None)
        self.assertIsNone(ats_api.count_open_roles_by_slug("autodesk.com", timeout=1))


class RipplingCountTests(unittest.TestCase):
    def setUp(self):
        self._orig = ats_api._get_json

    def tearDown(self):
        ats_api._get_json = self._orig

    def test_v1_primary_complete_list(self):
        ats_api._get_json = lambda url, timeout: (
            [{"name": "A"}, {"name": "B"}] if "/ats/v1/board/acme/jobs" in url else None)
        r = ats_api.count_open_roles("rippling", "acme")
        self.assertEqual(r["count"], 2)
        self.assertIn("/ats/v1/", r["api_url"])

    def test_v2_pagination_when_v1_down(self):
        def fake(url, timeout):
            if "/ats/v1/board/acme/jobs" in url:
                return None  # v1 retired
            if "/ats/v2/board/acme/jobs" in url:
                if "page=" not in url:
                    return {"items": [{"name": "A"}] * 20, "page": 0, "pageSize": 20,
                            "totalItems": 45, "totalPages": 3}
                if "page=1" in url:
                    return {"items": [{"name": "B"}] * 20}
                if "page=2" in url:
                    return {"items": [{"name": "C"}] * 5}
            return None
        ats_api._get_json = fake
        r = ats_api.count_open_roles("rippling", "acme")
        self.assertEqual(r["count"], 45)  # accumulated 20+20+5 == totalItems
        self.assertIn("/ats/v2/", r["api_url"])


class IndexDiscoveryTests(unittest.TestCase):
    def test_robots_careers_and_sitemaps(self):
        txt = ("User-agent: *\nDisallow: /tmp\n"
               "Sitemap: https://x.com/sitemap.xml\n"
               "# we are hiring: https://faire.com/careers\n")
        out = cascade.discover_via_robots("faire.com", lambda u: txt)
        self.assertIn("https://faire.com/careers", out["careers_urls"])
        self.assertIn("https://x.com/sitemap.xml", out["sitemaps"])

    def test_sitemap_index_recursion(self):
        def fake(url):
            if url.endswith("/sitemap.xml"):
                return ("<sitemapindex><sitemap><loc>https://k.com/job_listing-sitemap.xml"
                        "</loc></sitemap></sitemapindex>")
            if "job_listing" in url:
                return ("<urlset><url><loc>https://k.com/job/senior-eng/</loc></url>"
                        "<url><loc>https://k.com/about</loc></url></urlset>")
            return None
        found = cascade.discover_via_sitemap("k.com", fake)
        self.assertIn("https://k.com/job/senior-eng/", found)
        self.assertNotIn("https://k.com/about", found)  # non-careers URL excluded

    def test_structured_data_tokens(self):
        toks = cascade.extract_ats_tokens_from_json(
            '{"greenHouseToken":"credible","leverAccountName":"foo"}')
        self.assertIn(("greenhouse", "credible"), toks)
        self.assertIn(("lever", "foo"), toks)


class AcquirerAndSocialTests(unittest.TestCase):
    def test_acquirer_redirect_divergent_domain(self):
        self.assertTrue(cascade.detect_acquirer_redirect("https://nextroll.com/careers", "adroll.com")["acquired"])

    def test_acquirer_ignores_self_and_ats(self):
        self.assertFalse(cascade.detect_acquirer_redirect("https://careers.acme.com", "acme.com")["acquired"])
        self.assertFalse(cascade.detect_acquirer_redirect("https://boards.greenhouse.io/acme", "acme.com")["acquired"])

    def test_social_jobs_tab_capture(self):
        links = cascade.social_jobs_tab_links(
            '<a href="https://www.linkedin.com/company/accelo/jobs">jobs</a>')
        self.assertEqual(links, ["https://www.linkedin.com/company/accelo/jobs"])


class NewAtsHostRegexTests(unittest.TestCase):
    def test_new_vendor_hosts_match(self):
        html = ('<a href="https://acme.careers.hibob.com/jobs">a</a>'
                '<a href="https://app.careerpuck.com/job-board/color">b</a>'
                '<a href="https://co.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/x">c</a>'
                '<a href="https://workforcenow.adp.com/mascsr/default/mdf/recruitment/r.html?cid=z">d</a>')
        hits = cascade.EMBEDDED_ATS_URL_RE.findall(html)
        self.assertEqual(len(hits), 4)


class BoardUrlRoundTripTests(unittest.TestCase):
    def test_every_builder_round_trips_through_detect_ats(self):
        for ats in ("greenhouse", "lever", "ashby", "smartrecruiters",
                    "workable", "recruitee", "pinpoint", "rippling"):
            url = run.board_url_for(ats, "acme")
            self.assertIsNotNone(url, ats)
            detected = ats_api.detect_ats(url)
            self.assertIsNotNone(detected, "%s -> %s" % (ats, url))
            self.assertEqual(detected[0], ats)
            self.assertEqual(detected[1].lower(), "acme")


class ApiIndexPassGatingTests(unittest.TestCase):
    """Scale/correctness gating of run.discover_via_apis_and_indexes (no network)."""

    def setUp(self):
        self._slug = ats_api.count_open_roles_by_slug
        self._fetch = cascade.fetch_text
        ats_api.count_open_roles_by_slug = lambda *a, **k: None  # no network
        cascade.fetch_text = lambda *a, **k: None
        self.conn = cascade.connect(":memory:")

    def tearDown(self):
        ats_api.count_open_roles_by_slug = self._slug
        cascade.fetch_text = self._fetch
        self.conn.close()

    def test_eligibility_and_skip_known(self):
        c = self.conn
        # a: served ok, no board -> eligible
        cascade.upsert_page(c, "a.com", "https://a.com", 200, "httpx", True, "html", commit=True)
        # b: served ok, but a prior run already found an ATS board -> skip_known drops it
        cascade.upsert_page(c, "b.com", "https://b.com", 200, "httpx", True, "html", commit=True)
        cascade.upsert_discovered_page(c, "b.com", "https://boards.greenhouse.io/b", "other",
                                       "https://boards.greenhouse.io/b", 200, "httpx", True, "x",
                                       commit=True)
        # c: WAF-blocked apex (403) -> still eligible (slug-guess is HTML-independent)
        cascade.upsert_page(c, "c.com", "https://c.com", 403, "httpx", False, "", commit=True)
        # d: DNS/connection-dead (status 0) -> skipped to bound cost
        cascade.upsert_page(c, "d.com", "https://d.com", 0, "httpx", False, "", commit=True)

        stats = run.discover_via_apis_and_indexes(
            c, ["a.com", "b.com", "c.com", "d.com"], {}, timeout=1, max_workers=4)
        self.assertEqual(stats["probed"], 2)  # a (ok) + c (blocked); b known, d dead

    def test_refetch_disables_skip_known(self):
        c = self.conn
        cascade.upsert_page(c, "b.com", "https://b.com", 200, "httpx", True, "html", commit=True)
        cascade.upsert_discovered_page(c, "b.com", "https://boards.greenhouse.io/b", "careers",
                                       "https://boards.greenhouse.io/b", 200, "httpx", True, "x",
                                       commit=True)
        stats = run.discover_via_apis_and_indexes(
            c, ["b.com"], {}, timeout=1, max_workers=2, skip_known=False)
        self.assertEqual(stats["probed"], 1)  # refetch re-probes even known boards


if __name__ == "__main__":
    unittest.main()
