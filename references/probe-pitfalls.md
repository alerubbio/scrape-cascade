# Scrape-probe pitfalls (read before pointing this at a new list)

Hard-won lessons from prior large-scale scraping work. The cascade handles some of
these; the rest are on you when you write a rubric or extend a tier.

- **Catch-all / soft 200s.** Many hosts return HTTP 200 with a parking page, a
  "site for sale", or a generic CDN block. A status check is not enough. The
  cascade's `MIN_OK_HTML` floor catches the empty ones; for parked pages, lean on
  the judge (it returns `unknown`).
- **Match structure, not body text, where you can.** Body-text keyword matching is
  the cheap first pass and it over- and under-fires. Treat the keyword verdict as a
  filter, not the truth; the judge is the arbiter for ambiguous pages.
- **Multi-pattern URLs.** A signal may live at `/security`, `/trust`, `/careers`,
  `/jobs`, or a subdomain, not the homepage. The homepage rubric will miss it.
  Vendor/technographic detection needs a small per-domain crawl of those paths --
  that is a deliberate extension, not the default.
- **SPA / JS-only content.** httpx sees an empty shell for client-rendered sites.
  That is exactly what Tier 2 (Playwright) rescues. If Tier 2 still returns a shell,
  the content is behind interaction (login, click) -- out of scope for bulk classify.
- **Single-IP anti-bot reality.** With no managed proxy-rotation service, scraping
  from one IP will draw rate-limits and blocks at scale. The mitigations: the
  tiered-fingerprint cascade (httpx -> Jina Reader -> Playwright -> Camoufox, each a
  different signature), polite concurrency, backoff, and optional env-gated proxy
  routing. It is the $0 trade-off -- expect a higher drop rate than a paid proxy.
- **401 / 403 walls.** Some hosts hard-block non-browser fingerprints. Tier 3
  (Camoufox, a hardened Firefox that clears Cloudflare) is the last resort; if it
  also fails, record the domain as `unreachable` rather than guessing.
- **Cache the superset, slice per rubric.** Page text is cached rubric-agnostically
  in SQLite. Re-running a *different* rubric over the same list reuses fetched text
  and only re-runs scoring + judge -- no re-scrape. Use `--refetch` to force.
- **CSV is the answer; SQLite/JSONL is the state.** The results CSV stays identity +
  verdict only. Full reasons, hits, and tier live in the JSONL and the DB so the CSV
  does not rot.
