"""SSR-JSON board extraction + chrome-shell routing + tracking-param dedup.

Fixtures are trimmed recordings of jobs.bendingspoons.com (verified live
2026-06-10: 35 postings in __NEXT_DATA__ props.pageProps.list while the served
text was 1,034 chars of filter chrome ending in a zero-state) plus the embedded
Greenhouse/Lever SSR shapes. Hermetic — no network.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import cascade
import run
import ssr_json

BSPOONS_ITEM = {
    "id": "6686d0e2a65ca3994b3a415b",
    "jobTitle": "UX/UI designer",
    "isEvent": False,
    "status": "active",
    "highLevelDescription": "<span>At Bending Spoons, we&#8217;re striving to build "
                            "one of the all-time great companies.</span>",
    "officeLocations": [{"title": "Milan (Italy)",
                         "country": {"isoCode": "IT", "name": "Italy"}}],
    "requirements": [{"title": "Reasoning ability",
                      "description": "Given the necessary knowledge, you can solve "
                                     "complex problems."}],
    "responsibilities": ["<span><strong>Shape high-leverage product experiences."
                         "</strong> Design exceptional interfaces.</span>"],
    "typesOfContract": ["permanent", "fixed_term"],
    "typesOfWork": ["Design"],
    "workSchedule": "full_time",
    "applicationForm": {"personalDetails": []},
}

BSPOONS_ITEM_2 = dict(BSPOONS_ITEM, id="9986d0e2a65ca3994b3a4222",
                      jobTitle="Endpoint engineer", typesOfWork=["IT"])
EVENT_ITEM = dict(BSPOONS_ITEM, id="aaa", jobTitle="Open day", isEvent=True)
CLOSED_ITEM = dict(BSPOONS_ITEM, id="bbb", jobTitle="Old role", status="archived")


def next_data_html(items, extra=""):
    payload = {"props": {"pageProps": {"list": items}},
               "page": "/", "buildId": "x"}
    return (
        '<html><body><div id="__next"><div class="filters">All departments</div></div>'
        '<script id="__NEXT_DATA__" type="application/json">%s</script>%s</body></html>'
        % (json.dumps(payload), extra)
    )


# The served text of jobs.bendingspoons.com (trimmed, phrases intact): filter
# chrome + a zero-state that CONTAINS strong vocabulary ("open jobs").
BSPOONS_CHROME_TEXT = (
    "About usCareersEventsJobs\n\nAbout usCareersEventsJobs\n\n"
    "# Jobs at\nBending Spoons\n\nFilters\n\nAll departments\n\n"
    "All contract types\n\nAny locations\n\n"
    "Show only jobs suitable for students or new grads\n\n"
    "No open jobs with these properties.\n\n"
    "Questions about our recruiting process?\n\nCheck the FAQ.\n\n"
    "Company\n\nAbout usCareersJobsEventsComplianceSupport center\n\n"
    "Follow us\n\nLinkedInInstagramGlassdoorFacebookMediumX\n\n"
    "Partnership inquiries\n\npartnerships@example.com\n\n"
    "Copyright Bending Spoons Operations S.p.A. Via Nino Bonnet 10 Milan Italy "
    "Privacy and cookie policyYour cookie preferences\n\nImpossible. Maybe."
)
CHROME_SHELL_HTML = (
    '<html><head><script src="/a.js"></script><script src="/b.js"></script></head>'
    '<body><div id="__next"></div><a href="/about">About</a><a href="#">FAQ</a>'
    "<script>window.x=1</script><script>window.y=2</script></body></html>"
)


class ExtractionTests(unittest.TestCase):
    def test_bendingspoons_next_data(self):
        url = "https://jobs.bendingspoons.com/?utm_source=bendingspoons&utm_campaign=careers"
        res = ssr_json.postings_from_html(url, next_data_html(
            [BSPOONS_ITEM, BSPOONS_ITEM_2, EVENT_ITEM, CLOSED_ITEM]))
        self.assertIsNotNone(res)
        self.assertEqual(res["source"], "next_data")
        self.assertEqual(res["count"], 2)  # event + archived filtered out
        titles = {p["title"] for p in res["postings"]}
        self.assertEqual(titles, {"UX/UI designer", "Endpoint engineer"})
        p = next(x for x in res["postings"] if x["title"] == "UX/UI designer")
        self.assertEqual(p["location"], "Milan (Italy)")
        self.assertEqual(p["department"], "Design")
        self.assertEqual(p["posting_id"], "6686d0e2a65ca3994b3a415b")
        self.assertIn("all-time great companies", p["description_text"])
        self.assertIn("Reasoning ability: Given the necessary knowledge",
                      p["description_text"])
        self.assertIn("Shape high-leverage product experiences", p["description_text"])
        # synthesized per-posting URL: stable, unique, fragment-anchored
        self.assertTrue(p["url"].endswith("#ssr:6686d0e2a65ca3994b3a415b"))

    def test_greenhouse_and_lever_ssr_shapes(self):
        gh = {"title": "Endpoint Engineer", "location": {"name": "Remote — US"},
              "absolute_url": "https://boards.greenhouse.io/acme/jobs/123"}
        lever = {"text": "Platform Engineer",
                 "hostedUrl": "https://jobs.lever.co/acme/uuid-1",
                 "categories": {"location": "NYC", "commitment": "Full-time",
                                "team": "Infra"}}
        html = next_data_html([gh, lever])
        res = ssr_json.postings_from_html("https://acme.com/careers", html)
        self.assertEqual(res["count"], 2)
        by_title = {p["title"]: p for p in res["postings"]}
        self.assertEqual(by_title["Endpoint Engineer"]["url"],
                         "https://boards.greenhouse.io/acme/jobs/123")
        self.assertEqual(by_title["Endpoint Engineer"]["location"], "Remote — US")
        self.assertEqual(by_title["Platform Engineer"]["url"],
                         "https://jobs.lever.co/acme/uuid-1")

    def test_nav_blog_and_team_arrays_rejected(self):
        nav = [{"title": "About us", "url": "/about"},
               {"title": "Careers", "url": "/careers"}]
        blog = [{"title": "Life at Acme", "description": "Our culture story",
                 "publishedAt": "2026-01-01"}]
        team = [{"name": "Jane Doe", "role": "CTO", "location": "London"},
                {"name": "Joe Bloggs", "role": "VP Eng", "location": "NYC"}]
        people = [{"firstName": "Jane", "lastName": "Doe", "position": "Engineer",
                   "location": "Milan"}]
        for arr in (nav, blog, team, people):
            html = next_data_html(arr)
            self.assertIsNone(
                ssr_json.postings_from_html("https://acme.com/careers", html),
                "array should be rejected: %r" % arr[0])

    def test_remix_context_and_nuxt(self):
        jobs = json.dumps({"jobs": [{"jobTitle": "SRE", "location": "Berlin",
                                     "employmentType": "Full-time"}]})
        remix_html = ("<html><body><script>window.__remixContext = %s;</script>"
                      "</body></html>" % jobs)
        res = ssr_json.postings_from_html("https://a.com/careers", remix_html)
        self.assertEqual(res["source"], "remix")
        self.assertEqual(res["postings"][0]["title"], "SRE")
        # Nuxt 2 function-call payloads are JS, not JSON: must not crash, must not match
        nuxt_html = ("<html><body><script>window.__NUXT__=(function(a){return "
                     "{jobs:[]}}(1));</script></body></html>")
        self.assertIsNone(ssr_json.postings_from_html("https://a.com/careers", nuxt_html))
        nuxt_json_html = ("<html><body><script>window.__NUXT__={\"data\":[{\"openings\":"
                          "[{\"jobTitle\": \"DevOps\", \"location\": \"Oslo\"}]}]}"
                          "</script></body></html>")
        res = ssr_json.postings_from_html("https://a.com/careers", nuxt_json_html)
        self.assertEqual(res["source"], "nuxt")
        self.assertEqual(res["postings"][0]["title"], "DevOps")

    def test_dedupe_across_blobs_and_truncation(self):
        html = next_data_html(
            [BSPOONS_ITEM],
            extra='<script type="application/json">%s</script>'
                  % json.dumps({"alt": [BSPOONS_ITEM]}))
        res = ssr_json.postings_from_html("https://a.com/careers", html)
        self.assertEqual(res["count"], 1)
        old_cap = ssr_json.MAX_POSTINGS
        try:
            ssr_json.MAX_POSTINGS = 1
            res = ssr_json.postings_from_html(
                "https://a.com/careers", next_data_html([BSPOONS_ITEM, BSPOONS_ITEM_2]))
            self.assertEqual(res["count"], 2)
            self.assertTrue(res["truncated"])
            self.assertEqual(len(res["postings"]), 1)
        finally:
            ssr_json.MAX_POSTINGS = old_cap

    def test_format_block(self):
        res = {"count": 35, "postings": [
            {"title": "UX/UI designer", "location": "Milan (Italy)"},
            {"title": "Endpoint engineer", "location": None},
        ]}
        block = ssr_json.format_postings_block(res, max_titles=1)
        self.assertTrue(block.startswith(ssr_json.SSR_POSTINGS_MARKER))
        self.assertIn("35 open roles", block)
        self.assertIn("- UX/UI designer — Milan (Italy)", block)
        self.assertIn("(+1 more)", block)


class TrackingParamTests(unittest.TestCase):
    def test_utm_variants_collapse_to_one_key(self):
        hero = ("https://jobs.bendingspoons.com/?utm_source=bendingspoons"
                "&utm_medium=website&utm_campaign=careers")
        footer = ("https://jobs.bendingspoons.com/?utm_source=bendingspoons"
                  "&utm_medium=website&utm_content=footer_link")
        self.assertEqual(cascade.normalize_page_key(hero),
                         cascade.normalize_page_key(footer))
        self.assertEqual(cascade.normalize_page_key(hero),
                         "https://jobs.bendingspoons.com/")

    def test_meaningful_params_survive(self):
        self.assertEqual(
            cascade.normalize_page_key("https://a.com/jobs?dept=eng&gh_jid=123"),
            "https://a.com/jobs?dept=eng&gh_jid=123")
        self.assertEqual(
            cascade.normalize_page_key(
                "https://a.com/jobs?utm_source=x&dept=eng&fbclid=abc"),
            "https://a.com/jobs?dept=eng")

    def test_bare_paths_stripped_too(self):
        self.assertEqual(cascade.normalize_page_key("/careers?utm_source=nav"),
                         "/careers")
        self.assertEqual(cascade.normalize_page_key("/careers?team=infra&utm_id=9"),
                         "/careers?team=infra")


class ChromeShellTests(unittest.TestCase):
    def test_bendingspoons_chrome_is_shell_chrome(self):
        self.assertTrue(
            cascade.looks_like_chrome_shell(CHROME_SHELL_HTML, BSPOONS_CHROME_TEXT))
        self.assertEqual(
            cascade.render_hint_for(CHROME_SHELL_HTML, BSPOONS_CHROME_TEXT,
                                    page_type="careers"),
            "shell_chrome")

    def test_zero_state_does_not_read_as_strong(self):
        for phrase in ("No open jobs with these properties.",
                       "0 open positions", "No current openings", "no results"):
            self.assertFalse(
                cascade.STRONG_CAREERS_TEXT_RE.search(
                    cascade.ZERO_STATE_JOBS_RE.sub(" ", phrase)),
                phrase)

    def test_not_shell_when_listings_or_anchors_present(self):
        listing_text = BSPOONS_CHROME_TEXT + "\nSenior Engineer Full-time Milan"
        self.assertFalse(
            cascade.looks_like_chrome_shell(CHROME_SHELL_HTML, listing_text))
        anchor_html = CHROME_SHELL_HTML.replace(
            '<a href="/about">', '<a href="/jobs/123-senior-engineer">')
        self.assertFalse(
            cascade.looks_like_chrome_shell(anchor_html, BSPOONS_CHROME_TEXT))

    def test_ssr_block_suppresses_shell_chrome(self):
        enriched = (BSPOONS_CHROME_TEXT + "\n\n" + ssr_json.format_postings_block(
            {"count": 35, "postings": [{"title": "UX/UI designer",
                                        "location": "Milan (Italy)"}]}))
        self.assertIsNone(
            cascade.render_hint_for(CHROME_SHELL_HTML, enriched, page_type="careers"))

    def test_non_careers_pages_never_shell_chrome(self):
        self.assertIsNone(
            cascade.render_hint_for(CHROME_SHELL_HTML, BSPOONS_CHROME_TEXT,
                                    page_type="news"))
        self.assertIsNone(
            cascade.render_hint_for(CHROME_SHELL_HTML, BSPOONS_CHROME_TEXT))


class PersistWiringTests(unittest.TestCase):
    def setUp(self):
        self.conn = cascade.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def _res(self, html, page_type="careers"):
        url = ("https://jobs.bendingspoons.com/?utm_source=bendingspoons"
               "&utm_medium=website&utm_campaign=careers")
        return {"domain": "bendingspoons.com", "path": url, "page_type": page_type,
                "url": url, "status": 200, "ok": True, "html": html,
                "linked_from_homepage": True}

    def test_careers_page_gets_block_rows_and_no_render_hint(self):
        run.persist_page_result(
            self.conn, self._res(next_data_html([BSPOONS_ITEM, BSPOONS_ITEM_2])), "httpx")
        self.conn.commit()
        pg = cascade.get_discovered_page(
            self.conn, "bendingspoons.com", "https://jobs.bendingspoons.com/")
        self.assertIsNotNone(pg)  # tracking params stripped from the cache key
        self.assertIn(ssr_json.SSR_POSTINGS_MARKER, pg["text"])
        self.assertIn("2 open roles", pg["text"])
        self.assertIsNone(pg["render_hint"])
        rows = cascade.list_ssr_postings(self.conn, "bendingspoons.com")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["page_url"], "https://jobs.bendingspoons.com/")
        self.assertTrue(all(r["description_text"] for r in rows))

    def test_refetch_replaces_postings(self):
        run.persist_page_result(
            self.conn, self._res(next_data_html([BSPOONS_ITEM, BSPOONS_ITEM_2])), "httpx")
        run.persist_page_result(
            self.conn, self._res(next_data_html([BSPOONS_ITEM])), "httpx")
        self.conn.commit()
        self.assertEqual(len(cascade.list_ssr_postings(self.conn, "bendingspoons.com")), 1)

    def test_non_careers_page_not_mined(self):
        run.persist_page_result(
            self.conn, self._res(next_data_html([BSPOONS_ITEM]), page_type="news"), "httpx")
        self.conn.commit()
        self.assertEqual(cascade.list_ssr_postings(self.conn, "bendingspoons.com"), [])

    def test_rescue_selection_skips_ssr_mined_pages(self):
        run.persist_page_result(
            self.conn, self._res(next_data_html([BSPOONS_ITEM, BSPOONS_ITEM_2])), "httpx")
        self.conn.commit()
        target = {"domain": "bendingspoons.com",
                  "path": "https://jobs.bendingspoons.com/",
                  "url": "https://jobs.bendingspoons.com/",
                  "page_type": "careers", "linked_from_homepage": True}
        self.assertEqual(
            run.select_browser_rescue_page_targets(self.conn, [target]), [])
        # control: same page WITHOUT the SSR block (thin text) is selected
        cascade.upsert_discovered_page(
            self.conn, "bendingspoons.com", "https://jobs.bendingspoons.com/",
            "careers", "https://jobs.bendingspoons.com/", 200, "httpx", True,
            "Loading...", linked_from_homepage=True)
        self.assertEqual(
            len(run.select_browser_rescue_page_targets(self.conn, [target])), 1)

    def test_shell_chrome_hint_persisted_when_no_ssr_data(self):
        res = self._res(CHROME_SHELL_HTML)
        with_text = dict(res, html=CHROME_SHELL_HTML)
        # monkey-patch html_to_text to return the real chrome text for this HTML
        orig = cascade.html_to_text
        cascade.html_to_text = lambda h: BSPOONS_CHROME_TEXT if h == CHROME_SHELL_HTML else orig(h)
        try:
            run.persist_page_result(self.conn, with_text, "httpx")
            self.conn.commit()
        finally:
            cascade.html_to_text = orig
        pg = cascade.get_discovered_page(
            self.conn, "bendingspoons.com", "https://jobs.bendingspoons.com/")
        self.assertEqual(pg["render_hint"], "shell_chrome")


if __name__ == "__main__":
    unittest.main()
