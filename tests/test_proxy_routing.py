"""Offline tests for the proxy-pool routing lever (the IP-reputation hooks across the
httpx / Playwright / camoufox fetch tiers). No network -- pure config-plumbing checks.

The lever is the IP-reputation play: route scrape traffic off the host's own IP. These
tests pin the env contract, the URL parsing, fail-closed parsing, and that the lever is
fully inert when no proxy env is set."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import cascade

PROXY_ENVS = (
    "SCRAPE_CASCADE_PROXY_URL", "SCRAPE_PROXY_URL",
    "SCRAPE_CASCADE_PROXY_URL_STEALTH", "SCRAPE_PROXY_URL_STEALTH",
)


class ProxyRoutingTest(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in PROXY_ENVS}

    def tearDown(self):
        for k in PROXY_ENVS:
            os.environ.pop(k, None)
            if self._saved.get(k) is not None:
                os.environ[k] = self._saved[k]

    # --- _proxy_url: env contract -----------------------------------------
    def test_unset_is_none(self):
        self.assertIsNone(cascade._proxy_url())
        self.assertIsNone(cascade._proxy_url(stealth=True))

    def test_bulk_url_and_stealth_fallback(self):
        os.environ["SCRAPE_CASCADE_PROXY_URL"] = "http://u:p@gw:12321"
        self.assertEqual(cascade._proxy_url(), "http://u:p@gw:12321")
        # stealth (Tier 3) falls back to the bulk gateway when no stealth-specific var
        self.assertEqual(cascade._proxy_url(stealth=True), "http://u:p@gw:12321")

    def test_short_alias(self):
        os.environ["SCRAPE_PROXY_URL"] = "http://gw:8000"
        self.assertEqual(cascade._proxy_url(), "http://gw:8000")

    def test_stealth_override_distinct_from_bulk(self):
        os.environ["SCRAPE_CASCADE_PROXY_URL"] = "http://datacenter:1"
        os.environ["SCRAPE_CASCADE_PROXY_URL_STEALTH"] = "http://residential:2"
        self.assertEqual(cascade._proxy_url(), "http://datacenter:1")
        self.assertEqual(cascade._proxy_url(stealth=True), "http://residential:2")

    def test_whitespace_only_is_none(self):
        os.environ["SCRAPE_CASCADE_PROXY_URL"] = "   "
        self.assertIsNone(cascade._proxy_url())

    # --- _proxy_playwright: URL -> launch dict ----------------------------
    def test_pw_none_passthrough(self):
        self.assertIsNone(cascade._proxy_playwright(None))
        self.assertIsNone(cascade._proxy_playwright(""))

    def test_pw_with_creds_and_port(self):
        got = cascade._proxy_playwright("http://user:pa%20ss@host:12321")
        self.assertEqual(got["server"], "http://host:12321")
        self.assertEqual(got["username"], "user")
        self.assertEqual(got["password"], "pa ss")  # %20 url-decoded

    def test_pw_no_creds_socks(self):
        self.assertEqual(
            cascade._proxy_playwright("socks5://host:1080"),
            {"server": "socks5://host:1080"},
        )

    def test_pw_malformed_raises(self):
        # fail-closed: a bad proxy URL must raise, never silently launch un-proxied
        with self.assertRaises(ValueError):
            cascade._proxy_playwright("notaurl")

    # --- _make_client: inert when unset, proxy applied when set -----------
    def test_make_client_inert(self):
        c = cascade._make_client(10.0, 5)
        self.assertTrue(hasattr(c, "aclose"))  # an httpx.AsyncClient, no proxy

    def test_make_client_with_proxy_constructs(self):
        os.environ["SCRAPE_CASCADE_PROXY_URL"] = "http://u:p@gw:12321"
        c = cascade._make_client(10.0, 5)  # constructs lazily, no network
        self.assertTrue(hasattr(c, "aclose"))

    # --- camoufox child carries the proxy plumbing ------------------------
    def test_camoufox_child_has_proxy_and_compiles(self):
        src = cascade._CAMOUFOX_CHILD_SRC
        self.assertIn("SCRAPE_CASCADE_PROXY_URL", src)
        self.assertIn("proxy", src)
        compile(src, "<camoufox_child>", "exec")  # the child source is valid python


if __name__ == "__main__":
    unittest.main()
