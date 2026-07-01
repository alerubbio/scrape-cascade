import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
import cascade  # noqa: E402

RUN_PATH = SCRIPTS_DIR / "run.py"
SPEC = importlib.util.spec_from_file_location("scrape_cascade_run", RUN_PATH)
run = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run)


class EvidenceValidationTests(unittest.TestCase):
    def test_domain_hygiene_flags_personal_resume(self):
        text = (
            "Hany Example. Tech Operations Lead. Professional Summary. "
            "LinkedIn Profile Download Resume. Python Terraform Okta."
        )
        self.assertEqual(cascade.domain_hygiene("person.example", text), "personal_resume")

    def test_domain_hygiene_does_not_exclude_b2b_legal_ai(self):
        text = (
            "Professional class AI for your law firm. Platform solutions customers "
            "security resources company careers contact sales privacy policy."
        )
        self.assertEqual(cascade.domain_hygiene("harvey.ai", text), "company_like")

    def test_domain_hygiene_still_flags_local_law_firm(self):
        text = "Smith Legal is a local law firm. Practice areas, attorneys, contact us."
        self.assertEqual(cascade.domain_hygiene("smithlegal.example", text), "consumer_or_local")

    def test_snippets_strip_nul_and_control_bytes_for_csv_safety(self):
        snippet = cascade.evidence_snippet("prefix\x00\x01 SOC 2 compliance \x02tail", ["soc 2"])
        self.assertNotIn("\x00", snippet)
        self.assertNotIn("\x01", snippet)
        self.assertNotIn("\x02", snippet)
        self.assertIn("SOC 2", snippet)

    def test_html_to_text_preserves_embedded_ats_job_links(self):
        text = cascade.html_to_text(
            '<main><h1>Open roles</h1><script>{"url":"https://jobs.ashbyhq.com/example/open-platform-engineer"}</script></main>'
        )

        self.assertIn("Embedded ATS job links:", text)
        self.assertIn("https://jobs.ashbyhq.com/example/open-platform-engineer", text)

    def test_source_trust_uses_input_or_homepage_redirect_host_and_ats(self):
        self.assertEqual(
            cascade.source_trust("getgarner.com", "https://garnerhealth.com/", "https://garnerhealth.com/careers"),
            "company_site",
        )
        self.assertEqual(
            cascade.source_trust(
                "example.com",
                "https://example.com/",
                "https://boards.greenhouse.io/acme",
                linked_from_homepage=True,
            ),
            "linked_ats_candidate",
        )
        self.assertEqual(
            cascade.source_trust("example.com", "https://example.com/", "https://boards.greenhouse.io/acme"),
            "untrusted_external",
        )
        self.assertEqual(
            cascade.source_trust(
                "example.com",
                "https://example.com/",
                "https://apply.workable.com/example/",
                source_path="/careers",
                page_type="careers",
            ),
            "linked_ats_candidate",
        )
        self.assertEqual(
            cascade.source_trust(
                "example.com",
                "https://example.com/",
                "https://example.applicantstack.com/x/openings",
                source_path="/careers",
                page_type="careers",
            ),
            "linked_ats_candidate",
        )
        self.assertEqual(
            cascade.source_trust("example.com", "https://example.com/", "https://random.example/jobs"),
            "untrusted_external",
        )
        self.assertEqual(
            cascade.source_trust(
                "alteram.co.za",
                "https://www.alteram.co.za/",
                "https://alteram.in-tranet.co.za/system/recruitment/index.php",
                linked_from_homepage=True,
            ),
            "linked_ats_candidate",
        )
        self.assertEqual(
            cascade.source_trust("example.com", "https://example.com/", "https://www.businesswire.com/news/home/example"),
            "trusted_funding_outlet",
        )

    def test_evidence_tiers_separate_official_independent_weak_and_rejected(self):
        self.assertEqual(cascade.evidence_tier("company_like", "company_site", "hiring_activity"), "A")
        self.assertEqual(cascade.evidence_tier("company_like", "linked_ats_candidate", "hiring_activity"), "A")
        self.assertEqual(cascade.evidence_tier("company_like", "trusted_funding_outlet", "funding_or_growth"), "B")
        self.assertEqual(cascade.evidence_tier("company_like", "untrusted_external", "funding_or_growth"), "C")
        self.assertEqual(cascade.evidence_tier("personal_resume", "company_site", "hiring_activity"), "Rejected")
        self.assertEqual(cascade.evidence_tier("unknown", "company_site", "hiring_activity"), "C")
        self.assertEqual(cascade.evidence_tier("company_like", "company_site", "none"), "Rejected")

    def test_validated_evidence_rows_are_candidates_not_final_facts(self):
        rows = cascade.validated_evidence_rows(
            "example.com",
            "https://example.com/",
            "Company platform customers privacy policy contact sales careers. " * 4,
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "/careers",
                "url": "https://example.com/careers",
                "ok": 1,
                "text": "Open roles for a systems administrator managing MDM and device management. Apply today.",
            },
        )
        by_type = {row["evidence_type"]: row for row in rows}

        self.assertEqual(by_type["hiring_activity"]["evidence_status"], "trusted_source_candidate")
        self.assertEqual(by_type["it_ops_hiring"]["evidence_status"], "trusted_source_candidate")
        self.assertEqual(by_type["hiring_activity"]["source_trust"], "company_site")
        self.assertEqual(by_type["hiring_activity"]["evidence_tier"], "A")
        self.assertIn("open roles", by_type["hiring_activity"]["matched_terms"])

    def test_same_site_job_detail_apply_context_is_hiring_candidate(self):
        rows = cascade.validated_evidence_rows(
            "example.com",
            "https://example.com/",
            "Company platform customers privacy policy contact sales careers. " * 4,
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "/careers/gtm-engineer--business-operations--san-francisco",
                "url": "https://example.com/careers/gtm-engineer--business-operations--san-francisco",
                "ok": 1,
                "linked_from_homepage": 1,
                "text": (
                    "Back to all roles # GTM Engineer "
                    "Team Business Operations Location San Francisco, United States "
                    "Apply on Gem ABOUT EXAMPLE Build internal tools for revenue teams."
                ),
            },
        )
        by_type = {row["evidence_type"]: row for row in rows}

        self.assertEqual(by_type["hiring_activity"]["source_trust"], "company_site")
        self.assertEqual(by_type["hiring_activity"]["evidence_status"], "trusted_source_candidate")
        self.assertIn("apply", by_type["hiring_activity"]["matched_terms"])

    def test_same_site_job_detail_department_office_context_is_hiring_candidate(self):
        rows = cascade.validated_evidence_rows(
            "example.com",
            "https://example.com/",
            "Company platform customers privacy policy contact sales careers. " * 4,
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "/careers/example-job-1",
                "url": "https://example.com/careers/example-job-1",
                "ok": 1,
                "linked_from_homepage": 1,
                "text": (
                    "Careers # Senior Associate, Strategic Finance - GTM "
                    "Department Finance Office New York, NY Apply "
                    "Responsibilities include forecasting and revenue planning."
                ),
            },
        )
        by_type = {row["evidence_type"]: row for row in rows}

        self.assertEqual(by_type["hiring_activity"]["source_trust"], "company_site")
        self.assertEqual(by_type["hiring_activity"]["evidence_status"], "trusted_source_candidate")
        self.assertIn("apply", by_type["hiring_activity"]["matched_terms"])

    def test_company_careers_apply_for_this_role_context_is_hiring_candidate(self):
        rows = cascade.validated_evidence_rows(
            "example.com",
            "https://example.com/",
            "Company platform customers privacy policy contact sales careers. " * 4,
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "/careers",
                "url": "https://example.com/careers",
                "ok": 1,
                "linked_from_homepage": 1,
                "text": (
                    "## Product Engineer Portland, Maine Onsite $130-160k equity "
                    "Full-time first non-founder hire. Responsibilities include "
                    "building internal workflow software. Apply for this role "
                    "Name Email LinkedIn Resume Submit Application"
                ),
            },
        )
        by_type = {row["evidence_type"]: row for row in rows}

        self.assertEqual(by_type["hiring_activity"]["source_trust"], "company_site")
        self.assertEqual(by_type["hiring_activity"]["evidence_status"], "trusted_source_candidate")
        self.assertIn("apply", by_type["hiring_activity"]["matched_terms"])

    def test_wordpress_jobs_archive_context_is_hiring_candidate(self):
        rows = cascade.validated_evidence_rows(
            "example.com",
            "https://example.com/",
            "Company platform customers privacy policy contact sales careers. " * 4,
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "/jobs/",
                "url": "https://example.com/jobs/",
                "ok": 1,
                "linked_from_homepage": 1,
                "text": (
                    "# Archives: Jobs Post Type Description "
                    "## Controller Example is looking for a Controller to build accounting processes. "
                    "## Community Affairs Specialist Example is looking for a Community Affairs Specialist. "
                    "## Assistant Project Manager Example is looking for an Assistant Project Manager. "
                    "## Let's stay connected"
                ),
            },
        )
        by_type = {row["evidence_type"]: row for row in rows}

        self.assertEqual(by_type["hiring_activity"]["source_trust"], "company_site")
        self.assertEqual(by_type["hiring_activity"]["evidence_status"], "trusted_source_candidate")
        self.assertIn("job listings", by_type["hiring_activity"]["matched_terms"])

    def test_official_careers_redirect_to_ats_is_trusted_candidate(self):
        rows = cascade.validated_evidence_rows(
            "example.com",
            "https://example.com/",
            "Company platform customers privacy policy contact sales careers. " * 4,
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "/careers",
                "url": "https://apply.workable.com/example/",
                "ok": 1,
                "text": "Open positions. Software Engineer. Employment type full-time. Apply for this job.",
            },
        )
        by_type = {row["evidence_type"]: row for row in rows}

        self.assertEqual(by_type["hiring_activity"]["source_trust"], "linked_ats_candidate")
        self.assertEqual(by_type["hiring_activity"]["evidence_status"], "trusted_source_candidate")
        self.assertEqual(by_type["hiring_activity"]["evidence_tier"], "A")

    def test_linked_ats_no_open_roles_is_not_hiring_activity(self):
        rows = cascade.validated_evidence_rows(
            "example.com",
            "https://example.com/",
            "Company platform customers privacy policy contact sales careers. " * 4,
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "https://ats.rippling.com/example/jobs",
                "url": "https://ats.rippling.com/example/jobs",
                "ok": 1,
                "linked_from_homepage": 1,
                "text": "Example Careers. There are currently no open roles. Come back later! Powered by Rippling.",
            },
        )

        self.assertEqual(rows[0]["evidence_type"], "none")
        self.assertEqual(rows[0]["evidence_status"], "not_evidence")

    def test_apply_cookie_text_alone_is_not_hiring_activity(self):
        rows = cascade.validated_evidence_rows(
            "example.com",
            "https://example.com/",
            "Company platform customers privacy policy contact sales careers. " * 4,
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "/careers",
                "url": "https://example.com/careers",
                "ok": 1,
                "text": "Cookie settings. Apply preferences. Back Button. Cookie List. Clear filters.",
            },
        )

        self.assertEqual(rows[0]["evidence_type"], "none")
        self.assertEqual(rows[0]["evidence_status"], "not_evidence")

    def test_employment_detail_alone_on_company_site_is_not_hiring_activity(self):
        rows = cascade.validated_evidence_rows(
            "example.com",
            "https://example.com/",
            "Company platform customers privacy policy contact sales careers. " * 4,
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "/careers",
                "url": "https://example.com/careers",
                "ok": 1,
                "text": "Learn about our investment team. Full time professionals get mentoring and guidance.",
            },
        )

        self.assertEqual(rows[0]["evidence_type"], "none")
        self.assertEqual(rows[0]["evidence_status"], "not_evidence")

    def test_linked_ats_apply_page_remains_hiring_activity(self):
        rows = cascade.validated_evidence_rows(
            "example.com",
            "https://example.com/",
            "Company platform customers privacy policy contact sales careers. " * 4,
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "/jobs",
                "url": "https://apply.workable.com/example/j/ABC123/apply/",
                "ok": 1,
                "linked_from_homepage": 1,
                "text": "Senior Engineer Remote Full time Overview Application Apply for this job.",
            },
        )
        by_type = {row["evidence_type"]: row for row in rows}

        self.assertEqual(by_type["hiring_activity"]["source_trust"], "linked_ats_candidate")
        self.assertEqual(by_type["hiring_activity"]["evidence_status"], "trusted_source_candidate")

    def test_localized_recruiting_terms_count_as_hiring_activity(self):
        rows = cascade.validated_evidence_rows(
            "rikei.co.jp",
            "https://www.rikei.co.jp/",
            "Company platform customers privacy policy contact sales careers. " * 4,
            {
                "domain": "rikei.co.jp",
                "page_type": "careers",
                "path": "/recruit/career",
                "url": "https://www.rikei.co.jp/recruit/career/",
                "ok": 1,
                "text": "募集要項 中途採用 募集内容 職種 応募方法 待遇 給与",
            },
        )
        by_type = {row["evidence_type"]: row for row in rows}

        self.assertEqual(by_type["hiring_activity"]["evidence_status"], "trusted_source_candidate")
        self.assertIn("募集要項", by_type["hiring_activity"]["matched_terms"])

    def test_mdm_job_evidence_rejects_generic_endpoint_or_macos_mentions(self):
        rows = cascade.validated_evidence_rows(
            "example.com",
            "https://example.com/",
            "Company platform customers privacy policy contact sales careers. " * 4,
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "/careers",
                "url": "https://example.com/careers",
                "ok": 1,
                "text": "One endpoint for every model. The app works on Windows, MacOS, and Linux. Apply today.",
            },
        )
        self.assertNotIn("it_ops_hiring", {row["evidence_type"] for row in rows})

    def test_it_ops_evidence_allows_identity_ops_with_hiring_context(self):
        rows = cascade.validated_evidence_rows(
            "example.com",
            "https://example.com/",
            "Company platform customers privacy policy contact sales careers. " * 4,
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "/careers",
                "url": "https://example.com/careers",
                "ok": 1,
                "text": "Open roles. Apply if you have hands-on experience with identity providers such as Okta and Azure AD.",
            },
        )
        self.assertIn("it_ops_hiring", {row["evidence_type"] for row in rows})

    def test_hygiene_excludes_candidate_evidence(self):
        rows = cascade.validated_evidence_rows(
            "person.example",
            "https://person.example/",
            "Professional Summary LinkedIn Profile Download Resume.",
            {
                "domain": "person.example",
                "page_type": "careers",
                "path": "/careers",
                "url": "https://person.example/careers",
                "ok": 1,
                "text": "Open roles include mdm endpoint administrator.",
            },
        )

        self.assertTrue(rows)
        self.assertTrue(all(row["evidence_status"] == "excluded_hygiene" for row in rows))
        self.assertTrue(all(row["evidence_tier"] == "Rejected" for row in rows))

    def test_official_consumer_local_careers_hiring_is_not_hard_excluded(self):
        rows = cascade.validated_evidence_rows(
            "atis.life",
            "https://atis.life/",
            "View menu restaurant locations careers privacy policy.",
            {
                "domain": "atis.life",
                "page_type": "careers",
                "path": "/careers",
                "url": "https://careers.atis.life/",
                "ok": 1,
                "text": "Current job openings include Project Manager and Assistant General Manager.",
            },
        )
        by_type = {row["evidence_type"]: row for row in rows}

        self.assertEqual(cascade.domain_hygiene("atis.life", "View menu restaurant locations careers privacy policy."), "consumer_or_local")
        self.assertEqual(by_type["hiring_activity"]["source_trust"], "company_site")
        self.assertEqual(by_type["hiring_activity"]["evidence_status"], "trusted_source_candidate")
        self.assertEqual(by_type["hiring_activity"]["evidence_tier"], "A")

    def test_unknown_hygiene_is_review_needed_not_hard_excluded(self):
        rows = cascade.validated_evidence_rows(
            "example.com",
            "https://example.com/",
            "",
            {
                "domain": "example.com",
                "page_type": "security",
                "path": "/security",
                "url": "https://example.com/security",
                "ok": 1,
                "text": "Trust center with SOC 2 compliance.",
            },
        )

        self.assertEqual(rows[0]["evidence_status"], "review_needed")
        self.assertEqual(rows[0]["evidence_tier"], "C")

    def test_unlinked_ats_is_not_trusted_candidate(self):
        rows = cascade.validated_evidence_rows(
            "example.com",
            "https://example.com/",
            "Company platform customers privacy policy contact sales careers. " * 4,
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "https://boards.greenhouse.io/acme",
                "url": "https://boards.greenhouse.io/acme",
                "ok": 1,
                "text": "Open roles. Apply today.",
            },
        )

        self.assertEqual(rows[0]["source_trust"], "untrusted_external")
        self.assertEqual(rows[0]["evidence_status"], "untrusted_source_candidate")
        self.assertEqual(rows[0]["evidence_tier"], "C")

    def test_linked_ats_is_trusted_candidate(self):
        rows = cascade.validated_evidence_rows(
            "example.com",
            "https://example.com/",
            "Company platform customers privacy policy contact sales careers. " * 4,
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "https://boards.greenhouse.io/acme",
                "url": "https://boards.greenhouse.io/acme",
                "linked_from_homepage": 1,
                "ok": 1,
                "text": "Open roles. Apply today.",
            },
        )

        self.assertEqual(rows[0]["source_trust"], "linked_ats_candidate")
        self.assertEqual(rows[0]["evidence_status"], "trusted_source_candidate")
        self.assertEqual(rows[0]["evidence_tier"], "A")

    def test_company_linked_aaimtrack_board_is_trusted_candidate(self):
        rows = cascade.validated_evidence_rows(
            "seyerind.com",
            "https://www.seyerind.com/",
            "Seyer Industries careers and job listings.",
            {
                "domain": "seyerind.com",
                "page_type": "careers",
                "path": "/careers",
                "url": "https://seyerind.aaimtrack.com/jobs/1304455",
                "linked_from_homepage": 1,
                "ok": 1,
                "text": "Apply for this Position. Full time. Sign Up For Job Alerts.",
            },
        )

        self.assertEqual(rows[0]["source_trust"], "linked_ats_candidate")
        self.assertEqual(rows[0]["evidence_status"], "trusted_source_candidate")
        self.assertEqual(rows[0]["evidence_tier"], "A")

    def test_linked_ats_hiring_survives_thin_homepage_hygiene(self):
        rows = cascade.validated_evidence_rows(
            "example.com",
            "https://example.com/",
            "Careers.",
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "https://jobs.ashbyhq.com/example",
                "url": "https://jobs.ashbyhq.com/example",
                "linked_from_homepage": 1,
                "ok": 1,
                "text": "Open Positions (10). Full time. Apply today.",
            },
        )

        self.assertEqual(cascade.domain_hygiene("example.com", "Careers."), "parked_or_thin")
        self.assertEqual(rows[0]["source_trust"], "linked_ats_candidate")
        self.assertEqual(rows[0]["evidence_status"], "trusted_source_candidate")
        self.assertEqual(rows[0]["evidence_tier"], "A")

    def test_linked_paylocity_board_is_trusted_ats_candidate(self):
        rows = cascade.validated_evidence_rows(
            "example.com",
            "https://example.com/",
            "Careers.",
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "https://recruiting.paylocity.com/recruiting/jobs/All/example-board-0001/JSL-TECHNOLOGIES-INCORPORATED",
                "url": "https://recruiting.paylocity.com/recruiting/jobs/All/example-board-0001/JSL-TECHNOLOGIES-INCORPORATED",
                "linked_from_homepage": 1,
                "ok": 1,
                "text": "Current Job Openings. Software Engineer. Full time. Apply today.",
            },
        )

        self.assertEqual(rows[0]["source_trust"], "linked_ats_candidate")
        self.assertEqual(rows[0]["evidence_status"], "trusted_source_candidate")
        self.assertEqual(rows[0]["evidence_tier"], "A")

    def test_embedded_ats_jobs_on_company_careers_survive_thin_homepage_hygiene(self):
        rows = cascade.validated_evidence_rows(
            "example.org",
            "https://example.org/",
            "Careers.",
            {
                "domain": "example.org",
                "page_type": "careers",
                "path": "https://careers.example.org/",
                "url": "https://careers.example.org/",
                "ok": 1,
                "text": (
                    "Open roles Business Development 1 Engineering 6\n"
                    "Embedded ATS job links:\n"
                    "https://jobs.ashbyhq.com/example/open-platform-engineer\n"
                    "https://jobs.ashbyhq.com/example/product-designer\n"
                ),
            },
        )
        by_type = {row["evidence_type"]: row for row in rows}

        self.assertEqual(cascade.domain_hygiene("example.org", "Careers."), "parked_or_thin")
        self.assertEqual(by_type["hiring_activity"]["source_trust"], "company_site")
        self.assertEqual(by_type["hiring_activity"]["evidence_status"], "trusted_source_candidate")
        self.assertEqual(by_type["hiring_activity"]["evidence_tier"], "A")

    def test_non_careers_evidence_types_are_candidate_only(self):
        cases = [
            ("news", "We raised Series B funding led by Example Capital.", "funding_or_growth", "A"),
            ("security", "Trust center with SOC 2 and ISO 27001 compliance.", "security_maturity", "A"),
            ("procurement", "Supplier procurement terms and renewal vendor review.", "procurement_or_renewal", "A"),
        ]
        for page_type, text, evidence_type, tier in cases:
            with self.subTest(page_type=page_type):
                rows = cascade.validated_evidence_rows(
                    "example.com",
                    "https://example.com/",
                    "Company platform customers privacy policy contact sales careers. " * 4,
                    {
                        "domain": "example.com",
                        "page_type": page_type,
                        "path": "/" + page_type,
                        "url": f"https://example.com/{page_type}",
                        "ok": 1,
                        "text": text,
                    },
                )
                by_type = {row["evidence_type"]: row for row in rows}
                self.assertEqual(by_type[evidence_type]["evidence_status"], "trusted_source_candidate")
                self.assertEqual(by_type[evidence_type]["evidence_tier"], tier)

    def test_evidence_taxonomies_are_closed_for_rows(self):
        rows = cascade.validated_evidence_rows(
            "example.com",
            "https://example.com/",
            "Company platform customers privacy policy contact sales careers. " * 4,
            {
                "domain": "example.com",
                "page_type": "news",
                "path": "/news",
                "url": "https://www.businesswire.com/news/home/example",
                "ok": 1,
                "text": "Example announced Series C funding of $40 million.",
            },
        )
        for row in rows:
            self.assertIn(row["source_trust"], cascade.SOURCE_TRUST_VALUES)
            self.assertIn(row["domain_hygiene"], cascade.DOMAIN_HYGIENE_VALUES)
            self.assertIn(row["evidence_type"], set(cascade.EVIDENCE_TYPES) | {"none"})
            self.assertIn(row["evidence_status"], cascade.EVIDENCE_STATUS_VALUES)
            self.assertIn(row["evidence_tier"], cascade.EVIDENCE_TIERS)
        self.assertEqual(rows[0]["source_trust"], "trusted_funding_outlet")
        self.assertEqual(rows[0]["evidence_tier"], "B")

    def test_export_and_write_evidence_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = cascade.connect(str(Path(tmp) / "cache.db"))
            cascade.upsert_page(
                conn,
                "example.com",
                "https://example.com/",
                200,
                "httpx",
                True,
                "Company platform customers privacy policy contact sales careers. " * 4,
            )
            cascade.upsert_discovered_page(
                conn,
                "example.com",
                "/security",
                "security",
                "https://example.com/security",
                200,
                "httpx",
                True,
                "Trust center with SOC 2 and ISO 27001 compliance.",
            )
            rows = run.export_evidence_rows(conn, ["example.com"])
            csv_path = Path(tmp) / "evidence.csv"
            jsonl_path = Path(run.write_evidence_outputs(rows, str(csv_path)))

            with csv_path.open(newline="") as f:
                reader = csv.DictReader(f)
                self.assertEqual(
                    reader.fieldnames,
                    [
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
                    ],
                )
                csv_rows = list(reader)
            json_rows = [json.loads(line) for line in jsonl_path.read_text().splitlines()]

        self.assertEqual(csv_rows[0]["evidence_type"], "security_maturity")
        self.assertEqual(csv_rows[0]["evidence_tier"], "A")
        self.assertEqual(csv_rows[0]["source_host"], "example.com")
        self.assertEqual(json_rows[0]["source_trust"], "company_site")

    def test_export_evidence_rows_can_follow_capped_page_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = cascade.connect(str(Path(tmp) / "cache.db"))
            cascade.upsert_page(
                conn,
                "example.com",
                "https://example.com/",
                200,
                "httpx",
                True,
                "Company platform customers privacy policy contact sales careers. " * 4,
            )
            cascade.upsert_discovered_page(
                conn,
                "example.com",
                "/careers",
                "careers",
                "https://example.com/careers",
                200,
                "httpx",
                True,
                "Open roles for endpoint admins.",
            )
            cascade.upsert_discovered_page(
                conn,
                "example.com",
                "/security",
                "security",
                "https://example.com/security",
                200,
                "httpx",
                True,
                "SOC 2 trust center.",
            )
            allowed = run.allowed_keys_from_page_rows([
                {"domain": "example.com", "path": "/careers", "url": ""}
            ])
            rows = run.export_evidence_rows(conn, ["example.com"], allowed)

        self.assertEqual({row["source_url"] for row in rows}, {"https://example.com/careers"})


if __name__ == "__main__":
    unittest.main()
