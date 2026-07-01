"""Direct ATS board JSON APIs — count open roles without rendering HTML.

Most mid-market companies host jobs on an applicant-tracking system that exposes
a public, no-auth JSON board API keyed by a company slug. Once the cascade has
detected the ATS host + slug, hitting that API yields an exact count with zero
DOM fragility — retiring per-platform HTML parsing for the platforms covered
here. Every function returns ``None`` on any failure so callers fall through to
the existing HTML/browser tier instead of trusting a partial result.

Covered (public, no-auth GET): Greenhouse, Lever, Ashby, SmartRecruiters,
Workable, Recruitee, Pinpoint, Rippling. Workday is also covered, but via a
public, no-auth POST (its /wday/cxs/ board endpoint only answers POST) from an
already-known board URL — there is no slug-guess path for it. ADP WorkforceNow
(cid-keyed requisitions JSON) and iCIMS (server-rendered board HTML with pr=N
pagination) are URL-keyed like Workday: countable only from a discovered board
URL, never slug-guessed (live-verified 2026-06-10). Platforms needing customer
credentials (BambooHR, Jobvite, UKG, Teamtailor, Paylocity) remain the
HTML/browser tier's job.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Optional
from urllib.parse import parse_qs, urlparse

DEFAULT_TIMEOUT = 12.0

# UltiPro/UKG Pro board hosts share one URL shape; signin-us serves it too.
_ULTIPRO_HOSTS = frozenset({
    "recruiting.ultipro.com",
    "recruiting2.ultipro.com",
    "signin-us.ultipro.com",
})

# A browser-ish UA; some ATS edges 403 the default urllib/python agent.
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Per-host politeness throttle. At large-list scale, ALL Greenhouse boards hit one
# host (boards-api.greenhouse.io) etc.; >10 req/s can trip throttling/IP-blocks that
# would also hurt anything else sharing your IP. Reserve a slot per host (default ~8/s);
# set ATS_MIN_HOST_INTERVAL=0 to disable. Thread-safe across the discovery pool.
_MIN_HOST_INTERVAL = float(os.environ.get("ATS_MIN_HOST_INTERVAL", "0.12"))
_host_lock = threading.Lock()
_host_next = {}


def _throttle(host: str) -> None:
    if _MIN_HOST_INTERVAL <= 0 or not host:
        return
    with _host_lock:  # reserve the next slot for this host, then sleep OUTSIDE the lock
        now = time.monotonic()
        scheduled = max(now, _host_next.get(host, 0.0))
        _host_next[host] = scheduled + _MIN_HOST_INTERVAL
        wait = scheduled - now
    if wait > 0:
        time.sleep(wait)


# --------------------------------------------------------------------------- #
# slug detection: board/embed URL -> (ats_name, slug)                         #
# --------------------------------------------------------------------------- #
def _workday_site_segment(seg: list) -> Optional[str]:
    """The board SITE from a Workday URL's path segments.

    Board URLs are ``[locale]/<site>``; job-DETAIL URLs are
    ``<site>/job/<location>/<Title_R1008>``. Taking the LAST non-locale segment
    therefore turned every detail URL into a phantom per-job "board" (live:
    stem.com manufactured 9 failed boards from its postings, 2026-06-10).
    Site = the first non-locale segment BEFORE any job/jobs marker."""
    out = []
    for s in seg:
        if s.lower() in ("job", "jobs"):
            break
        if re.fullmatch(r"[a-zA-Z]{2}[-_][a-zA-Z]{2,3}", s):
            continue
        out.append(s)
    return out[0] if out else None


def detect_ats(url: str) -> Optional[tuple[str, str]]:
    """Return (ats_name, slug) if ``url`` is a recognized public-API ATS board."""
    if not url:
        return None
    p = urlparse(url if "//" in url else "https://" + url)
    host = (p.hostname or "").lower()
    path = p.path or ""
    seg = [s for s in path.split("/") if s]

    # Greenhouse — board, job-boards, embed widget, US + EU. The board DATA api
    # (boards-api.greenhouse.io) is UNIFIED: ".eu" is a display/embed host only and
    # boards-api.eu.* does not resolve (verified — an EU-widget board's jobs are
    # served by the US boards-api). So both US and EU widgets map to "greenhouse".
    if re.search(r"(^|\.)(boards|job-boards)(\.eu)?\.greenhouse\.io$", host):
        forq = parse_qs(p.query).get("for")
        if forq and forq[0].strip():
            return ("greenhouse", forq[0].strip())
        # /embed/job_board?for=...  handled above; else first non-embed segment
        cand = [s for s in seg if s not in ("embed", "job_board", "jobs", "js")]
        if cand:
            return ("greenhouse", cand[0])

    # Lever — exact board host only (NOT api.lever.co). EU data residency is handled
    # by the endpoint fallback list in _api_urls, so both hosts map to "lever".
    if host in ("jobs.lever.co", "jobs.eu.lever.co") and seg:
        return ("lever", seg[0])

    if host == "jobs.ashbyhq.com" and seg:
        return ("ashby", seg[0])

    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com") and seg:
        return ("smartrecruiters", seg[0])

    if host == "apply.workable.com" and seg:
        # apply.workable.com/<slug> or apply.workable.com/<slug>/j/<id>.
        # A BARE /j/<shortcode> posting URL carries no slug at all ("j" is a
        # path marker, not a company; live noise 2026-06-10).
        if seg[0] != "j":
            return ("workable", seg[0])
    if host.endswith(".workable.com") and host != "apply.workable.com":
        sub = host.split(".workable.com")[0]
        # workable's own portal/product subdomains are not company slugs
        # (jobs/jobseekers.workable.com are their job-search site; live noise)
        if sub and sub not in ("www", "jobs", "jobseekers", "careers", "help",
                               "resources", "status", "apply", "developers"):
            return ("workable", sub)

    if host.endswith(".recruitee.com"):
        sub = host.split(".recruitee.com")[0]
        if sub and sub != "www":
            return ("recruitee", sub)

    if host.endswith(".pinpointhq.com"):
        sub = host.split(".pinpointhq.com")[0]
        if sub and sub != "www":
            return ("pinpoint", sub)

    if host == "ats.rippling.com" and seg:
        # ats.rippling.com/<slug>/jobs, or locale-prefixed /en-US/<slug>/jobs
        is_locale = bool(re.fullmatch(r"[a-zA-Z]{2}[-_][a-zA-Z]{2,3}", seg[0]))
        if is_locale and len(seg) >= 3:
            return ("rippling", seg[1])
        if not is_locale:
            return ("rippling", seg[0])

    # Breezy — <slug>.breezy.hr (or <slug>-<region>.breezy.hr like 75f-apac.breezy.hr).
    # The slug is the subdomain; /json returns a public no-auth list of open positions.
    if host.endswith(".breezy.hr"):
        sub = host[: -len(".breezy.hr")]
        if sub and sub not in ("www", "app"):
            return ("breezy", sub)

    # UltiPro / UKG Pro — recruiting[2].ultipro.com/<OrgCode>/JobBoard/<UUID>.
    # No public JSON API exists; detect-only (slug = OrgCode/BoardUUID) so the
    # cascade can attribute the board without queueing an impossible count.
    if host in _ULTIPRO_HOSTS and len(seg) >= 3 and seg[1].lower() == "jobboard":
        return ("ultipro", f"{seg[0]}/{seg[2]}")

    # Workday — public board host <tenant>.<dc>.myworkdayjobs.com (dc like wd1/wd5/wd503).
    # Slug is "<tenant>/<site>" where <site> is the last non-locale path segment, so both
    # .../Opensity and .../en-US/Opensity collapse to the same site. NOTE the slug alone
    # loses the data-center; count_from_url re-parses the FULL url for the /wday/cxs/ POST.
    m = re.fullmatch(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com", host)
    if m:
        tenant = m.group(1)
        site = _workday_site_segment(seg)
        if site:
            return ("workday", f"{tenant}/{site}")

    # ADP WorkforceNow — embedded board recruitment.html?cid=<GUID>. The cid IS the
    # identity (URL-keyed; never slug-guessed): _adp_count probes the public
    # requisitions JSON with it. myjobs.adp.com (the newer Angular portal) carries a
    # company slug but exposes no public data path — detected so the board is known,
    # counted never (renders/auth only).
    if host == "workforcenow.adp.com":
        cid = (parse_qs(p.query).get("cid") or [""])[0].strip()
        if cid:
            return ("adp", cid)
    if host in ("myjobs.adp.com", "recruiting.adp.com") and seg:
        return ("adp_myjobs", seg[0])

    # iCIMS — careers-<tenant>.icims.com public board (server-rendered HTML;
    # <tenant>.icims.com WITHOUT the careers- prefix is the auth-walled portal).
    mi = re.fullmatch(r"careers-([a-z0-9-]+)\.icims\.com", host)
    if mi:
        return ("icims", mi.group(1))

    return None


# --------------------------------------------------------------------------- #
# per-ATS endpoint + count extraction                                         #
# --------------------------------------------------------------------------- #
def _api_urls(ats: str, slug: str) -> list:
    """Candidate API URLs to try in order; first parseable success wins.

    Most platforms have a single endpoint. Lever has real EU data residency, so we
    try US then EU (US-first means the common case never pays for the fallback).
    Greenhouse's board data API is unified at boards-api.greenhouse.io.
    """
    return {
        "greenhouse": [f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"],
        "lever": [f"https://api.lever.co/v0/postings/{slug}?mode=json",
                  f"https://api.eu.lever.co/v0/postings/{slug}?mode=json"],
        "ashby": [f"https://api.ashbyhq.com/posting-api/job-board/{slug}"],
        "smartrecruiters": [f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100"],
        "workable": [f"https://www.workable.com/api/accounts/{slug}?details=true"],
        "recruitee": [f"https://{slug}.recruitee.com/api/offers/"],
        "pinpoint": [f"https://{slug}.pinpointhq.com/postings.json"],
        "rippling": [f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"],
        # Breezy: <slug>.breezy.hr/json returns all published positions, no auth.
        # ultipro is deliberately absent — detect-only, no public API endpoint.
        "breezy": [f"https://{slug}.breezy.hr/json"],
    }.get(ats, [])


def _dept(d):
    """Best-effort department label from a possibly-dict value."""
    if isinstance(d, dict):
        return d.get("label") or d.get("name") or d.get("team")
    return d if isinstance(d, str) else None


def _extract(ats: str, data) -> Optional[tuple]:
    """Return (count, titles, departments) from parsed JSON, or None if shape is wrong.

    titles/departments are FULL lists (not samples) so downstream consumers can do
    per-title keyword analysis (role/function detection) and department mix.
    """
    try:
        if ats == "greenhouse":
            jobs = data.get("jobs") or []
            total = (data.get("meta") or {}).get("total")
            n = total if isinstance(total, int) else len(jobs)
            titles = [j.get("title") for j in jobs if j.get("title")]
            depts = [d.get("name") for j in jobs for d in (j.get("departments") or [])
                     if isinstance(d, dict) and d.get("name")]
            return n, titles, depts
        if ats == "lever":
            jobs = data if isinstance(data, list) else (data.get("postings") or [])
            titles = [j.get("text") for j in jobs if j.get("text")]
            depts = [(_dept((j.get("categories") or {}).get("team"))
                      or _dept((j.get("categories") or {}).get("department"))) for j in jobs]
            return len(jobs), titles, [d for d in depts if d]
        if ats == "ashby":
            jobs = (data.get("jobs") if isinstance(data, dict) else None) or []
            live = [j for j in jobs if j.get("isListed", True)]
            titles = [j.get("title") for j in live if j.get("title")]
            depts = [(_dept(j.get("departmentName")) or _dept(j.get("department"))) for j in live]
            return len(live), titles, [d for d in depts if d]
        if ats == "smartrecruiters":
            total = data.get("totalFound")
            content = data.get("content") or []
            n = total if isinstance(total, int) else len(content)
            titles = [j.get("name") for j in content if j.get("name")]
            depts = [_dept(j.get("department")) for j in content]
            return n, titles, [d for d in depts if d]
        if ats == "workable":
            jobs = data.get("jobs") if isinstance(data, dict) else None
            jobs = jobs or (data.get("results") if isinstance(data, dict) else None) or []
            titles = [j.get("title") for j in jobs if j.get("title")]
            depts = [_dept(j.get("department")) for j in jobs]
            return len(jobs), titles, [d for d in depts if d]
        if ats == "recruitee":
            jobs = data.get("offers") or []
            pub = [j for j in jobs if (j.get("status") or "published") == "published"]
            titles = [j.get("title") for j in pub if j.get("title")]
            depts = [_dept(j.get("department")) for j in pub]
            return len(pub), titles, [d for d in depts if d]
        if ats == "pinpoint":
            jobs = data.get("data") if isinstance(data, dict) else data
            jobs = jobs or []
            titles, depts = [], []
            for j in jobs:
                a = j.get("attributes") if isinstance(j, dict) and isinstance(j.get("attributes"), dict) else j
                if isinstance(a, dict):
                    if a.get("title"):
                        titles.append(a.get("title"))
                    d = _dept(a.get("department"))
                    if d:
                        depts.append(d)
            return len(jobs), titles, depts
        if ats == "rippling":
            jobs = data if isinstance(data, list) else (
                data.get("items") or data.get("jobs") or data.get("results") or []
            )
            titles = [(j.get("name") or j.get("title")) for j in jobs
                      if isinstance(j, dict) and (j.get("name") or j.get("title"))]
            depts = [_dept(j.get("department")) for j in jobs if isinstance(j, dict)]
            return len(jobs), titles, [d for d in depts if d]
        if ats == "breezy":
            # /json returns a JSON array of position objects.
            # Each item has: id, name, url, department (string), location, type.
            jobs = data if isinstance(data, list) else []
            titles = [j.get("name") for j in jobs if isinstance(j, dict) and j.get("name")]
            depts = [j.get("department") for j in jobs
                     if isinstance(j, dict) and isinstance(j.get("department"), str)]
            return len(jobs), titles, [d for d in depts if d]
        if ats == "workday":
            # /wday/cxs/<tenant>/<site>/jobs POST: {"total": <int>, "jobPostings":[{"title":...}]}.
            # "total" is the server's count of the full board (jobPostings is a page); use it.
            postings = data.get("jobPostings") or []
            total = data.get("total")
            n = total if isinstance(total, int) else len(postings)
            titles = [p.get("title") for p in postings if p.get("title")]
            return n, titles, []
    except (AttributeError, TypeError, KeyError):
        return None
    return None


# --------------------------------------------------------------------------- #
# fetch                                                                        #
# --------------------------------------------------------------------------- #
def _get_json(url: str, timeout: float):
    """GET ``url`` and parse JSON. Prefers httpx; falls back to stdlib urllib."""
    _throttle(urlparse(url).hostname or "")
    try:
        import httpx

        r = httpx.get(url, timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": _UA, "Accept": "application/json"})
        if r.status_code != 200:
            return None
        return r.json()
    except ImportError:
        pass
    except Exception:
        return None
    try:
        import urllib.request

        req = urllib.request.Request(
            url, headers={"User-Agent": _UA, "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _post_json(url: str, body: dict, timeout: float):
    """POST a JSON ``body`` to ``url`` and parse the JSON reply.

    Mirrors ``_get_json`` (httpx with stdlib urllib fallback, same UA/throttle), but
    Workday's /wday/cxs/ board endpoint only answers POST — a GET 405s — so this is a
    separate path rather than a GET. Returns None on any non-200 / parse failure.
    """
    _throttle(urlparse(url).hostname or "")
    headers = {"User-Agent": _UA, "Content-Type": "application/json",
               "Accept": "application/json"}
    try:
        import httpx

        r = httpx.post(url, json=body, timeout=timeout, follow_redirects=True,
                       headers=headers)
        if r.status_code != 200:
            return None
        return r.json()
    except ImportError:
        pass
    except Exception:
        return None
    try:
        import urllib.request

        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


RIPPLING_MAX_PAGES = 50  # safety cap on v2 pagination (50 * 20 = 1000 roles)


def _rippling_count(slug: str, timeout: float) -> Optional[dict]:
    """Rippling counting with the v1/v2 reality handled correctly.

    IMPORTANT — do NOT "fix" this to v2-only. Live verification (2026-06-04):
    * v1 (``ats/v1/board/<slug>/jobs``) returns the COMPLETE flat list in one GET,
      no pagination. For carbon-health: 306 rows.
    * v2 (``ats/v2/board/<slug>/jobs``) is the newer paginated API
      (``{items,page,pageSize,totalItems,totalPages}``, 0-indexed, pageSize=20).
      ``totalItems`` equals v1's count (same data) — but a naive single GET reads
      only page 0 (20 of 306), a silent 93% undercount on large boards.
    So v1 stays PRIMARY (complete + simplest); v2 is a paginated fallback only for
    the case where v1 is ever retired. (Both location-explode multi-site reqs; that
    is consistent with how the other ATS count raw postings.)
    """
    v1 = f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"
    data = _get_json(v1, timeout)
    if isinstance(data, list):
        ex = _extract("rippling", data)
        if ex is not None:
            count, titles, departments = ex
            return {"ats": "rippling", "slug": slug, "count": count, "titles": titles,
                    "departments": sorted(set(departments)), "api_url": v1}
    # Fallback: paginated v2. Must follow totalPages or we undercount to pageSize.
    base = f"https://api.rippling.com/platform/api/ats/v2/board/{slug}/jobs"
    first = _get_json(base, timeout)
    if not isinstance(first, dict) or not isinstance(first.get("items"), list):
        return None
    items = list(first["items"])
    total_pages = first.get("totalPages") if isinstance(first.get("totalPages"), int) else 1
    for pg in range(1, min(total_pages, RIPPLING_MAX_PAGES)):
        more = _get_json(f"{base}?page={pg}", timeout)
        if isinstance(more, dict) and isinstance(more.get("items"), list) and more["items"]:
            items.extend(more["items"])
        else:
            break
    ex = _extract("rippling", {"items": items})
    if ex is None:
        return None
    count, titles, departments = ex
    total_items = first.get("totalItems")
    if isinstance(total_items, int) and total_items > count:
        count = total_items  # trust the server total if we capped pagination
    return {"ats": "rippling", "slug": slug, "count": count, "titles": titles,
            "departments": sorted(set(departments)), "api_url": base}


def count_open_roles(ats: str, slug: str, timeout: float = DEFAULT_TIMEOUT) -> Optional[dict]:
    """Fetch the public API for (ats, slug). Returns a dict or None on failure.

    Result: ``{"ats", "slug", "count", "titles", "api_url"}``. A successful fetch
    with zero open roles returns ``count == 0`` (a real signal), distinct from
    ``None`` (fetch/parse failed → fall through to HTML tier).
    """
    if ats == "rippling":
        return _rippling_count(slug, timeout)
    # Try each endpoint; prefer the first with open roles (count>0). A 200-but-empty
    # board on the US endpoint must not block the EU fallback (EU-resident Lever boards).
    fallback = None
    for url in _api_urls(ats, slug):
        data = _get_json(url, timeout)
        if data is None:
            continue
        extracted = _extract(ats, data)
        if extracted is None:
            continue
        count, titles, departments = extracted
        result = {"ats": ats, "slug": slug, "count": count, "titles": titles,
                  "departments": sorted(set(departments)), "api_url": url}
        if count > 0:
            return result
        if fallback is None:
            fallback = result  # a real 0 -- keep it unless a later endpoint has roles
    return fallback


_WORKDAY_HOST_RE = re.compile(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com")
WORKDAY_PAGE_LIMIT = 20  # board JSON page size; "total" still reflects the full board


def _workday_parts(url: str):
    """Parse a Workday board URL into (tenant, dc, site), or None if it isn't one.

    Unlike the slug-based ATS, the /wday/cxs/ endpoint needs the data-center segment
    (wd503) that the "<tenant>/<site>" slug drops, so we extract all three pieces from
    the FULL url. <site> is the first non-locale segment before any job/jobs marker
    (a job-DETAIL URL must resolve to its BOARD, not a phantom per-job site).
    """
    if not url:
        return None
    p = urlparse(url if "//" in url else "https://" + url)
    host = (p.hostname or "").lower()
    m = _WORKDAY_HOST_RE.fullmatch(host)
    if not m:
        return None
    tenant, dc = m.group(1), m.group(2)
    seg = [s for s in (p.path or "").split("/") if s]
    site = _workday_site_segment(seg)
    if not site:
        return None
    return tenant, dc, site


def _workday_count(url: str, timeout: float = DEFAULT_TIMEOUT) -> Optional[dict]:
    """Count open roles on a Workday public board via its POST-only JSON endpoint.

    POST https://<tenant>.<dc>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs
    with {"limit","offset","searchText"} returns {"total": <int>, "jobPostings": [...]}.
    Returns the standard ATS result dict (ats/slug/count/titles/...) or None on failure.
    """
    parts = _workday_parts(url)
    if parts is None:
        return None
    tenant, dc, site = parts
    api = f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    data = _post_json(api, {"limit": WORKDAY_PAGE_LIMIT, "offset": 0, "searchText": ""},
                      timeout)
    if not isinstance(data, dict):
        return None
    ex = _extract("workday", data)
    if ex is None:
        return None
    count, titles, departments = ex
    return {"ats": "workday", "slug": f"{tenant}/{site}", "count": count,
            "titles": titles, "departments": sorted(set(departments)), "api_url": api}


def _get_status_json(url: str, timeout: float):
    """GET ``url`` -> (http_status, parsed_json_or_None). Unlike _get_json this
    preserves the status code so callers can tell an auth wall (401/403) from a
    dead board (404) -- the ADP classification needs the difference."""
    _throttle(urlparse(url).hostname or "")
    try:
        import httpx

        r = httpx.get(url, timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": _UA, "Accept": "application/json"})
        try:
            return r.status_code, (r.json() if r.status_code == 200 else None)
        except Exception:
            return r.status_code, None
    except ImportError:
        pass
    except Exception:
        return 0, None
    try:
        import urllib.request
        from urllib.error import HTTPError

        req = urllib.request.Request(
            url, headers={"User-Agent": _UA, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    return resp.status, None
                return 200, json.loads(resp.read().decode("utf-8", "replace"))
        except HTTPError as e:
            return e.code, None
    except Exception:
        return 0, None


def _get_text(url: str, timeout: float) -> Optional[str]:
    """GET ``url`` and return body text (HTML), or None. Same transport story as
    _get_json (httpx preferred, urllib fallback, browser-ish UA, throttled)."""
    _throttle(urlparse(url).hostname or "")
    try:
        import httpx

        r = httpx.get(url, timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": _UA})
        if r.status_code != 200:
            return None
        return r.text or ""
    except ImportError:
        pass
    except Exception:
        return None
    try:
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return resp.read().decode("utf-8", "replace")
    except Exception:
        return None


# ADP WorkforceNow public requisitions JSON (live-verified 2026-06-10): cid-keyed,
# no auth. Missing cid -> 500; non-GUID / unknown cid -> 404; walled tenant -> 401/403.
_ADP_REQUISITIONS_API = (
    "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/"
    "staffing/v1/job-requisitions?cid={cid}&lang=en_US"
)


def _adp_count(url: str, timeout: float = DEFAULT_TIMEOUT) -> Optional[dict]:
    """Count open requisitions on an ADP WorkforceNow board from its embed URL.

    The cid GUID comes from the DISCOVERED board URL (recruitment.html?cid=...) --
    URL-keyed like Workday, never slug-guessed. A 401/403 on a real cid is a real
    board behind an auth wall: report it as such (count None, auth_walled True)
    instead of letting the company read as unreachable. 404/500 -> None (no public
    board there)."""
    if not url:
        return None
    p = urlparse(url if "//" in url else "https://" + url)
    if (p.hostname or "").lower() != "workforcenow.adp.com":
        return None
    cid = (parse_qs(p.query).get("cid") or [""])[0].strip()
    if not cid:
        return None
    status, data = _get_status_json(_ADP_REQUISITIONS_API.format(cid=cid), timeout)
    if status in (401, 403):
        return {"ats": "adp", "slug": cid, "count": None, "titles": [],
                "departments": [], "auth_walled": True}
    if status != 200 or not isinstance(data, dict):
        return None
    reqs = data.get("jobRequisitions") or []
    total = (data.get("meta") or {}).get("totalNumber")
    count = total if isinstance(total, int) else len(reqs)
    titles = [r.get("requisitionTitle") for r in reqs
              if isinstance(r, dict) and r.get("requisitionTitle")]
    return {"ats": "adp", "slug": cid, "count": count, "titles": titles,
            "departments": [], "api_url": _ADP_REQUISITIONS_API.format(cid=cid)}


# iCIMS public board (live-verified 2026-06-10 on careers-cobank): the bare page is
# a JS iframe wrapper; in_iframe=1 returns server-rendered job cards. Pagination is
# pr=N (0-indexed, ~20/page, no total in HTML) -> walk pages until no new IDs.
ICIMS_MAX_PAGES = 10
_ICIMS_SEARCH_URL = "https://careers-{tenant}.icims.com/jobs/search?ss=1&in_iframe=1&pr={page}"
_ICIMS_JOB_LINK_RE = re.compile(r"href=\"[^\"]*?/jobs/(\d+)/[^\"]*?/job[^\"]*\"")
_ICIMS_TITLE_ATTR_RE = re.compile(r"title=\"(\d+) - ([^\"]+)\"")


def _icims_count(url: str, timeout: float = DEFAULT_TIMEOUT) -> Optional[dict]:
    """Count open roles on an iCIMS public board by walking its server-rendered
    search pages. HTML parsing is regex-on-structure (job-detail hrefs), not body
    text. Page cap is a deliberate bound: a board bigger than ICIMS_MAX_PAGES*20
    reports the floor it saw (callers treat count as >=, same as captured-doc
    floors elsewhere)."""
    if not url:
        return None
    p = urlparse(url if "//" in url else "https://" + url)
    m = re.fullmatch(r"careers-([a-z0-9-]+)\.icims\.com", (p.hostname or "").lower())
    if not m:
        return None
    tenant = m.group(1)
    ids: set = set()
    titles_by_id = {}
    for page in range(ICIMS_MAX_PAGES):
        html = _get_text(_ICIMS_SEARCH_URL.format(tenant=tenant, page=page), timeout)
        if not html:
            break
        page_ids = set(_ICIMS_JOB_LINK_RE.findall(html))
        new = page_ids - ids
        if not new:
            break
        ids |= new
        for jid, title in _ICIMS_TITLE_ATTR_RE.findall(html):
            titles_by_id.setdefault(jid, title.strip())
    if not ids and titles_by_id == {}:
        # zero could be a real empty board OR a dead tenant; only a fetched page
        # with the jobs-table scaffold proves the board. Without any page, None.
        return None
    return {"ats": "icims", "slug": tenant, "count": len(ids),
            "titles": [titles_by_id[j] for j in sorted(titles_by_id)],
            "departments": [],
            "api_url": _ICIMS_SEARCH_URL.format(tenant=tenant, page=0)}


def count_from_url(url: str, timeout: float = DEFAULT_TIMEOUT) -> Optional[dict]:
    """Convenience: detect ATS+slug from a board URL, then count.

    Workday, ADP, and iCIMS are special-cased BEFORE the slug dispatch: all three
    need more than a slug (Workday the data-center, ADP the cid query param, iCIMS
    the tenant host + HTML pagination), so they re-parse the FULL original URL.
    Each returns None fast on a host mismatch.
    """
    for url_keyed in (_workday_count, _adp_count, _icims_count):
        res = url_keyed(url, timeout)
        if res is not None:
            return res
    hit = detect_ats(url)
    if not hit:
        return None
    return count_open_roles(hit[0], hit[1], timeout=timeout)


# --------------------------------------------------------------------------- #
# posting harvest: board -> full job descriptions                              #
# --------------------------------------------------------------------------- #
# Counting tells you a board is alive; harvesting brings home the JDs. Field
# sources live-verified 2026-06-10 across all nine platforms. Six are one-call-
# complete (the board response carries descriptions); SmartRecruiters, Rippling,
# and Workday need capped per-posting detail calls. Posting "url" is always the
# platform's hosted/public posting URL, never the API URL.

DEFAULT_MAX_POSTINGS = 200


class _TextExtract(__import__("html.parser", fromlist=["HTMLParser"]).HTMLParser):
    """Stdlib HTML -> text: block tags become newlines, script/style dropped."""

    _BLOCK = {"p", "br", "li", "div", "ul", "ol", "h1", "h2", "h3", "h4", "h5",
              "h6", "tr", "section", "article"}
    _SKIP = {"script", "style", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth and data:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Strip HTML to readable text (stdlib only -- no html2text dependency here).
    Tolerates already-plain text (no tags -> returned nearly as-is)."""
    if not html:
        return ""
    if not isinstance(html, str):
        return ""  # structured payloads are the caller's job to flatten first
    parser = _TextExtract()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # malformed HTML: degrade to a regex strip rather than failing the posting
        stripped = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", stripped)
        return re.sub(r"[ \t]+", " ", text).strip()
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _posting(url, title, location=None, department=None, description_html=None,
             description_text=None, posting_id=None, published_at=None):
    if description_text is None:
        description_text = html_to_text(description_html or "")
    return {
        "url": url, "title": title, "location": location, "department": department,
        "description_html": description_html, "description_text": description_text,
        "posting_id": str(posting_id) if posting_id is not None else None,
        "published_at": published_at,
    }


def _postings_greenhouse(slug, timeout, max_postings):
    # content=true embeds each JD as HTML-ENTITY-ESCAPED markup -> unescape first.
    import html as _html

    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
                     timeout)
    if not isinstance(data, dict):
        return None
    jobs = data.get("jobs") or []
    total = (data.get("meta") or {}).get("total")
    count = total if isinstance(total, int) else len(jobs)
    out = []
    for j in jobs[:max_postings]:
        if not j.get("absolute_url"):
            continue
        raw = _html.unescape(j.get("content") or "")
        out.append(_posting(
            j["absolute_url"], j.get("title"),
            location=((j.get("location") or {}).get("name")),
            department=next((d.get("name") for d in (j.get("departments") or [])
                             if isinstance(d, dict) and d.get("name")), None),
            description_html=raw, posting_id=j.get("id"),
            published_at=j.get("first_published") or j.get("updated_at"),
        ))
    return count, out, 0


def _postings_lever(slug, timeout, max_postings):
    for api in (f"https://api.lever.co/v0/postings/{slug}?mode=json",
                f"https://api.eu.lever.co/v0/postings/{slug}?mode=json"):
        data = _get_json(api, timeout)
        jobs = data if isinstance(data, list) else None
        if jobs:  # a 200-but-empty US response must not block the EU fallback
            out = []
            for j in jobs[:max_postings]:
                if not j.get("hostedUrl"):
                    continue
                lists_txt = "\n".join(
                    f"{l.get('text') or ''}\n{html_to_text(l.get('content') or '')}"
                    for l in (j.get("lists") or [])
                )
                text = "\n\n".join(t for t in (
                    j.get("descriptionPlain"), lists_txt, j.get("additionalPlain")) if t)
                cats = j.get("categories") or {}
                out.append(_posting(
                    j["hostedUrl"], j.get("text"),
                    location=cats.get("location"),
                    department=_dept(cats.get("team")) or _dept(cats.get("department")),
                    description_html=j.get("description"), description_text=text or None,
                    posting_id=j.get("id"),
                    published_at=j.get("createdAt"),
                ))
            return len(jobs), out, 0
    return None


def _postings_ashby(slug, timeout, max_postings):
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout)
    if not isinstance(data, dict):
        return None
    live = [j for j in (data.get("jobs") or []) if j.get("isListed", True)]
    out = []
    for j in live[:max_postings]:
        if not (j.get("jobUrl") or j.get("applyUrl")):
            continue
        out.append(_posting(
            j.get("jobUrl") or j.get("applyUrl"), j.get("title"),
            location=j.get("location"),
            department=_dept(j.get("departmentName")) or _dept(j.get("department")),
            description_html=j.get("descriptionHtml"),
            description_text=j.get("descriptionPlain") or None,
            posting_id=j.get("id"), published_at=j.get("publishedAt"),
        ))
    return len(live), out, 0


def _postings_smartrecruiters(slug, timeout, max_postings):
    # list is paginated (limit/offset vs totalFound); descriptions need a per-
    # posting detail GET -> jobAd.sections.{...}.{title,text}.
    base = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    rows, total, offset = [], None, 0
    while True:
        data = _get_json(f"{base}?limit=100&offset={offset}", timeout)
        if not isinstance(data, dict):
            break
        content = data.get("content") or []
        total = data.get("totalFound") if isinstance(data.get("totalFound"), int) else total
        rows.extend(content)
        offset += len(content)
        if not content or offset >= (total or 0) or len(rows) >= max_postings:
            break
    if total is None:
        return None
    out, failures = [], 0
    for r in rows[:max_postings]:
        pid = r.get("id")
        if not pid:
            continue
        detail = _get_json(f"{base}/{pid}", timeout)
        if not isinstance(detail, dict):
            failures += 1
            continue
        sections = ((detail.get("jobAd") or {}).get("sections") or {})
        text = "\n\n".join(
            f"{(s.get('title') or k)}\n{html_to_text(s.get('text') or '')}"
            for k, s in sections.items() if isinstance(s, dict) and s.get("text")
        )
        url = detail.get("postingUrl") or r.get("postingUrl")
        if not url:
            failures += 1
            continue
        loc = (detail.get("location") or r.get("location") or {})
        out.append(_posting(
            url, detail.get("name") or r.get("name"),
            location=", ".join(x for x in (loc.get("city"), loc.get("country")) if x) or None,
            department=_dept((detail.get("department") or {}).get("label")),
            description_text=text or None, posting_id=pid,
            published_at=detail.get("releasedDate") or r.get("releasedDate"),
        ))
    return total, out, failures


def _postings_workable(slug, timeout, max_postings):
    # widget API is FIRST-PAGE-ONLY (no public pagination) -- a platform cap, so
    # count may be a floor on big boards; documented, not silent.
    data = _get_json(f"https://www.workable.com/api/accounts/{slug}?details=true", timeout)
    if not isinstance(data, dict):
        return None
    jobs = data.get("jobs") or []
    out = []
    for j in jobs[:max_postings]:
        if not j.get("url"):
            continue
        loc = j.get("city")
        if not loc and isinstance(j.get("locations"), list) and j["locations"]:
            loc = (j["locations"][0] or {}).get("city")
        out.append(_posting(
            j["url"], j.get("title"),
            location=loc,
            department=j.get("department"),
            description_html=j.get("description"),
            posting_id=j.get("shortcode") or j.get("id"),
            published_at=j.get("published_on") or j.get("created_at"),
        ))
    return len(jobs), out, 0


def _postings_recruitee(slug, timeout, max_postings):
    data = _get_json(f"https://{slug}.recruitee.com/api/offers/", timeout)
    if not isinstance(data, dict):
        return None
    offers = [o for o in (data.get("offers") or [])
              if (o.get("status") or "published") == "published"]
    out = []
    for o in offers[:max_postings]:
        if not o.get("careers_url"):
            continue
        desc = "\n\n".join(html_to_text(o.get(k) or "") for k in ("description", "requirements")
                           if o.get(k))
        out.append(_posting(
            o["careers_url"], o.get("title"), location=o.get("location"),
            department=o.get("department"),
            description_html=o.get("description"), description_text=desc or None,
            posting_id=o.get("id"), published_at=o.get("published_at") or o.get("created_at"),
        ))
    return len(offers), out, 0


def _postings_pinpoint(slug, timeout, max_postings):
    data = _get_json(f"https://{slug}.pinpointhq.com/postings.json", timeout)
    if not isinstance(data, dict):
        return None
    rows = data.get("data") or []
    out = []
    for r in rows[:max_postings]:
        if not r.get("url"):
            continue
        desc = "\n\n".join(
            html_to_text(r.get(k) or "") for k in
            ("description", "key_responsibilities", "skills_knowledge_expertise", "benefits")
            if r.get(k)
        )
        out.append(_posting(
            r["url"], r.get("title"),
            location=((r.get("location") or {}).get("name")
                      if isinstance(r.get("location"), dict) else r.get("location")),
            department=_dept(r.get("department")),
            description_text=desc or None, posting_id=r.get("id"),
            published_at=r.get("created_at"),
        ))
    return len(rows), out, 0


def _postings_rippling(slug, timeout, max_postings):
    # v1 list is complete but has NO descriptions; the per-uuid detail GET does.
    # v1 location-explodes (same uuid repeated per work location) -> dedupe.
    data = _get_json(f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs", timeout)
    if not isinstance(data, list):
        return None
    by_uuid = {}
    for r in data:
        u = r.get("uuid")
        if u and u not in by_uuid:
            by_uuid[u] = r
    out, failures = [], 0
    for u, r in list(by_uuid.items())[:max_postings]:
        detail = _get_json(
            f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs/{u}", timeout)
        desc = (detail or {}).get("description") if isinstance(detail, dict) else None
        # live shape (verified 2026-06-10): description is a DICT of HTML
        # sections ({"company": "<p>...", "role": ...}) -- flatten to one blob.
        if isinstance(desc, dict):
            desc = "\n\n".join(v for v in desc.values() if isinstance(v, str) and v)
        if not desc:
            failures += 1
        loc = r.get("workLocation")
        out.append(_posting(
            r.get("url") or f"https://ats.rippling.com/{slug}/jobs/{u}", r.get("name"),
            location=(loc.get("label") if isinstance(loc, dict) else loc),
            department=_dept(r.get("department")),
            description_html=desc, posting_id=u,
        ))
    return len(by_uuid), out, failures


def _postings_workday(board_url, timeout, max_postings):
    # paged POST list (limit 20; trust server "total"), then per-posting detail
    # GET {board}/wday/cxs/{tenant}/{site}{externalPath} with a JSON Accept.
    parts = _workday_parts(board_url)
    if parts is None:
        return None
    tenant, dc, site = parts
    base = f"https://{tenant}.{dc}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    rows, total, offset = [], None, 0
    while True:
        data = _post_json(api, {"limit": WORKDAY_PAGE_LIMIT, "offset": offset,
                                "searchText": ""}, timeout)
        if not isinstance(data, dict):
            break
        page = data.get("jobPostings") or []
        total = data.get("total") if isinstance(data.get("total"), int) else total
        rows.extend(page)
        offset += len(page)
        if not page or len(rows) >= max_postings or (total is not None and offset >= total):
            break
    if total is None:
        return None
    out, failures = [], 0
    for r in rows[:max_postings]:
        ext = r.get("externalPath")
        if not ext:
            failures += 1
            continue
        detail = _get_json(f"{base}/wday/cxs/{tenant}/{site}{ext}", timeout)
        info = (detail or {}).get("jobPostingInfo") if isinstance(detail, dict) else None
        if not isinstance(info, dict):
            failures += 1
            continue
        out.append(_posting(
            info.get("externalUrl") or f"{base}/{site}{ext}",
            info.get("title") or r.get("title"),
            location=info.get("location") or r.get("locationsText"),
            description_html=info.get("jobDescription"),
            posting_id=info.get("jobReqId"),
            published_at=info.get("startDate") or r.get("postedOn"),
        ))
    return total, out, failures


_POSTINGS_DISPATCH = {
    "greenhouse": _postings_greenhouse,
    "lever": _postings_lever,
    "ashby": _postings_ashby,
    "smartrecruiters": _postings_smartrecruiters,
    "workable": _postings_workable,
    "recruitee": _postings_recruitee,
    "pinpoint": _postings_pinpoint,
    "rippling": _postings_rippling,
}

# Families fetch_postings can actually harvest. adp/adp_myjobs/icims are
# detect/count-only -- drivers must not queue them for harvest.
HARVESTABLE_ATS = frozenset(_POSTINGS_DISPATCH) | {"workday"}


def fetch_postings(ats: str, slug: str, *, board_url: str | None = None,
                   timeout: float = DEFAULT_TIMEOUT,
                   max_postings: int = DEFAULT_MAX_POSTINGS) -> Optional[dict]:
    """Enumerate a confirmed board and return full postings with clean JD text.

    Returns None on total failure (mirror of count_open_roles), else::

        {"ats", "slug",
         "count",            # server total (authoritative; may exceed len(postings))
         "truncated",        # True when max_postings capped the harvest (no silent caps)
         "detail_failures",  # per-posting fetches that failed (skipped, not fatal)
         "postings": [{"url", "title", "location", "department",
                       "description_html", "description_text",
                       "posting_id", "published_at"}]}

    Workday is URL-keyed (the slug drops the data-center): pass board_url or use
    fetch_postings_from_url. ADP/iCIMS have no public per-posting JSON path and
    are not harvestable here (counts only).
    """
    if ats == "workday":
        res = _postings_workday(board_url, timeout, max_postings) if board_url else None
    else:
        fn = _POSTINGS_DISPATCH.get(ats)
        if fn is None:
            return None
        try:
            res = fn(slug, timeout, max_postings)
        except Exception:
            return None
    if res is None:
        return None
    count, postings, failures = res
    return {
        "ats": ats, "slug": slug, "count": count,
        "truncated": bool(count is not None and count > len(postings) + failures
                          and len(postings) + failures >= max_postings),
        "detail_failures": failures,
        "postings": postings,
    }


def fetch_postings_from_url(url: str, *, timeout: float = DEFAULT_TIMEOUT,
                            max_postings: int = DEFAULT_MAX_POSTINGS) -> Optional[dict]:
    """detect_ats + harvest from a FULL board URL (the only safe path for
    Workday, whose data-center lives in the host, not the slug)."""
    parts = _workday_parts(url)
    if parts is not None:
        tenant, _dc, site = parts
        return fetch_postings("workday", f"{tenant}/{site}", board_url=url,
                              timeout=timeout, max_postings=max_postings)
    hit = detect_ats(url)
    if not hit:
        return None
    return fetch_postings(hit[0], hit[1], timeout=timeout, max_postings=max_postings)


# --------------------------------------------------------------------------- #
# slug-guess router: domain -> candidate slugs -> probe every public ATS API   #
# --------------------------------------------------------------------------- #
# The HTML-INDEPENDENT discovery method. count_open_roles already works from a
# (ats, slug) pair; this derives candidate slugs from the bare domain and probes
# all 8 public APIs, so a board is found even when the homepage has no careers
# link at all (SPA shells, WAF-blocked sites, unlisted/hidden boards). A wrong
# slug returns a clean 404 (no false positive — just cost), so we cap variants
# and short-circuit on the first board with open roles.
SLUG_GUESS_ATS_ORDER = (
    "greenhouse", "lever", "ashby", "smartrecruiters",
    "workable", "recruitee", "rippling", "pinpoint",
)
# leading brand words that companies drop in their ATS slug (goshippo -> shippo).
_SLUG_STRIP_PREFIXES = ("go", "get", "try", "use", "join", "hey", "team", "the", "with")
# non-apex subdomains to strip off messy domain inputs before deriving the slug label.
_NON_APEX_SUBDOMAINS = frozenset({
    "www", "careers", "career", "jobs", "job", "apply", "app", "go", "get",
    "hire", "hiring", "talent", "work", "en", "us", "info", "boards", "about",
})


def slug_variants(domain: str, prior_names=None, limit: int = 6) -> list:
    """Ordered, de-duplicated candidate ATS slugs derived from a domain.

    Common case first (bare second-level label), then cheap morphological guesses
    that the corpus showed up: brand-prefix strip (goshippo->shippo), de-hyphen,
    ``inc``/``1`` suffixes (Ivanti1), and a Capitalized form (SmartRecruiters/Ashby
    ids are case-sensitive). Non-derivable rebrands (carbonhealth->carbon-health,
    mezmo->logdna) can only come via ``prior_names`` — there is no in-pipeline
    source for those today, so they remain a documented recall cap, not a bug.
    """
    raw = (domain or "").strip().lower()
    if "//" in raw:
        raw = urlparse(raw).hostname or raw
    raw = raw.split("/")[0].split("?")[0]
    parts = [p for p in raw.split(".") if p]
    # Drop leading non-apex subdomains so messy website values (www.acme.com,
    # careers.acme.com, go.acme.com) still yield the company label. Keep >=2 labels so
    # 2-label apexes (work.com) and ccTLDs (acme.co.uk -> acme) are never over-stripped.
    while len(parts) > 2 and parts[0] in _NON_APEX_SUBDOMAINS:
        parts = parts[1:]
    label = parts[0] if parts else ""

    out, seen = [], set()

    def add(s):
        s = (s or "").strip()
        for form in (s, s[:1].upper() + s[1:] if s else s):
            if form and form not in seen and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", form):
                seen.add(form)
                out.append(form)

    # High-value variants first so the cap drops the speculative suffixes, not the
    # hints most likely to be the real slug (prefix-strip, de-hyphen, prior brand).
    if label:
        add(label)
        for pre in _SLUG_STRIP_PREFIXES:
            if label.startswith(pre) and len(label) > len(pre) + 2:
                add(label[len(pre):])
        if "-" in label:
            add(label.replace("-", ""))
    for nm in (prior_names or []):
        cleaned = re.sub(r"[^a-z0-9-]", "", (nm or "").lower())
        add(cleaned)
        if "-" in cleaned:
            add(cleaned.replace("-", ""))
    if label:  # speculative suffixes last
        add(label + "inc")
        add(label + "1")
    return out[:limit]


def count_open_roles_by_slug(domain: str, prior_names=None, ats_subset=None,
                             timeout: float = DEFAULT_TIMEOUT, max_variants: int = 6) -> Optional[dict]:
    """Guess slugs from ``domain`` and probe public ATS APIs until a board is found.

    Returns the count_open_roles dict augmented with ``slug_variant`` and
    ``discovery='slug_guess'`` (plus ``slug_candidates_tried``), or None if no
    variant resolves to an ACTIVE board on any ATS.

    IMPORTANT — a blind slug guess requires ``count > 0`` to count as a find. The
    spec's "wrong slug = clean 404, no false positive" is FALSE in practice:
    SmartRecruiters (and others) return 200 with ``count == 0`` for arbitrary
    slugs, so a 0-role board from a *guessed* slug is untrustworthy (verified
    2026-06-04: autodesk->smartrecruiters/autodesk=0, carbonhealth->lever=0 are
    coincidental, not their real boards). A real active board (count>0) is its own
    confirmation. count==0 stays meaningful only for boards confirmed from a real
    HTML link via count_from_url, never from a blind guess. (Capacity-signal-wise
    a 0-role board contributes 0 anyway, so this drops noise, not signal.)
    """
    ats_list = list(ats_subset) if ats_subset else list(SLUG_GUESS_ATS_ORDER)
    variants = slug_variants(domain, prior_names, limit=max_variants)
    for slug in variants:
        for ats in ats_list:
            res = count_open_roles(ats, slug, timeout=timeout)
            if res is not None and res.get("count", 0) > 0:
                return {**res, "slug_variant": slug, "discovery": "slug_guess",
                        "slug_candidates_tried": variants}
    return None
