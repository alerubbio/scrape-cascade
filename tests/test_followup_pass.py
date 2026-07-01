"""Tests for discovery-recall PR-3: careers-subdomain probe demotion, wrong-page
re-mining at fetch time, and the unresolved-domain follow pass."""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import cascade
import run as run_mod

STRONG_TEXT = "Open positions: Senior Engineer, Designer. Full-time roles. " * 10
WEAK_HTML = ('<html><body><h1>Our Company</h1><p>We build things.</p>'
             '<a href="/company/careers">Careers</a></body></html>').ljust(600, " ")


class ProbeDemotionTests(unittest.TestCase):
    def _specs(self):
        return [
            {"page_type": "careers", "url": "https://careers.acme.com/", "path": "https://careers.acme.com/"},
            {"page_type": "careers", "path": "/careers"},
            {"page_type": "careers", "path": "/company/careers"},
            {"page_type": "careers", "path": "/jobs"},
        ]

    def test_subdomain_probe_loses_quota_race(self):
        picked = cascade.select_page_specs(self._specs(), max_pages_per_domain=3)
        urls = [s.get("url") or s.get("path") for s in picked]
        self.assertNotIn("https://careers.acme.com/", urls)  # probe no longer steals a slot
        self.assertIn("/careers", urls)
        self.assertIn("/company/careers", urls)

    def test_linked_careers_subdomain_still_wins(self):
        specs = self._specs()
        specs[0] = dict(specs[0], linked_from_homepage=True)  # a REAL link, not a guess
        picked = cascade.select_page_specs(specs, max_pages_per_domain=1)
        self.assertEqual(picked[0].get("url"), "https://careers.acme.com/")


class WrongPageDetectionTests(unittest.TestCase):
    def test_divergent_redirect_is_wrong_page(self):
        res = {"domain": "acme.com", "path": "/careers", "page_type": "careers",
               "url": "https://acme.com/company", "ok": True, "html": WEAK_HTML}
        self.assertTrue(run_mod._careers_fetch_is_wrong_page(res))

    def test_ats_landing_is_right_page(self):
        res = {"domain": "acme.com", "path": "/careers", "page_type": "careers",
               "url": "https://boards.greenhouse.io/acme", "ok": True, "html": WEAK_HTML}
        self.assertFalse(run_mod._careers_fetch_is_wrong_page(res))

    def test_strong_text_is_right_page(self):
        html = "<html><body>%s</body></html>" % STRONG_TEXT
        res = {"domain": "acme.com", "path": "/careers", "page_type": "careers",
               "url": "https://acme.com/careers", "ok": True, "html": html}
        self.assertFalse(run_mod._careers_fetch_is_wrong_page(res))

    def test_weak_text_same_url_is_wrong_page(self):
        res = {"domain": "acme.com", "path": "/careers", "page_type": "careers",
               "url": "https://acme.com/careers", "ok": True, "html": WEAK_HTML}
        self.assertTrue(run_mod._careers_fetch_is_wrong_page(res))



    def test_cross_domain_redirect_strong_text_still_wrong_page(self):
        # Acquirer/rebrand hop: othercorp.com/careers passes the path check and
        # its strong hiring text passes the strength check — only the
        # registrable-domain comparison (detect_acquirer_redirect) catches it.
        html = "<html><body>%s</body></html>" % STRONG_TEXT
        res = {"domain": "acme.com", "path": "/careers", "page_type": "careers",
               "url": "https://othercorp.com/careers", "ok": True, "html": html}
        self.assertTrue(run_mod._careers_fetch_is_wrong_page(res))

    def test_careers_subdomain_same_registrable_not_wrong(self):
        html = "<html><body>%s</body></html>" % STRONG_TEXT
        res = {"domain": "acme.com", "path": "/careers", "page_type": "careers",
               "url": "https://careers.acme.com/careers", "ok": True, "html": html}
        self.assertFalse(run_mod._careers_fetch_is_wrong_page(res))


class ReMineTests(unittest.TestCase):
    def test_wrong_page_re_mines_broader_careers_anchors(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        conn = cascade.connect(tmp.name)
        try:
            res = {"domain": "acme.com", "path": "/careers", "page_type": "careers",
                   "url": "https://acme.com/company", "ok": True, "html": WEAK_HTML}
            link_targets = {}
            found = run_mod.discover_child_career_targets(conn, res, link_targets)
            keys = [cascade.normalize_page_key(t.get("path") or t.get("url") or "") for t in found]
            self.assertIn("/company/careers", keys)  # the real anchor was recovered
            pg = cascade.get_discovered_page(conn, "acme.com", "/company/careers")
            self.assertEqual(pg["tier"], "candidate")  # recorded for the follow pass
        finally:
            conn.close()
            os.unlink(tmp.name)


class FollowPassSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.conn = cascade.connect(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def _seed(self, domain, path, page_type="careers", tier="candidate", ok=False,
              text="", url="", linked=False):
        cascade.upsert_discovered_page(
            self.conn, domain, path, page_type, url or "", 200 if ok else 0,
            tier, ok, text, linked_from_homepage=linked)

    def test_unresolved_detection(self):
        self._seed("weak.com", "/careers", tier="httpx", ok=True, text="join our team")
        self.assertTrue(run_mod.domain_is_unresolved(self.conn, "weak.com"))
        self._seed("strong.com", "/careers", tier="httpx", ok=True, text=STRONG_TEXT)
        self.assertFalse(run_mod.domain_is_unresolved(self.conn, "strong.com"))
        self._seed("ats.com", "https://jobs.lever.co/ats", tier="httpx", ok=True,
                   text="some board text", url="https://jobs.lever.co/ats")
        self.assertFalse(run_mod.domain_is_unresolved(self.conn, "ats.com"))

    def test_follow_targets_budget_and_candidates_only(self):
        for i in range(6):
            self._seed("un.com", f"/careers/team{i}")
        self._seed("un.com", "/careers/fetched", tier="httpx", ok=False)  # not a candidate
        out = run_mod.select_followup_careers_targets(self.conn, "un.com")
        self.assertEqual(len(out), run_mod.FOLLOWUP_CAREERS_BUDGET)
        self.assertTrue(all("/careers/team" in (t.get("path") or "") for t in out))

    def test_resolved_domain_gets_no_follow(self):
        self._seed("ok.com", "/careers", tier="httpx", ok=True, text=STRONG_TEXT)
        self._seed("ok.com", "/careers/more")
        self.assertEqual(run_mod.select_followup_careers_targets(self.conn, "ok.com"), [])

    def test_unlinked_candidates_qualify(self):
        # the whole point: re-mined anchors carry linked=0 and must still be followed
        self._seed("un2.com", "/company/careers", linked=False)
        out = run_mod.select_followup_careers_targets(self.conn, "un2.com")
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["linked_from_homepage"])


if __name__ == "__main__":
    unittest.main()
