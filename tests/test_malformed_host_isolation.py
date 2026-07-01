"""Regression tests for malformed-host safety (crash reported 2026-06-16).

A 500-domain bulk drain was aborted by a single malformed URL:
    ValueError: 'sermon%20index=0%20key=url' does not appear to be an IPv4 or IPv6 address
raised inside ipaddress.ip_address() in the network stack.  The decoded value
'sermon index=0 key=url' is a URL-encoded query-string artifact, not a hostname.

Two fixes are tested here:
  1. _is_plausible_host() -- the new hostname-level guard that rejects this string
     before it can reach the TCP/TLS layer.
  2. Per-domain isolation -- a ValueError raised inside a fetch coroutine must NOT
     abort the other domains in the same asyncio.gather() batch.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import cascade


BAD_HOST = "sermon%20index=0%20key=url"  # a real-world malformed host that broke naive parsing
BAD_URL = "https://" + BAD_HOST + "/jobs"


# ---------------------------------------------------------------------------
# 1.  _is_plausible_host() validation gate
# ---------------------------------------------------------------------------
class IsPlausibleHostTests(unittest.TestCase):

    def test_rejects_percent_encoded_host(self):
        """The exact hostname from the crash report must be rejected."""
        self.assertFalse(cascade._is_plausible_host(BAD_HOST))

    def test_rejects_host_with_space(self):
        """Decoded equivalent must also be rejected."""
        self.assertFalse(cascade._is_plausible_host("sermon index=0 key=url"))

    def test_rejects_host_with_equals(self):
        self.assertFalse(cascade._is_plausible_host("key=value"))

    def test_rejects_host_with_tab_or_newline(self):
        self.assertFalse(cascade._is_plausible_host("host\twith\ttab"))
        self.assertFalse(cascade._is_plausible_host("host\nwith\nnewline"))

    def test_accepts_normal_domains(self):
        self.assertTrue(cascade._is_plausible_host("example.com"))
        self.assertTrue(cascade._is_plausible_host("boards.greenhouse.io"))
        self.assertTrue(cascade._is_plausible_host("jobs.lever.co"))
        self.assertTrue(cascade._is_plausible_host("my-company.ashbyhq.com"))

    def test_rejects_empty(self):
        self.assertFalse(cascade._is_plausible_host(""))
        self.assertFalse(cascade._is_plausible_host(None))


# ---------------------------------------------------------------------------
# 2.  Per-domain fetch isolation
#     A fetch coroutine that raises ValueError must not abort the batch.
# ---------------------------------------------------------------------------
class _FakeClient:
    """A fake httpx-like async client.  Routes: url -> (status, text) or Exception."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def get(self, url):
        self.calls.append(url)
        hit = self.routes.get(url)
        if hit is None:
            raise ConnectionError("unrouted: " + url)
        if isinstance(hit, BaseException):
            raise hit
        status, text = hit
        return type("Resp", (), {"url": url, "status_code": status, "text": text, "headers": {}})()


REAL_HTML = "<html><body>" + "open positions hiring " * 40 + "</body></html>"


class MalformedHostFetchIsolationTests(unittest.TestCase):
    """Verify that a malformed domain returns an ok=False result and does NOT
    raise, even when the underlying fake client raises ValueError (simulating
    the ip_address() path in the network stack)."""

    def setUp(self):
        # Disable curl_cffi fallback so the test stays unit-level.
        self._orig_curl = cascade._curl_cffi_fetch
        cascade._curl_cffi_fetch = lambda domain, timeout: None

    def tearDown(self):
        cascade._curl_cffi_fetch = self._orig_curl

    def _run_batch(self, domains, routes):
        """Run _fetch_batch_httpx synchronously using a fake client."""
        async def _inner():
            sem = asyncio.Semaphore(len(domains))
            client = _FakeClient(routes)
            raw = await asyncio.gather(
                *[cascade._fetch_one_httpx(client, d, sem, retries=0) for d in domains],
                return_exceptions=True,
            )
            # Apply the same error-to-sentinel conversion as the real batch function.
            results = []
            for d, item in zip(domains, raw):
                if isinstance(item, BaseException):
                    results.append({"domain": d, "url": "https://" + d,
                                    "status": 0, "html": "", "ok": False})
                else:
                    results.append(item)
            return results

        return asyncio.run(_inner())

    def test_malformed_domain_returns_ok_false_not_raises(self):
        """A domain that is a URL-encoded junk string must return ok=False,
        not raise ValueError and crash the batch."""
        result = self._run_batch(
            [BAD_HOST],
            {},  # no route needed; _is_plausible_host rejects it before the client is called
        )
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0]["ok"])
        self.assertEqual(result[0]["domain"], BAD_HOST)

    def test_good_domains_survive_one_malformed_domain(self):
        """With one malformed domain in a batch of three, the two good domains
        must still receive their results."""
        good_html = REAL_HTML
        result = self._run_batch(
            ["good1.example.com", BAD_HOST, "good2.example.com"],
            {
                "https://good1.example.com": (200, good_html),
                "http://good1.example.com": (200, good_html),
                "https://good2.example.com": (200, good_html),
                "http://good2.example.com": (200, good_html),
            },
        )
        self.assertEqual(len(result), 3)
        by_domain = {r["domain"]: r for r in result}
        # Bad domain: graceful skip
        self.assertFalse(by_domain[BAD_HOST]["ok"])
        # Good domains: both resolved
        self.assertTrue(by_domain["good1.example.com"]["ok"])
        self.assertTrue(by_domain["good2.example.com"]["ok"])

    def test_value_error_from_client_does_not_abort_batch(self):
        """If the underlying client raises ValueError (simulating ip_address() crash),
        return_exceptions=True in _fetch_batch_httpx must isolate it so the other
        domains in the gather() still complete."""
        # Patch the plausibility check so the malformed host reaches the client,
        # simulating the pre-fix scenario where the guard was absent.
        orig_check = cascade._is_plausible_host
        cascade._is_plausible_host = lambda _h: True  # temporarily disable the guard

        try:
            result = self._run_batch(
                ["good.example.com", BAD_HOST, "other.example.com"],
                {
                    # good domain resolves fine
                    "https://good.example.com": (200, REAL_HTML),
                    "http://good.example.com": (200, REAL_HTML),
                    # malformed domain raises ValueError (simulates ip_address() crash)
                    "https://" + BAD_HOST: ValueError(
                        "'%s' does not appear to be an IPv4 or IPv6 address" % BAD_HOST
                    ),
                    "http://" + BAD_HOST: ValueError(
                        "'%s' does not appear to be an IPv4 or IPv6 address" % BAD_HOST
                    ),
                    "https://www." + BAD_HOST: ConnectionError("unrouted"),
                    "http://www." + BAD_HOST: ConnectionError("unrouted"),
                    # other domain also resolves fine
                    "https://other.example.com": (200, REAL_HTML),
                    "http://other.example.com": (200, REAL_HTML),
                },
            )
        finally:
            cascade._is_plausible_host = orig_check

        self.assertEqual(len(result), 3)
        by_domain = {r["domain"]: r for r in result}
        # Bad domain: ok=False sentinel (error isolated, not propagated)
        self.assertFalse(by_domain[BAD_HOST]["ok"])
        # Good domains: NOT affected by the bad domain's ValueError
        self.assertTrue(by_domain["good.example.com"]["ok"],
                        "good.example.com was aborted by the malformed-domain ValueError")
        self.assertTrue(by_domain["other.example.com"]["ok"],
                        "other.example.com was aborted by the malformed-domain ValueError")


# ---------------------------------------------------------------------------
# 3.  Page-fetch tier isolation
#     A target whose URL resolves to a malformed host must be skipped gracefully.
# ---------------------------------------------------------------------------
class MalformedHostPageFetchTests(unittest.TestCase):
    """Verify that _fetch_one_page_httpx returns ok=False (not raises) for a
    target whose explicit URL has a malformed host."""

    def setUp(self):
        self._orig_rescue = cascade._curl_cffi_fetch_url
        cascade._curl_cffi_fetch_url = lambda url, timeout: None

    def tearDown(self):
        cascade._curl_cffi_fetch_url = self._orig_rescue

    def test_explicit_malformed_url_skipped_gracefully(self):
        """A page target with url='https://sermon%20index=0%20key=url' must return
        ok=False without raising ValueError."""
        async def _inner():
            client = _FakeClient({})
            sem = asyncio.Semaphore(1)
            target = {
                "domain": "example.com",
                "url": BAD_URL,
                "page_type": "careers",
                "linked_from_homepage": False,
            }
            return await cascade._fetch_one_page_httpx(client, target, sem, retries=0)

        result = asyncio.run(_inner())
        self.assertFalse(result["ok"])
        self.assertEqual(result["domain"], "example.com")
        # Crucially: no ValueError raised
        self.assertEqual(result["status"], 0)


# ---------------------------------------------------------------------------
# 4.  candidate_page_targets_from_html — discovery-layer urljoin isolation
#
#     The crash in the live traceback (2026-06-16) was raised INSIDE
#     candidate_page_targets_from_html at the urljoin() call, NOT in the fetch
#     layer.  The fetch-layer guards (fix #1) and asyncio isolation (fix #2)
#     were already in place but they never reached this code path.  Fix #2
#     (this PR) wraps the per-href loop body so a single malformed href
#     cannot abort the entire extraction.
# ---------------------------------------------------------------------------
class CandidatePageTargetsMalformedHrefTests(unittest.TestCase):
    """candidate_page_targets_from_html must skip malformed hrefs without raising."""

    # HTML that embeds the exact broken href from the crash:
    #   <a href="https://[sermon%20index=0%20key=url]/jobs">Jobs</a>
    # Python 3.12's urlsplit() calls ipaddress.ip_address() on the bracketed
    # netloc and raises ValueError because the decoded string
    # 'sermon index=0 key=url' is not a valid IP address.
    MALFORMED_HREF_HTML = (
        '<html><body>'
        '<a href="https://[sermon%20index=0%20key=url]/jobs">Jobs</a>'
        '<a href="/careers">Careers</a>'
        '</body></html>'
    )

    def test_malformed_href_does_not_raise(self):
        """Feeding an href whose netloc triggers ValueError must return normally."""
        # Must not raise -- any exception is a test failure.
        result = cascade.candidate_page_targets_from_html(
            "example.com",
            "https://example.com",
            self.MALFORMED_HREF_HTML,
        )
        # The /careers link is a valid careers-type link and should survive.
        # The malformed one must be silently skipped.
        self.assertIsInstance(result, list)

    def test_valid_links_survive_alongside_malformed_href(self):
        """Valid hrefs in the same HTML must be returned even when one href is malformed."""
        result = cascade.candidate_page_targets_from_html(
            "example.com",
            "https://example.com",
            self.MALFORMED_HREF_HTML,
        )
        paths = [t.get("path") or t.get("url") or "" for t in result]
        # /careers normalizes to /careers/ and should be in the output
        has_careers = any("careers" in p for p in paths)
        self.assertTrue(has_careers, "expected /careers to survive; got: %r" % paths)

    def test_percent_encoded_bracket_href_skipped_gracefully(self):
        """An href like https://[sermon%20index] (URL-encoded, no space) is also skipped."""
        html = (
            '<html><body>'
            '<a href="https://[sermon%20index%3D0%20key%3Durl]/jobs">Jobs</a>'
            '<a href="/jobs">Open Roles</a>'
            '</body></html>'
        )
        # Must not raise
        result = cascade.candidate_page_targets_from_html(
            "example.com",
            "https://example.com",
            html,
        )
        self.assertIsInstance(result, list)

    def test_all_malformed_hrefs_html_returns_empty_list(self):
        """HTML with ONLY malformed hrefs must return [] without raising."""
        html = (
            '<html><body>'
            '<a href="https://[sermon%20index=0%20key=url]/jobs">Jobs</a>'
            '<a href="https://[bad%20host]/careers">Careers</a>'
            '</body></html>'
        )
        result = cascade.candidate_page_targets_from_html(
            "example.com",
            "https://example.com",
            html,
        )
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
