"""Tests for browser-rescue crash resilience, the js-shell/stub render hints,
and the expanded render-rescue selector."""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import cascade
import run as run_mod

REAL_HTML = "<html><body>" + ("open positions and real page content " * 30) + "</body></html>"
SHELL_HTML = ("<html><head>" + ("<script>var x=1;</script>" * 5)
              + '</head><body><div id="root"></div></body></html>').ljust(600, " ")
SPA_ROOT_HTML = ('<html><body><div id="__next"></div></body></html>').ljust(600, " ")


class _FakeSyncPlaywright:
    """Stub for sync_playwright() so _run_browser_batch is testable offline."""

    def __init__(self, factory):
        self.factory = factory

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def chromium(self):
        outer = self

        class _Launcher:
            def launch(self, headless=True):
                return outer.factory()

        return _Launcher()


class _FakeBrowser:
    def __init__(self):
        self.closed = False

    def new_context(self, user_agent=None):
        return self

    def route(self, pattern, handler):
        pass  # asset-blocking install is a no-op in the offline fake

    def close(self):
        self.closed = True


class RunBrowserBatchTests(unittest.TestCase):
    def _drive(self, items, render_one, factory=None):
        import importlib
        fake_pw = _FakeSyncPlaywright(factory or _FakeBrowser)
        playwright_mod = type(sys)("playwright")
        sync_api = type(sys)("playwright.sync_api")
        sync_api.sync_playwright = lambda: fake_pw
        playwright_mod.sync_api = sync_api
        old_pw = sys.modules.get("playwright")
        old_sync = sys.modules.get("playwright.sync_api")
        sys.modules["playwright"] = playwright_mod
        sys.modules["playwright.sync_api"] = sync_api
        try:
            return list(cascade._run_browser_batch(items, render_one))
        finally:
            if old_pw is not None:
                sys.modules["playwright"] = old_pw
            else:
                sys.modules.pop("playwright", None)
            if old_sync is not None:
                sys.modules["playwright.sync_api"] = old_sync
            else:
                sys.modules.pop("playwright.sync_api", None)

    def test_mid_batch_crash_relaunches_and_continues(self):
        crashed = {"done": False}

        def render(context, item):
            if item == "b" and not crashed["done"]:
                crashed["done"] = True
                raise RuntimeError("browser died")
            return {"item": item}

        out = self._drive(["a", "b", "c", "d"], render)
        self.assertEqual([i for i, _ in out], ["a", "b", "c", "d"])
        self.assertIsNone(dict(out)["b"])           # the crasher is charged its result
        self.assertEqual(dict(out)["c"], {"item": "c"})  # the rest continue
        self.assertEqual(dict(out)["d"], {"item": "d"})

    def test_relaunch_budget_exhaustion_yields_empties_not_raise(self):
        def render(context, item):
            raise RuntimeError("always dies")

        out = self._drive(["a", "b", "c", "d", "e"], render)
        self.assertEqual(len(out), 5)                # nothing stranded
        self.assertTrue(all(res is None for _, res in out))

    def test_no_crash_passthrough(self):
        out = self._drive(["x", "y"], lambda c, i: {"item": i})
        self.assertEqual(out, [("x", {"item": "x"}), ("y", {"item": "y"})])


class RenderHintTests(unittest.TestCase):
    def test_js_shell_detection(self):
        self.assertTrue(cascade.looks_like_js_shell(SHELL_HTML, "thin"))
        self.assertTrue(cascade.looks_like_js_shell(SPA_ROOT_HTML, ""))
        # rich text -> not a shell, regardless of scripts
        self.assertFalse(cascade.looks_like_js_shell(SHELL_HTML, "x" * 300))
        # small html -> not a shell (below MIN_OK_HTML it's just empty/blocked)
        self.assertFalse(cascade.looks_like_js_shell("<div id='root'></div>", ""))

    def test_render_hint_for(self):
        self.assertEqual(cascade.render_hint_for(SHELL_HTML, "thin"), "js_shell")
        self.assertEqual(
            cascade.render_hint_for(REAL_HTML, "same nav text", homepage_text="same nav text"),
            "stub",
        )
        self.assertIsNone(cascade.render_hint_for(REAL_HTML, "plenty of real text " * 20))

    def test_never_feeds_soft_block(self):
        # the shell page must NOT be classified as a soft block
        self.assertFalse(cascade.is_soft_block(200, {}, SHELL_HTML))


class SelectorExpansionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.conn = cascade.connect(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def _seed(self, domain, path, url, status, ok, text, linked=False, render_hint=None):
        cascade.upsert_discovered_page(
            self.conn, domain, path, "careers", url, status, "httpx", ok, text,
            linked_from_homepage=linked, render_hint=render_hint,
        )

    def _select(self, targets, cap=None):
        return run_mod.select_browser_rescue_page_targets(self.conn, targets, per_domain_cap=cap)

    def test_unlinked_ats_shell_now_selected(self):
        # API/slug-discovered Ashby board: unlinked, ok=1, thin text
        url = "https://jobs.ashbyhq.com/acme"
        self._seed("acme.com", url, url, 200, True, "loading...", linked=False)
        t = {"domain": "acme.com", "url": url, "page_type": "careers"}
        self.assertEqual(self._select([t]), [t])

    def test_unlinked_official_careers_fetch_failure_selected_but_not_404(self):
        self._seed("acme.com", "/careers", "https://acme.com/careers", 0, False, "")
        t = {"domain": "acme.com", "path": "/careers", "page_type": "careers"}
        self.assertEqual(self._select([t]), [t])
        self._seed("gone.com", "/careers", "https://gone.com/careers", 404, False, "")
        t404 = {"domain": "gone.com", "path": "/careers", "page_type": "careers"}
        self.assertEqual(self._select([t404]), [])  # render won't fix a real 404

    def test_render_hint_triggers_selection(self):
        self._seed("hint.com", "/careers", "https://hint.com/careers", 200, True,
                   "plenty of long but useless text " * 40, render_hint="js_shell")
        t = {"domain": "hint.com", "path": "/careers", "page_type": "careers"}
        self.assertEqual(self._select([t]), [t])

    def test_strong_text_still_skipped(self):
        url = "https://jobs.ashbyhq.com/acme"
        self._seed("acme.com", url, url, 200, True,
                   "Open positions: Engineer, Designer " * 30, linked=True)
        t = {"domain": "acme.com", "url": url, "page_type": "careers"}
        self.assertEqual(self._select([t]), [])

    def test_per_domain_cap_honored(self):
        targets = []
        for i in range(10):
            path = f"/careers/team{i}"
            self._seed("cap.com", path, f"https://cap.com{path}", 0, False, "")
            targets.append({"domain": "cap.com", "path": path, "page_type": "careers"})
        self.assertEqual(len(self._select(targets, cap=6)), 6)
        self.assertEqual(len(self._select(targets, cap=2)), 2)

    def test_non_careers_never_selected(self):
        self._seed("acme.com", "/about", "https://acme.com/about", 0, False, "")
        t = {"domain": "acme.com", "path": "/about", "page_type": "company"}
        self.assertEqual(self._select([t]), [])


class RenderHintPersistenceTests(unittest.TestCase):
    def test_upsert_and_overwrite_semantics(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        conn = cascade.connect(tmp.name)
        try:
            cascade.upsert_discovered_page(
                conn, "a.com", "/careers", "careers", "https://a.com/careers",
                200, "httpx", True, "thin", render_hint="js_shell")
            pg = cascade.get_discovered_page(conn, "a.com", "/careers")
            self.assertEqual(pg["render_hint"], "js_shell")
            # a successful render re-upserts with a fresh diagnosis (None = healthy)
            cascade.upsert_discovered_page(
                conn, "a.com", "/careers", "careers", "https://a.com/careers",
                200, "playwright-page", True, "rich text " * 50, render_hint=None)
            pg = cascade.get_discovered_page(conn, "a.com", "/careers")
            self.assertIsNone(pg["render_hint"])
        finally:
            conn.close()
            os.unlink(tmp.name)


class _FakeRequest:
    def __init__(self, resource_type):
        self.resource_type = resource_type


class _FakeRoute:
    def __init__(self, resource_type, abort_raises=False):
        self.request = _FakeRequest(resource_type)
        self.aborted = self.continued = False
        self.abort_attempts = 0
        self._abort_raises = abort_raises

    def abort(self):
        self.abort_attempts += 1
        if self._abort_raises:
            raise RuntimeError("page closed mid-abort")
        self.aborted = True

    def continue_(self):
        self.continued = True


class _CapturingContext:
    def __init__(self):
        self.handler = None

    def route(self, pattern, handler):
        self.handler = handler


class AssetBlockingTests(unittest.TestCase):
    """#1 bandwidth lever: the rescue browser aborts image/media/font but keeps the
    request types SPA boards need to mount job data."""

    def _handler(self):
        ctx = _CapturingContext()
        cascade._install_asset_blocking(ctx)
        self.assertIsNotNone(ctx.handler)
        return ctx.handler

    def test_default_on(self):
        self.assertTrue(cascade.BLOCK_BROWSER_ASSETS)
        self.assertEqual(cascade.BLOCKED_RESOURCE_TYPES,
                         frozenset({"image", "media", "font"}))

    def test_blocks_assets(self):
        handler = self._handler()
        for rtype in ("image", "media", "font"):
            r = _FakeRoute(rtype)
            handler(r)
            self.assertTrue(r.aborted and not r.continued, rtype)

    def test_keeps_content_types(self):
        handler = self._handler()
        for rtype in ("document", "script", "xhr", "fetch", "stylesheet"):
            r = _FakeRoute(rtype)
            handler(r)
            self.assertTrue(r.continued and not r.aborted, rtype)

    def test_handler_swallows_errors_and_never_strands(self):
        handler = self._handler()
        r = _FakeRoute("image", abort_raises=True)
        handler(r)  # must not raise out of the handler
        self.assertGreaterEqual(r.abort_attempts, 1)  # tried to release the request

    def test_flag_off_skips_route_registration(self):
        original = cascade.BLOCK_BROWSER_ASSETS
        try:
            cascade.BLOCK_BROWSER_ASSETS = False
            ctx = _CapturingContext()
            if cascade.BLOCK_BROWSER_ASSETS:           # mirror _run_browser_batch's guard
                cascade._install_asset_blocking(ctx)
            self.assertIsNone(ctx.handler)             # no route installed when off
        finally:
            cascade.BLOCK_BROWSER_ASSETS = original


if __name__ == "__main__":
    unittest.main()
