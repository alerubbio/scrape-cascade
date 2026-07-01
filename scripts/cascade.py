"""scrape-cascade engine: tiered, free-first web fetch + rubric scoring + LLM judge.

The tier order is the whole point: cheap deterministic fetch first, a real browser
only for what came back empty or blocked, and the LLM judge only for the genuinely
ambiguous residue. Free first, browser second, AI last -- never the reverse.

Heavy imports (httpx / html2text / playwright / undetected_chromedriver) are done
lazily inside the functions that need them, so Tier 1 + judge run before the rescue
tier is installed.

Durability model: the SQLite DB is the incrementally-written source of truth. Pages
are persisted as each fetch chunk lands; verdicts as each domain is decided. A crash
mid-run loses at most the in-flight chunk, and the CSV/JSONL are a projection of the
DB you can regenerate any time (run.py --export-only).
"""
from __future__ import annotations

import asyncio
from html import escape as html_escape, unescape as html_unescape
import json
import os
import random
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)  # keep the major in step with curl_cffi's chrome impersonation target
MIN_OK_HTML = 500  # below this many chars, treat as empty/blocked -> escalate a tier
MIN_OK_TEXT_JINA = 200  # Jina returns clean text (not html); a real reader page clears this
STUB_TEXT_CHARS = 200  # an "ok" page whose extracted text is thinner than this is a stub
DEFAULT_CLAUDE_JUDGE_MODEL = "haiku"  # fast + cheap enough for short classification
DEFAULT_CODEX_JUDGE_MODEL = None  # None means "use the local Codex CLI default"
DEFAULT_JUDGE_MODEL = DEFAULT_CLAUDE_JUDGE_MODEL  # backward-compatible public constant
DEFAULT_JUDGE_PROVIDER = "auto"
# transient HTTP statuses worth a backed-off retry (vs. a hard 404/410 we accept as-is)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
TEXT_CAP = 250_000
ASSET_EXTENSIONS = (
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".pdf", ".zip", ".mp4", ".mov", ".woff", ".woff2", ".ttf", ".eot",
)
# Browser-tier (Playwright) resource blocking. The rescue tier extracts DOM HTML +
# JS-rendered job links, never assets, so aborting image/media/font requests is
# pure saved bandwidth (the proxy-GB lever) plus lower memory + faster networkidle.
# script / xhr / fetch / stylesheet are deliberately NOT blocked -- SPA boards mount
# their job data through them. Default-on; run.py --render-assets flips the flag off
# as a per-run kill switch. NOTE: process-global, not thread-safe -- fine for the
# subprocess CLI model; a library consumer needing per-call control should gate at
# its own call site rather than mutating this.
BLOCK_BROWSER_ASSETS = True
BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})
ATS_HOST_HINTS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "workdayjobs.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "workable.com",
    "rippling.com",
    "bamboohr.com",
    "jobvite.com",
    "icims.com",
    "recruitee.com",
    "teamtailor.com",
    "personio.com",
    "successfactors.com",
    "breezy.hr",
    "aaimtrack.com",
    "applicantstack.com",
    "applytojob.com",
    "jobs.gem.com",
    "pinpointhq.com",
    "breathehr.com",
    "paylocity.com",
    "saashr.com",
    "ultipro.com",
    # 2026-06-04 careers-discovery research: corpus-confirmed ATS hosts silently missed before.
    "careers.hibob.com",
    "careerpuck.com",
    "hire.trakstar.com",
    "trinethire.com",
    "workforcenow.adp.com",
    "oraclecloud.com",
)
EXTERNAL_RECRUITMENT_LINK_RE = re.compile(
    r"\b(recruit|recruitment|vacancies|openings?|open positions?|job listings?)\b",
    re.I,
)
EMBEDDED_ATS_URL_RE = re.compile(
    r"https://(?:"
    r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io|"
    r"jobs\.lever\.co|"
    r"jobs\.ashbyhq\.com|"
    r"jobs\.jobvite\.com|"
    r"apply\.workable\.com|"
    r"[A-Za-z0-9-]+\.workable\.com|"
    r"[A-Za-z0-9-]+\.bamboohr\.com|"
    r"(?:careers|jobs)\.smartrecruiters\.com|"
    r"ats\.rippling\.com|"
    r"[A-Za-z0-9-]+\.recruitee\.com|"
    r"[A-Za-z0-9-]+\.teamtailor\.com|"
    r"[A-Za-z0-9-]+\.personio\.com|"
    r"[A-Za-z0-9-]+\.aaimtrack\.com|"
    r"[A-Za-z0-9-]+\.applicantstack\.com|"
    r"[A-Za-z0-9-]+\.applytojob\.com|"
    r"jobs\.gem\.com|"
    r"[A-Za-z0-9-]+\.pinpointhq\.com|"
    r"[A-Za-z0-9-]+\.breathehr\.com|"
    r"recruiting\d*\.ultipro\.com|"
    r"secure\d*\.saashr\.com|"
    r"recruiting\.paylocity\.com|"
    r"[A-Za-z0-9-]+\.careers\.hibob\.com|"
    r"[A-Za-z0-9-]+\.hire\.trakstar\.com"
    r")/[^\s\"'<>\\)]+"
    r"|https://[A-Za-z0-9-]+\.icims\.com/jobs/[^\s\"'<>\\)]+"
    r"|https://app\.careerpuck\.com/job-board/[^\s\"'<>\\)]+"
    r"|https://app\.trinethire\.com/companies/[^\s\"'<>\\)]+"
    r"|https://workforcenow\.adp\.com/mascsr/[^\s\"'<>\\)]+"
    r"|https://[A-Za-z0-9-]+\.fa\.[a-z0-9]+\.oraclecloud\.com/hcmUI/CandidateExperience/[^\s\"'<>\\)]+",
    re.I,
)
JOB_DISCOVERY_CONTROL_RE = re.compile(
    r"\b(open roles?|open positions?|current openings?|job openings?|open jobs?|"
    r"view jobs?|view openings?|see jobs?|see open roles?|all jobs|browse jobs?|"
    r"job listings?|vacancies|positions?|apply|departments?|locations?)\b|"
    r"採用|募集|職種",
    re.I,
)
JOB_DISCOVERY_CONTROL_NOISE_RE = re.compile(
    r"\b(book a demo|sign in|login|newsletter|cookie|privacy|terms|case stud(?:y|ies)|"
    r"talent acquisition|career sites?|candidate experience|customer stor(?:y|ies))\b",
    re.I,
)
TRUSTED_FUNDING_HOST_HINTS = (
    "businesswire.com",
    "prnewswire.com",
    "globenewswire.com",
    "sec.gov",
    "techcrunch.com",
    "reuters.com",
    "finsmes.com",
    "venturebeat.com",
    "crunchbase.com",
    "pitchbook.com",
    "builtin.com",
)
SOCIAL_HOST_HINTS = (
    "facebook.com", "instagram.com", "linkedin.com", "x.com", "twitter.com",
    "youtube.com", "tiktok.com", "glassdoor.com", "g2.com",
)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------- store
def connect(db_path):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # WAL + NORMAL: durable enough for a cache, far less lock-prone and faster than the
    # default rollback journal under the commit-per-chunk write pattern at scale.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pages (
            domain TEXT PRIMARY KEY, url TEXT, status INTEGER,
            tier TEXT, ok INTEGER, text TEXT, fetched_at TEXT)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS verdicts (
            domain TEXT, rubric TEXT, label TEXT, confidence REAL,
            method TEXT, reason TEXT, decided_at TEXT,
            PRIMARY KEY (domain, rubric))"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS discovered_pages (
            domain TEXT, path TEXT, page_type TEXT, url TEXT, status INTEGER,
            tier TEXT, ok INTEGER, text TEXT, fetched_at TEXT,
            linked_from_homepage INTEGER DEFAULT 0,
            PRIMARY KEY (domain, path))"""
    )
    _ensure_column(conn, "discovered_pages", "linked_from_homepage", "INTEGER DEFAULT 0")
    _ensure_column(conn, "discovered_pages", "render_hint", "TEXT")
    conn.execute(
        # Full postings mined from SSR JSON blobs (ssr_json.py) at fetch time —
        # the cache stores text, not HTML, so this is the only moment they exist.
        # Downstream: jd-store harvests these as JDs and counts them per domain
        # the same way ATS-API counts flow (custom boards have no counting API).
        """CREATE TABLE IF NOT EXISTS ssr_postings (
            domain TEXT, page_url TEXT, posting_key TEXT,
            url TEXT, title TEXT, location TEXT, department TEXT,
            description_text TEXT, description_html TEXT,
            source TEXT, published_at TEXT, fetched_at TEXT,
            PRIMARY KEY (domain, page_url, posting_key))"""
    )
    conn.commit()
    return conn


def _ensure_column(conn, table, column, ddl):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(%s)" % table)}
    if column not in cols:
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, ddl))


def get_page(conn, domain):
    row = conn.execute("SELECT * FROM pages WHERE domain=?", (domain,)).fetchone()
    return dict(row) if row else None


def get_verdict(conn, domain, rubric):
    row = conn.execute(
        "SELECT * FROM verdicts WHERE domain=? AND rubric=?", (domain, rubric)
    ).fetchone()
    return dict(row) if row else None


def get_discovered_page(conn, domain, path):
    row = conn.execute(
        "SELECT * FROM discovered_pages WHERE domain=? AND path=?",
        (domain, normalize_page_key(path)),
    ).fetchone()
    return dict(row) if row else None


def list_discovered_pages(conn, domain):
    rows = conn.execute(
        "SELECT * FROM discovered_pages WHERE domain=? ORDER BY page_type, path",
        (domain,),
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_page(conn, domain, url, status, tier, ok, text, commit=True):
    conn.execute(
        """INSERT INTO pages (domain,url,status,tier,ok,text,fetched_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(domain) DO UPDATE SET
             url=excluded.url, status=excluded.status, tier=excluded.tier,
             ok=excluded.ok, text=excluded.text, fetched_at=excluded.fetched_at""",
        (domain, url, status, tier, 1 if ok else 0, text, _now()),
    )
    if commit:
        conn.commit()


def upsert_discovered_page(
    conn,
    domain,
    path,
    page_type,
    url,
    status,
    tier,
    ok,
    text,
    commit=True,
    linked_from_homepage=False,
    render_hint=None,
):
    conn.execute(
        """INSERT INTO discovered_pages (domain,path,page_type,url,status,tier,ok,text,fetched_at,linked_from_homepage,render_hint)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(domain,path) DO UPDATE SET
             page_type=excluded.page_type, url=excluded.url, status=excluded.status,
             tier=excluded.tier, ok=excluded.ok, text=excluded.text,
             fetched_at=excluded.fetched_at,
             linked_from_homepage=MAX(discovered_pages.linked_from_homepage, excluded.linked_from_homepage),
             render_hint=excluded.render_hint""",
        (
            domain,
            normalize_page_key(path),
            page_type,
            url,
            status,
            tier,
            1 if ok else 0,
            cap_text(text),
            _now(),
            1 if linked_from_homepage else 0,
            render_hint,
        ),
    )
    if commit:
        conn.commit()


def replace_ssr_postings(conn, domain, page_url, source, postings, commit=True):
    """Store the postings mined from one careers page's SSR JSON. Replace, not
    upsert: the posting SET is what the page asserts right now, and a board that
    dropped roles must not keep stale rows in this run's cache."""
    conn.execute(
        "DELETE FROM ssr_postings WHERE domain=? AND page_url=?", (domain, page_url)
    )
    now = _now()
    for p in postings or []:
        key = str(p.get("posting_id") or p.get("url") or "")[:512]
        if not key:
            continue
        conn.execute(
            """INSERT OR REPLACE INTO ssr_postings
               (domain,page_url,posting_key,url,title,location,department,
                description_text,description_html,source,published_at,fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                domain,
                page_url,
                key,
                p.get("url") or "",
                p.get("title") or "",
                p.get("location") or "",
                p.get("department") or "",
                cap_text(p.get("description_text") or "", 20_000),
                cap_text(p.get("description_html") or "", 40_000),
                source or "",
                p.get("published_at") or "",
                now,
            ),
        )
    if commit:
        conn.commit()


def list_ssr_postings(conn, domain=None):
    if domain:
        rows = conn.execute(
            "SELECT * FROM ssr_postings WHERE domain=? ORDER BY page_url, title",
            (domain,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM ssr_postings ORDER BY domain, page_url, title"
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_verdict(conn, domain, rubric, label, confidence, method, reason, commit=True):
    conn.execute(
        """INSERT INTO verdicts (domain,rubric,label,confidence,method,reason,decided_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(domain,rubric) DO UPDATE SET
             label=excluded.label, confidence=excluded.confidence,
             method=excluded.method, reason=excluded.reason,
             decided_at=excluded.decided_at""",
        (domain, rubric, label, confidence, method, reason, _now()),
    )
    if commit:
        conn.commit()


# ----------------------------------------------------------- tier 1: httpx
def normalize_domain(raw):
    d = (raw or "").strip().lower()
    if not d:
        return ""
    d = re.sub(r"^https?://", "", d)
    d = d.split("/")[0].split("?")[0]
    return d.strip()


# Pure tracking params: stripping them merges hero-CTA / footer-link URL variants
# into ONE cache key (jobs.bendingspoons.com sat in the cache twice — one fetched,
# one stranded candidate burning a slot). Segment-level string surgery, not
# parse_qsl/urlencode, so the surviving params keep their original encoding and
# existing cache keys for non-tracking URLs stay byte-identical.
_TRACKING_PARAM_RE = re.compile(
    r"^(?:utm_[a-z0-9_]*|gclid|wbraid|gbraid|yclid|fbclid|msclkid|mc_cid|mc_eid|"
    r"igshid|spm|_hsenc|_hsmi)$",
    re.I,
)


def _strip_tracking_params(query):
    if not query:
        return ""
    kept = [seg for seg in query.split("&")
            if seg and not _TRACKING_PARAM_RE.match(seg.split("=", 1)[0])]
    return "&".join(kept)


def normalize_path(raw):
    path = str(raw or "").strip()
    if not path:
        return "/"
    if re.match(r"^https?://", path, flags=re.I):
        # Keep only the path/query from an accidental absolute URL so cache keys stay
        # domain-relative and reusable across http/https redirects.
        path = re.sub(r"^https?://[^/]+", "", path, flags=re.I)
    if not path.startswith("/"):
        path = "/" + path
    path = path.split("#")[0]
    if "?" in path:
        base, query = path.split("?", 1)
        query = _strip_tracking_params(query)
        if query:
            path = (base.rstrip("/") or "/") + "?" + query
        else:
            path = base.rstrip("/") or "/"
    else:
        path = path.rstrip("/") or "/"
    return path or "/"


def normalize_page_key(raw):
    value = str(raw or "").strip()
    if re.match(r"^https?://", value, flags=re.I):
        parsed = urlparse(value)
        path = parsed.path.rstrip("/") or "/"
        query = _strip_tracking_params(parsed.query)
        return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", query, ""))
    return normalize_path(value)


def cap_text(text, cap=TEXT_CAP):
    text = text or ""
    if len(text) <= cap:
        return text
    return text[:cap]


# Career-board subdomains: the HOST is the careers signal (jobs.bendingspoons.com).
# Needed since tracking-param stripping: the old keys often carried an accidental
# "careers" token in utm params, which is what used to keep these URLs eligible
# for careers routing. Path heuristics never see the host.
CAREERS_HOST_RE = re.compile(r"^(?:jobs?|careers?|apply|talent|recruiting|work)\.", re.I)


def is_careers_host(host_or_url):
    h = _host(host_or_url) if "//" in str(host_or_url or "") else str(host_or_url or "").lower()
    return bool(CAREERS_HOST_RE.match(h))


def page_type_for_path(path):
    p = normalize_path(path).lower()
    if any(x in p for x in ("career", "job", "recruit", "karriere")):
        return "careers"
    if re.search(r"(^|/)(open-positions?|open-roles?|current-openings?|vacancies)(/|$)", p):
        return "careers"
    if any(x in p for x in ("news", "press", "media", "blog")):
        return "news"
    if any(x in p for x in ("security", "trust", "compliance")):
        return "security"
    if any(x in p for x in ("procurement", "vendor", "legal")):
        return "procurement"
    if any(x in p for x in ("about", "company", "team")):
        return "company"
    return "other"


def page_specs_from_rubric(rubric, max_pages_per_domain=None):
    """Expand rubric crawl_paths into normalized page specs.

    A spec is intentionally light: it says which common path to try, how to classify
    the page, and which terms make the fetched page worth deeper validation later.
    """
    raw_specs = rubric.get("crawl_paths") or []
    evidence_by_type = rubric.get("page_evidence_terms") or {}
    specs, seen = [], set()
    if max_pages_per_domain is not None and max_pages_per_domain <= 0:
        return specs
    for raw in raw_specs:
        if isinstance(raw, str):
            page_type = page_type_for_path(raw)
            paths = [raw]
            terms = []
        else:
            page_type = str(raw.get("page_type") or raw.get("type") or "").strip() or None
            paths = raw.get("paths") or raw.get("path") or []
            if isinstance(paths, str):
                paths = [paths]
            terms = raw.get("evidence_terms") or raw.get("terms") or []
            if isinstance(terms, str):
                terms = [terms]
        for p in paths:
            path = normalize_path(p)
            if path in seen:
                continue
            seen.add(path)
            pt = page_type or page_type_for_path(path)
            type_terms = evidence_by_type.get(pt, [])
            if isinstance(type_terms, str):
                type_terms = [type_terms]
            specs.append(
                {
                    "path": path,
                    "page_type": pt,
                    "evidence_terms": list(dict.fromkeys([str(t) for t in (terms or []) + (type_terms or [])])),
                }
            )
    if max_pages_per_domain is not None:
        return select_page_specs(specs, max_pages_per_domain=max_pages_per_domain)
    return specs


def parse_page_type_quota(raw):
    """Parse CLI quota text like `careers=2,news=2,security=1`.

    Empty input means no quota. Values must be non-negative integers; callers can
    decide whether a zero quota is useful for their selection mode.
    """
    if not raw:
        return {}
    out = {}
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError("page type quota must use page_type=N: %s" % part)
        key, value = [p.strip().lower() for p in part.split("=", 1)]
        if not key:
            raise ValueError("page type quota has an empty page type")
        try:
            n = int(value)
        except ValueError as exc:
            raise ValueError("page type quota must be an integer: %s" % part) from exc
        if n < 0:
            raise ValueError("page type quota cannot be negative: %s" % part)
        out[key] = n
    return out


def parse_page_type_order(raw):
    if not raw:
        return []
    return [p.strip().lower() for p in str(raw).split(",") if p.strip()]


def _page_type_order(specs, preferred_order=None):
    seen = []
    available = {str(s.get("page_type") or "other").lower() for s in specs}
    for pt in preferred_order or []:
        if pt in available and pt not in seen:
            seen.append(pt)
    for spec in specs:
        pt = str(spec.get("page_type") or "other").lower()
        if pt not in seen:
            seen.append(pt)
    return seen


def select_page_specs(specs, max_pages_per_domain=None, page_type_quota=None, page_type_order=None):
    """Select a balanced source-page mix from expanded rubric specs.

    The original cap behavior took the first N rubric paths, which made capacity
    checks careers-heavy because `/careers`, `/careers/`, `/jobs`, and `/jobs/`
    appeared first. With no cap/quota this returns the original ordered specs. With a cap,
    it round-robins across page types unless explicit quotas are supplied.
    """
    specs = list(specs or [])
    if not specs:
        return []
    if max_pages_per_domain is not None and max_pages_per_domain <= 0:
        return []
    quotas = dict(page_type_quota or {})
    order = _page_type_order(specs, page_type_order)
    if max_pages_per_domain is None and not quotas and not page_type_order:
        return specs

    groups = {pt: [] for pt in order}
    for spec in specs:
        pt = str(spec.get("page_type") or "other").lower()
        groups.setdefault(pt, []).append(spec)
    def spec_priority(spec):
        raw = str(spec.get("url") or spec.get("path") or "")
        host = _host(raw)
        path = urlparse(raw).path.lower()
        is_ats = any(_host_matches_hint(host, hint) for hint in ATS_HOST_HINTS)
        is_careers_subdomain = bool(host and host.startswith("careers."))
        is_careers_hub = bool(re.search(r"(^|/)hub/careers/?$", path))
        jobish = bool(re.search(
            r"\b(careers?|jobs?|open[- ]positions?|open[- ]roles?|current[- ]openings?|vacancies)\b",
            raw,
            re.I,
        ))
        # The careers.{d} subdomain guess is a PENALTY, not a bonus: it NXDOMAINs
        # for most companies (sdr500_r1: 426 of 1,024 careers status-0s) and,
        # ranked above real paths, it stole a careers-quota slot exactly where
        # evidence was missing (carestack: /company/careers dropped while the
        # dead probe consumed the slot). Its URL matches `jobish` too, so the
        # penalty term is what actually sinks it below configured paths; a real
        # homepage-linked careers.{d} link still wins via linked_from_homepage.
        return (
            0 if spec.get("linked_from_homepage") else 1,
            0 if is_ats else 1,
            0 if is_careers_hub else 1,
            0 if jobish else 1,
            1 if is_careers_subdomain else 0,
        )

    for pt in groups:
        groups[pt].sort(key=spec_priority)

    selected, selected_ids, selected_counts = [], set(), {pt: 0 for pt in order}

    def add(spec):
        if id(spec) in selected_ids:
            return False
        if max_pages_per_domain is not None and len(selected) >= max_pages_per_domain:
            return False
        selected_ids.add(id(spec))
        selected.append(spec)
        pt = str(spec.get("page_type") or "other").lower()
        selected_counts[pt] = selected_counts.get(pt, 0) + 1
        return True

    for pt in order:
        quota = quotas.get(pt)
        if quota is None:
            continue
        for spec in groups.get(pt, [])[:quota]:
            if not add(spec):
                return selected

    while max_pages_per_domain is None or len(selected) < max_pages_per_domain:
        progressed = False
        fill_order = sorted(enumerate(order), key=lambda item: (selected_counts.get(item[1], 0), item[0]))
        for _idx, pt in fill_order:
            for spec in groups.get(pt, []):
                if id(spec) not in selected_ids:
                    if not add(spec):
                        return selected
                    progressed = True
                    break
        if not progressed:
            break
    return selected


def _host(value):
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    return (urlparse(value).hostname or "").lower().removeprefix("www.")


# Hostname characters that are valid in a real DNS label: letters, digits, hyphen, dot.
# A host string containing whitespace, percent-encoding (%20), equals signs, or
# similar query-string junk cannot be a plausible DNS hostname and must be dropped
# before it reaches the TCP connection layer (where ip_address() is called and
# raises ValueError uncaught in some dependency versions).
_PLAUSIBLE_HOSTNAME_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?)+$",
    re.I,
)


def _is_plausible_host(host):
    """Return False when *host* cannot be a valid DNS hostname.

    Rejects hosts that contain whitespace, percent-encoded characters (%XX),
    equals signs, or any other character that would corrupt a DNS lookup.  This
    is a fast pre-filter — it does NOT replace full URL validation, but it does
    stop malformed SSR-JSON artifacts like 'sermon%20index=0%20key=url' (decoded:
    'sermon index=0 key=url') from reaching ip_address() inside the network stack.
    """
    if not host:
        return False
    # Quick rejection: characters that are never valid in a hostname
    if "%" in host or " " in host or "=" in host or "\t" in host or "\n" in host:
        return False
    return bool(_PLAUSIBLE_HOSTNAME_RE.match(host))


def _safe_urljoin(base, href):
    """urljoin() wrapper that returns None instead of raising on malformed URLs.

    Python 3.12's urlsplit() calls ipaddress.ip_address() on bracketed netlocs
    and raises ValueError when the content is not a valid IP address.  A single
    bad href in a page's HTML must never abort the entire link-extraction loop.
    """
    try:
        return urljoin(base, href)
    except Exception:
        return None


def _host_matches_hint(host, hint):
    return host == hint or host.endswith("." + hint)


def _host_allowed(url, domain, base_url):
    host = _host(url)
    if not host:
        return False
    domain_host = _host(domain)
    base_host = _host(base_url)
    if host in (domain_host, base_host):
        return True
    if domain_host and host.endswith("." + domain_host):
        return True
    if base_host and host.endswith("." + base_host):
        return True
    return any(_host_matches_hint(host, h) for h in ATS_HOST_HINTS)


def _external_recruitment_link_allowed(url, text=""):
    host = _host(url)
    if not host:
        return False
    joined = " ".join([url or "", text or ""])
    return bool(EXTERNAL_RECRUITMENT_LINK_RE.search(joined))


def _is_ignored_link(url):
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return True
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if any(_host_matches_hint(host, h) for h in SOCIAL_HOST_HINTS):
        return True
    if _greenhouse_board_url_from_widget_url(url) or _bamboohr_board_url_from_widget_url(url):
        return False
    return any(path.endswith(ext) for ext in ASSET_EXTENSIONS)


def _link_page_type(url, text=""):
    joined = (url + " " + (text or "")).lower()
    host = _host(url)
    parsed = urlparse(url)
    path = parsed.path.lower()
    if re.search(
        r"(career-sites?|talent-acquisition|candidate-experience|event-recruiting|"
        r"/solutions/recruiters|college-career|licensing-opportunities|"
        r"equal-opportunities-policy|financial-services|get-involved|join-a-group)",
        joined,
    ):
        return ""
    if any(_host_matches_hint(host, h) for h in ATS_HOST_HINTS):
        return "careers"
    if re.search(r"(^|/)(recruit|recruitment)(/|$)", path):
        return "careers"
    career_phrase_terms = (
        "job openings",
        "open roles",
        "open positions",
        "current openings",
        "employment opportunities",
        "join us",
        "join our team",
        "work with us",
        "view jobs",
        "view openings",
        "view current career opportunities",
        "採用",
        "募集",
    )
    career_token_re = re.compile(
        r"\b(careers?|jobs?|opportunities|vacancies|karriere|stellenangebote)\b",
        re.I,
    )
    if any(x in joined for x in career_phrase_terms) or career_token_re.search(joined):
        return "careers"
    if any(x in joined for x in ("news", "press", "media", "blog")):
        return "news"
    if any(x in joined for x in ("security", "trust", "compliance")):
        return "security"
    if any(x in joined for x in ("procurement", "vendor", "supplier", "legal")):
        return "procurement"
    if any(x in joined for x in ("about", "company", "team")):
        return "company"
    return ""


def _embedded_jobvite_links_from_html(html, soup=None):
    links = []
    seen = set()

    def add(slug):
        slug = re.sub(r"[^A-Za-z0-9_-]+", "", (slug or "").strip())
        if not slug or slug in seen:
            return
        seen.add(slug)
        links.append((f"https://jobs.jobvite.com/{slug}/jobs?nl=1", "Jobvite open positions"))

    if soup is not None:
        for el in soup.select("[data-careersite]"):
            classes = set(el.get("class") or [])
            if "jv-careersite" in classes or "jobvite" in str(el).lower():
                add(el.get("data-careersite"))
    for match in re.finditer(r"data-careersite=[\"']([^\"']+)[\"']", html or "", flags=re.I):
        add(match.group(1))
    return links


def _embedded_rippling_links_from_html(html, soup=None):
    links = []
    seen = set()

    def add(slug):
        slug = re.sub(r"[^A-Za-z0-9_-]+", "", (slug or "").strip())
        if not slug or slug in seen:
            return
        seen.add(slug)
        links.append((f"https://ats.rippling.com/{slug}/jobs", "Rippling embedded job board"))

    raw = html or ""
    if "ripplingcdn.com/ats/embeds/job-board" not in raw and "rr-job-board" not in raw:
        return links
    if soup is not None:
        for el in soup.select("[data-job-board-id]"):
            add(el.get("data-job-board-id"))
    for match in re.finditer(r"data-job-board-id=[\"']([^\"']+)[\"']", raw, flags=re.I):
        add(match.group(1))
    return links


def _bad_ats_utility_url(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    raw = (url or "").lower()
    return (
        "${" in url
        or "{" in url
        or "}" in url
        or host.startswith("login.")
        or host == "admin.aaimtrack.com"
        or host in {"bamboohr.com", "www.bamboohr.com", "workable.com", "www.workable.com"}
        or (host == "jobseekers.workable.com" and path.startswith("/hc/"))
        or host in {"breezy.hr", "www.breezy.hr"}
        or host in {"paylocity.com", "www.paylocity.com", "pinpointhq.com", "www.pinpointhq.com"}
        or (host.endswith(".aaimtrack.com") and re.search(r"/(?:account|help|stats|widget)/|applicant-communication-policy", path))
        or (host.endswith(".icims.com") and not path.startswith("/jobs"))
        or (host == "jobs.gem.com" and path.rstrip("/") == "/gem/embed")
        or (host == "recruiting.paylocity.com" and re.search(
            r"/recruiting/(?:publicleads|bundles)/|/recruiting/jobs/(?:jobnotfound|getlogo)", path))
        or (host.startswith("recruiting") and host.endswith(".ultipro.com") and re.search(
            r"/(?:accessibility|account/register|authcode/postlogin)(?:/|$|\?)", path))
        or (host.endswith(".pinpointhq.com") and re.search(r"/register-your-interest(?:/|$)", path))
        or (host in {"app.rippling.com", "rippling.com", "www.rippling.com"} and re.search(r"^/legal/|^/products/hr/recruiting/?$|^/recruiting/?$", path))
        or "/share_image/" in path
        or path.endswith("/jobalerts")
        or path.endswith("/llms.txt")
        or (host.endswith("jobvite.com") and path.endswith("/apply"))
        or "poweredby" in raw
        or "privacy-policy" in path
        or "terms-of-service" in path
        # Workable error/auth pages — not job boards
        or (host in {"apply.workable.com", "jobs.workable.com"} and re.search(r"^/(?:oops|login|error)(?:/|$)", path))
        # TeamTailor non-board endpoints (map widgets, admin, API)
        or (host.endswith(".teamtailor.com") and re.search(r"/locations/map_details\b|^/api/|^/admin/", path))
        # OracleCloud non-board endpoints: sitemaps, images, blank stubs
        or (host.endswith(".oraclecloud.com") and re.search(
            r"/hcmui/(?:candidateexperience/sitemaps|candidateexperience/images|afr/blank)", path))
    )


def _greenhouse_board_url_from_widget_url(url):
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").rstrip("/")
    if host not in {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "boards.eu.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    }:
        return ""
    if path != "/embed/job_board/js":
        return ""
    board = (parse_qs(parsed.query or "").get("for") or [""])[0].strip()
    if not board:
        return ""
    board_host = "job-boards.eu.greenhouse.io" if ".eu.greenhouse.io" in host else "job-boards.greenhouse.io"
    return f"https://{board_host}/embed/job_board?for={board}"


def _bamboohr_board_url_from_widget_url(url):
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").rstrip("/")
    if not host.endswith(".bamboohr.com"):
        return ""
    if path == "/js/embed.js":
        return f"https://{host}/careers"
    return ""


def _embedded_ats_links_from_html(html, soup=None):
    links = []
    seen = set()

    def add(url, text="Embedded ATS job board"):
        url = html_unescape((url or "").strip().rstrip("\\.,;"))
        if not url or url in seen:
            return
        if _bad_ats_utility_url(url):
            return
        if _is_ignored_link(url):
            return
        host = _host(url)
        if not any(_host_matches_hint(host, hint) for hint in ATS_HOST_HINTS):
            return
        seen.add(url)
        # A widget JS URL is not fetchable HTML -- emit only the board URL it resolves
        # to, not the .js widget (which wastes a fetch slot and inflates reject counts).
        greenhouse_board = _greenhouse_board_url_from_widget_url(url)
        bamboohr_board = _bamboohr_board_url_from_widget_url(url)
        if greenhouse_board:
            add(greenhouse_board, "Greenhouse embedded job board")
        elif bamboohr_board:
            add(bamboohr_board, "BambooHR embedded job board")
        else:
            links.append((url, text))

    raw = html or ""
    for source in (raw, raw.replace("\\/", "/")):
        for match in EMBEDDED_ATS_URL_RE.finditer(source):
            add(match.group(0))
    if soup is not None:
        attrs = (
            "href",
            "src",
            "data-url",
            "data-href",
            "data-src",
            "data-iframe-src",
            "data-apply-url",
            "data-job-board",
            "data-board-url",
            "data-job-board-id",
            "onclick",
            "ng-href",
        )
        for el in soup.find_all(True):
            label = el.get_text(" ", strip=True) or "Embedded ATS job board"
            for attr in attrs:
                value = el.get(attr)
                if not value:
                    continue
                for match in EMBEDDED_ATS_URL_RE.finditer(str(value)):
                    add(match.group(0), label)
    links.extend(_embedded_jobvite_links_from_html(raw, soup=soup))
    links.extend(_embedded_rippling_links_from_html(raw, soup=soup))
    return links


# --------------------------------------------------------------------------- #
# net-new discovery methods (2026-06-04 careers-discovery research)            #
# HTML-independent / zero-or-low-cost routes that recover SPA / blocked /      #
# hidden careers boards the homepage-anchor + path-guess tiers miss. Each      #
# takes a ``fetch(url) -> text|None`` callable so it is decoupled + testable.  #
# --------------------------------------------------------------------------- #
CAREERS_URL_RE = re.compile(
    r"https?://[^\s\"'<>]*?/(?:careers?|jobs?|hiring|join-?us|join-our-team|"
    r"open-positions?|open-roles?|current-openings?|vacanc(?:y|ies)?|opportunities|"
    r"employment|[a-z-]*job[_-]listing[a-z-]*)(?:[/?#][^\s\"'<>]*)?",
    re.I,
)


def _sitemap_locs(xml):
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml or "", re.I)


def discover_via_robots(domain, fetch):
    """Scan /robots.txt for careers URLs (``Sitemap:`` lines + freeform comments).

    Returns ``{"careers_urls", "sitemaps"}``. Two corpus sites (fingerprint, faire)
    literally wrote their careers URL in a robots.txt comment; ``sitemaps`` feeds
    discover_via_sitemap so we follow declared sitemap indexes for free.
    """
    d = (domain or "").strip().strip("/")
    txt = fetch("https://%s/robots.txt" % d) if d else None
    if not txt:
        return {"careers_urls": [], "sitemaps": []}
    careers, sitemaps = [], []
    for line in txt.splitlines():
        m = re.match(r"(?i)\s*sitemap:\s*(\S+)", line)
        if m:
            sitemaps.append(m.group(1).strip())
        for um in CAREERS_URL_RE.finditer(line):
            careers.append(um.group(0))
    return {"careers_urls": list(dict.fromkeys(careers)),
            "sitemaps": list(dict.fromkeys(sitemaps))}


def discover_via_sitemap(domain, fetch, extra_sitemaps=None, max_children=15):
    """Fetch sitemap(s), RECURSE one level into child sitemaps, return careers URLs.

    The live engine had no sitemap code; single-level parsing misses child sitemaps
    (job_listing-sitemap.xml, page-sitemap.xml) where deep/non-standard careers URLs
    (dripcapital /en-in/careers/, komprise WP-Job-Manager) live. Recursion is capped
    at one level + ``max_children`` to stay a probe, not a crawl.
    """
    d = (domain or "").strip().strip("/")
    if not d:
        return []
    roots = ["https://%s/sitemap.xml" % d, "https://%s/sitemap_index.xml" % d]
    for s in (extra_sitemaps or []):
        if s not in roots:
            roots.append(s)
    found, children, seen_child = [], [], set()

    def consume(xml, allow_children):
        for loc in _sitemap_locs(xml):
            is_xml = loc.lower().split("?")[0].endswith(".xml")
            if is_xml and allow_children:
                if loc not in seen_child and loc not in roots:
                    seen_child.add(loc)
                    children.append(loc)
            elif not is_xml and CAREERS_URL_RE.search(loc):
                found.append(loc)

    for root in roots:
        xml = fetch(root)
        if xml:
            consume(xml, allow_children=True)
    for child in children[:max_children]:
        xml = fetch(child)
        if xml:
            consume(xml, allow_children=False)
    return list(dict.fromkeys(found))


_NEXT_DATA_ATS_PATTERNS = (
    ("greenhouse", re.compile(
        r'(?i)"(?:greenhouse[_-]?token|gh[_-]?token|board[_-]?token)"\s*:\s*"([A-Za-z0-9_-]{2,})"')),
    ("lever", re.compile(
        r'(?i)"(?:lever[_-]?account(?:[_-]?name)?|lever[_-]?site)"\s*:\s*"([A-Za-z0-9_-]{2,})"')),
    ("ashby", re.compile(
        r'(?i)"(?:ashby[_-]?(?:org|account|job[_-]?board[_-]?name))"\s*:\s*"([A-Za-z0-9_-]{2,})"')),
)


def extract_ats_tokens_from_json(html):
    """Parse embedded __NEXT_DATA__/APP_CONFIG JSON for ATS slugs -> [(ats, slug)].

    Some Next.js sites 404 their public ATS board URL but ship the board token in
    embedded JSON (credible greenHouseToken, brex Greenhouse-API-only). Feed the
    result to ats_api.count_open_roles. Additive — many sites omit it.
    """
    out, seen = [], set()
    for ats, rx in _NEXT_DATA_ATS_PATTERNS:
        for m in rx.finditer(html or ""):
            key = (ats, m.group(1))
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def registrable_domain(host_or_url):
    """Best-effort registrable domain (last two labels). Good enough for the
    href-host != crawled-domain divergence signal; not a public-suffix-list parse."""
    h = _host(host_or_url)
    parts = [p for p in h.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def detect_acquirer_redirect(final_host, domain):
    """Flag when a careers anchor/redirect lands on a DIFFERENT registrable domain.

    6+ corpus sites route careers to a parent (adroll->nextroll, clari->salesloft).
    The signal is href-host divergence, NOT anchor text. ATS hosts are excluded
    (greenhouse.io etc. are legit destinations, not acquirers).
    """
    fh = _host(final_host)
    fr, dr = registrable_domain(final_host), registrable_domain(domain)
    if fr and dr and fr != dr and not any(_host_matches_hint(fh, h) for h in ATS_HOST_HINTS):
        return {"acquired": True, "acquirer_host": fh,
                "acquirer_registrable": fr, "acquirer_slug": fr.split(".")[0]}
    return {"acquired": False}


SOCIAL_JOBS_TAB_RE = re.compile(
    r"https?://(?:[a-z0-9-]+\.)?(?:linkedin\.com/company/[^/\s\"'<>]+/jobs|"
    r"wellfound\.com/company/[^/\s\"'<>]+/jobs)",
    re.I,
)


def social_jobs_tab_links(html):
    """LinkedIn/Wellfound jobs-tab URLs (capture-only; both 403 bots). Preserves a
    careers signal for outreach instead of recording a miss; tag source=social_jobs_tab."""
    return list(dict.fromkeys(SOCIAL_JOBS_TAB_RE.findall(html or "")))


def candidate_page_targets_from_html(domain, base_url, html, max_links=None):
    """Extract same-site and ATS candidate pages from a homepage.

    This is still only discovery. External ATS links are kept as candidates because
    they are useful for later validation, but no trust decision is made here.
    """
    if not html:
        return []
    soup = None
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        raw_links = [(a.get("href") or "", a.get_text(" ", strip=True)) for a in soup.find_all("a")]
    except Exception:
        raw_links = [(m.group(1), "") for m in re.finditer(r'href=["\']([^"\']+)["\']', html, flags=re.I)]
    raw_links.extend(_embedded_ats_links_from_html(html, soup=soup))

    out, seen = [], set()
    for href, text in raw_links:
        try:
            href = (href or "").strip()
            if not href or href.startswith("#"):
                continue
            url = _safe_urljoin(base_url or ("https://" + domain), href)
            if url is None:
                continue
            if _is_ignored_link(url):
                continue
            if _bad_ats_utility_url(url):
                continue
            page_type = _link_page_type(url, text)
            if not page_type:
                continue
            if not _host_allowed(url, domain, base_url):
                if page_type != "careers" or not _external_recruitment_link_allowed(url, text):
                    continue
            parsed = urlparse(url)
            host = _host(url)
            domain_host = _host(domain)
            if (
                page_type == "careers"
                and not parsed.path.rstrip("/")
                and host == domain_host
                and not any(_host_matches_hint(_host(url), h) for h in ATS_HOST_HINTS)
            ):
                continue
            key = normalize_page_key(url)
            if key in seen:
                continue
            seen.add(key)
            target = {"domain": domain, "page_type": page_type}
            if host == domain_host:
                target["path"] = normalize_path(parsed.path or "/")
            else:
                target["path"] = key
                target["url"] = key
            target["linked_from_homepage"] = True
            out.append(target)
            if max_links and len(out) >= max_links:
                break
        except Exception:
            # A single malformed href must never abort the entire extraction loop.
            continue
    return out


_SPLASH_INFRA_HOST_SUBSTR = (
    "google", "gstatic", "googleapis", "cloudflare", "cloudfront", "akamai",
    "fonts.", "cdn", "facebook", "fbcdn", "doubleclick", "gtag", "segment",
    "hotjar", "hubspot", "wp.com", "w3.org", "schema.org", "gravatar", "youtube",
    "vimeo", "intercom", "zendesk", "sentry", "amazonaws", "azureedge", "jsdelivr",
    "unpkg", "bootstrapcdn", "typekit", "cookiebot", "onetrust", "wixstatic", "squarespace",
    # pervasive SaaS vendors commonly mentioned in prose/scripts (not the company itself)
    "stripe", "sendgrid", "twilio", "datadog", "auth0", "okta.com", "pagerduty",
    "newrelic", "mixpanel", "amplitude", "mailchimp", "atlassian", "github", "gitlab",
    "salesforce", "marketo", "pardot", "qualtrics", "optimizely", "calendly", "typeform",
)
_DOMAIN_TOKEN_RE = re.compile(
    r"\b([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.(?:ai|io|com|co|net|org|app|dev|tech))\b", re.I
)


def looks_like_thin_splash(html, careers_candidates):
    """True when a homepage yields no careers candidates and is a thin/JS-shell splash --
    the rebrand/interstitial case where the real hiring entity lives on another domain
    (e.g. sigfig.com -> tandems.ai). Conservative: only fires when there are no careers
    candidates at all, so it never competes with normal discovery."""
    if careers_candidates:
        return False
    if not html:
        return True
    return html.lower().count("<a ") <= 5


def outbound_entity_domains_from_html(domain, base_url, html, max_domains=6):
    """Outbound *company* domains worth probing one hop from a thin splash homepage.
    Scans hrefs AND visible text (the rebrand target is often a bare-text mention, not a
    link -- verified on sigfig.com). Excludes self, social, infra/CDN, ATS, and assets.
    Pure discovery: no fetch. The caller fetches each and re-runs careers discovery."""
    if not html:
        return []
    domain_host = _host(domain) or _host("https://" + str(domain))
    base_reg = domain_host[4:] if domain_host.startswith("www.") else domain_host
    out, seen = [], set()
    for m in _DOMAIN_TOKEN_RE.finditer(html):
        cand = m.group(1).lower()
        if cand in seen:
            continue
        seen.add(cand)
        if not base_reg or cand == base_reg or cand.endswith("." + base_reg) or base_reg.endswith("." + cand):
            continue
        if any(s in cand for s in _SPLASH_INFRA_HOST_SUBSTR):
            continue
        if any(_host_matches_hint(cand, h) for h in SOCIAL_HOST_HINTS):
            continue
        if any(_host_matches_hint(cand, h) for h in ATS_HOST_HINTS):
            continue  # ATS boards are already handled by candidate_page_targets_from_html
        if any(cand.endswith(ext) for ext in ASSET_EXTENSIONS):
            continue
        out.append(cand)
        if len(out) >= max_domains:
            break
    return out


SECOND_HOP_CAREERS_PATH_RE = re.compile(
    r"(^|/)(jobs?|careers?/jobs?|jobs?-listings?|jobs?-postings?|"
    r"jobs?-opportunities|open-positions?|open-roles?|current-openings?|vacancies|board)(/|$)",
    re.I,
)
DIRECT_CAREERS_JOB_PATH_RE = re.compile(
    r"(^|/)(careers?|jobs?)/(?!culture/?$|benefits?/?$|life/?$|values?/?$|"
    r"teams?/?$|faq/?$|internships?/?$|students?/?$|volunteers?/?$|"
    r"partnership/?$|professional-development/?$|privacy/?$|impact/?$|overview/?$|"
    r"employee-benefits?/?$|student-program/?$|events?/?$|blog/?$|news/?$|"
    r"leadership/?$)[^/?#]{4,}",
    re.I,
)


def candidate_child_career_targets_from_html(domain, base_url, html, max_links=None):
    """Extract high-confidence careers children from a fetched careers page.

    Homepage discovery finds "Careers" pages. Many official careers pages then link
    one step deeper to a branded ATS/listing page via "Open Positions" or "View
    jobs". Keep this deliberately narrow so page discovery does not become a crawl.
    """
    targets = []
    for target in candidate_page_targets_from_html(domain, base_url, html, max_links=max_links):
        try:
            if target.get("page_type") != "careers":
                continue
            raw = target.get("url") or target.get("path") or ""
            url = raw if re.match(r"^https?://", str(raw), flags=re.I) else _safe_urljoin(base_url or ("https://" + domain), raw)
            if url is None:
                continue
            host = _host(url)
            is_ats = any(_host_matches_hint(host, h) for h in ATS_HOST_HINTS)
            domain_host = _host(domain)
            path = urlparse(url).path or "/"
            same_company_job_detail = (
                not is_ats
                and domain_host
                and (host == domain_host or host.endswith("." + domain_host))
                and DIRECT_CAREERS_JOB_PATH_RE.search(path)
            )
            if not is_ats and not SECOND_HOP_CAREERS_PATH_RE.search(path) and not same_company_job_detail:
                continue
            if normalize_page_key(url) == normalize_page_key(base_url):
                continue
            target["linked_from_homepage"] = True
            targets.append(target)
        except Exception:
            # A single malformed href must never abort the entire extraction loop.
            continue
    return targets


def fetch_text(url, timeout=15.0):
    """Single-URL GET returning response text (any 2xx, any content-type) or None.

    Used by the index-discovery tier (robots.txt / sitemap.xml) where we want raw
    text, not the careers-page fetch logic. Prefers httpx, falls back to urllib.
    """
    try:
        import httpx

        r = httpx.get(url, timeout=timeout, follow_redirects=True, verify=False,
                      headers={"User-Agent": DEFAULT_UA})
        if r.status_code == 200 and r.text:
            return r.text
        return None
    except ImportError:
        pass
    except Exception:
        return None
    try:
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            if resp.status != 200:
                return None
            return resp.read().decode("utf-8", "replace")
    except Exception:
        return None


# --- Proxy pool routing (the IP-reputation lever) -----------------------------
# Off by default: with no env set, every tier fetches from this host's own IP,
# exactly as before (fully inert). When set, the IP-bearing tiers -- Tier 1 httpx
# (the bulk), Tier 2 Playwright, Tier 3 camoufox -- route through a rented proxy
# gateway, so the host's own IP never accumulates an automated-scraping footprint.
# (Tier 1.5 Jina already exits through r.jina.ai's own infra, so it needs no proxy.)
#
# Two knobs, so the cost-right shape is reachable -- a cheap datacenter pool for the
# already-unblocked bulk + a residential pool only for the hard anti-bot tail:
#   SCRAPE_CASCADE_PROXY_URL          -> all IP-bearing tiers   (alias SCRAPE_PROXY_URL)
#   SCRAPE_CASCADE_PROXY_URL_STEALTH  -> Tier-3 stealth override (alias SCRAPE_PROXY_URL_STEALTH;
#                                        falls back to the bulk URL when unset)
# Value is a full URL: "http://user:pass@host:port" (socks5://... also works for httpx).
#
# FAIL CLOSED: when a proxy is configured but cannot be wired, construction raises
# rather than silently fetching from the real IP -- a silent direct fallback would
# defeat the entire point of the lever.
def _proxy_url(stealth=False):
    """Configured proxy URL for a tier, or None. The stealth (Tier 3) tier prefers its
    own gateway and falls back to the bulk gateway."""
    if stealth:
        v = (os.environ.get("SCRAPE_CASCADE_PROXY_URL_STEALTH")
             or os.environ.get("SCRAPE_PROXY_URL_STEALTH"))
        if v and v.strip():
            return v.strip()
    v = (os.environ.get("SCRAPE_CASCADE_PROXY_URL")
         or os.environ.get("SCRAPE_PROXY_URL"))
    return v.strip() if (v and v.strip()) else None


def _proxy_playwright(url):
    """Convert a proxy URL to Playwright/Camoufox launch form:
    {"server": "scheme://host:port", "username": ..., "password": ...}, or None when
    url is falsy. Raises ValueError on a malformed URL (fail-closed: never launch a
    browser un-proxied when a proxy was requested)."""
    if not url:
        return None
    from urllib.parse import urlsplit, unquote
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        raise ValueError("proxy URL missing scheme/host: %r" % (url,))
    server = "%s://%s" % (parts.scheme, parts.hostname)
    if parts.port:
        server = "%s:%d" % (server, parts.port)
    out = {"server": server}
    if parts.username:
        out["username"] = unquote(parts.username)
    if parts.password:
        out["password"] = unquote(parts.password)
    return out


def _make_client(timeout, concurrency):
    import httpx

    kwargs = dict(
        follow_redirects=True,
        verify=False,  # scrape tolerance > cert strictness; this is classification
        timeout=httpx.Timeout(timeout),
        headers={"User-Agent": DEFAULT_UA},
        limits=httpx.Limits(
            max_connections=concurrency, max_keepalive_connections=concurrency
        ),
    )
    proxy = _proxy_url()
    if proxy:
        # httpx>=0.26 takes a single proxy= URL; routes the whole Tier-1 bulk off our IP.
        kwargs["proxy"] = proxy
    try:
        return httpx.AsyncClient(http2=True, **kwargs)
    except ImportError:
        return httpx.AsyncClient(**kwargs)  # h2 not installed -> http/1.1


def _backoff(attempt, base=0.5, cap=8.0):
    """Exponential backoff with jitter. Single-IP scraping has no managed proxy to
    absorb rate-limits, so a backed-off retry is the $0 mitigation the pitfalls doc
    promises -- not just an immediate give-up on the first transient blip."""
    return min(cap, base * (2 ** attempt)) + random.uniform(0, 0.5)


CHALLENGE_TITLE_RE = re.compile(
    r"<title[^>]*>\s*(?:just a moment|checking your browser|attention required!|"
    r"access (?:to this page )?(?:has been )?denied|are you a human|please verify you are a human)",
    re.I,
)
_SOFT_BLOCK_MARKERS = (
    "_cf_chl_opt", "/cdn-cgi/challenge-platform", "challenges.cloudflare.com",
    "ct.captcha-delivery.com", "client.px-cdn.net",
)


def is_soft_block(status, headers, html):
    """A 200 that is actually an anti-bot interstitial, not real content. Uses
    high-precision signals only (Cloudflare `cf-mitigated` header, challenge <title>,
    vendor challenge JS) -- deliberately NOT a raw byte-size heuristic, which would
    wrongly discard small-but-real pages. A soft block must never be recorded as a
    real page (that is the false "no careers page" failure mode)."""
    h = headers or {}
    try:
        if str(h.get("cf-mitigated", "")).lower() == "challenge":
            return True
    except Exception:
        pass
    body = html or ""
    if CHALLENGE_TITLE_RE.search(body):
        return True
    low = body.lower()
    return any(m in low for m in _SOFT_BLOCK_MARKERS)


def _domain_url_candidates(domain):
    """Ordered fetch candidates for a bare domain: apex first (https then http),
    then the www. variants. Callers must attempt a www variant only while NO
    prior candidate produced a real HTTP response -- a settled apex answer
    (even a 404/403) proves DNS + a listening server, so www adds nothing;
    the www hop only rescues apex-dead-but-www-alive DNS setups."""
    cands = ["https://" + domain, "http://" + domain]
    if not domain.startswith("www."):
        cands += ["https://www." + domain, "http://www." + domain]
    return cands


def _is_www_variant(url, domain):
    return not domain.startswith("www.") and ("://www." + domain) in url


def _extracted_text_len(html):
    """Cheap tag-strip text length -- enough to tell a stub/JS-shell from real
    content without the full html_to_text pipeline."""
    if not html:
        return 0
    body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", body)
    return len(re.sub(r"\s+", " ", text).strip())


_SPA_ROOT_RE = re.compile(r"<div[^>]+id=[\"'](?:root|app|__next|___gatsby)[\"']", re.I)


def looks_like_js_shell(html, text):
    """A 200 whose HTML is big enough but whose extracted text is thin AND whose
    markup says client-side app (script-heavy or a bare SPA root div). This is a
    RENDER-ROUTING signal only -- it must never feed is_soft_block; block labels
    stay high-precision (header/title/vendor-JS evidence)."""
    if not html or len(html) < MIN_OK_HTML:
        return False
    if len((text or "").strip()) >= STUB_TEXT_CHARS:
        return False
    return (
        len(re.findall(r"<script\b", html, flags=re.I)) >= 4
        or bool(_SPA_ROOT_RE.search(html))
    )


# Chrome-rich shells: a careers SPA that server-renders its filter UI (and often
# a zero-state) but mounts the rows client-side. Such pages clear STUB_TEXT_CHARS
# easily (bendingspoons: 1,034 chars of "Filters / All departments / Any
# locations") so js_shell never fires, yet a render is exactly what surfaces the
# listings when no SSR JSON was minable.
SHELL_CHROME_TEXT_CAP = 2500  # real listing pages are bigger; chrome shells are not
SHELL_CHROME_FILTER_RE = re.compile(
    r"\b(?:filters?|clear (?:all )?filters|all departments?|all teams?|"
    r"all locations?|any locations?|all offices?|all contract types?|"
    r"all job types?|all categories|show only)\b",
    re.I,
)
# A served zero-state ("No open jobs with these properties.") contains strong
# careers vocabulary ("open jobs") — strip it before judging the text strong,
# or every zero-state chrome shell would read as already-surfaced listings.
ZERO_STATE_JOBS_RE = re.compile(
    r"\b(?:no|zero|0)\s+(?:current\s+|open\s+)?"
    r"(?:jobs?|roles?|positions?|openings?|vacancies|results?)\b",
    re.I,
)
# Mirrors run.py's BROWSER_RESCUE_STRONG_CAREERS_RE (kept local: cascade must not
# import the orchestrator). Update both together.
STRONG_CAREERS_TEXT_RE = re.compile(
    r"\b(open roles?|open positions?|job openings?|current openings?|open jobs?|"
    r"job listings?|job vacancies|vacancies|greenhouse|lever|ashby|workday|"
    r"smartrecruiters|full[- ]time|part[- ]time|employment type)\b|採用|募集要項|職種",
    re.I,
)
JOB_DETAIL_ANCHOR_RE = re.compile(
    r"href=[\"'][^\"']*(?:/job/|/jobs/[^\"'?#/]|/positions?/[^\"'?#]|"
    r"/openings?/[^\"'?#]|/vacanc|gh_jid=|/apply(?:[\"'?/]|\b))",
    re.I,
)


def looks_like_chrome_shell(html, text):
    """Careers filter-chrome shell: enough text to dodge js_shell, ≥2 distinct
    filter-UI phrases, no job-detail anchors in the markup, and no strong careers
    evidence once the zero-state phrasing is stripped. Render-routing only."""
    t = (text or "").strip()
    if not t or len(t) < STUB_TEXT_CHARS or len(t) >= SHELL_CHROME_TEXT_CAP:
        return False
    hits = {m.lower() for m in SHELL_CHROME_FILTER_RE.findall(t)}
    if len(hits) < 2:
        return False
    if JOB_DETAIL_ANCHOR_RE.search(html or ""):
        return False
    if STRONG_CAREERS_TEXT_RE.search(ZERO_STATE_JOBS_RE.sub(" ", t)):
        return False
    return True


def render_hint_for(html, text, homepage_text=None, page_type=None):
    """Routing diagnosis of a stored fetch: 'stub' when the page text just mirrors
    the homepage (nav shell served for every path), 'js_shell' when the markup is
    a client-side app with thin text, 'shell_chrome' when a careers page is only
    filter chrome, else None. Overwritten on every re-fetch so the hint always
    describes the LAST stored attempt."""
    t = (text or "").strip()
    if homepage_text and t and t == (homepage_text or "").strip():
        return "stub"
    if looks_like_js_shell(html, text):
        return "js_shell"
    if (page_type or "") == "careers" and looks_like_chrome_shell(html, text):
        return "shell_chrome"
    return None


def _curl_cffi_fetch(domain, timeout):
    """Browser-TLS-impersonating fallback for domains httpx can't fetch (TLS-fingerprint
    gates). Sync; callers run it off the event loop. Returns an ok dict or None."""
    try:
        from curl_cffi import requests as _creq
    except ImportError:
        return None
    saw_response = False
    for url in _domain_url_candidates(domain):
        if _is_www_variant(url, domain) and saw_response:
            break  # apex answered; www won't change a settled answer
        try:
            r = _creq.get(url, impersonate="chrome", timeout=timeout, allow_redirects=True)
            saw_response = True
            html = r.text or ""
            if (r.status_code == 200 and len(html) >= MIN_OK_HTML
                    and not is_soft_block(r.status_code, r.headers, html)):
                return {"domain": domain, "url": str(r.url), "status": r.status_code,
                        "html": html, "ok": True}
        except Exception:
            continue
    return None


def _curl_cffi_fetch_url(url, timeout):
    """Page-level analog of _curl_cffi_fetch: browser-TLS fetch of one explicit
    URL. Returns {'url','status','html','ok'} only when it got real content,
    else None (the caller keeps its own last result)."""
    try:
        from curl_cffi import requests as _creq
    except ImportError:
        return None
    try:
        r = _creq.get(url, impersonate="chrome", timeout=timeout, allow_redirects=True)
        html = r.text or ""
        if (r.status_code == 200 and len(html) >= MIN_OK_HTML
                and not is_soft_block(r.status_code, r.headers, html)):
            return {"url": str(r.url), "status": r.status_code, "html": html, "ok": True}
    except Exception:
        pass
    return None


JINA_READER_BASE = "https://r.jina.ai/"
JINA_TIMEOUT = 30.0


def _jina_fetch_url(url, timeout=JINA_TIMEOUT):
    """Free, keyless render tier via Jina Reader (r.jina.ai). GETs the reader-mode
    rendering of one explicit URL and returns clean TEXT (markdown), not html.

    Jina renders JS server-side, so this rescues client-side-app / soft-blocked pages
    that httpx and curl_cffi can't read -- for the cost of one HTTP GET, no local
    browser, no API key. It is a THIRD-PARTY PROXY: only ever send public URLs through
    it (never an internal/authenticated URL), and the keyless endpoint is rate-limited
    (HTTP 429), so callers fire it on the small post-Tier-1 residue, not the bulk pass.
    A 429 (or any non-200) just returns None -> the next live tier picks the domain up.

    Returns {'url','status','text','html','ok'} on real content, else None. 'text' is
    the reader output stored verbatim -- do NOT re-run html_to_text on it (it is already
    clean text, and html2text would mangle the markdown)."""
    try:
        import httpx
    except ImportError:
        return None
    try:
        r = httpx.get(
            JINA_READER_BASE + url,
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": DEFAULT_UA,
                "Accept": "text/plain",
                "X-Return-Format": "markdown",
            },
        )
    except Exception:
        return None
    if r.status_code != 200:
        return None
    text = r.text or ""
    if len(text) < MIN_OK_TEXT_JINA:
        return None
    # Jina can return a short failure body WITH a 200 (upstream 4xx/5xx, or its own
    # notice); reject the obvious ones so a non-page isn't recorded as real content.
    low = text[:400].lstrip().lower()
    if low.startswith(("error", "warning: target url", "failed")) or "no content available" in low:
        return None
    return {"url": url, "status": 200, "text": text, "html": "", "ok": True}


def _jina_fetch(domain, timeout=JINA_TIMEOUT):
    """Domain-level Jina Reader fetch (apex over https). Jina follows redirects itself,
    so the www/http candidate sweep the httpx/curl tiers need is unnecessary here.
    Returns the same dict as _jina_fetch_url, or None."""
    return _jina_fetch_url("https://" + domain, timeout=timeout)


def _warrants_browser_fallback(status, html):
    """Only escalate to the curl_cffi browser fallback for failures it can plausibly
    fix: connection/TLS errors (status 0), explicit blocks (403/429), or a soft-block
    interstitial. A 404/410 or a thin-but-real 200 will not be fixed by a browser TLS
    fingerprint, so skip it -- firing curl on every miss is a perf hazard at scale."""
    st = status or 0
    if st in (0, 403, 429):
        return True
    return st == 200 and is_soft_block(st, {}, html or "")


def _is_dns_failure(exc):
    """True when a fetch exception is a DNS-resolution failure (getaddrinfo), not a
    TLS/connection error. curl_cffi shares the resolver, so a pure DNS miss won't be
    rescued by it -- but a TLS-fingerprint failure often IS, so the two must not be
    lumped together."""
    seen, cur = 0, exc
    while cur is not None and seen < 6:
        if isinstance(cur, socket.gaierror):
            return True
        m = str(cur).lower()
        if ("getaddrinfo" in m or "name or service not known" in m
                or "nodename nor servname" in m
                or "temporary failure in name resolution" in m):
            return True
        # Follow explicit chaining; stop at `raise X from None` (suppressed
        # context) so an unrelated DNS error left in __context__ can't leak in.
        if cur.__cause__ is not None:
            cur = cur.__cause__
        elif not getattr(cur, "__suppress_context__", False):
            cur = cur.__context__
        else:
            break
        seen += 1
    return False


async def _fetch_one_httpx(client, domain, sem, retries=2, timeout=20.0):
    async with sem:
        # Reject malformed domain strings before they reach the TCP/TLS layer.
        # A domain like 'sermon%20index=0%20key=url' (URL-encoded junk, decoded:
        # 'sermon index=0 key=url') is not a plausible hostname and will cause
        # ip_address() inside the network stack to raise ValueError.
        # A "www." prefix is itself a plausible host, so validate the domain as-is;
        # str.lstrip("www.") would strip a char-set (any leading w/.), not the prefix.
        if not _is_plausible_host(domain):
            return {"domain": domain, "url": "https://" + domain, "status": 0,
                    "html": "", "ok": False}
        last = {"domain": domain, "url": "https://" + domain, "status": 0, "html": "", "ok": False}
        saw_response = False  # any real HTTP answer from a prior candidate
        non_dns_failure = False  # a non-DNS error (TLS/conn) curl_cffi might still fix
        for url in _domain_url_candidates(domain):
            if _is_www_variant(url, domain) and saw_response:
                break  # apex answered (even 404/403); www only rescues dead-apex DNS
            for attempt in range(retries + 1):
                try:
                    r = await client.get(url)
                    saw_response = True
                    html = r.text or ""
                    ok = (r.status_code == 200 and len(html) >= MIN_OK_HTML
                          and not is_soft_block(r.status_code, r.headers, html))
                    last = {"domain": domain, "url": str(r.url), "status": r.status_code,
                            "html": html, "ok": ok}
                    if ok:
                        return last
                    # retry the same candidate only on a transient status; otherwise it
                    # is a settled answer (404/410/etc.) -> fall through to the next.
                    if r.status_code in RETRYABLE_STATUS and attempt < retries:
                        await asyncio.sleep(_backoff(attempt))
                        continue
                    break
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception as e:
                    if not _is_dns_failure(e):
                        non_dns_failure = True  # TLS/conn error -> curl_cffi may still rescue
                    if attempt < retries:
                        await asyncio.sleep(_backoff(attempt))
                        continue
                    break  # exhausted this candidate -> try the next
        # httpx couldn't get real content (TLS-fingerprint gate, soft block, or error).
        # Try a browser-TLS-impersonating fetch before giving up -- only on failure, so
        # the common path pays nothing. But skip it when EVERY candidate failed at DNS
        # resolution: curl shares the resolver, so it would only re-fail (and burn proxy
        # GB doing it). A TLS/connection failure still escalates -- curl's impersonation
        # is exactly the fix for TLS-fingerprint blocks.
        if (_warrants_browser_fallback(last.get("status"), last.get("html", ""))
                and (saw_response or non_dns_failure)):
            try:
                rescued = await asyncio.get_running_loop().run_in_executor(
                    None, _curl_cffi_fetch, domain, timeout
                )
            except Exception:
                rescued = None
            if rescued:
                return rescued
        return last


async def _fetch_batch_httpx(domains, concurrency, timeout):
    sem = asyncio.Semaphore(concurrency)
    client = _make_client(timeout, concurrency)
    async with client:
        # return_exceptions=True: a per-domain unhandled exception is returned as
        # the result value rather than propagating and aborting the whole batch.
        # The filter below replaces any exception result with an ok=False sentinel
        # so callers always receive a plain list of dicts.
        raw = await asyncio.gather(
            *[_fetch_one_httpx(client, d, sem, timeout=timeout) for d in domains],
            return_exceptions=True,
        )
    results = []
    for d, item in zip(domains, raw):
        if isinstance(item, BaseException):
            import sys as _sys
            print(
                "[scrape-cascade] domain %s raised %s in fetch tier: %s" % (d, type(item).__name__, item),
                file=_sys.stderr,
            )
            results.append({"domain": d, "url": "https://" + d, "status": 0, "html": "", "ok": False})
        else:
            results.append(item)
    return results


def fetch_batch_httpx(domains, concurrency=50, timeout=20.0):
    """Fetch ONE chunk concurrently. The caller (run.py) drives this in bounded chunks
    and persists each before requesting the next, so peak memory is one chunk of HTML
    -- never the whole list -- and a crash loses at most the in-flight chunk."""
    return asyncio.run(_fetch_batch_httpx(domains, concurrency, timeout))


async def _fetch_one_page_httpx(client, target, sem, retries=1, timeout=20.0):
    async with sem:
        domain = target["domain"]
        explicit_url = target.get("url") if re.match(r"^https?://", str(target.get("url") or ""), flags=re.I) else None
        path = normalize_page_key(target.get("path") or explicit_url or "/")
        page_type = target.get("page_type") or page_type_for_path(path)
        # Validate the target host before building URLs.  A malformed URL like
        # 'https://sermon%20index=0%20key=url' (hostname contains percent-encoded
        # spaces / query-string junk) must be skipped here — passing it to
        # client.get() can raise an uncaught ValueError from ip_address() inside
        # the network stack and abort the entire asyncio.gather() batch.
        _target_host = _host(explicit_url or domain)
        if not _is_plausible_host(_target_host):
            return {
                "domain": domain,
                "path": path,
                "page_type": page_type,
                "url": explicit_url or ("https://" + domain + normalize_path(path)),
                "status": 0,
                "html": "",
                "ok": False,
                "linked_from_homepage": bool(target.get("linked_from_homepage")),
            }
        last = {
            "domain": domain,
            "path": path,
            "page_type": page_type,
            "url": explicit_url or ("https://" + domain + normalize_path(path)),
            "status": 0,
            "html": "",
            "ok": False,
            "linked_from_homepage": bool(target.get("linked_from_homepage")),
        }
        if explicit_url:
            urls = [explicit_url]
        else:
            # same host x scheme matrix as the homepage tier; www variants are
            # attempted only while no prior candidate produced a real response.
            p = normalize_path(path)
            urls = [base + p for base in _domain_url_candidates(domain)]
        saw_response = False
        for url in urls:
            if not explicit_url and _is_www_variant(url, domain) and saw_response:
                break
            for attempt in range(retries + 1):
                try:
                    r = await client.get(url)
                    saw_response = True
                    html = r.text or ""
                    ok = r.status_code == 200 and len(html) >= MIN_OK_HTML
                    last = {
                        "domain": domain,
                        "path": path,
                        "page_type": page_type,
                        "url": str(r.url),
                        "status": r.status_code,
                        "html": html,
                        "ok": ok,
                        "linked_from_homepage": bool(target.get("linked_from_homepage")),
                    }
                    if ok:
                        break
                    if r.status_code in RETRYABLE_STATUS and attempt < retries:
                        await asyncio.sleep(_backoff(attempt))
                        continue
                    break
                except Exception:
                    if attempt < retries:
                        await asyncio.sleep(_backoff(attempt))
                        continue
                    break
            if last["ok"]:
                break
        # Escalate to the browser-TLS fetch for failures it can plausibly fix
        # (same gate as the homepage tier: 0/403/429/soft-block), PLUS the
        # careers-stub class: a 200 with big-enough HTML whose extracted text is
        # thinner than a real page (httpx got a server-side stub; a browser UA
        # gets the real board -- the datapoem.com pattern). This is a better-fetch
        # retry, never a block label, so soft-block detection stays high-precision.
        wants_rescue = _warrants_browser_fallback(last.get("status"), last.get("html", ""))
        careers_stub = (
            not wants_rescue
            and page_type == "careers"
            and last.get("ok")
            and _extracted_text_len(last.get("html", "")) < STUB_TEXT_CHARS
        )
        if wants_rescue or careers_stub:
            try:
                rescued = await asyncio.get_running_loop().run_in_executor(
                    None, _curl_cffi_fetch_url, last["url"], timeout
                )
            except Exception:
                rescued = None
            if rescued and (
                not last.get("ok")
                or _extracted_text_len(rescued.get("html", "")) > _extracted_text_len(last.get("html", ""))
            ):
                last = dict(
                    last, url=rescued["url"], status=rescued["status"],
                    html=rescued["html"], ok=True,
                )
        return last


async def _fetch_batch_pages_httpx(targets, concurrency, timeout):
    sem = asyncio.Semaphore(concurrency)
    client = _make_client(timeout, concurrency)
    async with client:
        # return_exceptions=True: an unexpected exception from one page target is
        # returned as the result value instead of aborting the whole batch.  This
        # mirrors the homepage batch; the filter below converts exception results
        # to ok=False dicts so callers always see a uniform list.
        raw = await asyncio.gather(
            *[_fetch_one_page_httpx(client, t, sem, timeout=timeout) for t in targets],
            return_exceptions=True,
        )
    results = []
    for t, item in zip(targets, raw):
        if isinstance(item, BaseException):
            import sys as _sys
            domain = t.get("domain", "?")
            path = t.get("path") or t.get("url") or "?"
            page_type = t.get("page_type") or "other"
            print(
                "[scrape-cascade] %s/%s raised %s in page-fetch tier: %s"
                % (domain, path, type(item).__name__, item),
                file=_sys.stderr,
            )
            results.append({
                "domain": domain,
                "path": normalize_page_key(path),
                "page_type": page_type,
                "url": t.get("url") or ("https://" + domain),
                "status": 0,
                "html": "",
                "ok": False,
                "linked_from_homepage": bool(t.get("linked_from_homepage")),
            })
        else:
            results.append(item)
    return results


def fetch_batch_pages_httpx(targets, concurrency=50, timeout=20.0):
    return asyncio.run(_fetch_batch_pages_httpx(targets, concurrency, timeout))


def _target_url_candidates(target):
    domain = target["domain"]
    explicit_url = None
    for value in (target.get("url"), target.get("path")):
        if re.match(r"^https?://", str(value or ""), flags=re.I):
            explicit_url = str(value)
            break
    if explicit_url:
        return [explicit_url]
    path = normalize_path(target.get("path") or "/")
    return [scheme + domain + path for scheme in ("https://", "http://")]


def html_to_text(html):
    if not html:
        return ""
    try:
        import html2text

        h = html2text.HTML2Text()
        h.ignore_links = True
        h.ignore_images = True
        h.ignore_emphasis = True
        h.body_width = 0
        text = h.handle(html)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)  # crude tag strip fallback
    embedded_source = "\n".join([html or "", (html or "").replace("\\/", "/")])
    ats_urls = sorted({html_unescape(url.rstrip("\\")) for url in EMBEDDED_ATS_URL_RE.findall(embedded_source)})
    if ats_urls:
        text = (text or "").rstrip() + "\n\nEmbedded ATS job links:\n" + "\n".join(ats_urls) + "\n"
    return text


# -------------------------------------------------- tier 2/3: browser rescue
def _settle_playwright_page(page, per_page_wait=1500):
    try:
        page.wait_for_load_state(
            "networkidle",
            timeout=min(max(per_page_wait * 3, 1000), 5000),
        )
    except Exception:
        pass
    try:
        page.wait_for_timeout(per_page_wait)
    except Exception:
        pass
    try:
        page.evaluate(
            """async (stepWait) => {
                const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
                const height = Math.max(
                    document.body ? document.body.scrollHeight : 0,
                    document.documentElement ? document.documentElement.scrollHeight : 0
                );
                const viewport = Math.max(window.innerHeight || 800, 500);
                const steps = Math.min(10, Math.max(2, Math.ceil(height / viewport)));
                for (let i = 1; i <= steps; i += 1) {
                    window.scrollTo(0, Math.floor(height * i / steps));
                    await delay(stepWait);
                }
                window.scrollTo(0, 0);
                await delay(stepWait);
            }""",
            min(max(per_page_wait // 4, 150), 500),
        )
    except Exception:
        pass
    try:
        page.wait_for_timeout(min(max(per_page_wait // 2, 500), 1500))
    except Exception:
        pass


def _click_playwright_job_controls(page, per_page_wait=1500, max_clicks=10):
    """Open rendered careers controls that hide job boards behind JS.

    This is intentionally scoped to job-ish controls. It handles common no-href
    buttons, accordions, dropdowns, and listing cards without turning the page pass
    into a general crawler.
    """
    try:
        clicked = page.evaluate(
            """async ({wait, maxClicks}) => {
                const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
                const jobish = /(open roles?|open positions?|current openings?|job openings?|open jobs?|view jobs?|view openings?|see jobs?|see open roles?|all jobs|browse jobs?|job listings?|vacancies|positions?|apply|departments?|locations?)/i;
                const noise = /(book a demo|sign in|login|newsletter|cookie|privacy|terms|case stud(?:y|ies)|talent acquisition|career sites?|candidate experience|customer stor(?:y|ies))/i;
                const visible = el => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 4 && rect.height > 4;
                };
                window.__scrapeCascadeOpenedUrls = window.__scrapeCascadeOpenedUrls || [];
                if (!window.__scrapeCascadeOpenPatched) {
                    const originalOpen = window.open;
                    window.open = function(url) {
                        try {
                            if (url) window.__scrapeCascadeOpenedUrls.push(new URL(String(url), window.location.href).href);
                        } catch (err) {}
                        return null;
                    };
                    window.__scrapeCascadeOpenPatched = true;
                    window.__scrapeCascadeOriginalOpen = originalOpen;
                }
                document.querySelectorAll('a[target="_blank"]').forEach(a => { a.target = '_self'; });
                const selectors = [
                    'button',
                    '[role="button"]',
                    'summary',
                    '[aria-controls]',
                    '[aria-expanded]',
                    '[data-toggle]',
                    '[data-tab]',
                    '[data-testid]',
                    '[onclick]',
                    'a[href^="#"]',
                    'a[href^="javascript:"]',
                    '[class*="accordion"]',
                    '[class*="opening"]',
                    '[class*="position"]',
                    '[class*="jobs"]',
                    '[class*="job-card"]'
                ].join(',');
                const candidates = [];
                for (const el of Array.from(document.querySelectorAll(selectors))) {
                    if (!visible(el)) continue;
                    const text = [
                        el.innerText || '',
                        el.textContent || '',
                        el.getAttribute('aria-label') || '',
                        el.getAttribute('title') || '',
                        el.getAttribute('href') || '',
                        el.getAttribute('class') || '',
                        el.getAttribute('data-testid') || '',
                        el.getAttribute('data-toggle') || '',
                        el.getAttribute('data-tab') || ''
                    ].join(' ').replace(/\\s+/g, ' ').trim();
                    if (!text || noise.test(text) || !jobish.test(text)) continue;
                    const rect = el.getBoundingClientRect();
                    candidates.push({el, top: rect.top, text});
                }
                candidates.sort((a, b) => a.top - b.top);
                const clicked = [];
                const seen = new Set();
                for (const item of candidates) {
                    if (clicked.length >= maxClicks) break;
                    const key = item.text.slice(0, 160).toLowerCase();
                    if (seen.has(key)) continue;
                    seen.add(key);
                    try {
                        item.el.scrollIntoView({block: 'center', inline: 'center'});
                        await delay(Math.max(100, Math.floor(wait / 4)));
                        item.el.click();
                        clicked.push(item.text.slice(0, 180));
                        await delay(wait);
                    } catch (err) {}
                }
                return clicked;
            }""",
            {"wait": min(max(per_page_wait // 2, 350), 1200), "maxClicks": max_clicks},
        )
    except Exception:
        clicked = []
    try:
        page.wait_for_load_state("domcontentloaded", timeout=min(max(per_page_wait * 2, 1000), 5000))
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=min(max(per_page_wait * 3, 1500), 6000))
    except Exception:
        pass
    try:
        page.wait_for_timeout(min(max(per_page_wait // 2, 500), 1500))
    except Exception:
        pass
    return clicked or []


def _opened_job_links_html(page):
    """Snapshot job-board URLs opened via window.open during click rescue."""
    try:
        urls = page.evaluate("""() => Array.from(new Set(window.__scrapeCascadeOpenedUrls || []))""")
    except Exception:
        urls = []
    rows = []
    seen = set()
    for url in urls or []:
        url = str(url or "").strip()
        if not url or url in seen:
            continue
        if _is_ignored_link(url) or _bad_ats_utility_url(url):
            continue
        host = _host(url)
        is_ats = any(_host_matches_hint(host, hint) for hint in ATS_HOST_HINTS)
        is_careers_path = page_type_for_path(urlparse(url).path or "") == "careers"
        if not is_ats and not is_careers_path and not JOB_DISCOVERY_CONTROL_RE.search(url):
            continue
        seen.add(url)
        rows.append(f'<a href="{html_escape(url, quote=True)}">Opened job board</a>')
    if not rows:
        return ""
    return "\n<!-- opened job links -->\n" + "\n".join(rows) + "\n"


def _rendered_job_links_html(page):
    """Snapshot job links that only exist in the rendered DOM after clicks."""
    try:
        links = page.evaluate(
            """() => {
                const attrs = [
                    'href', 'src', 'data-url', 'data-href', 'data-src',
                    'data-iframe-src', 'data-apply-url', 'data-job-board',
                    'data-board-url', 'data-careers-url', 'data-job-url',
                    'ng-href', 'onclick'
                ];
                const nodes = Array.from(document.querySelectorAll('a, iframe, script, [onclick], [data-url], [data-href], [data-src], [data-iframe-src], [data-apply-url], [data-job-board], [data-board-url], [data-careers-url], [data-job-url], [ng-href]'));
                const out = [];
                const add = (url, text) => {
                    if (!url) return;
                    const raw = String(url).trim();
                    if (!raw || raw.startsWith('#') || raw.startsWith('mailto:') || raw.startsWith('tel:')) return;
                    let resolved = raw;
                    try {
                        if (/^(https?:)?\\/\\//i.test(raw) || raw.startsWith('/')) {
                            resolved = new URL(raw, window.location.href).href;
                        }
                    } catch (err) {}
                    out.push({url: resolved, text: String(text || '').replace(/\\s+/g, ' ').trim()});
                };
                for (const el of nodes) {
                    const text = [
                        el.innerText || '',
                        el.textContent || '',
                        el.getAttribute('aria-label') || '',
                        el.getAttribute('title') || ''
                    ].join(' ');
                    for (const attr of attrs) add(el.getAttribute(attr), text);
                }
                return out.slice(0, 250);
            }"""
        )
    except Exception:
        links = []
    rows = []
    seen = set()
    for link in links or []:
        url = str((link or {}).get("url") or "").strip()
        text = str((link or {}).get("text") or "").strip() or url
        if not url or url in seen:
            continue
        if _is_ignored_link(url) or _bad_ats_utility_url(url):
            continue
        joined = " ".join([url, text])
        host = _host(url)
        is_ats = any(_host_matches_hint(host, hint) for hint in ATS_HOST_HINTS)
        is_careers_path = page_type_for_path(urlparse(url).path or "") == "careers"
        if not is_ats and not is_careers_path and not JOB_DISCOVERY_CONTROL_RE.search(joined):
            continue
        seen.add(url)
        rows.append(f'<a href="{html_escape(url, quote=True)}">{html_escape(text)}</a>')
    if not rows:
        return ""
    return "\n<!-- rendered job links -->\n" + "\n".join(rows) + "\n"


_MAX_BROWSER_RELAUNCHES = 2


def _install_asset_blocking(context):
    """Abort image/media/font requests in the rescue browser context. Pure
    bandwidth savings: we read page.content() (DOM HTML) + JS-rendered links, not
    assets. script/xhr/fetch/stylesheet are NOT blocked -- SPA boards need them."""
    def _route(route):
        # Every intercepted request must be handled exactly once or page.goto()
        # hangs until timeout. On any Playwright error (page closing mid-flight,
        # route already handled) make one best-effort abort to release the request,
        # then give up -- never leave it pending.
        rtype = route.request.resource_type
        try:
            if rtype in BLOCKED_RESOURCE_TYPES:
                route.abort()
            else:
                route.continue_()
            return
        except Exception:
            pass
        try:
            route.abort()
        except Exception:
            pass
    context.route("**/*", _route)


def _run_browser_batch(items, render_one):
    """Drive a Playwright batch with crash-resilient relaunches.

    A mid-batch browser/context death (chromium OOM, transport closed) used to
    raise out of the generator and strand every remaining item -- sdr500_r1: 65
    of 81 failed homepages never received their render. Here a death is caught,
    the crashing item yields None (the caller maps it to an empty result), the
    browser is relaunched (capped), and iteration continues. When relaunches are
    exhausted the remainder yields None instead of raising.
    """
    from playwright.sync_api import sync_playwright

    items = list(items)
    idx, relaunches = 0, 0
    with sync_playwright() as p:
        browser, context = None, None
        while idx < len(items):
            if context is None:
                if relaunches > _MAX_BROWSER_RELAUNCHES:
                    break
                try:
                    _pw_proxy = _proxy_playwright(_proxy_url())  # off-IP rescue when configured
                    browser = p.chromium.launch(
                        headless=True,
                        **({"proxy": _pw_proxy} if _pw_proxy else {}),
                    )
                    context = browser.new_context(user_agent=DEFAULT_UA)
                    if BLOCK_BROWSER_ASSETS:
                        _install_asset_blocking(context)
                except Exception:
                    try:
                        if browser is not None:
                            browser.close()  # launch ok but context/route failed: don't leak it
                    except Exception:
                        pass
                    relaunches += 1
                    browser, context = None, None
                    continue
            item = items[idx]
            try:
                result = render_one(context, item)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                # browser-level death: scrap this browser, charge the crasher its
                # result (None), and continue the batch on a fresh launch.
                try:
                    browser.close()
                except Exception:
                    pass
                browser, context = None, None
                relaunches += 1
                idx += 1
                yield item, None
                continue
            idx += 1
            yield item, result
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        for item in items[idx:]:
            yield item, None  # relaunch budget exhausted: explicit empties, not a strand


def _render_homepage(context, domain, timeout=30, per_page_wait=1500):
    """Render one domain's homepage in an existing context. Per-URL navigation
    errors are swallowed (try the next candidate); a dead page/browser re-raises
    so the batch driver can relaunch."""
    html, status = "", 0
    page = context.new_page()
    try:
        for url in ("https://" + domain, "http://" + domain):
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                _settle_playwright_page(page, per_page_wait=per_page_wait)
                html = page.content() or ""
                status = resp.status if resp else 0
                if len(html) >= MIN_OK_HTML:
                    break
            except Exception:
                if page.is_closed():
                    raise  # browser death, not a nav failure -> driver relaunches
                continue
    finally:
        try:
            page.close()
        except Exception:
            pass
    return {"html": html, "status": status}


def rescue_playwright_batch(domains, timeout=30, per_page_wait=1500):
    """Tier 2: real Chromium render for JS-heavy / soft-blocked pages.

    One browser reused across the batch (a fresh page per domain), yielding
    (domain, {html,status}) incrementally; mid-batch browser deaths relaunch
    instead of stranding the queue (see _run_browser_batch).
    """
    def render(context, domain):
        return _render_homepage(context, domain, timeout=timeout, per_page_wait=per_page_wait)

    for domain, res in _run_browser_batch(domains, render):
        yield domain, (res or {"html": "", "status": 0})


def fetch_playwright(domain, timeout=30):
    """Single-domain Tier 2 convenience wrapper over the reusing batch generator."""
    for _d, res in rescue_playwright_batch([domain], timeout=timeout):
        return res
    return {"html": "", "status": 0}


def _empty_page_result(target):
    explicit_key = target.get("url") or target.get("path") or "/"
    path = normalize_page_key(explicit_key)
    page_type = target.get("page_type") or page_type_for_path(path)
    cands = _target_url_candidates(target)
    return {
        "domain": target["domain"],
        "path": path,
        "page_type": page_type,
        "url": cands[0] if cands else "",
        "status": 0,
        "html": "",
        "ok": False,
        "linked_from_homepage": bool(target.get("linked_from_homepage")),
    }


def _render_one_page(context, target, timeout=30, per_page_wait=1500):
    """Render one discovered-page target in an existing context. Per-URL nav
    errors are swallowed; a dead page/browser re-raises for the driver."""
    domain = target["domain"]
    explicit_key = target.get("url") or target.get("path") or "/"
    path = normalize_page_key(explicit_key)
    page_type = target.get("page_type") or page_type_for_path(path)
    html, status, final_url = "", 0, ""
    page = context.new_page()
    try:
        for url in _target_url_candidates(target):
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                _settle_playwright_page(page, per_page_wait=per_page_wait)
                if page_type == "careers":
                    _click_playwright_job_controls(page, per_page_wait=per_page_wait)
                    _settle_playwright_page(page, per_page_wait=per_page_wait)
                html = (page.content() or "") + _rendered_job_links_html(page) + _opened_job_links_html(page)
                status = resp.status if resp else 0
                final_url = page.url or url
                if len(html) >= MIN_OK_HTML:
                    break
            except Exception:
                if page.is_closed():
                    raise  # browser death, not a nav failure -> driver relaunches
                continue
    finally:
        try:
            page.close()
        except Exception:
            pass
    return {
        "domain": domain,
        "path": path,
        "page_type": page_type,
        "url": final_url or (_target_url_candidates(target)[0] if _target_url_candidates(target) else ""),
        "status": status,
        "html": html,
        "ok": len(html) >= MIN_OK_HTML,
        "linked_from_homepage": bool(target.get("linked_from_homepage")),
    }


def rescue_playwright_page_batch(targets, timeout=30, per_page_wait=1500):
    """Tier 2 page render for JS/lazy-loaded discovered pages.

    Homepage rescue only catches empty domains. Careers pages often return valid
    static HTML while their job listings mount after rendering or scroll, so this
    page-level pass is opt-in from run.py and intended for narrow high-value paths.
    Mid-batch browser deaths relaunch instead of stranding the queue.
    """
    def render(context, target):
        return _render_one_page(context, target, timeout=timeout, per_page_wait=per_page_wait)

    for target, res in _run_browser_batch(targets, render):
        yield res if res is not None else _empty_page_result(target)


def fetch_undetected(domain, timeout=30):
    """Tier 3: last-resort anti-bot bypass via undetected-chromedriver.

    Best-effort: uc is sensitive to the local Chrome version and can fail to launch.
    Any import/launch/driver error degrades to an empty result (domain -> unreachable)
    rather than raising, so one bad domain never kills a bulk run. Kept per-domain
    (fresh driver each) on purpose: it's the last-resort residue and a poisoned driver
    shouldn't cascade across domains.
    """
    try:
        import undetected_chromedriver as uc
    except Exception:
        return {"html": "", "status": 0}

    opts = uc.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    driver, html = None, ""
    try:
        driver = uc.Chrome(options=opts)
        driver.set_page_load_timeout(timeout)
        for scheme in ("https://", "http://"):
            try:
                driver.get(scheme + domain)
                html = driver.page_source or ""
                if len(html) >= MIN_OK_HTML:
                    break
            except Exception:
                continue
    except Exception:
        html = ""  # uc launch / version mismatch -> graceful empty
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
    return {"html": html, "status": 0}


# Child script for the isolated Camoufox fetch. Runs in its OWN process so a driver
# crash/hang can be killed by the parent without taking the batch down. Emits a single
# sentinel-prefixed JSON line on stdout; all browser noise goes to a discarded stderr.
_CAMOUFOX_CHILD_SRC = r'''
import json, os, sys
from urllib.parse import urlsplit, unquote
domain = sys.argv[1]
timeout = float(sys.argv[2])

def _proxy():
    # Tier 3 prefers the stealth gateway, falls back to the bulk gateway; inherited
    # from the parent process env. Raises on a malformed URL -> the child errors to
    # empty (fail-closed: the browser is never launched un-proxied when one was set).
    url = (os.environ.get("SCRAPE_CASCADE_PROXY_URL_STEALTH")
           or os.environ.get("SCRAPE_PROXY_URL_STEALTH")
           or os.environ.get("SCRAPE_CASCADE_PROXY_URL")
           or os.environ.get("SCRAPE_PROXY_URL"))
    if not url or not url.strip():
        return None
    p = urlsplit(url.strip())
    if not p.scheme or not p.hostname:
        raise ValueError("bad proxy url")
    server = "%s://%s" % (p.scheme, p.hostname)
    if p.port:
        server = "%s:%d" % (server, p.port)
    out = {"server": server}
    if p.username: out["username"] = unquote(p.username)
    if p.password: out["password"] = unquote(p.password)
    return out

html, status, headers = "", 0, {}
try:
    from camoufox.sync_api import Camoufox
    _kw = {"headless": True}
    _px = _proxy()
    if _px:
        _kw["proxy"] = _px
    with Camoufox(**_kw) as browser:
        page = browser.new_page()
        for scheme in ("https://", "http://"):
            try:
                resp = page.goto(scheme + domain, timeout=int(timeout * 1000),
                                 wait_until="domcontentloaded")
                status = resp.status if resp else 0
                try:
                    headers = dict(resp.headers) if resp else {}
                except Exception:
                    headers = {}
                try:
                    page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass
                html = page.content() or ""
                if len(html) >= 500:
                    break
            except Exception:
                continue
except Exception:
    html, status, headers = "", 0, {}
sys.stdout.write("@@CFOX@@" + json.dumps({"html": html, "status": status, "headers": headers}))
sys.stdout.flush()
'''


def fetch_camoufox(domain, timeout=30):
    """Tier 3 (stealth): anti-bot bypass via Camoufox -- a hardened Firefox launched
    through Playwright. Ships its OWN browser binary (not managed system Chrome), so it
    launches in managed environments that block Chrome's remote-debugging (CDP) port
    and natively clears Cloudflare Turnstile/interstitials with no external solver.

    Runs in an ISOLATED subprocess with a hard wall-clock kill. The Playwright-Firefox
    driver can crash on a page that throws a malformed pageerror (observed live on a
    Cloudflare challenge) and then hang the Python client indefinitely -- which at scale
    would stall a whole batch on one bad domain. The child is its own process-group
    leader, so on timeout we SIGKILL the entire group (browser children included). Any
    crash/hang/error degrades to an empty result; one bad domain never kills the run.

    Returns {'html','status'} -- html only for a real, non-soft-block 200 (a 403 or
    challenge render is reported via status but never returned as content)."""
    import json
    import os
    import signal
    import subprocess
    import sys

    data = None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", _CAMOUFOX_CHILD_SRC, domain, str(timeout)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, start_new_session=True,
        )
        try:
            out, _ = proc.communicate(timeout=timeout + 20)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)  # nuke the browser tree
            except Exception:
                pass
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            return {"html": "", "status": 0}
        if out and "@@CFOX@@" in out:
            data = json.loads(out.split("@@CFOX@@", 1)[1].strip())
    except Exception:
        data = None

    if not data:
        return {"html": "", "status": 0}
    html = data.get("html") or ""
    status = data.get("status") or 0
    headers = data.get("headers") or {}
    if status == 200 and len(html) >= MIN_OK_HTML and not is_soft_block(status, headers, html):
        return {"html": html, "status": status}
    return {"html": "", "status": status}  # block/challenge/empty -> not real content


def fetch_stealth(domain, timeout=30):
    """Tier-3 dispatcher: prefer Camoufox (ships its own browser -> works under MDM,
    clears Cloudflare), fall back to undetected-chromedriver only if Camoufox is absent
    or comes back empty. Replaces the direct fetch_undetected call so existing
    --undetected / --stealth runs transparently get the working stealth tier even where
    uc can't launch (the Chrome-version-sensitivity failure on this Mac)."""
    got = fetch_camoufox(domain, timeout=timeout)
    if len((got or {}).get("html") or "") >= MIN_OK_HTML:
        return got
    legacy = fetch_undetected(domain, timeout=timeout)
    if len((legacy or {}).get("html") or "") >= MIN_OK_HTML:
        return legacy
    return got or {"html": "", "status": 0}


def doctor(check_browsers=True, check_jina=True, check_proxy=True):
    """Preflight that reports which fetch tiers are actually live on THIS machine,
    rather than discovering a dead tier mid-run (the classic uc-won't-launch-on-this-Mac
    silent degradation). Prints a table and returns {tier: (status, detail)}.

    status: OK | DOWN | MISSING | SKIP. Diagnostic only -- never raises."""
    import importlib
    import sys as _sys

    results = {}

    def _mark(name, status, detail=""):
        results[name] = (status, detail)

    _mark("python", "OK", _sys.version.split()[0])

    for mod, label in [
        ("httpx", "httpx (Tier 1)"),
        ("curl_cffi", "curl_cffi (TLS-fingerprint fallback)"),
        ("html2text", "html2text"),
        ("lxml", "lxml"),
        ("bs4", "beautifulsoup4"),
        ("yaml", "pyyaml"),
    ]:
        try:
            m = importlib.import_module(mod)
            _mark(label, "OK", str(getattr(m, "__version__", "")))
        except Exception as e:
            _mark(label, "MISSING", type(e).__name__)

    if check_browsers:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True)
                b.new_page().goto("about:blank")
                b.close()
            _mark("playwright/chromium (Tier 2)", "OK", "launch+nav ok")
        except Exception as e:
            _mark("playwright/chromium (Tier 2)", "DOWN", "%s: %s" % (type(e).__name__, str(e)[:80]))

        try:
            from camoufox.sync_api import Camoufox
            with Camoufox(headless=True) as browser:
                browser.new_page().goto("about:blank")
            _mark("camoufox (Tier 3 stealth)", "OK", "launch+nav ok (MDM-safe)")
        except Exception as e:
            _mark("camoufox (Tier 3 stealth)", "DOWN", "%s: %s" % (type(e).__name__, str(e)[:80]))

        try:
            import undetected_chromedriver as uc
            opts = uc.ChromeOptions()
            opts.add_argument("--headless=new")
            opts.add_argument("--no-sandbox")
            d = uc.Chrome(options=opts)
            d.quit()
            _mark("undetected-chromedriver (legacy Tier 3)", "OK", "launch ok")
        except Exception as e:
            _mark("undetected-chromedriver (legacy Tier 3)", "DOWN", "%s: %s" % (type(e).__name__, str(e)[:80]))
    else:
        for k in ("playwright/chromium (Tier 2)", "camoufox (Tier 3 stealth)",
                  "undetected-chromedriver (legacy Tier 3)"):
            _mark(k, "SKIP", "browser checks disabled")

    if check_jina:
        try:
            got = _jina_fetch_url("https://example.com", timeout=20.0)
            if got and got.get("ok"):
                _mark("jina reader (free render tier)", "OK", "r.jina.ai reachable")
            else:
                _mark("jina reader (free render tier)", "DOWN", "no content from r.jina.ai")
        except Exception as e:
            _mark("jina reader (free render tier)", "DOWN", type(e).__name__)
    else:
        _mark("jina reader (free render tier)", "SKIP", "jina check disabled")

    if check_proxy:
        stealth_set = bool(os.environ.get("SCRAPE_CASCADE_PROXY_URL_STEALTH")
                           or os.environ.get("SCRAPE_PROXY_URL_STEALTH"))
        bulk = _proxy_url()
        if not bulk and not stealth_set:
            _mark("proxy pool (IP-reputation lever)", "SKIP",
                  "not configured -- fetching direct from this host's IP")
        else:
            import httpx as _hx
            echo = "https://api.ipify.org"
            direct = "?"
            try:
                with _hx.Client(verify=False, timeout=10.0, follow_redirects=True) as c:
                    direct = c.get(echo).text.strip()
            except Exception:
                direct = "?"

            def _probe(label, url):
                # Confirms the gateway is reachable AND that traffic actually exits off-IP.
                # A DOWN here on the Mini is the "web filter is blocking the proxy itself"
                # signal -- distinct from being able to reach the vendor's signup page.
                try:
                    with _hx.Client(proxy=url, verify=False, timeout=25.0,
                                    follow_redirects=True) as c:
                        ip = c.get(echo).text.strip()
                    if ip and ip != direct:
                        _mark(label, "OK", "exit IP %s (host IP %s) -- routes off-IP" % (ip, direct))
                    elif ip and ip == direct:
                        _mark(label, "DOWN", "exit IP == host IP %s -- proxy NOT applied" % direct)
                    else:
                        _mark(label, "DOWN", "no IP returned via proxy")
                except Exception as e:
                    _mark(label, "DOWN", "%s: %s -- gateway blocked/unreachable "
                          "(web filter? egress port? TLS MITM?)" % (type(e).__name__, str(e)[:50]))

            if bulk:
                _probe("proxy pool: bulk (Tier 1/2)", bulk)
            if stealth_set:
                _probe("proxy pool: stealth (Tier 3)", _proxy_url(stealth=True))
    else:
        _mark("proxy pool (IP-reputation lever)", "SKIP", "proxy check disabled")

    name_w = max(len(k) for k in results)
    print("scrape-cascade doctor -- fetch-tier liveness on this machine")
    print("-" * (name_w + 22))
    for name, (status, detail) in results.items():
        print("  %-*s  %-7s  %s" % (name_w, name, status, detail))
    print("-" * (name_w + 22))
    down = [k for k, (s, _) in results.items() if s == "DOWN"]
    missing = [k for k, (s, _) in results.items() if s == "MISSING"]
    if down or missing:
        print("note: DOWN/MISSING tiers degrade gracefully -- the cascade falls through to "
              "the next live tier. Tier 3 prefers camoufox over undetected-chromedriver.")
    return results


# ----------------------------------------------------------------- scoring
def _count_hits(text, terms):
    total, found = 0, []
    for t in terms or []:
        t = str(t).strip().lower()
        if not t:
            continue
        if re.search(r"[^\x00-\x7f]", t):
            n = text.count(t)
        else:
            n = len(re.findall(r"\b" + re.escape(t) + r"\b", text))
        if n:
            total += n
            found.append(t)
    return total, found


def score_text(text, rubric):
    """Cheap deterministic verdict. Returns (label, confidence, detail).

    label == 'ambiguous' means the keyword pass had no clear signal -> escalate
    to the judge.
    """
    text_l = (text or "").lower()
    pos, pos_terms = _count_hits(text_l, rubric.get("positive", []))
    neg, neg_terms = _count_hits(text_l, rubric.get("negative", []))
    detail = {"pos_hits": pos, "neg_hits": neg, "pos_terms": pos_terms, "neg_terms": neg_terms}
    if pos == 0 and neg == 0:
        return "ambiguous", 0.0, detail
    net = pos - neg
    total = pos + neg
    confidence = abs(net) / total if total else 0.0
    threshold = float(rubric.get("confident_threshold", 0.6))
    # Thin evidence is not confidence: one stray keyword hit (net=1) must NOT skip
    # the judge just because there were no opposing hits. Require a minimum net.
    min_net = int(rubric.get("min_net_hits", 2))
    if abs(net) >= min_net and confidence >= threshold:
        label = rubric["positive_label"] if net > 0 else rubric["negative_label"]
        return label, round(confidence, 3), detail
    return "ambiguous", round(confidence, 3), detail


def _clean_snippet(text):
    text = (text or "").replace("\x00", " ")
    text = "".join(ch if ch in "\t\n\r" or ord(ch) >= 32 else " " for ch in text)
    return re.sub(r"\s+", " ", text).strip()


def evidence_snippet(text, terms, window=180):
    text = _clean_snippet(text)
    if not text:
        return ""
    lower = text.lower()
    for term in terms or []:
        term_l = str(term or "").strip().lower()
        if not term_l:
            continue
        idx = lower.find(term_l)
        if idx >= 0:
            start = max(0, idx - window)
            end = min(len(text), idx + len(term_l) + window)
            return text[start:end]
    return text[: window * 2]


def page_evidence(row, rubric, spec=None):
    """Return lightweight evidence metadata for a fetched page.

    This is discovery, not validation. A matched term means "send this page to the
    stricter extractor", not "accept this as score-ready evidence".
    """
    page_type = (spec or {}).get("page_type") or (row or {}).get("page_type") or "other"
    terms = []
    if spec:
        terms.extend(spec.get("evidence_terms") or [])
    terms.extend((rubric.get("page_evidence_terms") or {}).get(page_type, []))
    terms = list(dict.fromkeys(str(t) for t in terms if str(t or "").strip()))
    text = (row or {}).get("text") or ""
    match_count, matched_terms = _count_hits(text.lower(), terms)
    return {
        "match_count": match_count,
        "matched_terms": matched_terms,
        "snippet": evidence_snippet(text, matched_terms or terms),
    }


def domain_hygiene(domain, homepage_text):
    text = _clean_snippet(homepage_text).lower()
    if not text:
        return "unknown"
    personal_hits = sum(1 for t in (
        "download resume", "professional summary", "linkedin profile", "my resume",
        "curriculum vitae", "personal website", "portfolio of work",
    ) if t in text)
    business_hits = sum(1 for t in (
        "customers", "platform", "solutions", "company", "careers", "contact sales",
        "privacy policy", "terms of service",
    ) if t in text)
    if personal_hits >= 2 and business_hits < 3:
        return "personal_resume"
    consumer_hits = sum(1 for t in (
        "add to cart", "restaurant", "view menu", "wedding photography",
        "dental practice", "for sale by owner",
    ) if t in text)
    legal_practice_hit = "law firm" in text and not any(t in text for t in (
        "legal ai", "legal technology", "platform", "solutions", "customers",
        "contact sales", "security", "careers",
    ))
    if (consumer_hits or legal_practice_hit) and business_hits < 3:
        return "consumer_or_local"
    if len(text) < 120 or any(t in text for t in ("domain for sale", "parking page", "buy this domain")):
        return "parked_or_thin"
    return "company_like"


SOURCE_TRUST_VALUES = {
    "fetch_failed",
    "unknown",
    "company_site",
    "linked_ats_candidate",
    "trusted_funding_outlet",
    "untrusted_external",
}
DOMAIN_HYGIENE_VALUES = {
    "unknown",
    "personal_resume",
    "consumer_or_local",
    "parked_or_thin",
    "company_like",
}
EVIDENCE_STATUS_VALUES = {
    "fetch_failed",
    "not_evidence",
    "excluded_hygiene",
    "review_needed",
    "trusted_source_candidate",
    "untrusted_source_candidate",
}
EVIDENCE_TIERS = {"A", "B", "C", "Rejected"}
HARD_EXCLUDE_HYGIENE = {"personal_resume", "consumer_or_local", "parked_or_thin"}


def has_embedded_ats_job_links(text):
    return bool(EMBEDDED_ATS_URL_RE.search(text or ""))


def hygiene_excludes_evidence(hygiene, trust, page_type, evidence_type, text=None):
    if hygiene not in HARD_EXCLUDE_HYGIENE:
        return False
    if evidence_type == "hiring_activity" and page_type == "careers" and trust == "linked_ats_candidate":
        return False
    if (
        hygiene == "parked_or_thin"
        and evidence_type == "hiring_activity"
        and page_type == "careers"
        and trust == "company_site"
        and has_embedded_ats_job_links(text)
    ):
        return False
    if (
        hygiene == "consumer_or_local"
        and evidence_type == "hiring_activity"
        and page_type == "careers"
        and trust in ("company_site", "linked_ats_candidate")
    ):
        return False
    return True


def _official_careers_origin(domain, source_path="", page_type=""):
    if page_type != "careers":
        return False
    value = str(source_path or "").strip()
    if not value:
        return False
    if re.match(r"^https?://", value, flags=re.I):
        host = _host(value)
        domain_host = _host(domain)
        if not (host == domain_host or (domain_host and host.endswith("." + domain_host))):
            return False
        path = urlparse(value).path or "/"
    else:
        path = normalize_path(value)
    return page_type_for_path(path) == "careers"


def source_trust(domain, homepage_url, page_url, ok=True, linked_from_homepage=False, source_path="", page_type=""):
    if not ok:
        return "fetch_failed"
    if _bad_ats_utility_url(page_url):
        return "untrusted_external"
    host = _host(page_url)
    if not host:
        return "unknown"
    domain_host = _host(domain)
    homepage_host = _host(homepage_url)
    if host == domain_host or (domain_host and host.endswith("." + domain_host)):
        return "company_site"
    if host == homepage_host or (homepage_host and host.endswith("." + homepage_host)):
        return "company_site"
    if linked_from_homepage and (
        any(_host_matches_hint(host, h) for h in ATS_HOST_HINTS)
        or _external_recruitment_link_allowed(page_url)
    ):
        return "linked_ats_candidate"
    if any(_host_matches_hint(host, h) for h in ATS_HOST_HINTS) and _official_careers_origin(domain, source_path, page_type):
        return "linked_ats_candidate"
    if any(_host_matches_hint(host, h) for h in TRUSTED_FUNDING_HOST_HINTS):
        return "trusted_funding_outlet"
    return "untrusted_external"


def evidence_tier(hygiene, trust, evidence_type, page_type=None, text=None):
    """Map a source/evidence row into an accuracy tier for downstream gates.

    Tier A: company-controlled source or company-linked ATS candidate.
    Tier B: independent trusted funding/news outlet candidate.
    Tier C: weak/untrusted external source, research-only.
    Rejected: hygiene failure, fetch failure, or no evidence.
    """
    if evidence_type == "none" or trust == "fetch_failed" or hygiene_excludes_evidence(hygiene, trust, page_type, evidence_type, text):
        return "Rejected"
    if hygiene == "unknown":
        return "C"
    if trust in ("company_site", "linked_ats_candidate"):
        return "A"
    if trust == "trusted_funding_outlet":
        return "B"
    return "C"


EVIDENCE_TYPES = {
    "hiring_activity": {
        "page_types": {"careers"},
        "terms": [
            "open roles",
            "open positions",
            "current openings",
            "current job openings",
            "job openings",
            "job listing",
            "job listings",
            "vacancies",
            "browse our vacancies",
            "view current career opportunities",
            "recruiting process",
            "apply",
            "we're hiring",
            "greenhouse",
            "lever",
            "ashby",
            "workday",
            "jobvite",
            "bamboohr",
            "workable",
            "job alerts",
            "full-time",
            "full time",
            "part-time",
            "part time",
            "employment type",
            "remote",
            "on-site",
            "recruit",
            "採用",
            "募集要項",
            "応募方法",
            "職種",
            "待遇",
        ],
    },
    "it_ops_hiring": {
        "page_types": {"careers"},
        "terms": [
            "mdm",
            "device management",
            "endpoint management",
            "identity provider",
            "identity providers",
            "okta",
            "azure ad",
            "google workspace",
            "corporate it",
            "workplace technology",
            "systems administrator",
            "it administrator",
            "infrastructure engineer",
        ],
    },
    "funding_or_growth": {
        "page_types": {"news", "company"},
        "terms": ["raised", "funding", "financing", "investment", "series a", "series b", "series c", "series d", "growth equity"],
    },
    "security_maturity": {
        "page_types": {"security", "company"},
        "terms": ["trust center", "soc 2", "iso 27001", "compliance", "gdpr", "hipaa", "security"],
    },
    "procurement_or_renewal": {
        "page_types": {"procurement", "security", "company"},
        "terms": ["procurement", "rfp", "vendor", "supplier", "renewal", "vendor review"],
    },
}


def evidence_type_hits(page_type, text):
    text_l = (text or "").lower()
    hits = []
    for evidence_type, cfg in EVIDENCE_TYPES.items():
        allowed_types = cfg.get("page_types") or set()
        if allowed_types and page_type not in allowed_types:
            continue
        count, terms = _count_hits(text_l, cfg.get("terms", []))
        if evidence_type == "it_ops_hiring":
            def _near_job_context():
                context_terms = (
                    "open roles", "open positions", "current openings",
                    "job openings", "job listings", "apply", "job",
                    "role", "position", "responsibilities", "requirements",
                    "qualifications", "nice-to-haves", "greenhouse", "lever",
                    "ashby", "workday", "jobvite", "bamboohr", "smartrecruiters",
                )
                for term in terms:
                    pattern = r"\b" + re.escape(term.lower()) + r"\b"
                    for m in re.finditer(pattern, text_l):
                        start = max(0, m.start() - 450)
                        end = min(len(text_l), m.end() + 450)
                        if _count_hits(text_l[start:end], context_terms)[0]:
                            return True
                return False
            explicit_infra = any(t in terms for t in (
                "mdm", "device management", "endpoint management",
                "systems administrator", "it administrator", "infrastructure engineer",
            ))
            identity_ops = any(t in terms for t in (
                "identity provider", "identity providers", "okta", "azure ad",
                "google workspace", "corporate it", "workplace technology",
            ))
            if not _near_job_context() or (not explicit_infra and not identity_ops):
                continue
        if count:
            hits.append((evidence_type, count, terms))
    if page_type == "careers" and WORDPRESS_JOBS_CONTEXT_RE.search(text or ""):
        if not any(evidence_type == "hiring_activity" for evidence_type, _count, _terms in hits):
            hits.append(("hiring_activity", 1, ["job listings"]))
    return hits


JOB_LISTING_HIRING_TERMS = {
    "open roles",
    "open positions",
    "current openings",
    "current job openings",
    "job openings",
    "job listing",
    "job listings",
    "vacancies",
    "browse our vacancies",
    "view current career opportunities",
    "recruiting process",
    "greenhouse",
    "lever",
    "ashby",
    "workday",
    "jobvite",
    "bamboohr",
    "workable",
    "smartrecruiters",
    "採用",
    "募集要項",
    "応募方法",
    "職種",
    "待遇",
}
EMPLOYMENT_DETAIL_TERMS = {
    "full-time",
    "full time",
    "part-time",
    "part time",
    "employment type",
    "remote",
    "on-site",
}
SAME_SITE_JOB_DETAIL_CONTEXT_RE = re.compile(
    r"(?:\b(?:back to all roles?|all roles?)\b.{0,1200}"
    r"\b(?:team|department)\b.{0,400}\b(?:location|office)\b.{0,400}\bapply\b"
    r"|\bdepartment\b.{0,500}\boffice\b.{0,500}\bapply\b)",
    re.I | re.S,
)
SINGLE_ROLE_APPLY_CONTEXT_RE = re.compile(
    r"\b[A-Z][A-Za-z0-9&/+',.() -]{3,90}\b.{0,500}\bapply for this role\b",
    re.I | re.S,
)
WORDPRESS_JOBS_CONTEXT_RE = re.compile(
    r"(?:\bArchives:\s*Jobs\b.{0,160}\bPost Type Description\b|"
    r"^#\s+[A-Z][A-Za-z0-9&/+',.() -]{3,90}\b.{0,700}\b(?:is looking for|is seeking|seeks)\b)",
    re.I | re.S | re.M,
)
NO_OPEN_ROLES_RE = re.compile(
    r"\b(?:there are )?currently no (?:open roles?|open positions?|job openings?|"
    r"positions?|vacancies)\b|\bno (?:open roles?|open positions?|job openings?|"
    r"positions?|vacancies|jobs) (?:available|at this time)\b|come back later",
    re.I,
)
ROLE_CARD_CONTEXT_RE = re.compile(
    r"\b(?:view job|apply for this job|apply for this role)\b|"
    r"\bdepartment\b.{0,240}\b(?:remote|location|office)\b",
    re.I | re.S,
)


def credible_hiring_activity_terms(terms, text, trust):
    terms = set(terms or [])
    if NO_OPEN_ROLES_RE.search(text or "") and not ROLE_CARD_CONTEXT_RE.search(text or ""):
        return False
    if terms & JOB_LISTING_HIRING_TERMS:
        return True
    if trust == "linked_ats_candidate" and (
        "apply" in terms
        or bool(terms & EMPLOYMENT_DETAIL_TERMS)
        or has_embedded_ats_job_links(text)
    ):
        return True
    if trust == "company_site" and "apply" in terms and SAME_SITE_JOB_DETAIL_CONTEXT_RE.search(text or ""):
        return True
    if trust == "company_site" and "apply" in terms and SINGLE_ROLE_APPLY_CONTEXT_RE.search(text or ""):
        return True
    return False


def validated_evidence_rows(domain, homepage_url, homepage_text, page_row):
    """Convert a fetched page into validation metadata rows.

    These rows are still candidates. They separate source/hygiene/trust metadata from
    scoring so downstream systems can fail closed instead of treating any keyword hit
    as a production-ready fact.
    """
    hygiene = domain_hygiene(domain, homepage_text)
    page_type = page_row.get("page_type") or "other"
    ok = bool(page_row.get("ok"))
    source_url = page_row.get("url") or page_row.get("path") or ""
    linked_from_homepage = bool(page_row.get("linked_from_homepage"))
    trust = source_trust(
        domain,
        homepage_url,
        source_url,
        ok=ok,
        linked_from_homepage=linked_from_homepage,
        source_path=page_row.get("path") or "",
        page_type=page_type,
    )
    text = page_row.get("text") or ""
    hits = evidence_type_hits(page_type, text) if ok else []
    hits = [
        (evidence_type, count, terms)
        for evidence_type, count, terms in hits
        if evidence_type != "hiring_activity" or credible_hiring_activity_terms(terms, text, trust)
    ]
    if not hits:
        return [{
            "domain": domain,
            "source_url": source_url,
            "source_host": _host(source_url),
            "page_type": page_type,
            "source_trust": trust,
            "domain_hygiene": hygiene,
            "evidence_tier": evidence_tier(hygiene, trust, "none", page_type, text),
            "evidence_type": "none",
            "evidence_status": "not_evidence" if ok else "fetch_failed",
            "match_count": 0,
            "matched_terms": [],
            "snippet": "",
            "reason": "no source-specific evidence terms matched" if ok else "page fetch failed",
        }]
    rows = []
    for evidence_type, count, terms in hits:
        status = "candidate"
        reason = "candidate evidence; downstream validation required"
        if hygiene_excludes_evidence(hygiene, trust, page_type, evidence_type, text):
            status = "excluded_hygiene"
            reason = "domain hygiene gate did not look company-like"
        elif hygiene == "unknown":
            status = "review_needed"
            reason = "homepage hygiene unknown; human or downstream validation required"
        elif trust in ("company_site", "linked_ats_candidate", "trusted_funding_outlet"):
            status = "trusted_source_candidate"
        elif trust == "untrusted_external":
            status = "untrusted_source_candidate"
        rows.append({
            "domain": domain,
            "source_url": source_url,
            "source_host": _host(source_url),
            "page_type": page_type,
            "source_trust": trust,
            "domain_hygiene": hygiene,
            "evidence_tier": evidence_tier(hygiene, trust, evidence_type, page_type, text),
            "evidence_type": evidence_type,
            "evidence_status": status,
            "match_count": count,
            "matched_terms": terms,
            "snippet": evidence_snippet(text, terms),
            "reason": reason,
        })
    return rows


# ------------------------------------------------------------ label hygiene
def _norm_label(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def coerce_label(raw, rubric):
    """Snap a judge-returned label to one of the rubric's allowed labels or 'unknown'.

    The judge is told to return only allowed labels, but LLMs drift ("B2B SaaS",
    "saas", "not SaaS"). Recording the raw string pollutes the output taxonomy, so we
    normalize (strip non-alphanumerics) and match. If the normalized text contains
    exactly one label token we take it; if it's ambiguous (e.g. the negative label is
    a superset of the positive, matching both) we fall back to 'unknown' rather than
    guess.
    """
    allowed = [rubric["positive_label"], rubric["negative_label"], "unknown"]
    nraw = _norm_label(raw)
    if not nraw:
        return "unknown"
    for a in allowed:
        if nraw == _norm_label(a):
            return a
    contained = [a for a in (rubric["positive_label"], rubric["negative_label"])
                 if _norm_label(a) and _norm_label(a) in nraw]
    return contained[0] if len(contained) == 1 else "unknown"


# ------------------------------------------------------------- judge provider
JUDGE_TEMPLATE = """You are classifying a website from its visible text.
Use ONLY the provided website text. Do not browse, run tools, or use outside facts.

TASK: {description}

Allowed labels: {labels}
- Choose "{positive_label}" or "{negative_label}".
- If the text is empty, a parking/placeholder page, or genuinely undecidable, use "unknown".
{judge_instructions}

Return ONLY a JSON object -- no prose, no code fences:
{{"label": "<one allowed label or unknown>", "confidence": <0.0-1.0>, "reason": "<=20 words"}}

WEBSITE TEXT (truncated):
---
{text}
---"""


def _binary_exists(binary):
    if not binary:
        return False
    if os.path.sep in binary:
        return Path(binary).exists()
    return shutil.which(binary) is not None


def resolve_judge_provider(provider=None):
    """Choose the LLM judge provider for this runtime.

    auto prefers the runtime directory hint when present, then Codex, then Claude.
    That keeps the same shared skill usable across agent runtimes without requiring
    per-runtime source forks.
    """
    raw = (
        provider
        or os.environ.get("SCRAPE_CASCADE_JUDGE_PROVIDER")
        or os.environ.get("JUDGE_PROVIDER")
        or DEFAULT_JUDGE_PROVIDER
    )
    provider = str(raw).strip().lower()
    if provider in ("codex", "claude"):
        return provider
    if provider != "auto":
        raise ValueError("unsupported judge provider: %s" % raw)

    runtime_codex = "." + "codex"
    runtime_claude = "." + "claude"
    parts = set(Path(__file__).resolve().parts)
    codex_bin = os.environ.get("CODEX_BIN", "codex")
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    if runtime_codex in parts and _binary_exists(codex_bin):
        return "codex"
    if runtime_claude in parts and _binary_exists(claude_bin):
        return "claude"
    if _binary_exists(codex_bin):
        return "codex"
    if _binary_exists(claude_bin):
        return "claude"
    return "codex"


def default_judge_model(provider):
    provider = resolve_judge_provider(provider)
    if provider == "claude":
        return (
            os.environ.get("CLAUDE_JUDGE_MODEL")
            or os.environ.get("JUDGE_MODEL")
            or DEFAULT_CLAUDE_JUDGE_MODEL
        )
    return os.environ.get("CODEX_JUDGE_MODEL") or os.environ.get("JUDGE_MODEL") or DEFAULT_CODEX_JUDGE_MODEL


def _extract_claude_result_text(stdout):
    """Unwrap the `claude -p --output-format json` envelope to the model's text.

    Envelope shape: {"type":"result","is_error":bool,"result":"<model text>",...}.
    Returns (text, cli_errored). Falls back to raw stdout if it isn't an envelope
    (e.g. someone overrides to --output-format text)."""
    out = (stdout or "").strip()
    try:
        env = json.loads(out)
        if isinstance(env, dict) and "result" in env:
            return (env.get("result") or ""), bool(env.get("is_error"))
    except Exception:
        pass
    return out, False


def _codex_schema(rubric):
    labels = [rubric["positive_label"], rubric["negative_label"], "unknown"]
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "label": {"type": "string", "enum": labels},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string", "maxLength": 120},
        },
        "required": ["label", "confidence", "reason"],
    }


def _run_claude_judge(prompt, rubric, model, binary, timeout):
    cmd = [binary, "-p", prompt]
    if model:
        cmd.extend(["--model", model])
    # --strict-mcp-config (no --mcp-config) skips MCP server boot; --output-format
    # json gives a parseable envelope instead of free-form stdout. stderr is
    # captured+ignored (broken hooks can spew there on subprocess exit).
    cmd.extend(["--output-format", "json", "--strict-mcp-config"])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    result_text, cli_errored = _extract_claude_result_text(proc.stdout)
    if proc.returncode != 0 and not result_text.strip():
        cli_errored = True
    return result_text, cli_errored


def _run_codex_judge(prompt, rubric, model, binary, timeout):
    with tempfile.TemporaryDirectory(prefix="scrape-cascade-judge-") as tmp:
        tmp_path = Path(tmp)
        schema_path = tmp_path / "schema.json"
        output_path = tmp_path / "last_message.json"
        schema_path.write_text(json.dumps(_codex_schema(rubric)), encoding="utf-8")
        cmd = [
            binary,
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--output-schema",
            str(schema_path),
            "-o",
            str(output_path),
        ]
        if model:
            cmd.extend(["--model", model])
        cmd.append("-")
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
        if output_path.exists():
            result_text = output_path.read_text(encoding="utf-8")
        else:
            result_text = proc.stdout
        return result_text, proc.returncode != 0 and not result_text.strip()


def _extract_json_object(text):
    out = (text or "").strip()
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    m = re.search(r"\{.*\}", out, re.DOTALL)
    if not m:
        raise ValueError("no json object")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("json result is not an object")
    return data


SYNTHETIC_EVIDENCE_MARKERS = (
    "Embedded job postings (SSR JSON",
    "Embedded ATS job links",
)


def select_judge_text(text, rubric, max_chars=6000, head_chars=800, window=450,
                      max_hits_per_term=8):
    """Build the judge's text window from evidence-bearing regions instead of a
    blind head-truncation.

    The default judge path feeds the model ``text[:max_chars]`` -- the first
    ``max_chars`` characters. On long pages whose decisive evidence sits past that
    cut (notably careers pages whose mined postings block is appended at the end,
    and pages fronted by a cookie/consent wall) the judge never sees the signal.

    This keeps a small head slice for page identity (title/brand), always includes
    the cascade's own synthetic evidence blocks (SSR/ATS postings -- which exist
    *so the judge reads them*), then fills the remaining budget with +/-``window``
    char slices around matched positive+negative rubric terms, in document order,
    merging overlaps. Total length stays <= ``max_chars``.

    Falls back to head-truncation when the page has no keyword hits and no
    synthetic markers -- genuine no-signal, where the head is as good as anything.
    Opt-in: only used when ``judge(..., evidence_window=True)``.
    """
    text = text or ""
    if len(text) <= max_chars:
        return text
    lower = text.lower()
    spans = [(0, min(head_chars, len(text)))]
    for marker in SYNTHETIC_EVIDENCE_MARKERS:
        i = text.find(marker)
        if i >= 0:
            spans.append((i, min(len(text), i + 3000)))
    terms = [str(t).strip().lower()
             for t in (list(rubric.get("positive") or []) + list(rubric.get("negative") or []))
             if str(t).strip()]
    for term in terms:
        start, found = 0, 0
        while found < max_hits_per_term:
            i = lower.find(term, start)
            if i < 0:
                break
            spans.append((max(0, i - window), min(len(text), i + len(term) + window)))
            start = i + len(term)
            found += 1
    if len(spans) == 1:  # only the head slice -> no signal -> preserve current behavior
        return text[:max_chars]
    spans.sort()
    merged = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out, used = [], 0
    for s, e in merged:
        if used >= max_chars:
            break
        chunk = text[s:e]
        if used + len(chunk) > max_chars:
            chunk = chunk[:max_chars - used]
        out.append(chunk)
        used += len(chunk)
    return "\n…\n".join(out)


def judge(
    text,
    rubric,
    claude_bin=None,
    model=None,
    timeout=120,
    max_chars=6000,
    provider=None,
    judge_bin=None,
    evidence_window=False,
):
    try:
        provider = resolve_judge_provider(provider)
    except Exception as e:
        return {"label": "unknown", "confidence": 0.0, "reason": "judge_provider_error:%s" % type(e).__name__}
    model = model if model is not None else default_judge_model(provider)
    labels = ", ".join([rubric["positive_label"], rubric["negative_label"], "unknown"])
    prompt = JUDGE_TEMPLATE.format(
        description=rubric.get("description", ""),
        labels=labels,
        positive_label=rubric["positive_label"],
        negative_label=rubric["negative_label"],
        judge_instructions=rubric.get("judge_instructions", ""),
        text=(select_judge_text(text, rubric, max_chars=max_chars)
              if evidence_window else (text or "")[:max_chars]),
    )
    if provider == "claude":
        binary = judge_bin or claude_bin or os.environ.get("CLAUDE_BIN", "claude")
        run = _run_claude_judge
    else:
        binary = judge_bin or os.environ.get("CODEX_BIN", "codex")
        run = _run_codex_judge
    try:
        result_text, cli_errored = run(prompt, rubric, model, binary, timeout)
    except Exception as e:
        return {"label": "unknown", "confidence": 0.0, "reason": "judge_error:%s" % type(e).__name__}
    if cli_errored:
        return {"label": "unknown", "confidence": 0.0, "reason": "judge_cli_error:%s" % provider}
    try:
        data = _extract_json_object(result_text)
    except Exception:
        return {"label": "unknown", "confidence": 0.0, "reason": "judge_parse_error"}
    try:
        confidence = float(data.get("confidence", 0.0))
        return {
            "label": coerce_label(str(data.get("label", "unknown")), rubric),
            "confidence": max(0.0, min(1.0, confidence)),
            "reason": str(data.get("reason", ""))[:120],
        }
    except Exception:
        return {"label": "unknown", "confidence": 0.0, "reason": "judge_json_error"}
