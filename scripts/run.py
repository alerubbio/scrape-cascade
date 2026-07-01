#!/usr/bin/env python3
"""scrape-cascade orchestrator.

Stage order, each step only seeing what the cheaper step could not answer:
  Tier 1   httpx fast pass        -> cleaned text + keyword score (most domains resolve here)
  Tier 2/3 Playwright / undetected -> rescue only the empty/blocked (opt-in via flags)
  Judge    Codex/Claude CLI        -> only the genuinely ambiguous

Durability: the SQLite DB is written incrementally (pages per fetch-chunk, verdicts
per decision) and is the source of truth. The CSV/JSONL are a projection of the DB --
regenerate them any time with --export-only, including after a crash mid-run. Re-runs
skip already-decided domains (resume), so a killed bulk run picks up where it left off.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cascade  # noqa: E402
import ats_api  # noqa: E402
import ssr_json  # noqa: E402

try:
    import yaml
except ImportError:
    sys.exit("Missing pyyaml -- run: .venv/bin/pip install -r requirements.txt")

SKILL_DIR = Path(__file__).resolve().parent.parent
DOMAIN_COLUMNS = ("domain", "website", "url", "company_domain")
BROWSER_RESCUE_STRONG_CAREERS_RE = re.compile(
    r"\b(open roles?|open positions?|job openings?|current openings?|open jobs?|"
    r"job listings?|job vacancies|vacancies|greenhouse|lever|ashby|workday|"
    r"smartrecruiters|full[- ]time|part[- ]time|employment type)\b|採用|募集要項|職種",
    re.I,
)
BROWSER_RESCUE_WEAK_CAREERS_RE = re.compile(
    r"\b(careers?|jobs?|join our team|work with us|we'?re hiring|hiring|apply)\b|採用|募集",
    re.I,
)
THIN_RENDERED_TEXT_CHARS = 500
MAX_BROWSER_RESCUE_CAREERS_PER_DOMAIN = 4


def is_bad_browser_rescue_url(value):
    if cascade._bad_ats_utility_url(value):
        return True
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    raw = str(value or "").lower()
    return (
        "${" in str(value or "")
        or "{" in str(value or "")
        or "}" in str(value or "")
        or host.startswith("login.")
        or "/share_image/" in path
        or path.endswith("/jobalerts")
        or "poweredby" in raw
    )


def load_rubric(path):
    # utf-8-sig: tolerate a BOM on a hand-edited / Windows-saved rubric.
    with open(path, encoding="utf-8-sig") as f:
        r = yaml.safe_load(f)
    for k in ("name", "positive_label", "negative_label"):
        if k not in r:
            raise ValueError("rubric missing required key: %s" % k)
    return r


def _column_index(header, domain_column=None):
    if not header:
        return 0, False
    normalized = [str(c or "").strip().lower() for c in header]
    if domain_column:
        wanted = str(domain_column).strip()
        if wanted.isdigit():
            idx = int(wanted)
            if idx >= len(header):
                raise ValueError("--domain-column index out of range: %s" % wanted)
            return idx, any(c in DOMAIN_COLUMNS for c in normalized)
        wanted_l = wanted.lower()
        if wanted_l not in normalized:
            raise ValueError("--domain-column not found in CSV header: %s" % wanted)
        return normalized.index(wanted_l), True
    for candidate in DOMAIN_COLUMNS:
        if candidate in normalized:
            return normalized.index(candidate), True
    return 0, normalized[0] in DOMAIN_COLUMNS


def read_domains(path, limit=None, domain_column=None):
    domains, seen = [], set()
    # utf-8-sig strips a leading BOM (Excel-exported CSVs carry one) so the first
    # domain isn't corrupted and the header check still fires; errors=replace keeps a
    # stray non-UTF8 byte from killing the whole load.
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        first = f.readline()
        if not first:
            return domains
        f.seek(0)
        if "," in first or domain_column:
            reader = csv.reader(f)
            first_row = next(reader, [])
            idx, has_header = _column_index(first_row, domain_column)
            if not has_header and idx < len(first_row):
                d = cascade.normalize_domain(first_row[idx])
                if d and d not in seen:
                    seen.add(d)
                    domains.append(d)
                if limit and len(domains) >= limit:
                    return domains
            for row in reader:
                if idx >= len(row):
                    continue
                d = cascade.normalize_domain(row[idx])
                if d and d not in seen:
                    seen.add(d)
                    domains.append(d)
                if limit and len(domains) >= limit:
                    break
            return domains
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            d = cascade.normalize_domain(raw)
            if d and d not in seen:
                seen.add(d)
                domains.append(d)
            if limit and len(domains) >= limit:
                break
    return domains


def export_rows(conn, rubric_name, domains):
    """Project the current DB verdicts into output rows, in input order. Domains with
    no verdict yet surface as 'pending' so an --export-only after a partial run is
    honest about what is and isn't decided."""
    rows = []
    for d in domains:
        v = cascade.get_verdict(conn, d, rubric_name)
        if v:
            rows.append({"domain": d, "label": v["label"], "confidence": v["confidence"],
                         "method": v["method"], "reason": v["reason"]})
        else:
            rows.append({"domain": d, "label": "pending", "confidence": 0.0,
                         "method": "none", "reason": "no verdict in db"})
    return rows


def write_outputs(rows, output_path):
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="") as f:  # CSV is the answer: identity + verdict
        w = csv.writer(f)
        w.writerow(["domain", "label", "confidence", "method"])
        for r in rows:
            w.writerow([r["domain"], r["label"], r["confidence"], r["method"]])
    jsonl_path = str(Path(output_path).with_suffix(".jsonl"))
    with open(jsonl_path, "w") as f:  # JSONL is the full state: reasons too
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return jsonl_path


def csv_safe(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    value = value.replace("\x00", " ")
    return "".join(ch if ch in "\t\n\r" or ord(ch) >= 32 else " " for ch in value)


def default_pages_output(output_path):
    path = Path(output_path)
    return str(path.with_name(path.stem + "_pages.csv"))


def default_evidence_output(output_path):
    path = Path(output_path)
    return str(path.with_name(path.stem + "_evidence.csv"))


def merge_page_targets(targets):
    merged = {}
    order = []
    for target in targets or []:
        key = cascade.normalize_page_key(target.get("path") or target.get("url"))
        if not key:
            continue
        if key not in merged:
            merged[key] = dict(target)
            order.append(key)
        else:
            cur = merged[key]
            cur["linked_from_homepage"] = bool(cur.get("linked_from_homepage")) or bool(target.get("linked_from_homepage"))
            if target.get("url") and not cur.get("url"):
                cur["url"] = target.get("url")
            if target.get("evidence_terms") and not cur.get("evidence_terms"):
                cur["evidence_terms"] = target.get("evidence_terms")
            if target.get("page_type") and (not cur.get("page_type") or cur.get("page_type") == "other"):
                cur["page_type"] = target.get("page_type")
    return [merged[key] for key in order]


def configured_page_targets(domain, specs):
    targets = [
        {
            "domain": domain,
            "path": spec["path"],
            "page_type": spec["page_type"],
            "evidence_terms": spec.get("evidence_terms") or [],
            "linked_from_homepage": False,
        }
        for spec in specs
    ]
    targets.append({
        "domain": domain,
        "path": "https://careers." + domain.rstrip("/") + "/",
        "url": "https://careers." + domain.rstrip("/") + "/",
        "page_type": "careers",
        "evidence_terms": [],
        "linked_from_homepage": False,
    })
    return targets


def stored_page_targets(conn, domain):
    targets = []
    for pg in cascade.list_discovered_pages(conn, domain):
        key = pg.get("path") or pg.get("url")
        targets.append({
            "domain": domain,
            "path": key,
            "url": pg.get("url") if str(key or "").startswith("http") else None,
            "page_type": pg.get("page_type") or "other",
            "linked_from_homepage": bool(pg.get("linked_from_homepage")),
        })
    return targets


def select_targets_for_domain(
    conn,
    domain,
    specs,
    link_targets=None,
    max_pages_per_domain=None,
    page_type_quota=None,
    page_type_order=None,
    include_stored=True,
):
    candidates = configured_page_targets(domain, specs)
    candidates.extend(link_targets or [])
    if include_stored:
        candidates.extend(stored_page_targets(conn, domain))
    candidates = merge_page_targets(candidates)
    return cascade.select_page_specs(
        candidates,
        max_pages_per_domain=max_pages_per_domain,
        page_type_quota=page_type_quota,
        page_type_order=page_type_order,
    )


def targets_needing_fetch(conn, targets, refetch=False, candidate_only=False):
    out = []
    seen = set()
    for target in targets or []:
        key = target.get("path") or target.get("url")
        if not key:
            continue
        norm = cascade.normalize_page_key(key)
        if norm in seen:
            continue
        pg = None if refetch else cascade.get_discovered_page(conn, target["domain"], key)
        if candidate_only:
            linked = bool(target.get("linked_from_homepage")) or bool((pg or {}).get("linked_from_homepage"))
            host = cascade._host(str(target.get("url") or target.get("path") or (pg or {}).get("url") or ""))
            is_ats = any(cascade._host_matches_hint(host, hint) for hint in cascade.ATS_HOST_HINTS)
            page_type = target.get("page_type") or (pg or {}).get("page_type") or ""
            if page_type != "careers" and not is_ats:
                continue
            if not linked and not is_ats:
                continue
            if pg and pg.get("tier") != "candidate":
                continue
        elif pg and pg.get("ok") and not refetch:
            continue
        out.append(target)
        seen.add(norm)
    return out


def select_browser_rescue_page_targets(conn, targets, per_domain_cap=None):
    """Pick discovered pages worth a render/scroll pass.

    Keep this narrow: page-level Playwright is for high-value career evidence that
    often lazy-loads. Broad configured 404 guesses stay out of the browser tier,
    but careers evidence must NOT hide behind linked_from_homepage -- ATS boards
    discovered via the API/slug route are recorded unlinked yet are exactly the
    JS-shell class a render fixes (sdr500_r1: 433 empty-body 200s, most unlinked).
    """
    cap = per_domain_cap if per_domain_cap is not None else MAX_BROWSER_RESCUE_CAREERS_PER_DOMAIN
    selected = []
    seen = set()
    seen_final_urls = set()
    per_domain_counts = {}
    for target in targets or []:
        if (target.get("page_type") or "") != "careers":
            continue
        domain = target["domain"]
        key = cascade.normalize_page_key(target.get("path") or target.get("url") or "/")
        pg = cascade.get_discovered_page(conn, domain, key)
        final_url = (pg or {}).get("url") or target.get("url") or target.get("path") or ""
        if is_bad_browser_rescue_url(str(final_url)) or is_bad_browser_rescue_url(str(key)):
            continue
        linked = bool(target.get("linked_from_homepage")) or bool((pg or {}).get("linked_from_homepage"))
        text = (pg or {}).get("text") or ""
        if ssr_json.SSR_POSTINGS_MARKER in text:
            # Fetch time already mined structured listings out of this page's
            # SSR JSON; a render pass would only re-surface what we have.
            continue
        host = cascade._host(str(final_url))
        ats_host = any(cascade._host_matches_hint(host, hint) for hint in cascade.ATS_HOST_HINTS)
        linked_ats = linked and ats_host
        strong_ats_text = (
            ats_host
            and bool(pg and pg.get("ok"))
            and bool(BROWSER_RESCUE_STRONG_CAREERS_RE.search(text))
        )
        if strong_ats_text:
            continue
        key_host = cascade._host(str(key))
        domain_host = cascade._host(domain)
        # Host check matters since tracking-param stripping: jobs.<domain> board
        # keys used to carry an accidental "careers" token in their utm params,
        # which was all that kept them careers-eligible here.
        careers_host = cascade.is_careers_host(key_host) or cascade.is_careers_host(host)
        careerish_url = (
            cascade.page_type_for_path(str(key)) == "careers"
            or cascade.page_type_for_path(str(final_url)) == "careers"
            or ats_host
            or careers_host
        )
        if not careerish_url:
            continue
        official_careers_guess = (
            (cascade.page_type_for_path(str(key)) == "careers"
             or cascade.is_careers_host(key_host))
            and (
                not str(key).lower().startswith("http")
                or key_host == domain_host
                or (domain_host and key_host.endswith("." + domain_host))
            )
        )
        fetch_status = int((pg or {}).get("status") or 0)
        render_fixable_failure = (
            bool(pg and not pg.get("ok")) and fetch_status in (0, 403, 429)
        )
        needs_render = (
            "#" in str(final_url)
            or (linked_ats and not strong_ats_text)
            or (linked and (not pg or not pg.get("ok")))
            # render-fixable hard failures on careers evidence, linked or not
            # (404s stay excluded: a render will not fix a genuine miss)
            or ((ats_host or official_careers_guess) and render_fixable_failure)
            # thin-text 200s: the JS-shell class; was linked-only, which skipped
            # every API/slug-discovered board (recorded with linked=0)
            or (
                (linked or ats_host or official_careers_guess)
                and bool(pg and pg.get("ok"))
                and len(text.strip()) < THIN_RENDERED_TEXT_CHARS
            )
            or ((pg or {}).get("render_hint") in ("js_shell", "stub", "shell_chrome"))
            or (
                linked
                and bool(pg and pg.get("ok"))
                and BROWSER_RESCUE_WEAK_CAREERS_RE.search(text)
                and not BROWSER_RESCUE_STRONG_CAREERS_RE.search(text)
            )
            or (
                official_careers_guess
                and bool(pg and pg.get("ok"))
                and BROWSER_RESCUE_WEAK_CAREERS_RE.search(" ".join([str(key), str(final_url), text]))
                and not BROWSER_RESCUE_STRONG_CAREERS_RE.search(text)
            )
            or (
                official_careers_guess
                and bool(pg and pg.get("ok"))
                and cascade.JOB_DISCOVERY_CONTROL_RE.search(text)
            )
        )
        if not needs_render:
            continue
        if (domain, key) in seen:
            continue
        final_key = cascade.normalize_page_key(final_url or key)
        if final_key and (domain, final_key) in seen_final_urls:
            continue
        if per_domain_counts.get(domain, 0) >= cap:
            continue
        seen.add((domain, key))
        if final_key:
            seen_final_urls.add((domain, final_key))
        per_domain_counts[domain] = per_domain_counts.get(domain, 0) + 1
        selected.append(target)
    return selected


def export_page_rows(conn, rubric, domains, specs, include_unlisted=True, dedupe_final_url=False):
    rows = []
    specs_by_path = {s["path"]: s for s in specs}
    for d in domains:
        seen = set()
        seen_final_urls = set()
        for spec in specs:
            path = spec["path"]
            seen.add(cascade.normalize_page_key(path))
            pg = cascade.get_discovered_page(conn, d, path)
            if pg:
                final_url_key = cascade.normalize_page_key(pg.get("url") or "") if int(bool(pg.get("ok"))) else ""
                if dedupe_final_url and final_url_key and final_url_key in seen_final_urls:
                    continue
                if final_url_key:
                    seen_final_urls.add(final_url_key)
                evidence = cascade.page_evidence(pg, rubric, specs_by_path.get(path))
                rows.append({
                    "domain": d,
                    "page_type": spec["page_type"],
                    "path": path,
                    "url": pg.get("url") or "",
                    "status": pg.get("status") or 0,
                    "ok": int(bool(pg.get("ok"))),
                    "method": pg.get("tier") or "none",
                    "match_count": evidence["match_count"],
                    "matched_terms": evidence["matched_terms"],
                    "snippet": evidence["snippet"],
                    "linked_from_homepage": int(bool(pg.get("linked_from_homepage"))),
                })
            else:
                rows.append({
                    "domain": d,
                    "page_type": spec["page_type"],
                    "path": path,
                    "url": "",
                    "status": 0,
                    "ok": 0,
                    "method": "none",
                    "match_count": 0,
                    "matched_terms": [],
                    "snippet": "no page fetch in db",
                    "linked_from_homepage": int(bool(spec.get("linked_from_homepage"))),
                })
        if include_unlisted:
            for pg in cascade.list_discovered_pages(conn, d):
                key = cascade.normalize_page_key(pg.get("path"))
                if key in seen:
                    continue
                final_url_key = cascade.normalize_page_key(pg.get("url") or "") if int(bool(pg.get("ok"))) else ""
                if dedupe_final_url and final_url_key and final_url_key in seen_final_urls:
                    continue
                if final_url_key:
                    seen_final_urls.add(final_url_key)
                evidence = cascade.page_evidence(pg, rubric, None)
                rows.append({
                    "domain": d,
                    "page_type": pg.get("page_type") or "other",
                    "path": pg.get("path") or "",
                    "url": pg.get("url") or "",
                    "status": pg.get("status") or 0,
                    "ok": int(bool(pg.get("ok"))),
                    "method": pg.get("tier") or "none",
                    "match_count": evidence["match_count"],
                    "matched_terms": evidence["matched_terms"],
                    "snippet": evidence["snippet"],
                    "linked_from_homepage": int(bool(pg.get("linked_from_homepage"))),
                })
    return rows


def export_page_rows_for_targets(conn, rubric, domains, targets_by_domain, dedupe_final_url=True):
    rows = []
    for domain in domains:
        targets = targets_by_domain.get(domain, [])
        rows.extend(export_page_rows(
            conn,
            rubric,
            [domain],
            targets,
            include_unlisted=False,
            dedupe_final_url=dedupe_final_url,
        ))
    return rows


def record_candidate_targets(conn, targets):
    for target in targets:
        key = target.get("path") or target.get("url")
        if not key:
            continue
        existing = cascade.get_discovered_page(conn, target["domain"], key)
        if existing:
            if target.get("linked_from_homepage") and not existing.get("linked_from_homepage"):
                conn.execute(
                    "UPDATE discovered_pages SET linked_from_homepage=1 WHERE domain=? AND path=?",
                    (target["domain"], cascade.normalize_page_key(key)),
                )
            continue
        cascade.upsert_discovered_page(
            conn,
            target["domain"],
            key,
            target.get("page_type") or "other",
            target.get("url") or "",
            0,
            "candidate",
            False,
            "",
            commit=False,
            linked_from_homepage=bool(target.get("linked_from_homepage")),
        )


def persist_page_result(conn, res, tier):
    """Extract text, mine SSR JSON postings, diagnose a render hint, and upsert
    one fetched page result.

    All of it must happen here, at fetch time -- the cache stores text, not
    HTML, so a post-pass can never reconstruct the js-shell/stub diagnosis or
    re-mine the SSR blobs (bendingspoons: 35 postings lived only in the HTML)."""
    text = cascade.html_to_text(res["html"]) if res["ok"] else ""
    ssr = None
    if res.get("ok") and (res.get("page_type") or "") == "careers":
        ssr = ssr_json.postings_from_html(res.get("url") or "", res.get("html") or "")
        if ssr and ssr.get("postings"):
            # Same mechanism as "Embedded ATS job links:": synthetic text the
            # judge can read. The block's "N open roles" reads as strong careers
            # evidence, which also suppresses the shell_chrome render hint.
            text = (text or "").rstrip() + "\n\n" + ssr_json.format_postings_block(ssr) + "\n"
    hint = None
    if res.get("ok") and not (ssr and ssr.get("postings")):
        # No hint when SSR mining succeeded: the hint names what a render could
        # fix, and a render cannot improve on already-extracted structured rows
        # (a board whose visible text is thin would otherwise read as js_shell).
        homepage = cascade.get_page(conn, res["domain"]) or {}
        hint = cascade.render_hint_for(res.get("html", ""), text,
                                       homepage_text=homepage.get("text"),
                                       page_type=res.get("page_type"))
    cascade.upsert_discovered_page(
        conn,
        res["domain"],
        res["path"],
        res["page_type"],
        res["url"],
        res["status"],
        tier,
        res["ok"],
        text,
        commit=False,
        linked_from_homepage=bool(res.get("linked_from_homepage")),
        render_hint=hint,
    )
    if ssr and ssr.get("postings"):
        cascade.replace_ssr_postings(
            conn,
            res["domain"],
            cascade.normalize_page_key(res.get("url") or res.get("path") or "/"),
            ssr.get("source") or "",
            ssr["postings"],
            commit=False,
        )


def _careers_fetch_is_wrong_page(res):
    """An ok careers fetch that landed somewhere that is not really the careers
    page: a divergent redirect to a non-careers path (carestack /careers ->
    /company) or page text with no strong careers evidence. The real anchor is
    usually in the HTML in hand RIGHT NOW -- the cache stores text, not HTML, so
    fetch time is the only moment the broader re-miner can run."""
    final_url = res.get("url") or ""
    final_host = cascade._host(final_url)
    if any(cascade._host_matches_hint(final_host, h) for h in cascade.ATS_HOST_HINTS):
        return False  # landed on an ATS board: that IS the careers page
    # Acquirer/rebrand hop: a redirect to a different registrable domain is the
    # wrong page even when it lands on /careers with strong hiring text — those
    # postings belong to the other entity. Only the domain comparison catches
    # that case; the path and text checks below both pass it.
    if cascade.detect_acquirer_redirect(final_url, res.get("domain") or "")["acquired"]:
        return True
    key_moved = cascade.normalize_page_key(final_url) != (res.get("path") or "")
    landed_not_careers = cascade.page_type_for_path(urlparse(final_url).path or "/") != "careers"
    if key_moved and landed_not_careers:
        return True
    text = cascade.html_to_text(res.get("html") or "")
    return not BROWSER_RESCUE_STRONG_CAREERS_RE.search(text)


def discover_child_career_targets(conn, res, link_targets):
    if not res.get("ok") or (res.get("page_type") or "") != "careers" or not res.get("html"):
        return []
    base_url = res.get("url") or ("https://" + res["domain"])
    found = cascade.candidate_child_career_targets_from_html(
        res["domain"],
        base_url,
        res["html"],
        max_links=20,
    )
    if _careers_fetch_is_wrong_page(res):
        # wrong-page re-mine: run the broader homepage-grade miner over this HTML,
        # keep only careers targets, dedupe against the narrow miner's finds.
        seen_keys = {cascade.normalize_page_key(t.get("path") or t.get("url") or "") for t in found}
        for t in cascade.candidate_page_targets_from_html(res["domain"], base_url, res["html"]):
            if (t.get("page_type") or "") != "careers":
                continue
            key = cascade.normalize_page_key(t.get("path") or t.get("url") or "")
            if not key or key in seen_keys or key == (res.get("path") or ""):
                continue
            seen_keys.add(key)
            found.append(t)
    if not found:
        return []
    link_targets.setdefault(res["domain"], []).extend(found)
    record_candidate_targets(conn, found)
    return found


def domain_is_unresolved(conn, domain):
    """True when no stored page proves this domain's careers presence: no ok
    ATS-host page with text and no ok careers page with strong careers text."""
    for pg in cascade.list_discovered_pages(conn, domain):
        if not pg.get("ok"):
            continue
        text = (pg.get("text") or "")
        host = cascade._host(str(pg.get("url") or ""))
        if text.strip() and any(cascade._host_matches_hint(host, h) for h in cascade.ATS_HOST_HINTS):
            return False
        if (pg.get("page_type") or "") == "careers" and BROWSER_RESCUE_STRONG_CAREERS_RE.search(text):
            return False
    return True


FOLLOWUP_CAREERS_BUDGET = 3


def select_followup_careers_targets(conn, domain, budget=FOLLOWUP_CAREERS_BUDGET):
    """Bounded last-chance rescue for a domain that is still unresolved after the
    normal stages: its unfetched careers candidates, best-first, NO linked
    requirement (re-mined wrong-page anchors carry linked=0 -- exactly the rows
    the quota-driven candidate_only filter leaves stranded). One round, on top
    of (not inside) the page quota; resolved domains return nothing."""
    if not domain_is_unresolved(conn, domain):
        return []
    cands = []
    for pg in cascade.list_discovered_pages(conn, domain):
        if (pg.get("tier") or "") != "candidate" or (pg.get("page_type") or "") != "careers":
            continue
        key = pg.get("url") or pg.get("path") or ""
        if is_bad_browser_rescue_url(str(key)):
            continue
        cands.append({
            "domain": domain,
            "path": pg.get("path"),
            "url": pg.get("url") or None,
            "page_type": "careers",
            "linked_from_homepage": bool(pg.get("linked_from_homepage")),
        })
    cands = merge_page_targets(cands)
    return cascade.select_page_specs(cands, max_pages_per_domain=budget)


# Canonical board URL that cascade.detect_ats + ATS_HOST_HINTS round-trip on, so a
# slug-guessed board flows through the normal discovered_pages -> feature pipeline
# (the downstream extractor re-detects the ATS host and counts it authoritatively).
_BOARD_URL_BUILDERS = {
    "greenhouse": lambda s: "https://boards.greenhouse.io/%s" % s,
    "lever": lambda s: "https://jobs.lever.co/%s" % s,
    "ashby": lambda s: "https://jobs.ashbyhq.com/%s" % s,
    "smartrecruiters": lambda s: "https://jobs.smartrecruiters.com/%s" % s,
    "workable": lambda s: "https://apply.workable.com/%s" % s,
    "recruitee": lambda s: "https://%s.recruitee.com/" % s,
    "pinpoint": lambda s: "https://%s.pinpointhq.com/" % s,
    "rippling": lambda s: "https://ats.rippling.com/%s/jobs" % s,
}


def board_url_for(ats, slug):
    fn = _BOARD_URL_BUILDERS.get(ats)
    return fn(slug) if (fn and slug) else None


def _domain_link_hosts(link_targets, domain):
    has_ats = has_careers = False
    for t in link_targets.get(domain, []):
        host = cascade._host(t.get("url") or t.get("path") or "")
        if host and any(cascade._host_matches_hint(host, h) for h in cascade.ATS_HOST_HINTS):
            has_ats = True
        if (t.get("page_type") or "") == "careers":
            has_careers = True
    return has_ats, has_careers


def _has_stored_careers_or_ats(conn, domain):
    """True if a prior run already found a careers/ATS page for this domain (ok).

    Lets re-runs over the same pool skip the ATS-API probe entirely — critical at
    large-list scale so we don't re-hit Greenhouse/Lever for every domain every run.
    """
    for pg in cascade.list_discovered_pages(conn, domain):
        if not pg.get("ok"):
            continue
        host = cascade._host(pg.get("url") or pg.get("path") or "")
        if (pg.get("page_type") or "") == "careers":
            return True
        if host and any(cascade._host_matches_hint(host, h) for h in cascade.ATS_HOST_HINTS):
            return True
    return False


def discover_via_apis_and_indexes(conn, domains, link_targets, timeout=15.0,
                                  max_workers=12, skip_known=True):
    """HTML-independent careers discovery (net-new 2026-06-04 research spec).

    * ATS slug-guess via the 8 public board APIs — runs for served AND WAF-blocked
      apexes (the slug is derived from the domain, not the HTML, so it is the sole
      finder for blocked/SPA/unlisted boards). Skipped only when the homepage tier
      already surfaced an ATS board link, or a prior run already found one.
    * robots.txt + sitemap (with index recursion) for SERVED-but-link-less homepages
      (js_shell/thin splashes); skipped for blocked apexes (they 403 those too).

    Any board/careers URL found is persisted as a candidate target so it rides the
    existing fetch + feature pipeline. Returns a stats dict.
    """
    def _eligible(d):
        # slug-guess is HTML-independent: include ok + WAF-blocked (server alive, >=400);
        # skip only DNS/connection-dead domains (status 0) to bound cost at scale.
        pg = cascade.get_page(conn, d)
        if pg is None or pg.get("ok"):
            return True
        return int(pg.get("status") or 0) >= 400

    candidates = [d for d in domains if _eligible(d)]
    if skip_known:
        candidates = [d for d in candidates if not _has_stored_careers_or_ats(conn, d)]
    ok_set = {d for d in candidates if (cascade.get_page(conn, d) or {}).get("ok")}

    def work(d):
        has_ats, has_careers = _domain_link_hosts(link_targets, d)
        out = []
        if not has_ats:
            try:
                hit = ats_api.count_open_roles_by_slug(d, timeout=timeout)
            except Exception:
                hit = None
            if hit:
                burl = board_url_for(hit.get("ats"), hit.get("slug") or hit.get("slug_variant"))
                if burl:
                    out.append(("slug", burl))
        # index methods need a served homepage; a 403 apex 403s robots/sitemap too.
        if d in ok_set and not has_ats and not has_careers:
            try:
                fetch = lambda u: cascade.fetch_text(u, timeout)  # noqa: E731
                rb = cascade.discover_via_robots(d, fetch)
                for u in rb.get("careers_urls", [])[:5]:
                    out.append(("index", u))
                for u in cascade.discover_via_sitemap(d, fetch, extra_sitemaps=rb.get("sitemaps"))[:5]:
                    out.append(("index", u))
            except Exception:
                pass
        return d, out

    new_targets, stats = [], {"slug_boards": 0, "index_urls": 0, "probed": len(candidates)}
    if not candidates:
        return stats
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for d, out in ex.map(work, candidates):
            for kind, url in out:
                target = {"domain": d, "path": url, "url": url,
                          "page_type": "careers", "linked_from_homepage": False}
                new_targets.append(target)
                link_targets.setdefault(d, []).append(target)
                stats["slug_boards" if kind == "slug" else "index_urls"] += 1
    if new_targets:
        record_candidate_targets(conn, new_targets)
        conn.commit()
    return stats


def write_page_outputs(rows, output_path):
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "domain",
            "page_type",
            "path",
            "url",
            "status",
            "ok",
            "method",
            "match_count",
            "matched_terms",
            "snippet",
            "linked_from_homepage",
        ])
        for r in rows:
            w.writerow([
                csv_safe(r["domain"]),
                csv_safe(r["page_type"]),
                csv_safe(r["path"]),
                csv_safe(r["url"]),
                r["status"],
                r["ok"],
                csv_safe(r["method"]),
                r["match_count"],
                csv_safe(json.dumps(r["matched_terms"])),
                csv_safe(r["snippet"]),
                r.get("linked_from_homepage", 0),
            ])
    jsonl_path = str(Path(output_path).with_suffix(".jsonl"))
    with open(jsonl_path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return jsonl_path


def allowed_keys_from_page_rows(rows):
    allowed = {}
    for row in rows or []:
        allowed.setdefault(row["domain"], set()).add(cascade.normalize_page_key(row["path"] or row["url"]))
    return allowed


def export_evidence_rows(conn, domains, allowed_keys_by_domain=None):
    rows = []
    for d in domains:
        homepage = cascade.get_page(conn, d) or {}
        for pg in cascade.list_discovered_pages(conn, d):
            page_rows = cascade.validated_evidence_rows(
                d,
                homepage.get("url") or "",
                homepage.get("text") or "",
                pg,
            )
            if allowed_keys_by_domain is not None:
                key = cascade.normalize_page_key(pg.get("path") or pg.get("url"))
                if key not in allowed_keys_by_domain.get(d, set()):
                    extra_linked_ats_hiring = any(
                        row.get("source_trust") == "linked_ats_candidate"
                        and row.get("evidence_status") == "trusted_source_candidate"
                        and row.get("evidence_type") == "hiring_activity"
                        for row in page_rows
                    )
                    if not extra_linked_ats_hiring:
                        continue
            rows.extend(page_rows)
    return rows


def write_evidence_outputs(rows, output_path):
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "domain",
        "source_url",
        "source_host",
        "page_type",
        "source_trust",
        "domain_hygiene",
        "evidence_tier",
        "evidence_type",
        "evidence_status",
        "match_count",
        "matched_terms",
        "snippet",
        "reason",
    ]
    with open(output_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            row = dict(r)
            row["matched_terms"] = json.dumps(row.get("matched_terms") or [])
            w.writerow({field: csv_safe(row.get(field, "")) for field in fields})
    jsonl_path = str(Path(output_path).with_suffix(".jsonl"))
    with open(jsonl_path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return jsonl_path


def print_evidence_summary(rows, output_path, jsonl_path):
    by_status, by_type, by_trust, by_tier = {}, {}, {}, {}
    for r in rows:
        by_status[r["evidence_status"]] = by_status.get(r["evidence_status"], 0) + 1
        by_type[r["evidence_type"]] = by_type.get(r["evidence_type"], 0) + 1
        by_trust[r["source_trust"]] = by_trust.get(r["source_trust"], 0) + 1
        by_tier[r.get("evidence_tier") or ""] = by_tier.get(r.get("evidence_tier") or "", 0) + 1
    print("\n=== evidence candidates ===")
    print("  rows             %d" % len(rows))
    print("--- status ---")
    for k in sorted(by_status):
        print("  %-26s %d" % (k, by_status[k]))
    print("--- evidence_type ---")
    for k in sorted(by_type):
        print("  %-26s %d" % (k, by_type[k]))
    print("--- source_trust ---")
    for k in sorted(by_trust):
        print("  %-26s %d" % (k, by_trust[k]))
    print("--- evidence_tier ---")
    for k in sorted(by_tier):
        print("  %-26s %d" % (k, by_tier[k]))
    print("evidence csv:   %s" % output_path)
    print("evidence jsonl: %s" % jsonl_path)


def print_page_summary(rows, output_path, jsonl_path):
    by_type, with_matches, ok = {}, 0, 0
    for r in rows:
        by_type[r["page_type"]] = by_type.get(r["page_type"], 0) + 1
        if int(r["ok"]):
            ok += 1
        if int(r["match_count"]):
            with_matches += 1
    print("\n=== discovered pages ===")
    print("  rows             %d" % len(rows))
    print("  fetched_ok        %d" % ok)
    print("  with_matches      %d" % with_matches)
    print("--- page_type ---")
    for k in sorted(by_type):
        print("  %-16s %d" % (k, by_type[k]))
    print("pages csv:   %s" % output_path)
    print("pages jsonl: %s" % jsonl_path)


def print_summary(rows, judged, output_path, jsonl_path, rubric_name, judge_provider):
    counts, methods = {}, {}
    for r in rows:
        counts[r["label"]] = counts.get(r["label"], 0) + 1
        methods[r["method"]] = methods.get(r["method"], 0) + 1
    print("\n=== verdicts (%s) ===" % rubric_name)
    for k in sorted(counts):
        print("  %-16s %d" % (k, counts[k]))
    print("--- method ---")
    for k in sorted(methods):
        print("  %-16s %d" % (k, methods[k]))
    print("judge (%s) calls this run: %d" % (judge_provider, judged))
    print("csv:   %s" % output_path)
    print("jsonl: %s" % jsonl_path)


def main():
    ap = argparse.ArgumentParser(description="Tiered free-first scrape + classify cascade")
    ap.add_argument("--rubric", required=False, help="rubric YAML (required unless --doctor)")
    ap.add_argument("--input", required=False,
                    help="domains file (one per line, or CSV w/ domain in col 1); required unless --doctor")
    ap.add_argument("--db", default=str(SKILL_DIR / "data" / "cache.db"))
    ap.add_argument("--output", default=str(SKILL_DIR / "data" / "results.csv"))
    ap.add_argument("--domain-column", default=None,
                    help="CSV domain column name or zero-based index (auto-detects domain/website/url)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=50, help="Tier-1 in-flight requests")
    ap.add_argument("--chunk-size", type=int, default=500, help="Tier-1 fetch+persist chunk (bounds peak memory)")
    ap.add_argument("--timeout", type=float, default=20.0, help="Tier-1 per-request timeout (s)")
    ap.add_argument("--jina", action="store_true",
                    help="enable the free, keyless Jina Reader (r.jina.ai) render tier for empty/blocked "
                         "domains -- one HTTP GET, no local browser. Rate-limited (best for the small "
                         "post-Tier-1 residue); public URLs only (third-party proxy). Runs before Playwright.")
    ap.add_argument("--rescue", action="store_true", help="enable Tier 2 (Playwright) for empty/blocked")
    ap.add_argument("--stealth", action="store_true",
                    help="enable Tier 3 stealth browser (Camoufox -- ships its own Firefox, works under "
                         "MDM, clears Cloudflare; falls back to undetected-chromedriver). Mops up domains "
                         "still empty after Tiers 1/2.")
    ap.add_argument("--undetected", action="store_true",
                    help="back-compat alias for --stealth (Tier 3 now prefers Camoufox over uc)")
    ap.add_argument("--doctor", action="store_true",
                    help="report which fetch tiers are actually live on this machine, then exit "
                         "(no --rubric/--input needed)")
    ap.add_argument("--no-judge", action="store_true", help="skip the LLM judge; leave ambiguous")
    ap.add_argument("--judge-concurrency", type=int, default=4, help="parallel LLM judges")
    ap.add_argument("--judge-provider", choices=["auto", "codex", "claude"], default="auto",
                    help="LLM judge provider (default: auto; can also use $SCRAPE_CASCADE_JUDGE_PROVIDER)")
    ap.add_argument("--judge-bin", default=None, help="override judge binary (or use $CODEX_BIN / $CLAUDE_BIN)")
    ap.add_argument("--judge-model", default=None, help="model for the judge (provider default unless set)")
    ap.add_argument("--judge-evidence-window", action="store_true",
                    help="feed the judge evidence-dense windows (head + mined postings + "
                         "rubric-term context) instead of a blind head-truncation; helps long "
                         "careers/capacity pages whose signal is appended past the truncation cut")
    ap.add_argument("--refetch", action="store_true", help="ignore cached pages and re-fetch (implies --rejudge)")
    ap.add_argument("--rejudge", action="store_true", help="ignore cached verdicts and re-score/re-judge")
    ap.add_argument("--export-only", action="store_true", help="skip all work; just (re)write CSV/JSONL from the DB")
    ap.add_argument("--discover-pages", action="store_true",
                    help="fetch rubric crawl_paths and emit page-level discovery/evidence rows")
    ap.add_argument("--no-api-discovery", dest="api_discovery", action="store_false",
                    help="disable the HTML-independent ATS-slug-guess + sitemap/robots careers "
                         "discovery pass (on by default with --discover-pages)")
    ap.set_defaults(api_discovery=True)
    ap.add_argument("--pages-output", default=None,
                    help="CSV for --discover-pages output (default: <output stem>_pages.csv)")
    ap.add_argument("--max-pages-per-domain", type=int, default=None,
                    help="cap rubric crawl_paths per domain during discovery/testing")
    ap.add_argument("--page-type-quota", default=None,
                    help="balanced page selection quotas, e.g. careers=2,news=2,security=1")
    ap.add_argument("--page-type-order", default=None,
                    help="comma-separated page type priority for balanced selection")
    ap.add_argument("--browser-rescue-pages", action="store_true",
                    help="render/scroll selected careers pages whose listings may lazy-load")
    ap.add_argument("--browser-rescue-per-domain", type=int, default=6,
                    help="max careers pages rendered per domain by --browser-rescue-pages "
                         "(the single knob bounding browser-tier volume)")
    ap.add_argument("--browser-page-wait-ms", type=int, default=900,
                    help="settle/click wait budget for --browser-rescue-pages (milliseconds)")
    ap.add_argument("--extract-evidence", action="store_true",
                    help="emit source-trust/domain-hygiene/evidence candidate rows from discovered pages")
    ap.add_argument("--evidence-output", default=None,
                    help="CSV for --extract-evidence output (default: <output stem>_evidence.csv)")
    ap.add_argument("--render-assets", action="store_true",
                    help="load images/media/fonts in the browser tier (default: blocked to save "
                         "bandwidth/memory). Per-run kill switch if a site needs assets to render its board.")
    args = ap.parse_args()
    if args.doctor:
        cascade.doctor()
        return
    if not args.rubric or not args.input:
        ap.error("--rubric and --input are required (unless --doctor)")
    if args.refetch:
        args.rejudge = True  # re-fetched pages must be re-scored; don't trust stale verdicts
    if args.render_assets:
        cascade.BLOCK_BROWSER_ASSETS = False  # per-run override of the default-on asset block

    rubric = load_rubric(args.rubric)
    domains = read_domains(args.input, args.limit, args.domain_column)
    conn = cascade.connect(args.db)
    try:
        judge_provider = "disabled" if args.no_judge else cascade.resolve_judge_provider(args.judge_provider)
    except ValueError as e:
        sys.exit(str(e))
    print("loaded %d domains | rubric=%s" % (len(domains), rubric["name"]))
    try:
        page_type_quota = cascade.parse_page_type_quota(args.page_type_quota)
        page_type_order = cascade.parse_page_type_order(args.page_type_order)
    except ValueError as e:
        sys.exit(str(e))
    all_page_specs = cascade.page_specs_from_rubric(rubric)
    link_targets = {}

    # ---- recovery path: dump whatever the DB already holds and exit
    if args.export_only:
        rows = export_rows(conn, rubric["name"], domains)
        jsonl_path = write_outputs(rows, args.output)
        print_summary(rows, 0, args.output, jsonl_path, rubric["name"], judge_provider)
        page_rows_for_evidence = None
        if args.discover_pages:
            pages_output = args.pages_output or default_pages_output(args.output)
            targets_by_domain = {
                d: select_targets_for_domain(
                    conn,
                    d,
                    all_page_specs,
                    max_pages_per_domain=args.max_pages_per_domain,
                    page_type_quota=page_type_quota,
                    page_type_order=page_type_order,
                )
                for d in domains
            }
            page_rows = export_page_rows_for_targets(conn, rubric, domains, targets_by_domain)
            pages_jsonl = write_page_outputs(page_rows, pages_output)
            print_page_summary(page_rows, pages_output, pages_jsonl)
            page_rows_for_evidence = page_rows
        if args.extract_evidence:
            evidence_output = args.evidence_output or default_evidence_output(args.output)
            allowed = allowed_keys_from_page_rows(page_rows_for_evidence) if args.max_pages_per_domain is not None else None
            evidence_rows = export_evidence_rows(conn, domains, allowed)
            evidence_jsonl = write_evidence_outputs(evidence_rows, evidence_output)
            print_evidence_summary(evidence_rows, evidence_output, evidence_jsonl)
        return

    # ---- Tier 1: httpx fast pass, in bounded chunks, persisting each before the next
    todo = []
    for d in domains:
        pg = None if args.refetch else cascade.get_page(conn, d)
        if not pg or not pg["ok"]:
            todo.append(d)
    if todo:
        print("tier1 httpx: fetching %d (concurrency=%d, chunk=%d) ..."
              % (len(todo), args.concurrency, args.chunk_size))
        for i in range(0, len(todo), args.chunk_size):
            chunk = todo[i:i + args.chunk_size]
            for res in cascade.fetch_batch_httpx(chunk, args.concurrency, args.timeout):
                text = cascade.html_to_text(res["html"]) if res["ok"] else ""
                if args.discover_pages and res["ok"]:
                    found = cascade.candidate_page_targets_from_html(res["domain"], res["url"], res["html"])
                    if not found and cascade.looks_like_thin_splash(res["html"], found):
                        hop_domains = cascade.outbound_entity_domains_from_html(
                            res["domain"], res["url"], res["html"]
                        )
                        if hop_domains:
                            for hop in cascade.fetch_batch_httpx(hop_domains, len(hop_domains), args.timeout):
                                if hop["ok"]:
                                    hop_found = cascade.candidate_page_targets_from_html(
                                        hop["domain"], hop["url"], hop["html"]
                                    )
                                    found.extend(hop_found)
                    link_targets.setdefault(res["domain"], []).extend(found)
                    record_candidate_targets(conn, found)
                cascade.upsert_page(conn, res["domain"], res["url"], res["status"],
                                    "httpx", res["ok"], text, commit=False)
            conn.commit()  # checkpoint each chunk: a crash loses at most this chunk
            print("  tier1 persisted %d/%d" % (min(i + args.chunk_size, len(todo)), len(todo)))

    # ---- Tiers 1.5/2/3: rescue the empties (opt-in). Free/cheap tiers escalate first:
    # Jina (one GET) -> Playwright (local browser) -> stealth (Camoufox/uc). Each tier
    # only touches domains still empty after the cheaper ones ran.

    # Tier 1.5: Jina Reader -- free, keyless, render-capable; no local browser. Stores
    # the reader's clean text directly (NOT html2text'd). Public URLs only; rate-limited.
    if args.jina:
        jina_empties = [d for d in domains if not (cascade.get_page(conn, d) or {}).get("ok")]
        print("rescue(jina): %d domains via r.jina.ai ..." % len(jina_empties))
        jdone = 0
        for d in jina_empties:
            try:
                got = cascade._jina_fetch(d)
            except Exception:
                got = None
            if got and got.get("ok"):
                cascade.upsert_page(conn, d, got.get("url", "https://" + d),
                                    got.get("status", 200), "jina", True,
                                    got.get("text") or "", commit=False)
            jdone += 1
            if jdone % 10 == 0 or jdone == len(jina_empties):
                conn.commit()
                print("  rescue(jina) %d/%d" % (jdone, len(jina_empties)))
        conn.commit()

    # Tier 2: Playwright -- one browser reused across the batch.
    if args.rescue:
        empties = [d for d in domains if not (cascade.get_page(conn, d) or {}).get("ok")]
        print("rescue(playwright): %d domains need a browser ..." % len(empties))
        done = 0
        try:
            for d, got in cascade.rescue_playwright_batch(empties):
                ok = len(got["html"]) >= cascade.MIN_OK_HTML
                text = cascade.html_to_text(got["html"]) if ok else ""
                if args.discover_pages and ok:
                    found = cascade.candidate_page_targets_from_html(d, "https://" + d, got["html"])
                    if not found and cascade.looks_like_thin_splash(got["html"], found):
                        hop_domains = cascade.outbound_entity_domains_from_html(
                            d, "https://" + d, got["html"]
                        )
                        if hop_domains:
                            for hop in cascade.fetch_batch_httpx(hop_domains, len(hop_domains), args.timeout):
                                if hop["ok"]:
                                    hop_found = cascade.candidate_page_targets_from_html(
                                        hop["domain"], hop["url"], hop["html"]
                                    )
                                    found.extend(hop_found)
                    link_targets.setdefault(d, []).extend(found)
                    record_candidate_targets(conn, found)
                cascade.upsert_page(conn, d, "https://" + d, got.get("status", 0),
                                    "playwright", ok, text, commit=False)
                done += 1
                if done % 20 == 0 or done == len(empties):
                    conn.commit()
                    print("  rescue(playwright) %d/%d" % (done, len(empties)))
            conn.commit()
        except Exception as e:
            conn.commit()  # keep everything persisted so far; browser death != run death
            print("  rescue(playwright) aborted (%s) after %d; stealth mop-up will retry residue"
                  % (type(e).__name__, done))

    # Tier 3: stealth browser -- Camoufox (ships its own Firefox -> launches under MDM,
    # clears Cloudflare) with undetected-chromedriver as a legacy fallback. Mops up
    # whatever is still empty after Tiers 1/2, regardless of which of those ran.
    if args.stealth or args.undetected:
        still = [d for d in domains if not (cascade.get_page(conn, d) or {}).get("ok")]
        print("rescue(stealth): %d still empty ..." % len(still))
        for n, d in enumerate(still, 1):
            try:
                got = cascade.fetch_stealth(d)
            except Exception:
                got = {"html": "", "status": 0}
            ok = len(got["html"]) >= cascade.MIN_OK_HTML
            text = cascade.html_to_text(got["html"]) if ok else ""
            if args.discover_pages and ok:
                found = cascade.candidate_page_targets_from_html(d, "https://" + d, got["html"])
                if not found and cascade.looks_like_thin_splash(got["html"], found):
                    hop_domains = cascade.outbound_entity_domains_from_html(
                        d, "https://" + d, got["html"]
                    )
                    if hop_domains:
                        for hop in cascade.fetch_batch_httpx(hop_domains, len(hop_domains), args.timeout):
                            if hop["ok"]:
                                hop_found = cascade.candidate_page_targets_from_html(
                                    hop["domain"], hop["url"], hop["html"]
                                )
                                found.extend(hop_found)
                link_targets.setdefault(d, []).extend(found)
                record_candidate_targets(conn, found)
            cascade.upsert_page(conn, d, "https://" + d, got.get("status", 0),
                                "stealth", ok, text, commit=False)
            if n % 10 == 0 or n == len(still):
                conn.commit()
                print("  rescue(stealth) %d/%d" % (n, len(still)))
        conn.commit()

    # ---- optional page-aware discovery: fetch configured source paths for every domain.
    # This is candidate evidence acquisition only; strict source validation belongs to
    # downstream extractors.
    if args.discover_pages:
        # Only the careers use-case wants the ATS-API/sitemap/robots tier; gate it to
        # careers rubrics so non-careers consumers (trust-center, etc.) don't pay the
        # ATS-probe tax on every domain.
        careers_in_rubric = any((s.get("page_type") == "careers") for s in (all_page_specs or []))
        if getattr(args, "api_discovery", True) and careers_in_rubric:
            api_stats = discover_via_apis_and_indexes(
                conn, domains, link_targets,
                # ATS APIs are CDN-fast; cap below the 20s page timeout so a few hanging
                # probes don't dominate wall-clock across a large pool.
                timeout=min(args.timeout, 12.0),
                max_workers=min(getattr(args, "concurrency", 12) or 12, 12),
                skip_known=not args.refetch,
            )
            if api_stats.get("slug_boards") or api_stats.get("index_urls"):
                print("discover-pages api/index: probed %d domains -> +%d ATS boards via "
                      "slug-guess, +%d careers URLs via sitemap/robots"
                      % (api_stats.get("probed", 0), api_stats["slug_boards"], api_stats["index_urls"]))
        has_stored_pages = False if args.refetch else any(cascade.list_discovered_pages(conn, d) for d in domains)
        if not all_page_specs and not any(link_targets.values()) and not has_stored_pages:
            print("discover-pages: rubric has no crawl_paths; skipping page discovery")
        else:
            targets = []
            all_selected_targets = []
            selected_targets_by_domain = {}
            child_targets = []
            for d in domains:
                selected = select_targets_for_domain(
                    conn,
                    d,
                    all_page_specs,
                    link_targets.get(d, []),
                    max_pages_per_domain=args.max_pages_per_domain,
                    page_type_quota=page_type_quota,
                    page_type_order=page_type_order,
                    include_stored=not args.refetch,
                )
                selected_targets_by_domain[d] = selected
                all_selected_targets.extend(selected)
                for target in selected:
                    targets.extend(targets_needing_fetch(conn, [target], refetch=args.refetch))
            if targets:
                print("discover-pages httpx: fetching %d pages (%d domains x %d paths max) ..."
                      % (len(targets), len(domains), args.max_pages_per_domain or len(all_page_specs)))
                for i in range(0, len(targets), args.chunk_size):
                    chunk = targets[i:i + args.chunk_size]
                    for res in cascade.fetch_batch_pages_httpx(chunk, args.concurrency, args.timeout):
                        try:
                            persist_page_result(conn, res, "httpx")
                            child_targets.extend(discover_child_career_targets(conn, res, link_targets))
                        except Exception as _exc:
                            print("[scrape-cascade] %s/%s raised %s in persist; skipping: %s"
                                  % (res.get("domain", "?"), res.get("path", "?"),
                                     type(_exc).__name__, _exc), file=sys.stderr)
                    conn.commit()
                    print("  discover-pages persisted %d/%d"
                          % (min(i + args.chunk_size, len(targets)), len(targets)))
            else:
                print("discover-pages: all configured pages already fetched (use --refetch to redo)")
            if args.browser_rescue_pages:
                rescue_targets = select_browser_rescue_page_targets(
                    conn, all_selected_targets, per_domain_cap=args.browser_rescue_per_domain)
                if rescue_targets:
                    print("discover-pages playwright: rendering %d careers pages ..." % len(rescue_targets))
                    done = 0
                    try:
                        for res in cascade.rescue_playwright_page_batch(
                            rescue_targets,
                            timeout=args.timeout,
                            per_page_wait=args.browser_page_wait_ms,
                        ):
                            persist_page_result(conn, res, "playwright-page")
                            child_targets.extend(discover_child_career_targets(conn, res, link_targets))
                            done += 1
                            conn.commit()
                            if done % 5 == 0 or done == len(rescue_targets):
                                print("  discover-pages playwright %d/%d" % (done, len(rescue_targets)))
                    except Exception as e:
                        conn.commit()
                        print("  discover-pages playwright aborted (%s) after %d" % (type(e).__name__, done))
                else:
                    print("discover-pages playwright: no selected careers pages needed browser rescue")
            child_targets = merge_page_targets(child_targets)
            for d in domains:
                selected = select_targets_for_domain(
                    conn,
                    d,
                    all_page_specs,
                    link_targets.get(d, []),
                    max_pages_per_domain=args.max_pages_per_domain,
                    page_type_quota=page_type_quota,
                    page_type_order=page_type_order,
                    include_stored=not args.refetch,
                )
                child_targets.extend(targets_needing_fetch(conn, selected, refetch=args.refetch, candidate_only=True))
            child_targets = merge_page_targets(child_targets)
            child_fetch_targets = []
            for target in child_targets:
                child_fetch_targets.extend(targets_needing_fetch(conn, [target], refetch=args.refetch))
            if child_fetch_targets:
                nested_child_targets = []
                print("discover-pages child links: fetching %d company-linked careers pages ..." % len(child_fetch_targets))
                for i in range(0, len(child_fetch_targets), args.chunk_size):
                    chunk = child_fetch_targets[i:i + args.chunk_size]
                    for res in cascade.fetch_batch_pages_httpx(chunk, args.concurrency, args.timeout):
                        try:
                            persist_page_result(conn, res, "httpx")
                            nested_child_targets.extend(discover_child_career_targets(conn, res, link_targets))
                        except Exception as _exc:
                            print("[scrape-cascade] %s/%s raised %s in child-links discover; skipping: %s"
                                  % (res.get("domain", "?"), res.get("path", "?"),
                                     type(_exc).__name__, _exc), file=sys.stderr)
                    conn.commit()
                    print("  discover-pages child links persisted %d/%d"
                          % (min(i + args.chunk_size, len(child_fetch_targets)), len(child_fetch_targets)))
                if args.browser_rescue_pages:
                    child_rescue_targets = select_browser_rescue_page_targets(
                        conn, child_fetch_targets, per_domain_cap=args.browser_rescue_per_domain)
                    if child_rescue_targets:
                        print("discover-pages child playwright: rendering %d careers pages ..." % len(child_rescue_targets))
                        done = 0
                        try:
                            for res in cascade.rescue_playwright_page_batch(
                                child_rescue_targets,
                                timeout=args.timeout,
                                per_page_wait=args.browser_page_wait_ms,
                            ):
                                try:
                                    persist_page_result(conn, res, "playwright-page")
                                    nested_child_targets.extend(discover_child_career_targets(conn, res, link_targets))
                                except Exception as _exc:
                                    print("[scrape-cascade] %s/%s raised %s in child-playwright discover; skipping: %s"
                                          % (res.get("domain", "?"), res.get("path", "?"),
                                             type(_exc).__name__, _exc), file=sys.stderr)
                                done += 1
                                conn.commit()
                                if done % 5 == 0 or done == len(child_rescue_targets):
                                    print("  discover-pages child playwright %d/%d" % (done, len(child_rescue_targets)))
                        except Exception as e:
                            conn.commit()
                            print("  discover-pages child playwright aborted (%s) after %d" % (type(e).__name__, done))
                nested_child_targets = merge_page_targets(nested_child_targets)
                nested_fetch_targets = []
                for target in nested_child_targets:
                    key = target.get("path") or target.get("url")
                    pg = None if args.refetch else cascade.get_discovered_page(conn, target["domain"], key)
                    if not pg or not pg.get("ok"):
                        nested_fetch_targets.append(target)
                if nested_fetch_targets:
                    print("discover-pages nested child links: fetching %d company-linked careers pages ..." % len(nested_fetch_targets))
                    for i in range(0, len(nested_fetch_targets), args.chunk_size):
                        chunk = nested_fetch_targets[i:i + args.chunk_size]
                        for res in cascade.fetch_batch_pages_httpx(chunk, args.concurrency, args.timeout):
                            persist_page_result(conn, res, "httpx")
                        conn.commit()
                        print("  discover-pages nested child links persisted %d/%d"
                              % (min(i + args.chunk_size, len(nested_fetch_targets)), len(nested_fetch_targets)))
                    if args.browser_rescue_pages:
                        nested_rescue_targets = select_browser_rescue_page_targets(
                            conn, nested_fetch_targets, per_domain_cap=args.browser_rescue_per_domain)
                        if nested_rescue_targets:
                            print("discover-pages nested child playwright: rendering %d careers pages ..." % len(nested_rescue_targets))
                            done = 0
                            try:
                                for res in cascade.rescue_playwright_page_batch(
                                    nested_rescue_targets,
                                    timeout=args.timeout,
                                    per_page_wait=args.browser_page_wait_ms,
                                ):
                                    persist_page_result(conn, res, "playwright-page")
                                    done += 1
                                    conn.commit()
                                    if done % 5 == 0 or done == len(nested_rescue_targets):
                                        print("  discover-pages nested child playwright %d/%d" % (done, len(nested_rescue_targets)))
                            except Exception as e:
                                conn.commit()
                                print("  discover-pages nested child playwright aborted (%s) after %d" % (type(e).__name__, done))
            final_candidate_targets = []
            for d in domains:
                selected = select_targets_for_domain(
                    conn,
                    d,
                    all_page_specs,
                    link_targets.get(d, []),
                    max_pages_per_domain=args.max_pages_per_domain,
                    page_type_quota=page_type_quota,
                    page_type_order=page_type_order,
                    include_stored=not args.refetch,
                )
                final_candidate_targets.extend(
                    targets_needing_fetch(conn, selected, refetch=args.refetch, candidate_only=True)
                )
            final_candidate_targets = merge_page_targets(final_candidate_targets)
            if final_candidate_targets:
                final_child_targets = []
                final_link_chunk_size = min(args.chunk_size, 25)
                print("discover-pages final candidate links: fetching %d linked careers pages ..." % len(final_candidate_targets), flush=True)
                for i in range(0, len(final_candidate_targets), final_link_chunk_size):
                    chunk = final_candidate_targets[i:i + final_link_chunk_size]
                    for res in cascade.fetch_batch_pages_httpx(chunk, args.concurrency, args.timeout):
                        try:
                            persist_page_result(conn, res, "httpx")
                            final_child_targets.extend(discover_child_career_targets(conn, res, link_targets))
                        except Exception as _exc:
                            print("[scrape-cascade] %s/%s raised %s in final-candidate discover; skipping: %s"
                                  % (res.get("domain", "?"), res.get("path", "?"),
                                     type(_exc).__name__, _exc), file=sys.stderr)
                    conn.commit()
                    print("  discover-pages final candidate links persisted %d/%d"
                          % (min(i + final_link_chunk_size, len(final_candidate_targets)), len(final_candidate_targets)), flush=True)
                if args.browser_rescue_pages:
                    final_candidate_rescue_targets = select_browser_rescue_page_targets(
                        conn, final_candidate_targets, per_domain_cap=args.browser_rescue_per_domain)
                    if final_candidate_rescue_targets:
                        print("discover-pages final candidate playwright: rendering %d linked careers pages ..."
                              % len(final_candidate_rescue_targets), flush=True)
                        done = 0
                        try:
                            for res in cascade.rescue_playwright_page_batch(
                                final_candidate_rescue_targets,
                                timeout=args.timeout,
                                per_page_wait=args.browser_page_wait_ms,
                            ):
                                try:
                                    persist_page_result(conn, res, "playwright-page")
                                    final_child_targets.extend(discover_child_career_targets(conn, res, link_targets))
                                except Exception as _exc:
                                    print("[scrape-cascade] %s/%s raised %s in final-candidate-playwright discover; skipping: %s"
                                          % (res.get("domain", "?"), res.get("path", "?"),
                                             type(_exc).__name__, _exc), file=sys.stderr)
                                done += 1
                                if done % 5 == 0 or done == len(final_candidate_rescue_targets):
                                    conn.commit()
                                    print("  discover-pages final candidate playwright %d/%d"
                                          % (done, len(final_candidate_rescue_targets)), flush=True)
                            conn.commit()
                        except Exception as e:
                            conn.commit()
                            print("  discover-pages final candidate playwright aborted (%s) after %d"
                                  % (type(e).__name__, done), flush=True)
                final_child_targets = merge_page_targets(final_child_targets)
                final_fetch_targets = []
                for target in final_child_targets:
                    final_fetch_targets.extend(targets_needing_fetch(conn, [target], refetch=args.refetch))
                final_fetch_targets = merge_page_targets(final_fetch_targets)
                if final_fetch_targets:
                    print("discover-pages final child links: fetching %d linked ATS/listing pages ..." % len(final_fetch_targets), flush=True)
                    for i in range(0, len(final_fetch_targets), final_link_chunk_size):
                        chunk = final_fetch_targets[i:i + final_link_chunk_size]
                        for res in cascade.fetch_batch_pages_httpx(chunk, args.concurrency, args.timeout):
                            persist_page_result(conn, res, "httpx")
                        conn.commit()
                        print("  discover-pages final child links persisted %d/%d"
                              % (min(i + final_link_chunk_size, len(final_fetch_targets)), len(final_fetch_targets)), flush=True)

            # ---- follow pass: domains still unresolved (no ok ATS page, no ok
            #      strong-careers page) get a bounded last-chance fetch of their
            #      stranded careers candidates -- the rows the quota and the
            #      linked-only candidate filter left behind (sdr500_r1: 501
            #      candidate rows across 104 domains, concentrated exactly where
            #      evidence was missing). Single round, no recursion.
            followup_targets = []
            for d in domains:
                followup_targets.extend(select_followup_careers_targets(conn, d))
            if followup_targets:
                follow_chunk_size = min(args.chunk_size, 25)
                print("discover-pages follow pass: fetching %d stranded careers candidates on unresolved domains ..."
                      % len(followup_targets), flush=True)
                for i in range(0, len(followup_targets), follow_chunk_size):
                    chunk = followup_targets[i:i + follow_chunk_size]
                    for res in cascade.fetch_batch_pages_httpx(chunk, args.concurrency, args.timeout):
                        persist_page_result(conn, res, "httpx")
                    conn.commit()
                    print("  discover-pages follow pass persisted %d/%d"
                          % (min(i + follow_chunk_size, len(followup_targets)), len(followup_targets)), flush=True)
                if args.browser_rescue_pages:
                    follow_rescue_targets = select_browser_rescue_page_targets(
                        conn, followup_targets, per_domain_cap=args.browser_rescue_per_domain)
                    if follow_rescue_targets:
                        print("discover-pages follow playwright: rendering %d careers pages ..."
                              % len(follow_rescue_targets), flush=True)
                        done = 0
                        try:
                            for res in cascade.rescue_playwright_page_batch(
                                follow_rescue_targets,
                                timeout=args.timeout,
                                per_page_wait=args.browser_page_wait_ms,
                            ):
                                persist_page_result(conn, res, "playwright-page")
                                done += 1
                                conn.commit()
                                if done % 5 == 0 or done == len(follow_rescue_targets):
                                    print("  discover-pages follow playwright %d/%d"
                                          % (done, len(follow_rescue_targets)), flush=True)
                        except Exception as e:
                            conn.commit()
                            print("  discover-pages follow playwright aborted (%s) after %d"
                                  % (type(e).__name__, done), flush=True)

    # ---- score (cheap) then judge (only ambiguous). Resume-aware: a domain already
    #      decided on real content is left alone; only fetch_failed verdicts are
    #      re-evaluated (a later --rescue may have filled the page).
    to_judge, skipped = [], 0
    _score_errors = 0
    for d in domains:
        try:
            if not args.rejudge:
                ev = cascade.get_verdict(conn, d, rubric["name"])
                if ev and ev["method"] != "fetch_failed":
                    skipped += 1
                    continue
            text = (cascade.get_page(conn, d) or {}).get("text") or ""
            if not text:
                cascade.upsert_verdict(conn, d, rubric["name"], "unreachable", 0.0,
                                       "fetch_failed", "no content after cascade", commit=False)
                continue
            label, conf, detail = cascade.score_text(text, rubric)
            if label != "ambiguous":
                cascade.upsert_verdict(conn, d, rubric["name"], label, conf, "keyword",
                                       "pos=%d neg=%d" % (detail["pos_hits"], detail["neg_hits"]), commit=False)
            elif args.no_judge:
                cascade.upsert_verdict(conn, d, rubric["name"], "ambiguous", conf, "keyword",
                                       "ambiguous; judge disabled", commit=False)
            else:
                to_judge.append((d, text))
        except Exception as _exc:
            # Per-domain error isolation: one malformed/problematic domain must never
            # abort the scoring pass for the remaining 499 good domains.
            _score_errors += 1
            print("[scrape-cascade] domain %s raised %s in scoring pass; skipping: %s"
                  % (d, type(_exc).__name__, _exc), file=sys.stderr)
            try:
                cascade.upsert_verdict(conn, d, rubric["name"], "unreachable", 0.0,
                                       "fetch_failed", "error in scoring: %s" % type(_exc).__name__, commit=False)
            except Exception:
                pass  # don't let the error-handler itself abort the loop
    conn.commit()
    if _score_errors:
        print("scoring pass: %d domain(s) raised errors and were skipped" % _score_errors)
    if skipped:
        print("resume: %d domains already decided (use --rejudge to redo)" % skipped)

    # ---- judge the ambiguous residue in parallel. Warm the provider with ONE call
    #      first so any prompt/context cache can settle before fan-out.
    judged = 0
    if to_judge:
        n = len(to_judge)
        model = args.judge_model if args.judge_model is not None else cascade.default_judge_model(judge_provider)
        model_display = model or "default"
        print("judge: %d ambiguous -> %s (model=%s, concurrency=%d) ..."
              % (n, judge_provider, model_display, args.judge_concurrency))

        def _run_one(item):
            dom, txt = item
            return dom, cascade.judge(
                txt,
                rubric,
                provider=judge_provider,
                judge_bin=args.judge_bin,
                model=args.judge_model,
                evidence_window=args.judge_evidence_window,
            )

        def _apply(dom, verdict):
            cascade.upsert_verdict(conn, dom, rubric["name"], verdict["label"],
                                   verdict["confidence"], "llm", verdict["reason"])

        pending = list(to_judge)
        if args.judge_concurrency > 1 and len(pending) > 1:
            d0, v0 = _run_one(pending.pop(0))
            _apply(d0, v0)
            judged += 1
        with ThreadPoolExecutor(max_workers=max(1, args.judge_concurrency)) as ex:
            futs = {ex.submit(_run_one, it): it for it in pending}
            for fut in as_completed(futs):
                item = futs[fut]
                try:
                    d, v = fut.result()
                    _apply(d, v)
                except Exception as _exc:
                    # Per-domain isolation: a judge failure on one domain must not
                    # abort the remaining judging work.
                    d = item[0]
                    print("[scrape-cascade] domain %s raised %s in judge; leaving ambiguous: %s"
                          % (d, type(_exc).__name__, _exc), file=sys.stderr)
                    try:
                        cascade.upsert_verdict(conn, d, rubric["name"], "ambiguous", 0.0,
                                               "llm", "judge error: %s" % type(_exc).__name__)
                    except Exception:
                        pass
                judged += 1
                if judged % 25 == 0 or judged == n:
                    print("  judged %d/%d" % (judged, n))

    # ---- outputs: a projection of the DB (which already holds every verdict)
    rows = export_rows(conn, rubric["name"], domains)
    jsonl_path = write_outputs(rows, args.output)
    print_summary(rows, judged, args.output, jsonl_path, rubric["name"], judge_provider)
    if args.discover_pages:
        pages_output = args.pages_output or default_pages_output(args.output)
        targets_by_domain = {
            d: select_targets_for_domain(
                conn,
                d,
                all_page_specs,
                link_targets.get(d, []),
                max_pages_per_domain=args.max_pages_per_domain,
                page_type_quota=page_type_quota,
                page_type_order=page_type_order,
                include_stored=not args.refetch,
            )
            for d in domains
        }
        page_rows = export_page_rows_for_targets(conn, rubric, domains, targets_by_domain)
        pages_jsonl = write_page_outputs(page_rows, pages_output)
        print_page_summary(page_rows, pages_output, pages_jsonl)
    else:
        page_rows = None
    if args.extract_evidence:
        evidence_output = args.evidence_output or default_evidence_output(args.output)
        allowed = allowed_keys_from_page_rows(page_rows) if args.max_pages_per_domain is not None else None
        evidence_rows = export_evidence_rows(conn, domains, allowed)
        evidence_jsonl = write_evidence_outputs(evidence_rows, evidence_output)
        print_evidence_summary(evidence_rows, evidence_output, evidence_jsonl)


if __name__ == "__main__":
    main()
