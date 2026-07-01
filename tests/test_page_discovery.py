import csv
import importlib.util
import json
import sqlite3
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


RUBRIC = {
    "name": "capacity_signal_test",
    "positive_label": "capacity_signal",
    "negative_label": "no_capacity_signal",
    "crawl_paths": [
        {
            "page_type": "careers",
            "paths": ["/careers", "/careers/", "jobs"],
            "evidence_terms": ["open roles", "apply"],
        },
        {"page_type": "security", "paths": ["/trust-center"]},
    ],
    "page_evidence_terms": {
        "careers": ["endpoint management", "sso", "okta"],
        "security": ["soc 2", "iso 27001"],
    },
}


class PageDiscoveryTests(unittest.TestCase):
    def test_page_specs_expand_dedupe_and_merge_terms(self):
        specs = cascade.page_specs_from_rubric(RUBRIC)

        self.assertEqual([s["path"] for s in specs], ["/careers", "/jobs", "/trust-center"])
        self.assertEqual(specs[0]["page_type"], "careers")
        self.assertEqual(
            specs[0]["evidence_terms"],
            ["open roles", "apply", "endpoint management", "sso", "okta"],
        )

    def test_page_specs_limit(self):
        specs = cascade.page_specs_from_rubric(RUBRIC, max_pages_per_domain=2)
        self.assertEqual([s["path"] for s in specs], ["/careers", "/trust-center"])

    def test_recruit_paths_are_careers_pages(self):
        self.assertEqual(cascade.page_type_for_path("/recruit"), "careers")
        self.assertEqual(cascade.page_type_for_path("/recruit/career"), "careers")
        self.assertEqual(cascade.page_type_for_path("/open-positions"), "careers")
        self.assertEqual(cascade.page_type_for_path("/current-openings"), "careers")

    def test_select_page_specs_round_robins_page_types_under_cap(self):
        rubric = {
            "name": "unit",
            "positive_label": "yes",
            "negative_label": "no",
            "crawl_paths": [
                {"page_type": "careers", "paths": ["/careers", "/jobs", "/join-us"]},
                {"page_type": "company", "paths": ["/about"]},
                {"page_type": "news", "paths": ["/news"]},
                {"page_type": "security", "paths": ["/trust"]},
                {"page_type": "procurement", "paths": ["/procurement"]},
            ],
        }
        specs = cascade.page_specs_from_rubric(rubric)
        selected = cascade.select_page_specs(specs, max_pages_per_domain=5)

        self.assertEqual(
            [(s["page_type"], s["path"]) for s in selected],
            [
                ("careers", "/careers"),
                ("company", "/about"),
                ("news", "/news"),
                ("security", "/trust"),
                ("procurement", "/procurement"),
            ],
        )

    def test_select_page_specs_honors_page_type_quotas_then_fills(self):
        rubric = {
            "name": "unit",
            "positive_label": "yes",
            "negative_label": "no",
            "crawl_paths": [
                {"page_type": "careers", "paths": ["/careers", "/jobs", "/join-us"]},
                {"page_type": "news", "paths": ["/news", "/press"]},
                {"page_type": "security", "paths": ["/trust"]},
            ],
        }
        specs = cascade.page_specs_from_rubric(rubric)
        selected = cascade.select_page_specs(
            specs,
            max_pages_per_domain=4,
            page_type_quota={"careers": 2, "news": 1},
            page_type_order=["news", "careers", "security"],
        )

        self.assertEqual(
            [(s["page_type"], s["path"]) for s in selected],
            [
                ("news", "/news"),
                ("careers", "/careers"),
                ("careers", "/jobs"),
                ("security", "/trust"),
            ],
        )

    def test_parse_page_type_quota_rejects_invalid_values(self):
        self.assertEqual(cascade.parse_page_type_quota("careers=2, news=1"), {"careers": 2, "news": 1})
        with self.assertRaises(ValueError):
            cascade.parse_page_type_quota("careers=two")

    def test_page_specs_zero_limit(self):
        self.assertEqual(cascade.page_specs_from_rubric(RUBRIC, max_pages_per_domain=0), [])

    def test_old_db_migration_preserves_existing_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "cache.db"
            raw = sqlite3.connect(db)
            raw.execute(
                "CREATE TABLE pages (domain TEXT PRIMARY KEY, url TEXT, status INTEGER, "
                "tier TEXT, ok INTEGER, text TEXT, fetched_at TEXT)"
            )
            raw.execute(
                "CREATE TABLE verdicts (domain TEXT, rubric TEXT, label TEXT, confidence REAL, "
                "method TEXT, reason TEXT, decided_at TEXT, PRIMARY KEY (domain, rubric))"
            )
            raw.execute(
                "INSERT INTO pages VALUES (?,?,?,?,?,?,?)",
                ("example.com", "https://example.com", 200, "httpx", 1, "homepage", "now"),
            )
            raw.commit()
            raw.close()

            conn = cascade.connect(str(db))
            self.assertEqual(cascade.get_page(conn, "example.com")["text"], "homepage")
            cascade.upsert_discovered_page(
                conn, "example.com", "/careers", "careers", "https://example.com/careers", 200, "httpx", True, "x"
            )
            self.assertEqual(cascade.get_discovered_page(conn, "example.com", "/careers")["ok"], 1)

    def test_candidate_links_keep_relevant_same_site_and_ats_only(self):
        html = """
        <a href="/careers/">Careers</a>
        <a href="/careers#roles">Careers duplicate</a>
        <a href="https://boards.greenhouse.io/acme">Open roles</a>
        <a href="https://linkedin.com/company/acme">LinkedIn</a>
        <a href="mailto:jobs@example.com">Email</a>
        <a href="/assets/logo.png">Logo</a>
        <a href="https://other.example/jobs">Other jobs</a>
        """
        targets = cascade.candidate_page_targets_from_html(
            "example.com",
            "https://www.example.com/",
            html,
        )

        self.assertEqual(
            [(t["page_type"], t["path"]) for t in targets],
            [
                ("careers", "/careers"),
                ("careers", "https://boards.greenhouse.io/acme"),
            ],
        )
        self.assertTrue(all(t["linked_from_homepage"] for t in targets))

    def test_candidate_links_preserve_company_subdomain_targets(self):
        html = '<a href="https://jobs.gingerlabs.com/">Jobs</a>'
        targets = cascade.candidate_page_targets_from_html(
            "gingerlabs.com",
            "https://notability.com/",
            html,
        )

        self.assertEqual(targets[0]["path"], "https://jobs.gingerlabs.com/")
        self.assertEqual(targets[0]["url"], "https://jobs.gingerlabs.com/")
        self.assertTrue(targets[0]["linked_from_homepage"])

    def test_candidate_links_keep_homepage_linked_external_recruitment_system(self):
        html = '<a href="https://alteram.in-tranet.co.za/system/recruitment/index.php">Vacancies</a>'
        targets = cascade.candidate_page_targets_from_html(
            "alteram.co.za",
            "https://www.alteram.co.za/",
            html,
        )

        self.assertEqual(
            [(t["page_type"], t["path"], t.get("url")) for t in targets],
            [
                (
                    "careers",
                    "https://alteram.in-tranet.co.za/system/recruitment/index.php",
                    "https://alteram.in-tranet.co.za/system/recruitment/index.php",
                )
            ],
        )
        self.assertTrue(targets[0]["linked_from_homepage"])

    def test_candidate_links_detect_localized_careers_language(self):
        html = """
        <a href="/de/karriere">Karriere</a>
        <a href="/recruit">採用情報</a>
        <a href="/en/career">Career</a>
        <a href="/company">Unternehmen</a>
        """
        targets = cascade.candidate_page_targets_from_html(
            "tacto.ai",
            "https://www.tacto.ai/de",
            html,
        )

        self.assertEqual(
            [(t["page_type"], t["path"]) for t in targets],
            [
                ("careers", "/de/karriere"),
                ("careers", "/recruit"),
                ("careers", "/en/career"),
                ("company", "/company"),
            ],
        )

    def test_child_career_links_follow_branded_ats_from_careers_page(self):
        html = """
        <a href="/en/product">Product</a>
        <a href="https://jobs.ashbyhq.com/tacto">Open Positions</a>
        <a href="https://linkedin.com/company/tacto">LinkedIn</a>
        """
        targets = cascade.candidate_child_career_targets_from_html(
            "tacto.ai",
            "https://www.tacto.ai/en/career",
            html,
        )

        self.assertEqual(
            [(t["page_type"], t["path"], t.get("url")) for t in targets],
            [("careers", "https://jobs.ashbyhq.com/tacto", "https://jobs.ashbyhq.com/tacto")],
        )
        self.assertTrue(targets[0]["linked_from_homepage"])

    def test_child_career_links_follow_applytojob_paylocity_saashr_ultipro_boards(self):
        html = """
        <a href="https://sendoso.applytojob.com/apply">View Openings</a>
        <a href="https://recruiting.paylocity.com/recruiting/jobs/All/example-board-0001/JSL-TECHNOLOGIES-INCORPORATED">Search Current Job Openings</a>
        <a href="https://secure7.saashr.com/ta/6206855.careers?CareersSearch=&lang=en-US">View Open Roles</a>
        <a href="https://recruiting2.ultipro.com/FLO1013FLFR/JobBoard/example-board-0002/">View open positions</a>
        """
        targets = cascade.candidate_child_career_targets_from_html(
            "example.com",
            "https://www.example.com/careers",
            html,
        )

        paths = [t["path"] for t in targets]
        self.assertIn("https://sendoso.applytojob.com/apply", paths)
        self.assertIn(
            "https://recruiting.paylocity.com/recruiting/jobs/All/example-board-0001/JSL-TECHNOLOGIES-INCORPORATED",
            paths,
        )
        self.assertIn("https://secure7.saashr.com/ta/6206855.careers?CareersSearch=&lang=en-US", paths)
        self.assertIn("https://recruiting2.ultipro.com/FLO1013FLFR/JobBoard/example-board-0002", paths)
        self.assertTrue(all(t["page_type"] == "careers" for t in targets))
        self.assertTrue(all(t["linked_from_homepage"] for t in targets))

    def test_child_career_links_follow_gem_and_pinpoint_boards(self):
        html = """
        <script>window.__gemJobBoardUrl = "https://jobs.gem.com/ocient-inc-"</script>
        <a href="https://thecrossinglv.pinpointhq.com/">Apply Here</a>
        """
        targets = cascade.candidate_child_career_targets_from_html(
            "example.com",
            "https://www.example.com/careers",
            html,
        )

        paths = [t["path"] for t in targets]
        self.assertIn("https://jobs.gem.com/ocient-inc-", paths)
        self.assertIn("https://thecrossinglv.pinpointhq.com/", paths)
        self.assertTrue(all(t["page_type"] == "careers" for t in targets))
        self.assertTrue(all(t["linked_from_homepage"] for t in targets))

    def test_child_career_links_follow_same_site_listing_paths(self):
        html = """
        <a href="/join-us/jobs-listing/">Explore Job Openings</a>
        <a href="/careers/board">See Open Jobs</a>
        <a href="/about-asme/careers-at-asme/job-opportunities">Browse our vacancies</a>
        """
        targets = cascade.candidate_child_career_targets_from_html(
            "example.com",
            "https://www.example.com/join-us",
            html,
        )

        self.assertEqual(
            [(t["page_type"], t["path"]) for t in targets],
            [
                ("careers", "/join-us/jobs-listing"),
                ("careers", "/careers/board"),
                ("careers", "/about-asme/careers-at-asme/job-opportunities"),
            ],
        )
        self.assertTrue(all(t["linked_from_homepage"] for t in targets))

    def test_child_career_links_follow_embedded_jobvite_widget(self):
        html = """
        <h1>Browse Open Positions</h1>
        <div class="jv-careersite" data-careersite="arteris"></div>
        <script src="https://jobs.jobvite.com/__assets__/scripts/careersite/public/iframe.js"></script>
        """
        targets = cascade.candidate_child_career_targets_from_html(
            "arteris.com",
            "https://www.arteris.com/careers/open-positions/",
            html,
        )

        self.assertEqual(
            [(t["page_type"], t["path"], t.get("url")) for t in targets],
            [("careers", "https://jobs.jobvite.com/arteris/jobs?nl=1", "https://jobs.jobvite.com/arteris/jobs?nl=1")],
        )
        self.assertTrue(targets[0]["linked_from_homepage"])

    def test_child_career_links_follow_greenhouse_widget_to_board_url(self):
        html = """
        <h1>Open Positions</h1>
        <script src="https://boards.greenhouse.io/embed/job_board/js?for=noomgrowth"></script>
        """
        targets = cascade.candidate_child_career_targets_from_html(
            "noom.com",
            "https://www.noom.com/careers/job-listings/",
            html,
        )

        paths = [t["path"] for t in targets]
        self.assertNotIn("https://boards.greenhouse.io/embed/job_board/js?for=noomgrowth", paths)  # widget JS resolves to the board, not itself a fetch target
        self.assertIn("https://job-boards.greenhouse.io/embed/job_board?for=noomgrowth", paths)
        self.assertTrue(all(t["linked_from_homepage"] for t in targets))

    def test_child_career_links_follow_greenhouse_eu_widget_to_eu_board_url(self):
        html = """
        <h1>Open Positions</h1>
        <script src="https://boards.eu.greenhouse.io/embed/job_board/js?for=eonio"></script>
        """
        targets = cascade.candidate_child_career_targets_from_html(
            "eon.io",
            "https://www.eon.io/careers",
            html,
        )

        paths = [t["path"] for t in targets]
        self.assertNotIn("https://boards.eu.greenhouse.io/embed/job_board/js?for=eonio", paths)  # widget JS resolves to the board, not itself a fetch target
        self.assertIn("https://job-boards.eu.greenhouse.io/embed/job_board?for=eonio", paths)
        self.assertTrue(all(t["linked_from_homepage"] for t in targets))

    def test_child_career_links_follow_bamboohr_embed_to_board_url(self):
        html = """
        <h1>Open Positions</h1>
        <script src="https://cim.bamboohr.com/js/embed.js"></script>
        """
        targets = cascade.candidate_child_career_targets_from_html(
            "cim.io",
            "https://www.cim.io/about-us/open-positions",
            html,
        )

        paths = [t["path"] for t in targets]
        self.assertNotIn("https://cim.bamboohr.com/js/embed.js", paths)  # widget JS resolves to the board, not itself a fetch target
        self.assertIn("https://cim.bamboohr.com/careers", paths)
        self.assertTrue(all(t["linked_from_homepage"] for t in targets))

    def test_child_career_links_follow_rippling_embed_to_board_url(self):
        html = """
        <h1>Open positions</h1>
        <div id="rr-job-board" data-job-board-id="evidation"></div>
        <script src="https://static-assets.ripplingcdn.com/ats/embeds/job-board.v1.js" async></script>
        """
        targets = cascade.candidate_child_career_targets_from_html(
            "evidation.com",
            "https://evidation.com/open-positions",
            html,
        )

        self.assertEqual(
            [(t["page_type"], t["path"], t.get("url")) for t in targets],
            [
                (
                    "careers",
                    "https://ats.rippling.com/evidation/jobs",
                    "https://ats.rippling.com/evidation/jobs",
                )
            ],
        )
        self.assertTrue(targets[0]["linked_from_homepage"])

    def test_child_career_links_follow_icims_iframe_wrapper(self):
        html = """
        <h1>Open Positions</h1>
        <script>
        icimsFrame.src = 'https:\\/\\/careers-hayward.icims.com\\/jobs\\/search?ss=1&in_iframe=1';
        </script>
        <noscript>
          <iframe src="https://careers-hayward.icims.com/jobs/search?ss=1&amp;in_iframe=1"></iframe>
        </noscript>
        """
        targets = cascade.candidate_child_career_targets_from_html(
            "hayward.com",
            "https://www.hayward.com/careers",
            html,
        )

        self.assertEqual(
            [(t["page_type"], t["path"], t.get("url")) for t in targets],
            [
                (
                    "careers",
                    "https://careers-hayward.icims.com/jobs/search?ss=1&in_iframe=1",
                    "https://careers-hayward.icims.com/jobs/search?ss=1&in_iframe=1",
                )
            ],
        )
        self.assertTrue(targets[0]["linked_from_homepage"])

    def test_child_career_links_follow_same_site_job_detail_pages(self):
        html = """
        <h1>Open Positions</h1>
        <a href="/careers/software-engineer">Software Engineer</a>
        <a href="/careers/culture">Culture</a>
        <a href="/careers/benefits">Benefits</a>
        <a href="/careers/impact">Impact</a>
        <a href="/careers/overview">Overview</a>
        <a href="/careers/student-program">Student Program</a>
        """
        targets = cascade.candidate_child_career_targets_from_html(
            "example.com",
            "https://www.example.com/careers",
            html,
        )

        self.assertEqual(
            [(t["page_type"], t["path"]) for t in targets],
            [("careers", "/careers/software-engineer")],
        )
        self.assertTrue(targets[0]["linked_from_homepage"])

    def test_rendered_job_links_html_keeps_clicked_dom_ats_and_job_links(self):
        class FakePage:
            def evaluate(self, _script):
                return [
                    {"url": "https://jobs.ashbyhq.com/example", "text": "Open roles"},
                    {"url": "https://example.com/careers/software-engineer", "text": "Software Engineer"},
                    {"url": "https://example.com/privacy", "text": "Privacy"},
                ]

        html = cascade._rendered_job_links_html(FakePage())

        self.assertIn("https://jobs.ashbyhq.com/example", html)
        self.assertIn("https://example.com/careers/software-engineer", html)
        self.assertNotIn("https://example.com/privacy", html)

    def test_opened_job_links_html_keeps_popup_ats_urls(self):
        class FakePage:
            def evaluate(self, _script):
                return [
                    "https://recruiting.paylocity.com/recruiting/jobs/All/abc/Example",
                    "https://linkedin.com/company/example",
                ]

        html = cascade._opened_job_links_html(FakePage())

        self.assertIn("https://recruiting.paylocity.com/recruiting/jobs/All/abc/Example", html)
        self.assertNotIn("linkedin.com", html)

    def test_candidate_links_trim_trailing_punctuation_from_embedded_ats_urls(self):
        html = """
        <p>See all job listings at https://skedulo.bamboohr.com/careers.</p>
        """
        targets = cascade.candidate_child_career_targets_from_html(
            "skedulo.com",
            "https://www.skedulo.com/careers/",
            html,
        )

        self.assertEqual(targets[0]["path"], "https://skedulo.bamboohr.com/careers")

    def test_candidate_links_extract_embedded_ats_urls_without_anchors(self):
        html = """
        <script>
        window.__jobs = {
          greenhouse: "https://boards.greenhouse.io/acme",
          workable: "https://apply.workable.com/acme/",
          bamboo: "https://acme.bamboohr.com/careers/list"
          icims: "https:\\/\\/careers-acme.icims.com\\/jobs\\/search?ss=1&in_iframe=1"
        };
        const staticAsset = "https://c-5038-20230807-acme.i.icims.com/static/fonts/Lato.woff2";
        </script>
        """
        targets = cascade.candidate_page_targets_from_html(
            "example.com",
            "https://example.com/careers",
            html,
        )

        paths = {t["path"] for t in targets}
        self.assertIn("https://boards.greenhouse.io/acme", paths)
        self.assertIn("https://apply.workable.com/acme", paths)
        self.assertIn("https://acme.bamboohr.com/careers/list", paths)
        self.assertIn("https://careers-acme.icims.com/jobs/search?ss=1&in_iframe=1", paths)
        self.assertNotIn("https://c-5038-20230807-acme.i.icims.com/static/fonts/Lato.woff2", paths)
        self.assertTrue(all(t["page_type"] == "careers" for t in targets))
        self.assertTrue(all(t["linked_from_homepage"] for t in targets))

    def test_html_to_text_appends_icims_job_detail_links(self):
        html = """
        <ul class="iCIMS_JobsTable">
          <li class="iCIMS_JobCardItem">
            <a href="https://careers-hayward.icims.com/jobs/5256/zone-manager/job?in_iframe=1">Zone Manager</a>
          </li>
          <li class="iCIMS_JobCardItem">
            <a href="https://careers-hayward.icims.com/jobs/5255/technical-sales-manager/job?in_iframe=1">Technical Sales Manager</a>
          </li>
        </ul>
        """

        text = cascade.html_to_text(html)

        self.assertIn("Embedded ATS job links:", text)
        self.assertIn("https://careers-hayward.icims.com/jobs/5256/zone-manager/job?in_iframe=1", text)
        self.assertIn("https://careers-hayward.icims.com/jobs/5255/technical-sales-manager/job?in_iframe=1", text)

    def test_embedded_ats_urls_reject_widget_noise(self):
        html = """
        <script>
        const urls = [
          "https://jobs.jobvite.com/arc/jobs",
          "https://jobs.jobvite.com/arc/${poweredByUrl}",
          "http://login.jobvite.com/",
          "https://jobs.jobvite.com/arc/jobAlerts",
          "https://evangel.bamboohr.com/jobs/share_image/28",
          "https://careers-hayward.icims.com/customer/account/login"
        ];
        </script>
        """
        targets = cascade.candidate_page_targets_from_html(
            "example.com",
            "https://example.com/careers",
            html,
        )

        self.assertEqual([t["path"] for t in targets], ["https://jobs.jobvite.com/arc/jobs"])

    def test_candidate_links_reject_ats_utility_anchors(self):
        html = """
        <a href="https://jobs.jobvite.com/arc/jobs">All jobs</a>
        <a href="https://jobs.jobvite.com/arc/jobAlerts">Job alerts</a>
        <a href="https://jobs.jobvite.com/arc/${poweredByUrl}">Powered by</a>
        <a href="https://www.bamboohr.com/privacy-policy">Privacy Policy</a>
        <a href="https://breezy.hr/attract#advertise-jobs">Breezy marketing</a>
        <a href="https://ats.rippling.com/evidation/jobs">Rippling jobs</a>
        <a href="https://app.rippling.com/legal/privacy">Rippling privacy</a>
        <a href="https://www.rippling.com/products/hr/recruiting">Rippling recruiting product</a>
        <a href="https://recruiting.paylocity.com/Recruiting/Jobs/JobNotFound">Paylocity missing job</a>
        <a href="https://recruiting.paylocity.com/Recruiting/PublicLeads/New/abc">Paylocity lead form</a>
        <a href="https://www.paylocity.com/">Paylocity marketing</a>
        <a href="https://recruiting.ultipro.com/ORI1005ORIH/JobBoard/abc/Accessibility">UltiPro accessibility</a>
        <a href="https://recruiting.ultipro.com/ORI1005ORIH/JobBoard/abc/Account/Register">UltiPro register</a>
        <a href="https://jobseekers.workable.com/hc/en-us/categories/360002069553-Your-Application-Profile">Workable help center</a>
        <a href="https://jobs.gem.com/gem/embed">Gem embed asset</a>
        <a href="https://thecrossinglv.pinpointhq.com/register-your-interest/new">Register interest</a>
        <a href="https://www.pinpointhq.com/">Pinpoint marketing</a>
        """
        targets = cascade.candidate_page_targets_from_html(
            "example.com",
            "https://example.com/careers",
            html,
        )

        self.assertEqual(
            [t["path"] for t in targets],
            ["https://jobs.jobvite.com/arc/jobs", "https://ats.rippling.com/evidation/jobs"],
        )

    def test_candidate_links_do_not_treat_jobsite_product_pages_as_careers(self):
        html = """
        <a href="/digital-printing/construction-jobsite-graphics/">Construction Jobsite Graphics</a>
        <a href="/careers">Careers</a>
        """
        targets = cascade.candidate_page_targets_from_html(
            "e-arc.com",
            "https://www.e-arc.com/",
            html,
        )

        self.assertEqual(
            [(t["page_type"], t["path"]) for t in targets],
            [("careers", "/careers")],
        )

    def test_capped_page_selection_prefers_linked_ats_over_jobsite_noise(self):
        specs = [
            {"domain": "e-arc.com", "page_type": "careers", "path": "/digital-printing/construction-jobsite-graphics", "linked_from_homepage": True},
            {"domain": "e-arc.com", "page_type": "careers", "path": "/careers", "linked_from_homepage": False},
            {"domain": "e-arc.com", "page_type": "careers", "path": "https://jobs.jobvite.com/arc/jobs/viewall", "url": "https://jobs.jobvite.com/arc/jobs/viewall", "linked_from_homepage": True},
        ]

        selected = cascade.select_page_specs(specs, max_pages_per_domain=1, page_type_quota={"careers": 1})

        self.assertEqual(selected[0]["path"], "https://jobs.jobvite.com/arc/jobs/viewall")

    def test_child_career_links_stay_narrow(self):
        html = """
        <a href="/en/resources">Resources</a>
        <a href="/en/about">Company</a>
        <a href="/en/career#why">Why Tacto?</a>
        """
        targets = cascade.candidate_child_career_targets_from_html(
            "tacto.ai",
            "https://www.tacto.ai/en/career",
            html,
        )

        self.assertEqual(targets, [])

    def test_candidate_links_reject_product_recruiting_pages_and_root_links(self):
        html = """
        <a href="/">Apply Now</a>
        <a href="/platform/talent-acquisition/career-sites/">Career Sites</a>
        <a href="/solutions/recruiters/">Recruiter Solutions</a>
        <a href="/about/careers">Careers</a>
        """
        targets = cascade.candidate_page_targets_from_html(
            "beamery.com",
            "https://beamery.com/",
            html,
        )

        self.assertEqual(
            [(t["page_type"], t["path"]) for t in targets],
            [("careers", "/about/careers")],
        )

    def test_selector_prioritizes_homepage_linked_ats_within_capped_type(self):
        static = [
            {"domain": "example.com", "page_type": "careers", "path": "/careers"},
            {"domain": "example.com", "page_type": "careers", "path": "/jobs"},
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "https://boards.greenhouse.io/acme",
                "url": "https://boards.greenhouse.io/acme",
                "linked_from_homepage": True,
            },
            {"domain": "example.com", "page_type": "news", "path": "/news"},
        ]
        selected = cascade.select_page_specs(
            static,
            max_pages_per_domain=2,
            page_type_quota={"careers": 1, "news": 1},
        )

        self.assertEqual([s["path"] for s in selected], ["https://boards.greenhouse.io/acme", "/news"])

    def test_configured_targets_include_careers_subdomain(self):
        specs = cascade.page_specs_from_rubric(RUBRIC)
        targets = run.configured_page_targets("uniswap.org", specs)

        self.assertIn(
            {
                "domain": "uniswap.org",
                "path": "https://careers.uniswap.org/",
                "url": "https://careers.uniswap.org/",
                "page_type": "careers",
                "evidence_terms": [],
                "linked_from_homepage": False,
            },
            targets,
        )

    def test_selector_demotes_unlinked_careers_subdomain_guess(self):
        # 2026-06-10 reversal of the old priority: the blind careers.{d} probe
        # NXDOMAINs for most companies (sdr500_r1: 426 of 1,024 careers
        # status-0s) and used to steal a quota slot from real configured paths.
        # A homepage-LINKED careers subdomain still outranks everything.
        selected = cascade.select_page_specs(
            [
                {"domain": "uniswap.org", "page_type": "careers", "path": "/careers"},
                {"domain": "uniswap.org", "page_type": "careers", "path": "/jobs"},
                {
                    "domain": "uniswap.org",
                    "page_type": "careers",
                    "path": "https://careers.uniswap.org/",
                    "url": "https://careers.uniswap.org/",
                },
            ],
            max_pages_per_domain=2,
            page_type_quota={"careers": 2},
        )

        self.assertEqual([s["path"] for s in selected], ["/careers", "/jobs"])

    def test_selector_keeps_linked_careers_subdomain_first(self):
        selected = cascade.select_page_specs(
            [
                {"domain": "uniswap.org", "page_type": "careers", "path": "/careers"},
                {
                    "domain": "uniswap.org",
                    "page_type": "careers",
                    "path": "https://careers.uniswap.org/",
                    "url": "https://careers.uniswap.org/",
                    "linked_from_homepage": True,
                },
            ],
            max_pages_per_domain=1,
            page_type_quota={"careers": 1},
        )

        self.assertEqual([s["path"] for s in selected], ["https://careers.uniswap.org/"])

    def test_selector_prioritizes_careers_hub_over_generic_jobs(self):
        selected = cascade.select_page_specs(
            [
                {"domain": "perplexity.ai", "page_type": "careers", "path": "/careers"},
                {"domain": "perplexity.ai", "page_type": "careers", "path": "/jobs"},
                {"domain": "perplexity.ai", "page_type": "careers", "path": "/hub/careers"},
            ],
            max_pages_per_domain=2,
            page_type_quota={"careers": 2},
        )

        self.assertEqual([s["path"] for s in selected], ["/hub/careers", "/careers"])

    def test_run_target_selection_does_not_starve_homepage_candidates(self):
        specs = [
            {"domain": "example.com", "page_type": "careers", "path": "/careers"},
            {"domain": "example.com", "page_type": "careers", "path": "/jobs"},
            {"domain": "example.com", "page_type": "news", "path": "/news"},
        ]
        link_targets = [
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "https://boards.greenhouse.io/acme",
                "url": "https://boards.greenhouse.io/acme",
                "linked_from_homepage": True,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            conn = cascade.connect(str(Path(tmp) / "cache.db"))
            selected = run.select_targets_for_domain(
                conn,
                "example.com",
                specs,
                link_targets=link_targets,
                max_pages_per_domain=2,
                page_type_quota={"careers": 1, "news": 1},
            )

        self.assertEqual([s["path"] for s in selected], ["https://boards.greenhouse.io/acme", "/news"])

    def test_late_candidate_drain_fetches_selected_linked_careers_candidates_only(self):
        specs = [
            {"domain": "example.com", "page_type": "careers", "path": "/careers"},
            {"domain": "example.com", "page_type": "careers", "path": "/jobs"},
            {"domain": "example.com", "page_type": "news", "path": "/news"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            conn = cascade.connect(str(Path(tmp) / "cache.db"))
            cascade.upsert_discovered_page(
                conn,
                "example.com",
                "/jobs",
                "careers",
                "https://example.com/jobs",
                404,
                "httpx",
                False,
                "",
            )
            run.record_candidate_targets(
                conn,
                [
                    {
                        "domain": "example.com",
                        "page_type": "careers",
                        "path": "/open-positions",
                        "linked_from_homepage": True,
                    }
                ],
            )
            conn.commit()
            selected = run.select_targets_for_domain(
                conn,
                "example.com",
                specs,
                max_pages_per_domain=2,
                page_type_quota={"careers": 1, "news": 1},
            )
            to_fetch = run.targets_needing_fetch(conn, selected, candidate_only=True)

        self.assertEqual([t["path"] for t in selected], ["/open-positions", "/news"])
        self.assertEqual([t["path"] for t in to_fetch], ["/open-positions"])

    def test_refetch_selection_can_ignore_stale_stored_targets(self):
        specs = [{"domain": "example.com", "page_type": "careers", "path": "/careers"}]
        with tempfile.TemporaryDirectory() as tmp:
            conn = cascade.connect(str(Path(tmp) / "cache.db"))
            run.record_candidate_targets(
                conn,
                [
                    {
                        "domain": "example.com",
                        "page_type": "careers",
                        "path": "/platform/talent-acquisition/career-sites",
                        "url": "https://example.com/platform/talent-acquisition/career-sites",
                        "linked_from_homepage": True,
                    }
                ],
            )
            conn.commit()
            selected = run.select_targets_for_domain(
                conn,
                "example.com",
                specs,
                max_pages_per_domain=2,
                include_stored=False,
            )

        self.assertCountEqual([s["path"] for s in selected], ["https://careers.example.com/", "/careers"])

    def test_browser_rescue_pages_keeps_narrow_careers_set(self):
        targets = [
            {"domain": "example.com", "page_type": "careers", "path": "/careers"},
            {"domain": "example.com", "page_type": "careers", "path": "/jobs", "linked_from_homepage": True},
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "https://apply.workable.com/example",
                "url": "https://apply.workable.com/example",
                "linked_from_homepage": True,
            },
            {"domain": "example.com", "page_type": "news", "path": "/news", "linked_from_homepage": True},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            conn = cascade.connect(str(Path(tmp) / "cache.db"))
            cascade.upsert_discovered_page(
                conn,
                "example.com",
                "/careers",
                "careers",
                "https://example.com/company#company-careers",
                200,
                "httpx",
                True,
                "Careers",
            )
            cascade.upsert_discovered_page(
                conn,
                "example.com",
                "https://apply.workable.com/example",
                "careers",
                "https://apply.workable.com/example",
                200,
                "httpx",
                True,
                "x",
                linked_from_homepage=True,
            )
            selected = run.select_browser_rescue_page_targets(conn, targets)

        self.assertEqual(
            [s["path"] for s in selected],
            ["/careers", "/jobs", "https://apply.workable.com/example"],
        )

    def test_browser_rescue_pages_includes_official_careers_guess_with_weak_rendered_text(self):
        targets = [
            {"domain": "example.com", "page_type": "careers", "path": "/careers"},
            {"domain": "example.com", "page_type": "news", "path": "/news"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            conn = cascade.connect(str(Path(tmp) / "cache.db"))
            cascade.upsert_discovered_page(
                conn,
                "example.com",
                "/careers",
                "careers",
                "https://example.com/careers",
                200,
                "httpx",
                True,
                "Careers. Join our team. Use the Open Positions control below.",
            )
            selected = run.select_browser_rescue_page_targets(conn, targets)

        self.assertEqual([s["path"] for s in selected], ["/careers"])

    def test_browser_rescue_pages_includes_linked_open_positions_child(self):
        targets = [
            {"domain": "suki.ai", "page_type": "careers", "path": "/open-positions", "linked_from_homepage": True},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            conn = cascade.connect(str(Path(tmp) / "cache.db"))
            cascade.upsert_discovered_page(
                conn,
                "suki.ai",
                "/open-positions",
                "careers",
                "https://www.suki.ai/open-positions/",
                200,
                "httpx",
                True,
                "Current Openings. Company Resources Social Policies.",
                linked_from_homepage=True,
            )
            selected = run.select_browser_rescue_page_targets(conn, targets)

        self.assertEqual([s["path"] for s in selected], ["/open-positions"])

    def test_browser_rescue_pages_skips_linked_ats_with_strong_job_text(self):
        targets = [
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "https://careers-example.icims.com/jobs/search?ss=1&in_iframe=1",
                "url": "https://careers-example.icims.com/jobs/search?ss=1&in_iframe=1",
                "linked_from_homepage": True,
            },
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "https://apply.workable.com/example",
                "url": "https://apply.workable.com/example",
                "linked_from_homepage": True,
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            conn = cascade.connect(str(Path(tmp) / "cache.db"))
            cascade.upsert_discovered_page(
                conn,
                "example.com",
                "https://careers-example.icims.com/jobs/search?ss=1&in_iframe=1",
                "careers",
                "https://careers-example.icims.com/jobs/search?ss=1&in_iframe=1",
                200,
                "httpx",
                True,
                "Job Listings. Here are our current job openings. Full-time. Apply.",
                linked_from_homepage=True,
            )
            cascade.upsert_discovered_page(
                conn,
                "example.com",
                "https://apply.workable.com/example",
                "careers",
                "https://apply.workable.com/example",
                200,
                "httpx",
                True,
                "Apply.",
                linked_from_homepage=True,
            )
            selected = run.select_browser_rescue_page_targets(conn, targets)

        self.assertEqual([s["path"] for s in selected], ["https://apply.workable.com/example"])

    def test_browser_rescue_pages_skips_ats_widget_utility_urls(self):
        targets = [
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "https://jobs.jobvite.com/acme/jobs",
                "url": "https://jobs.jobvite.com/acme/jobs",
                "linked_from_homepage": True,
            },
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "https://jobs.jobvite.com/acme/${poweredByUrl}",
                "url": "https://jobs.jobvite.com/acme/${poweredByUrl}",
                "linked_from_homepage": True,
            },
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "https://example.com/dei",
                "url": "https://example.com/dei",
                "linked_from_homepage": True,
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            conn = cascade.connect(str(Path(tmp) / "cache.db"))
            selected = run.select_browser_rescue_page_targets(conn, targets)

        self.assertEqual([s["path"] for s in selected], ["https://jobs.jobvite.com/acme/jobs"])

    def test_browser_rescue_pages_include_unlinked_blocked_official_careers_guess(self):
        targets = [
            {
                "domain": "1stpetvet.com",
                "page_type": "careers",
                "path": "https://careers.1stpetvet.com/",
                "url": "https://careers.1stpetvet.com/",
                "linked_from_homepage": False,
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            conn = cascade.connect(str(Path(tmp) / "cache.db"))
            cascade.upsert_discovered_page(
                conn,
                "1stpetvet.com",
                "https://careers.1stpetvet.com/",
                "careers",
                "https://careers.1stpetvet.com/",
                403,
                "httpx",
                False,
                "",
                linked_from_homepage=False,
            )
            selected = run.select_browser_rescue_page_targets(conn, targets)

        self.assertEqual([s["path"] for s in selected], ["https://careers.1stpetvet.com/"])

    def test_browser_rescue_pages_include_unlinked_blocked_ats_board(self):
        targets = [
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "https://apply.workable.com/example",
                "url": "https://apply.workable.com/example",
                "linked_from_homepage": False,
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            conn = cascade.connect(str(Path(tmp) / "cache.db"))
            cascade.upsert_discovered_page(
                conn,
                "example.com",
                "https://apply.workable.com/example",
                "careers",
                "https://apply.workable.com/example",
                403,
                "httpx",
                False,
                "",
                linked_from_homepage=False,
            )
            selected = run.select_browser_rescue_page_targets(conn, targets)

        self.assertEqual([s["path"] for s in selected], ["https://apply.workable.com/example"])

    def test_candidate_links_reject_ats_host_suffix_trick(self):
        html = '<a href="https://greenhouse.io.evil.example/jobs">Open roles</a>'
        targets = cascade.candidate_page_targets_from_html(
            "example.com",
            "https://example.com/",
            html,
        )
        self.assertEqual(targets, [])

    def test_record_candidate_targets_makes_link_discovery_resumable(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = cascade.connect(str(Path(tmp) / "cache.db"))
            run.record_candidate_targets(
                conn,
                [
                    {
                        "domain": "example.com",
                        "page_type": "careers",
                        "path": "https://boards.greenhouse.io/acme",
                        "url": "https://boards.greenhouse.io/acme",
                        "linked_from_homepage": True,
                    }
                ],
            )
            conn.commit()

            got = cascade.get_discovered_page(
                conn,
                "example.com",
                "https://boards.greenhouse.io/acme",
            )

        self.assertEqual(got["tier"], "candidate")
        self.assertEqual(got["ok"], 0)
        self.assertEqual(got["linked_from_homepage"], 1)

    def test_record_candidate_targets_does_not_downgrade_fetched_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = cascade.connect(str(Path(tmp) / "cache.db"))
            cascade.upsert_discovered_page(
                conn,
                "example.com",
                "https://boards.greenhouse.io/acme",
                "careers",
                "https://boards.greenhouse.io/acme",
                200,
                "httpx",
                True,
                "Open roles",
            )
            run.record_candidate_targets(
                conn,
                [
                    {
                        "domain": "example.com",
                        "page_type": "careers",
                        "path": "https://boards.greenhouse.io/acme",
                        "url": "https://boards.greenhouse.io/acme",
                    }
                ],
            )
            got = cascade.get_discovered_page(conn, "example.com", "https://boards.greenhouse.io/acme")

        self.assertEqual(got["tier"], "httpx")
        self.assertEqual(got["ok"], 1)
        self.assertEqual(got["text"], "Open roles")

    def test_record_candidate_targets_can_add_homepage_provenance_to_existing_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = cascade.connect(str(Path(tmp) / "cache.db"))
            cascade.upsert_discovered_page(
                conn,
                "example.com",
                "https://boards.greenhouse.io/acme",
                "careers",
                "https://boards.greenhouse.io/acme",
                200,
                "httpx",
                True,
                "Open roles",
            )
            run.record_candidate_targets(
                conn,
                [
                    {
                        "domain": "example.com",
                        "page_type": "careers",
                        "path": "https://boards.greenhouse.io/acme",
                        "url": "https://boards.greenhouse.io/acme",
                        "linked_from_homepage": True,
                    }
                ],
            )
            got = cascade.get_discovered_page(conn, "example.com", "https://boards.greenhouse.io/acme")

        self.assertEqual(got["linked_from_homepage"], 1)

    def test_discover_child_career_targets_records_ats_for_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = cascade.connect(str(Path(tmp) / "cache.db"))
            found = run.discover_child_career_targets(
                conn,
                {
                    "domain": "tacto.ai",
                    "page_type": "careers",
                    "url": "https://www.tacto.ai/en/career",
                    "ok": True,
                    "html": '<a href="https://jobs.ashbyhq.com/tacto">Open Positions</a>',
                },
                {},
            )
            got = cascade.get_discovered_page(conn, "tacto.ai", "https://jobs.ashbyhq.com/tacto")

        self.assertEqual(found[0]["path"], "https://jobs.ashbyhq.com/tacto")
        self.assertEqual(got["tier"], "candidate")
        self.assertEqual(got["linked_from_homepage"], 1)

    def test_export_page_rows_can_hide_unlisted_candidates_for_capped_runs(self):
        specs = [{"path": "/careers", "page_type": "careers", "evidence_terms": ["open roles"]}]
        with tempfile.TemporaryDirectory() as tmp:
            conn = cascade.connect(str(Path(tmp) / "cache.db"))
            cascade.upsert_discovered_page(
                conn,
                "example.com",
                "/careers",
                "careers",
                "https://example.com/careers",
                200,
                "httpx",
                True,
                "Open roles",
            )
            run.record_candidate_targets(
                conn,
                [
                    {
                        "domain": "example.com",
                        "page_type": "careers",
                        "path": "https://boards.greenhouse.io/acme",
                        "url": "https://boards.greenhouse.io/acme",
                    }
                ],
            )
            rows = run.export_page_rows(
                conn,
                RUBRIC,
                ["example.com"],
                specs,
                include_unlisted=False,
            )

        self.assertEqual([row["path"] for row in rows], ["/careers"])

    def test_capped_evidence_export_keeps_extra_linked_ats_hiring_candidates(self):
        allowed_rows = [{"domain": "example.com", "path": "/careers"}]
        with tempfile.TemporaryDirectory() as tmp:
            conn = cascade.connect(str(Path(tmp) / "cache.db"))
            cascade.upsert_page(
                conn,
                "example.com",
                "https://example.com/",
                200,
                "httpx",
                True,
                "Example company platform customers security privacy careers contact sales. " * 4,
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
                "Open roles",
                linked_from_homepage=True,
            )
            cascade.upsert_discovered_page(
                conn,
                "example.com",
                "https://example.aaimtrack.com/jobs/12345",
                "careers",
                "https://example.aaimtrack.com/jobs/12345",
                200,
                "playwright-page",
                True,
                "Apply for this Position. Full time.",
                linked_from_homepage=True,
            )
            cascade.upsert_discovered_page(
                conn,
                "example.com",
                "https://example.aaimtrack.com/account/login.php",
                "careers",
                "https://example.aaimtrack.com/account/login.php",
                200,
                "httpx",
                True,
                "Login Email Address Password.",
                linked_from_homepage=True,
            )
            cascade.upsert_discovered_page(
                conn,
                "example.com",
                "https://admin.aaimtrack.com/applicant-communication-policy",
                "careers",
                "https://admin.aaimtrack.com/applicant-communication-policy/",
                200,
                "httpx",
                True,
                "Communications concerning applications you apply to.",
                linked_from_homepage=True,
            )
            cascade.upsert_discovered_page(
                conn,
                "example.com",
                "https://example.aaimtrack.com/widget/refer_io.php",
                "careers",
                "https://example.aaimtrack.com/widget/refer_io.php",
                200,
                "httpx",
                True,
                "Sign Up For Job Alerts.",
                linked_from_homepage=True,
            )
            allowed = run.allowed_keys_from_page_rows(allowed_rows)
            rows = run.export_evidence_rows(conn, ["example.com"], allowed)

        urls = [row["source_url"] for row in rows]
        self.assertIn("https://example.com/careers", urls)
        self.assertIn("https://example.aaimtrack.com/jobs/12345", urls)
        self.assertNotIn("https://example.aaimtrack.com/account/login.php", urls)
        self.assertNotIn("https://admin.aaimtrack.com/applicant-communication-policy/", urls)
        self.assertNotIn("https://example.aaimtrack.com/widget/refer_io.php", urls)

    def test_export_page_rows_dedupes_redirected_final_urls(self):
        specs = [
            {"path": "/careers", "page_type": "careers", "evidence_terms": ["open roles"]},
            {"path": "/jobs", "page_type": "careers", "evidence_terms": ["open roles"]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            conn = cascade.connect(str(Path(tmp) / "cache.db"))
            for path in ["/careers", "/jobs"]:
                cascade.upsert_discovered_page(
                    conn,
                    "example.com",
                    path,
                    "careers",
                    "https://example.com/careers",
                    200,
                    "httpx",
                    True,
                    "Open roles",
                )
            rows = run.export_page_rows(
                conn,
                RUBRIC,
                ["example.com"],
                specs,
                include_unlisted=False,
                dedupe_final_url=True,
            )

        self.assertEqual([row["path"] for row in rows], ["/careers"])

    def test_spec_page_type_wins_over_cached_page_type_on_export(self):
        rubric = {
            "name": "unit",
            "positive_label": "yes",
            "negative_label": "no",
            "page_evidence_terms": {"security": ["soc 2"]},
        }
        specs = [{"path": "/trust", "page_type": "security", "evidence_terms": []}]
        with tempfile.TemporaryDirectory() as tmp:
            conn = cascade.connect(str(Path(tmp) / "cache.db"))
            cascade.upsert_discovered_page(
                conn,
                "example.com",
                "/trust",
                "careers",
                "https://example.com/trust",
                200,
                "httpx",
                True,
                "SOC 2 report",
            )
            rows = run.export_page_rows(conn, rubric, ["example.com"], specs)

        self.assertEqual(rows[0]["page_type"], "security")
        self.assertIn("soc 2", rows[0]["matched_terms"])

    def test_discovered_page_text_is_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = cascade.connect(str(Path(tmp) / "cache.db"))
            cascade.upsert_discovered_page(
                conn,
                "example.com",
                "/jobs",
                "careers",
                "https://example.com/jobs",
                200,
                "httpx",
                True,
                "x" * (cascade.TEXT_CAP + 100),
            )
            got = cascade.get_discovered_page(conn, "example.com", "/jobs")
            self.assertEqual(len(got["text"]), cascade.TEXT_CAP)

    def test_export_page_rows_includes_lightweight_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = cascade.connect(str(Path(tmp) / "cache.db"))
            cascade.upsert_discovered_page(
                conn,
                "example.com",
                "/careers/",
                "careers",
                "https://example.com/careers",
                200,
                "httpx",
                True,
                "Open roles include a macOS endpoint administrator using endpoint management and okta.",
            )

            specs = cascade.page_specs_from_rubric(RUBRIC)
            rows = run.export_page_rows(conn, RUBRIC, ["example.com"], specs)

        careers = rows[0]
        self.assertEqual(careers["domain"], "example.com")
        self.assertEqual(careers["path"], "/careers")
        self.assertEqual(careers["page_type"], "careers")
        self.assertEqual(careers["ok"], 1)
        self.assertGreaterEqual(careers["match_count"], 3)
        self.assertIn("open roles", careers["matched_terms"])
        self.assertIn("endpoint management", careers["matched_terms"])
        self.assertIn("endpoint administrator", careers["snippet"])

    def test_write_page_outputs_writes_csv_and_jsonl(self):
        rows = [
            {
                "domain": "example.com",
                "page_type": "careers",
                "path": "/careers",
                "url": "https://example.com/careers",
                "status": 200,
                "ok": 1,
                "method": "httpx",
                "match_count": 2,
                "matched_terms": ["open roles", "mdm"],
                "snippet": "Open roles include MDM admins.",
                "linked_from_homepage": 0,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "pages.csv"
            jsonl_path = Path(run.write_page_outputs(rows, str(csv_path)))

            with csv_path.open(newline="") as f:
                csv_rows = list(csv.DictReader(f))
            json_rows = [json.loads(line) for line in jsonl_path.read_text().splitlines()]

        self.assertEqual(csv_rows[0]["domain"], "example.com")
        self.assertEqual(json.loads(csv_rows[0]["matched_terms"]), ["open roles", "mdm"])
        self.assertEqual(json_rows[0]["snippet"], "Open roles include MDM admins.")

    def test_write_page_outputs_strips_nul_bytes_from_csv(self):
        rows = [
            {
                "domain": "example.com",
                "page_type": "news",
                "path": "/news",
                "url": "https://example.com/news",
                "status": 200,
                "ok": 1,
                "method": "httpx",
                "match_count": 1,
                "matched_terms": ["funding"],
                "snippet": "Funding\x00JFIF\x01payload",
                "linked_from_homepage": 0,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "pages.csv"
            jsonl_path = Path(run.write_page_outputs(rows, str(csv_path)))
            csv_text = csv_path.read_text()
            json_text = jsonl_path.read_text()

        self.assertNotIn("\x00", csv_text)
        self.assertNotIn("\x01", csv_text)
        self.assertIn("Funding", csv_text)
        self.assertIn("\\u0000", json_text)


if __name__ == "__main__":
    unittest.main()
