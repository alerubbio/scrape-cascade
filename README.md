# scrape-cascade

[![tests](https://github.com/alerubbio/scrape-cascade/actions/workflows/tests.yml/badge.svg)](https://github.com/alerubbio/scrape-cascade/actions/workflows/tests.yml)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Classify a large list of websites against a rubric — for $0, with no paid scraping API.**

`scrape-cascade` fetches and classifies many domains through a **free-first, tiered**
pipeline. Each step only sees what the cheaper step below it couldn't answer, so most
domains resolve in seconds on the cheapest tier and only the genuinely ambiguous few ever
reach a browser or an LLM.

The question you're answering is **config, not code** — a small YAML rubric. Point the same
engine at "is this B2B SaaS?", "is this company hiring?", "is this an online store?", or
"is this domain parked/dead?" without touching Python.

| Tier | Tool | Sees | Cost |
|------|------|------|------|
| **1 — fast pass** | `httpx` (async, ~50 at once, retry+backoff) + `curl_cffi` TLS-fingerprint fallback + `html2text` + keyword rubric | every domain | free |
| **1.5 — Jina** (`--jina`) | Jina Reader `r.jina.ai` — one keyless GET, renders JS server-side, no local browser | Tier-1 empties/blocks | free |
| **2 — rescue** (`--rescue`) | `playwright` (one reused Chromium) | residue after Tier 1/1.5 | free |
| **3 — stealth** (`--stealth`) | `camoufox` (its own hardened Firefox → clears Cloudflare; MDM-safe) | residue after Tier 2 | free |
| **judge** | `codex exec` or `claude -p` with JSON-schema output | only the genuinely ambiguous | your local LLM-CLI plan |

**The rule: free first, browser second, AI last — never the reverse.** A browser only starts
for what's still empty; the LLM judge only reads the residue keywords couldn't call.

---

## Why it exists

Managed scraping APIs (Firecrawl and friends) are excellent but metered. When you need to
classify tens of thousands of domains and the job must cost **$0 at the margin**, this is the
self-hosted trade-off: you give up managed proxy rotation and pay for it with a tiered
fingerprint cascade, polite concurrency, and backoff. It's built to survive a bulk run —
incremental persistence, automatic resume, and a pre-flight `--doctor` check.

- **Config-driven** — use cases are YAML rubrics, not forks of the code.
- **Crash-resilient** — SQLite (WAL) is the source of truth, written per chunk. A killed run
  resumes exactly where it stopped and never re-judges what it already decided.
- **Cache the superset** — fetched page text is stored rubric-agnostically, so re-running a
  *different* rubric over the same list reuses the text and only re-scores. **One fetch, many
  classifications.**
- **Bring your own local LLM CLI** — the judge shells out to `codex` or `claude`; there's no
  paid SDK call in the hot path.
- **Fail-closed proxy routing** — optional, env-gated; if a proxy is set but can't be wired,
  the run raises rather than silently leaking your real IP.

## Use cases (the shipped rubrics)

The same engine, four different jobs — each is one YAML file in [`rubrics/`](rubrics/):

| Rubric | Question |
|--------|----------|
| [`b2b_saas.yaml`](rubrics/b2b_saas.yaml) | Is this a B2B SaaS company (vs a services shop, consumer app, or dead page)? |
| [`hiring_signal.yaml`](rubrics/hiring_signal.yaml) | Is this company actively hiring / growing (open roles, an ATS board, recent funding)? |
| [`ecommerce_store.yaml`](rubrics/ecommerce_store.yaml) | Is this primarily an online store? |
| [`dead_or_parked_domain.yaml`](rubrics/dead_or_parked_domain.yaml) | Is this domain parked / expired / a placeholder, or a live site? |

Write your own by copying one and editing the keyword lists — see
[Writing a rubric](#writing-a-rubric).

## Setup

Requires **Python 3.10+** (3.12 recommended). `curl_cffi` (the TLS-fingerprint fallback)
installs automatically.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .            # Tier 1 + judge — the minimal free core
# optional heavier tiers:
pip install -e ".[browser]" # Tier 2/3 browsers
playwright install chromium # Tier 2 Chromium (only for --rescue)
python -m camoufox fetch    # Tier 3 stealth Firefox (only for --stealth)
```

Then confirm which tiers are actually live on this machine — it catches a browser that
won't launch **before** a bulk run quietly depends on it:

```bash
scrape-cascade --doctor
```

## Run

```bash
# Tier 1 + judge only (no browser) — the cheapest pass:
scrape-cascade --rubric rubrics/b2b_saas.yaml --input sample_domains.txt

# At scale, turn on the free escalation tiers. Each only touches the residue the
# cheaper tiers left empty:
scrape-cascade --rubric rubrics/b2b_saas.yaml --input domains.txt \
  --jina --rescue --stealth --concurrency 50

# Same list, different question — reuses cached page text, no re-scrape:
scrape-cascade --rubric rubrics/ecommerce_store.yaml --input domains.txt

# Recover the CSV/JSONL from the DB after a crash (no fetching, no judging):
scrape-cascade --rubric rubrics/b2b_saas.yaml --input domains.txt --export-only
```

(You can also run it without installing: `python scripts/run.py --rubric ...`.)

**Resume is automatic.** Re-running the same rubric skips every domain already decided on
real content and re-evaluates only the `fetch_failed` ones (a later `--rescue` may have
filled them). Force a clean redo with `--rejudge` or `--refetch`.

Key flags: `--rescue` (Tier 2), `--stealth` (Tier 3), `--jina` (Tier 1.5), `--no-judge`
(skip the LLM, leave ambiguous), `--limit N`, `--concurrency` / `--timeout`,
`--judge-provider auto|codex|claude`, `--judge-concurrency`, `--domain-column`,
`--rejudge`, `--refetch`, `--export-only`, `--db` / `--output`.

## Writing a rubric

Rubrics are config, not code — copy one in [`rubrics/`](rubrics/) and edit. The keyword
lists only need to catch the *obvious* cases cheaply; the judge handles the rest.

```yaml
name: my_rubric
description: <what we're deciding — also fed to the judge>
positive_label: yes_thing
negative_label: not_thing
confident_threshold: 0.6        # keyword confidence below this -> escalate to judge
min_net_hits: 2                 # also need >=2 net hits; thin evidence -> judge
positive: [ ... terms that signal the positive class ... ]
negative: [ ... terms that signal the negative class ... ]
judge_instructions: <extra guidance for the LLM judge on edge cases>
```

> **Why `min_net_hits`:** a single keyword hit can otherwise produce confidence 1.0 and skip
> the judge — enough to confidently mislabel, say, a news site as SaaS. Thin evidence
> escalates to the judge instead.

## Outputs

- `data/results.csv` — the answer: `domain,label,confidence,method`.
- `data/results.jsonl` — full per-domain state incl. judge reasons and keyword hits.
- `data/cache.db` — SQLite (WAL), the **incrementally-written source of truth**. `pages` is
  rubric-agnostic homepage text; `verdicts` is per-rubric. The CSV/JSONL are a projection of
  this DB — a crash loses at most the in-flight chunk, and `--export-only` regenerates them.

`method` tells you which tier decided each row: `keyword`, `llm`, `fetch_failed`, or `none`.

## The judge

Provider-aware and **local**. `--judge-provider auto` uses `codex` inside a Codex runtime,
`claude` inside a Claude runtime, then falls back to whichever CLI is available. Override
binaries with `--judge-bin`, `CODEX_BIN`, or `CLAUDE_BIN`; the model with `--judge-model`
or `JUDGE_MODEL`. Returned labels are coerced to an allowed rubric label or `unknown`, so the
judge can't pollute the taxonomy with drift like "B2B SaaS". Only the genuinely ambiguous
domains reach it, and one warm-up call primes the provider before the fan-out — CLI judge
calls are the throughput bottleneck by design, so `--judge-concurrency` defaults to a modest 4.

## Proxy routing (optional)

Off by default and fully inert when unset. When configured via `SCRAPE_CASCADE_PROXY_URL`
(see [`.env.example`](.env.example)), every IP-bearing tier (Tier 1 httpx bulk, Tier 2
Playwright, Tier 3 Camoufox) routes through the proxy so a high-volume unattended scrape
never accrues a footprint against your own IP. It's **fail-closed**: a misconfigured proxy
raises rather than silently falling back to your real IP. `--doctor` probes the gateway and
prints the exit IP so you can confirm routing before a bulk run.

## Advanced use case: careers / hiring-velocity

For hiring-signal rubrics, the cascade adds an **API-first** count path on top of the
homepage classifier:

- **ATS-API tier** ([`scripts/ats_api.py`](scripts/ats_api.py)) — when discovery finds a known
  ATS board, it hits the platform's **public JSON board API** for an exact open-role count +
  titles + departments, no HTML parsing. Covers Greenhouse, Lever, Ashby, SmartRecruiters,
  Workable, Recruitee, Pinpoint, and Rippling.
- **Page discovery** (`--discover-pages`) fetches common source paths (careers, news,
  security, about) and emits candidate evidence rows.
- **Evidence tiers** (`--extract-evidence`) is a conservative metadata layer that marks a
  page's source trust (official/company `A`, trusted-independent `B`, weak-external `C`,
  `Rejected`) — candidates for downstream validation, not production facts.

```bash
scrape-cascade --rubric rubrics/hiring_signal.yaml --input domains.txt \
  --rescue --discover-pages --extract-evidence --max-pages-per-domain 8 \
  --page-type-quota careers=2,news=2,security=2,company=1 --no-judge
```

`count == 0` from an ATS API is a **real** "no open roles" signal; `None` means the fetch
failed and only `None` falls through to HTML — the two are never conflated. Full per-platform
endpoint notes (and their quirks: Workable is first-page-only, SmartRecruiters needs
pagination past 100, Greenhouse's `.eu` host is display-only) live in the `ats_api.py`
docstrings.

## Known limitations

Honest about the trade-offs — see [`references/probe-pitfalls.md`](references/probe-pitfalls.md).

- **Single-IP block rate.** No managed proxy rotation means a higher block/drop rate from one
  IP at scale than a paid API would absorb. The tiered fingerprints + backoff + optional proxy
  routing are the $0 mitigations.
- **Splash / rebrand hop.** The logic to recover a thin-splash → real-entity → jobs-board hop
  (`looks_like_thin_splash` / `outbound_entity_domains_from_html`) is implemented and tested
  but **not yet wired into the live crawl** — a known roadmap item.
- **Legacy Tier 3.** `undetected-chromedriver` is Chrome-version-sensitive and doesn't import
  on Python 3.12; Camoufox is the working stealth tier and `--doctor` flags the difference.
- Homepage verdicts are homepage-level signals; strict trust decisions belong in downstream
  validators.

## Development

```bash
pip install -e ".[dev]"
pytest -q          # 312 offline tests; browser/network tiers are lazy + mocked
```

## License

MIT — see [LICENSE](LICENSE).
