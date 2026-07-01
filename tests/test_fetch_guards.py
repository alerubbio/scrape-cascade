"""Tests for fetch-tier guards: soft-block detection (and splash-harvest, added later)."""
import asyncio
import os
import socket
import ssl
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import cascade


class _Resp:
    def __init__(self, url, status=200, text="", headers=None):
        self.url, self.status_code, self.text = url, status, text
        self.headers = headers or {}


class _FakeClient:
    """routes: url -> (status, text) | Exception instance (raised). Unrouted
    urls raise ConnectionError (the status-0 class)."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    async def get(self, url):
        self.calls.append(url)
        hit = self.routes.get(url)
        if hit is None:
            raise ConnectionError("unrouted")
        if isinstance(hit, Exception):
            raise hit
        status, text = hit
        return _Resp(url, status, text)


REAL_HTML = "<html><body>" + ("open positions and real page content " * 30) + "</body></html>"
STUB_HTML = ("<html><head>" + ("<script>var x=1;</script>" * 40)
             + '</head><body><div id="root"></div></body></html>')


class SoftBlockTests(unittest.TestCase):
    def test_cf_mitigated_header(self):
        self.assertTrue(cascade.is_soft_block(200, {"cf-mitigated": "challenge"}, "<html>x</html>"))

    def test_challenge_titles(self):
        self.assertTrue(cascade.is_soft_block(200, {}, "<html><head><title>Just a moment...</title></head></html>"))
        self.assertTrue(cascade.is_soft_block(403, {}, "<title>Access to this page has been denied</title>"))
        self.assertTrue(cascade.is_soft_block(200, {}, "<title>Are you a human</title>"))

    def test_vendor_markers(self):
        self.assertTrue(cascade.is_soft_block(200, {}, "<script>window._cf_chl_opt={x:1}</script>"))
        self.assertTrue(cascade.is_soft_block(200, {}, "<script src='https://ct.captcha-delivery.com/c.js'></script>"))

    def test_clean_pages_not_blocked(self):
        self.assertFalse(cascade.is_soft_block(
            200, {}, "<html><head><title>Careers at Acme</title></head><body>Open positions (5)</body></html>"))
        self.assertFalse(cascade.is_soft_block(200, {}, ""))
        self.assertFalse(cascade.is_soft_block(200, None, "small but real content"))

    def test_attention_required_requires_cloudflare_bang(self):
        self.assertTrue(cascade.is_soft_block(403, {}, "<title>Attention Required! | Cloudflare</title>"))
        # a legit Zendesk/Jira-style "Attention required" page (no '!') must NOT be flagged
        self.assertFalse(cascade.is_soft_block(200, {}, "<title>Attention required</title>"))


class SplashHarvestTests(unittest.TestCase):
    def test_harvests_outbound_entity_from_text_mention(self):
        html = ('<html><body>Pick one. <a href="#">Enterprise</a>'
                ' ... we are now tandems.ai ...'
                ' <a href="https://twitter.com/x">tw</a>'
                ' <script src="https://cdn.googleapis.com/a.js"></script></body></html>')
        out = cascade.outbound_entity_domains_from_html("sigfig.com", "https://sigfig.com", html)
        self.assertIn("tandems.ai", out)        # bare-text rebrand target captured
        self.assertNotIn("sigfig.com", out)     # self excluded
        self.assertNotIn("twitter.com", out)    # social excluded
        self.assertNotIn("cdn.googleapis.com", out)  # infra/CDN excluded

    def test_thin_splash_trigger_only_when_no_careers(self):
        self.assertTrue(cascade.looks_like_thin_splash("<html><a >x</a></html>", []))
        # has careers candidates -> not a splash to chase
        self.assertFalse(cascade.looks_like_thin_splash("<html><a >x</a></html>", [{"page_type": "careers"}]))
        # content-rich page (>5 anchors) -> not a thin splash
        self.assertFalse(cascade.looks_like_thin_splash("<a ><a ><a ><a ><a ><a ><a >", []))

    def test_excludes_saas_vendor_mentions(self):
        html = "we use stripe.com and twilio.com; our new home is acmecorp.ai"
        out = cascade.outbound_entity_domains_from_html("oldco.com", "https://oldco.com", html)
        self.assertIn("acmecorp.ai", out)
        self.assertNotIn("stripe.com", out)
        self.assertNotIn("twilio.com", out)


class EmbeddedAtsWidgetTests(unittest.TestCase):
    def test_widget_js_resolves_to_board_only(self):
        gh = '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>'
        urls = [u for u, _ in cascade._embedded_ats_links_from_html(gh)]
        self.assertIn("https://job-boards.greenhouse.io/embed/job_board?for=acme", urls)
        self.assertFalse(any("/embed/job_board/js" in u for u in urls))  # widget JS not emitted
        bh = '<script src="https://acme.bamboohr.com/js/embed.js"></script>'
        urls = [u for u, _ in cascade._embedded_ats_links_from_html(bh)]
        self.assertIn("https://acme.bamboohr.com/careers", urls)
        self.assertFalse(any(u.endswith("/js/embed.js") for u in urls))


class BrowserFallbackGateTests(unittest.TestCase):
    def test_only_escalates_block_like_failures(self):
        self.assertTrue(cascade._warrants_browser_fallback(0, ""))      # connection/TLS error
        self.assertTrue(cascade._warrants_browser_fallback(403, ""))    # blocked
        self.assertTrue(cascade._warrants_browser_fallback(429, ""))
        self.assertTrue(cascade._warrants_browser_fallback(200, "<title>Just a moment...</title>"))  # soft-block
        self.assertFalse(cascade._warrants_browser_fallback(404, ""))   # genuinely absent
        self.assertFalse(cascade._warrants_browser_fallback(410, ""))
        self.assertFalse(cascade._warrants_browser_fallback(200, "<html>real thin page</html>"))


class DomainUrlCandidateTests(unittest.TestCase):
    def test_matrix_order_apex_first_then_www(self):
        self.assertEqual(
            cascade._domain_url_candidates("acme.com"),
            ["https://acme.com", "http://acme.com",
             "https://www.acme.com", "http://www.acme.com"],
        )

    def test_no_double_www(self):
        self.assertEqual(
            cascade._domain_url_candidates("www.acme.com"),
            ["https://www.acme.com", "http://www.acme.com"],
        )

    def test_extracted_text_len_strips_script_and_tags(self):
        self.assertEqual(cascade._extracted_text_len("<script>var x=1;</script>"), 0)
        self.assertGreater(cascade._extracted_text_len(REAL_HTML), 500)
        self.assertLess(cascade._extracted_text_len(STUB_HTML), cascade.STUB_TEXT_CHARS)


class HomepageWwwGatingTests(unittest.TestCase):
    def _fetch(self, client):
        sem = asyncio.Semaphore(1)
        return asyncio.run(cascade._fetch_one_httpx(client, "acme.com", sem, retries=0))

    def setUp(self):
        self._orig = cascade._curl_cffi_fetch
        cascade._curl_cffi_fetch = lambda domain, timeout: None  # isolate the httpx matrix

    def tearDown(self):
        cascade._curl_cffi_fetch = self._orig

    def test_settled_apex_skips_www(self):
        client = _FakeClient({
            "https://acme.com": (404, ""),
            "http://acme.com": (404, ""),
            "https://www.acme.com": (200, REAL_HTML),  # must never be reached
        })
        res = self._fetch(client)
        self.assertEqual(res["status"], 404)
        self.assertNotIn("https://www.acme.com", client.calls)

    def test_dead_apex_falls_through_to_www(self):
        client = _FakeClient({
            "https://www.acme.com": (200, REAL_HTML),  # apex urls unrouted -> ConnectionError
        })
        res = self._fetch(client)
        self.assertTrue(res["ok"])
        self.assertEqual(res["url"], "https://www.acme.com")


class PageFallbackTests(unittest.TestCase):
    def setUp(self):
        self.rescue_calls = []
        self._orig = cascade._curl_cffi_fetch_url
        self.rescue_result = None

        def fake_rescue(url, timeout):
            self.rescue_calls.append(url)
            return self.rescue_result

        cascade._curl_cffi_fetch_url = fake_rescue

    def tearDown(self):
        cascade._curl_cffi_fetch_url = self._orig

    def _fetch(self, routes, target):
        client = _FakeClient(routes)
        sem = asyncio.Semaphore(1)
        res = asyncio.run(cascade._fetch_one_page_httpx(client, target, sem, retries=0))
        return res, client

    def test_fires_on_blocked_page_and_rescued_wins(self):
        self.rescue_result = {"url": "https://acme.com/careers", "status": 200,
                              "html": REAL_HTML, "ok": True}
        res, _ = self._fetch(
            {"https://acme.com/careers": (403, "")},
            {"domain": "acme.com", "path": "/careers", "page_type": "careers"},
        )
        self.assertEqual(self.rescue_calls, ["https://acme.com/careers"])
        self.assertTrue(res["ok"])
        self.assertEqual(res["html"], REAL_HTML)

    def test_does_not_fire_on_404(self):
        res, _ = self._fetch(
            {"https://acme.com/careers": (404, ""), "http://acme.com/careers": (404, "")},
            {"domain": "acme.com", "path": "/careers", "page_type": "careers"},
        )
        self.assertEqual(self.rescue_calls, [])
        self.assertFalse(res["ok"])

    def test_fires_on_careers_stub_200(self):
        self.rescue_result = {"url": "https://acme.com/careers", "status": 200,
                              "html": REAL_HTML, "ok": True}
        res, _ = self._fetch(
            {"https://acme.com/careers": (200, STUB_HTML)},
            {"domain": "acme.com", "path": "/careers", "page_type": "careers"},
        )
        self.assertEqual(len(self.rescue_calls), 1)
        self.assertEqual(res["html"], REAL_HTML)  # richer rescued result wins

    def test_stub_keeps_original_when_rescue_not_richer(self):
        self.rescue_result = None  # rescue failed -> keep the stub 200
        res, _ = self._fetch(
            {"https://acme.com/careers": (200, STUB_HTML)},
            {"domain": "acme.com", "path": "/careers", "page_type": "careers"},
        )
        self.assertEqual(len(self.rescue_calls), 1)
        self.assertTrue(res["ok"])
        self.assertEqual(res["html"], STUB_HTML)

    def test_no_stub_retry_for_non_careers(self):
        res, _ = self._fetch(
            {"https://acme.com/about": (200, STUB_HTML)},
            {"domain": "acme.com", "path": "/about", "page_type": "company"},
        )
        self.assertEqual(self.rescue_calls, [])
        self.assertTrue(res["ok"])

    def test_explicit_url_no_www_matrix(self):
        res, client = self._fetch(
            {"https://boards.greenhouse.io/acme": (404, "")},
            {"domain": "acme.com", "url": "https://boards.greenhouse.io/acme",
             "page_type": "careers"},
        )
        self.assertEqual(client.calls, ["https://boards.greenhouse.io/acme"])

    def test_path_target_www_fallthrough_on_dead_apex(self):
        self.rescue_result = None
        res, client = self._fetch(
            {"https://www.acme.com/careers": (200, REAL_HTML)},  # apex unrouted -> raises
            {"domain": "acme.com", "path": "/careers", "page_type": "careers"},
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["url"], "https://www.acme.com/careers")


class DnsFailureDetectionTests(unittest.TestCase):
    """#4B: distinguish a DNS-resolution miss (curl can't fix) from a TLS/conn error."""

    def test_gaierror_is_dns(self):
        self.assertTrue(cascade._is_dns_failure(socket.gaierror(8, "nodename nor servname provided")))

    def test_message_match_is_dns(self):
        self.assertTrue(cascade._is_dns_failure(Exception("getaddrinfo failed")))

    def test_wrapped_cause_is_dns(self):
        e = ConnectionError("connect failed")
        e.__cause__ = socket.gaierror(8, "Name or service not known")
        self.assertTrue(cascade._is_dns_failure(e))

    def test_non_dns_errors_are_not_dns(self):
        self.assertFalse(cascade._is_dns_failure(ConnectionError("connection reset by peer")))
        self.assertFalse(cascade._is_dns_failure(Exception("SSL: TLSV1_ALERT handshake failure")))


class DnsShortCircuitTests(unittest.TestCase):
    """#4B: a domain that fails purely at DNS must NOT trigger the curl_cffi rescue
    (curl shares the resolver); a non-DNS failure still escalates (TLS rescue case)."""

    def setUp(self):
        self.curl_calls = []
        self._orig = cascade._curl_cffi_fetch

        def fake_curl(domain, timeout):
            self.curl_calls.append(domain)
            return None

        cascade._curl_cffi_fetch = fake_curl

    def tearDown(self):
        cascade._curl_cffi_fetch = self._orig

    def _fetch(self, routes):
        sem = asyncio.Semaphore(1)
        return asyncio.run(cascade._fetch_one_httpx(_FakeClient(routes), "dead.com", sem, retries=0))

    def test_all_dns_failure_skips_curl(self):
        gai = socket.gaierror(8, "nodename nor servname provided, or not known")
        res = self._fetch({u: gai for u in cascade._domain_url_candidates("dead.com")})
        self.assertEqual(self.curl_calls, [])     # pure DNS death -> no curl rescue
        self.assertEqual(res["status"], 0)

    def test_non_dns_failure_still_escalates(self):
        res = self._fetch({})  # unrouted -> ConnectionError('unrouted'), a non-DNS error
        self.assertEqual(self.curl_calls, ["dead.com"])  # curl still tried (could be TLS)

    def test_real_tls_error_still_escalates(self):
        err = ssl.SSLError("TLSV1_ALERT_INTERNAL_ERROR handshake failure")
        self._fetch({u: err for u in cascade._domain_url_candidates("dead.com")})
        self.assertEqual(self.curl_calls, ["dead.com"])  # genuine TLS failure -> curl rescue

    def test_mixed_tls_apex_dns_www_still_escalates(self):
        # apex fails TLS (curl might fix), www fails DNS -> non_dns_failure sticks -> escalate
        cands = cascade._domain_url_candidates("dead.com")
        gai = socket.gaierror(8, "nodename nor servname provided")
        tls = ssl.SSLError("TLSV1_ALERT handshake failure")
        self._fetch({cands[0]: tls, cands[1]: tls, cands[2]: gai, cands[3]: gai})
        self.assertEqual(self.curl_calls, ["dead.com"])


if __name__ == "__main__":
    unittest.main()
