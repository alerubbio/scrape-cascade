#!/usr/bin/env python3
"""SSR-JSON job-board extraction: mine job postings out of server-rendered JSON.

Custom careers boards (Next.js, Remix, some Nuxt) often ship the COMPLETE posting
list server-side in a JSON script blob while the visible rows mount client-side.
html_to_text strips scripts, so the cascade used to keep ~1KB of filter chrome and
throw the listings away (bendingspoons: 35 postings, 447KB __NEXT_DATA__, judged
"careers_no_openings" off the chrome). This module parses those blobs and mines
job-shaped arrays so fetch time — the only moment the HTML is in hand — captures
(a) an authoritative postings count + titles for the judge, and (b) full JDs for
the harvester.

Mining is evidence-gated, not path-gated: any array of dicts qualifies when its
items carry a title-ish key plus job corroborators (location/contract/apply/req-id
families), so it works on boards we have never seen. Callers gate on careers-type
pages; this module never fetches.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from html import unescape as html_unescape
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ats_api  # noqa: E402  (stdlib-only; reused for description html->text)

# Marker prefixed to the synthetic text block appended to careers-page text.
# run.py keys off it: a page that already yielded structured listings must not
# burn a browser-rescue slot, and the judge reads it as listing evidence.
SSR_POSTINGS_MARKER = "Embedded job postings (SSR JSON"

MAX_BLOB_BYTES = 8_000_000  # parse budget per JSON blob, not a correctness bound
MAX_BLOBS = 25
MAX_POSTINGS = 300  # safety cap per page; count keeps the true pre-cap total
MAX_WALK_NODES = 60_000
MAX_WALK_DEPTH = 14
MAX_TITLE_CHARS = 300
DESCRIPTION_HTML_CAP = 40_000

_NEXT_DATA_RE = re.compile(
    r"<script[^>]*\bid=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>", re.I | re.S
)
_JSON_SCRIPT_RE = re.compile(
    r"<script[^>]*\btype=[\"']application/json[\"'][^>]*>(.*?)</script>", re.I | re.S
)
# JS-assigned SSR contexts: parse only when the payload is pure JSON (raw_decode
# fails fast on JS-isms like unquoted keys/function bodies — that is the filter).
_JS_ASSIGN_RES = (
    ("remix", re.compile(r"window\.__remixContext\s*=\s*", re.I)),
    ("nuxt", re.compile(r"window\.__NUXT__\s*=\s*", re.I)),
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# --- job-shaped item classification -----------------------------------------
# Strong title keys are job-vocabulary ("jobTitle", "position"); generic title
# keys ("title", "name", "text") also carry nav links, blog cards, and team
# rosters, so they demand more corroboration. Bare "role" is deliberately NOT a
# title key: team-member cards ({name, role: "CTO", location}) would pass.
_STRONG_TITLE_KEYS = ("jobtitle", "positiontitle", "jobname", "roletitle", "position")
_GENERIC_TITLE_KEYS = ("title", "name", "text")

# Person-card fingerprints: a careers page's team/testimonial sections are the
# classic false-positive arrays. Any of these keys disqualifies the item.
_PERSON_KEYS = {
    "firstname", "lastname", "fullname", "avatar", "headshot", "bio",
    "linkedin", "linkedinurl", "twitter", "email",
}

_CLOSED_STATUSES = {
    "closed", "archived", "draft", "inactive", "expired", "unpublished",
    "filled", "paused", "deleted",
}


def _norm_key(key):
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _family_for_key(nk):
    """Map a normalized item key to a corroborator family, or None.

    Strong families (location/employment/apply/ident) are job-specific; weak
    families (org/date/desc) also occur on blog and event cards.
    """
    if "location" in nk or "remote" in nk or nk in (
        "city", "cities", "country", "countries", "office", "offices",
        "workplace", "workplacetype",
    ):
        return "location"
    if ("contract" in nk or "employment" in nk or "commitment" in nk
            or "schedule" in nk or nk in ("jobtype", "jobtypes", "workmode")):
        return "employment"
    if ("apply" in nk or "hostedurl" in nk or "absoluteurl" in nk
            or "applicationurl" in nk or "applicationform" in nk):
        return "apply"
    if "requisition" in nk or nk in (
        "jobid", "reqid", "postingid", "shortcode", "jobcode", "jobref",
    ):
        return "ident"
    if "department" in nk or "team" in nk or nk in (
        "category", "categories", "division", "function", "discipline",
        "typesofwork",
    ):
        return "org"
    if "published" in nk or "posted" in nk or nk == "dateposted":
        return "date"
    if "description" in nk or nk in (
        "requirements", "responsibilities", "qualifications",
    ):
        return "desc"
    return None


_STRONG_FAMILIES = {"location", "employment", "apply", "ident"}


def _clean_title(value):
    if not isinstance(value, str):
        return ""
    title = _WS_RE.sub(" ", _TAG_RE.sub(" ", value)).strip()
    if not (2 < len(title) <= MAX_TITLE_CHARS):
        return ""
    if re.match(r"^https?://", title, flags=re.I):
        return ""
    if "@" in title and " " not in title:
        return ""  # bare email
    return title


def _as_text(value, depth=0):
    """Best-effort flatten of a JSON value to display text (dicts prefer their
    name/title-ish member; lists join). Used for location/department fields."""
    if depth > 4 or value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return _WS_RE.sub(" ", _TAG_RE.sub(" ", html_unescape(value))).strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("title", "name", "label", "displayname", "text", "value", "city"):
            for k, v in value.items():
                if _norm_key(k) == key:
                    got = _as_text(v, depth + 1)
                    if got:
                        return got
        return ""
    if isinstance(value, list):
        parts = [p for p in (_as_text(v, depth + 1) for v in value[:8]) if p]
        return ", ".join(dict.fromkeys(parts))
    return ""


def _list_lines(value):
    """Flatten a requirements/responsibilities-style list into text lines.
    Items are either strings (possibly HTML) or {title, description} dicts."""
    lines = []
    if not isinstance(value, list):
        value = [value]
    for item in value[:40]:
        if isinstance(item, dict):
            title = _as_text(item.get("title") or item.get("name"))
            desc = ""
            for k, v in item.items():
                if "description" in _norm_key(k):
                    desc = _as_text(v)
                    break
            line = ": ".join([p for p in (title, desc) if p])
        else:
            line = _as_text(item)
        if line:
            lines.append(line)
    return lines


def _first_value(item, predicate):
    for k, v in item.items():
        if predicate(_norm_key(k)):
            return v
    return None


def _build_description(item):
    """Assemble a JD from description-ish members plus requirement/responsibility
    sections. Returns (html, text); either may be empty when the board ships
    titles only."""
    parts = []
    for k, v in item.items():
        if "description" in _norm_key(k) and isinstance(v, str) and v.strip():
            parts.append(v)
    for section in ("responsibilities", "requirements", "qualifications"):
        value = next((v for k, v in item.items() if _norm_key(k) == section), None)
        if value is None:
            continue
        lines = _list_lines(value)
        if lines:
            parts.append(
                "<h3>%s</h3><ul>%s</ul>"
                % (section.title(), "".join("<li>%s</li>" % ln for ln in lines))
            )
    html = "\n".join(parts)[:DESCRIPTION_HTML_CAP]
    return html, (ats_api.html_to_text(html) if html else "")


def _classify_item(item):
    """Return a normalized posting dict when the item is job-shaped, else None."""
    if not isinstance(item, dict) or not item or len(item) > 80:
        return None
    norm_keys = {_norm_key(k): k for k in item}
    if _PERSON_KEYS & set(norm_keys):
        return None
    if item.get("isEvent") is True:
        return None
    status = item.get("status") or item.get("state")
    if isinstance(status, str) and status.strip().lower() in _CLOSED_STATUSES:
        return None

    title, title_tier = "", None
    for nk in _STRONG_TITLE_KEYS:
        if nk in norm_keys:
            title = _clean_title(item[norm_keys[nk]])
            if title:
                title_tier = "strong"
                break
    if not title:
        for nk in _GENERIC_TITLE_KEYS:
            if nk in norm_keys:
                title = _clean_title(item[norm_keys[nk]])
                if title:
                    title_tier = "generic"
                    break
    if not title:
        return None

    families = set()
    for nk in norm_keys:
        fam = _family_for_key(nk)
        if fam:
            families.add(fam)
    strong = families & _STRONG_FAMILIES
    if title_tier == "strong":
        if not strong and len(families) < 2:
            return None
    else:
        if not strong or len(families) < 2:
            return None

    location = _as_text(_first_value(item, lambda nk: _family_for_key(nk) == "location"
                                     and "remote" not in nk))
    if not location:
        location = _as_text(_first_value(item, lambda nk: _family_for_key(nk) == "location"))
    department = _as_text(_first_value(item, lambda nk: _family_for_key(nk) == "org"))
    description_html, description_text = _build_description(item)
    posting_id = ""
    for nk in ("id", "jobid", "postingid", "requisitionid", "reqid", "shortcode", "slug"):
        if nk in norm_keys:
            raw = item[norm_keys[nk]]
            if isinstance(raw, (str, int)) and str(raw).strip():
                posting_id = str(raw).strip()
                break
    published_at = ""
    raw_date = _first_value(item, lambda nk: _family_for_key(nk) == "date")
    if isinstance(raw_date, (str, int)):
        published_at = str(raw_date).strip()
    url = ""
    for nk in ("absoluteurl", "hostedurl", "applyurl", "applicationurl", "url", "href"):
        if nk in norm_keys and isinstance(item[norm_keys[nk]], str):
            candidate = item[norm_keys[nk]].strip()
            if candidate:
                url = candidate
                break

    return {
        "url": url,
        "title": title,
        "location": location or None,
        "department": department or None,
        "description_html": description_html,
        "description_text": description_text,
        "posting_id": posting_id or None,
        "published_at": published_at or None,
    }


# --- blob discovery + walking -------------------------------------------------
def _decode_json(payload):
    payload = (payload or "").strip()
    if not payload or len(payload) > MAX_BLOB_BYTES:
        return None
    try:
        return json.loads(payload)
    except Exception:
        pass
    try:  # rare: JSON entity-escaped into the script body
        return json.loads(html_unescape(payload))
    except Exception:
        return None


def _decode_js_assignment(html, match):
    tail = html[match.end():match.end() + MAX_BLOB_BYTES]
    tail = tail.lstrip()
    if not tail.startswith(("{", "[")):
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(tail)
        return obj
    except Exception:
        return None


def _json_blobs(html):
    """Yield (source, parsed) for every SSR JSON blob in the page, dedup by
    content. application/json scripts are included wholesale — the job-shape
    gate downstream is what keeps analytics/config blobs out."""
    seen = set()
    blobs = []
    for source, rx in (("next_data", _NEXT_DATA_RE), ("json_script", _JSON_SCRIPT_RE)):
        for m in rx.finditer(html or ""):
            raw = m.group(1)
            digest = hashlib.sha1(raw[:4096].encode("utf-8", "replace")).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            obj = _decode_json(raw)
            if obj is not None:
                blobs.append((source, obj))
            if len(blobs) >= MAX_BLOBS:
                return blobs
    for source, rx in _JS_ASSIGN_RES:
        for m in rx.finditer(html or ""):
            obj = _decode_js_assignment(html, m)
            if obj is not None:
                blobs.append((source, obj))
            if len(blobs) >= MAX_BLOBS:
                return blobs
    return blobs


def _walk_arrays(node, budget, depth=0):
    """Yield every list-of-dicts in the parsed JSON, bounded by a node budget so
    a pathological blob cannot stall fetch persistence."""
    if depth > MAX_WALK_DEPTH or budget[0] <= 0:
        return
    budget[0] -= 1
    if isinstance(node, list):
        if node and all(isinstance(x, dict) for x in node):
            yield node
        for child in node[:500]:
            yield from _walk_arrays(child, budget, depth + 1)
    elif isinstance(node, dict):
        for child in node.values():
            yield from _walk_arrays(child, budget, depth + 1)


def _dedupe_key(posting):
    if posting.get("posting_id"):
        return ("id", posting["posting_id"])
    return ("tl", (posting["title"] or "").lower(), (posting.get("location") or "").lower())


def postings_from_html(page_url, html):
    """Mine job postings from SSR JSON in a fetched careers page.

    Returns {"source", "count", "truncated", "postings": [...]} with postings in
    the ats_api.fetch_postings shape, or None when the page carries none. Caller
    gates on page_type == "careers"; this function is pure parsing.
    """
    if not html or ("__NEXT_DATA__" not in html and "application/json" not in html
                    and "__remixContext" not in html and "__NUXT__" not in html):
        return None
    found, source = [], None
    seen = set()
    for blob_source, obj in _json_blobs(html):
        budget = [MAX_WALK_NODES]
        for arr in _walk_arrays(obj, budget):
            for item in arr[:1000]:
                posting = _classify_item(item)
                if posting is None:
                    continue
                key = _dedupe_key(posting)
                if key in seen:
                    continue
                seen.add(key)
                if posting.get("url"):
                    posting["url"] = urljoin(page_url or "", posting["url"])
                else:
                    anchor = posting.get("posting_id") or hashlib.sha1(
                        ("%s|%s" % (posting["title"], posting.get("location") or ""))
                        .encode("utf-8", "replace")).hexdigest()[:12]
                    posting["url"] = "%s#ssr:%s" % ((page_url or "").split("#")[0], anchor)
                found.append(posting)
                source = source or blob_source
    if not found:
        return None
    count = len(found)
    truncated = count > MAX_POSTINGS
    return {
        "source": source,
        "count": count,
        "truncated": truncated,
        "postings": found[:MAX_POSTINGS],
    }


def format_postings_block(result, max_titles=60):
    """Synthetic text block appended to the stored page text (same mechanism as
    "Embedded ATS job links:") so the judge sees listings the renderer hides.
    The "open roles" phrasing is deliberate: it reads as strong careers evidence
    and overrides a server-rendered zero-state ("No open jobs ...") upstream."""
    postings = result.get("postings") or []
    count = result.get("count", len(postings))
    lines = ["%s, %d open roles):" % (SSR_POSTINGS_MARKER, count)]
    for posting in postings[:max_titles]:
        line = "- %s" % posting["title"]
        if posting.get("location"):
            line += " — %s" % posting["location"]
        lines.append(line)
    if len(postings) > max_titles:
        lines.append("(+%d more)" % (len(postings) - max_titles))
    return "\n".join(lines)
