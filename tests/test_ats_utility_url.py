"""Tests for cascade._bad_ats_utility_url: new empty_body patterns.

Added in the empty_body intake task (t_caf21635):
  - Paylocity GetLogoFile / GetLogoFileById (logo fetches, not boards)
  - UltiPro AuthCode/PostLogin auth-redirect loops
  - TeamTailor /locations/map_details widgets
  - OracleCloud sitemaps, images, and afr/blank stubs
  - Workable /oops and /login error pages
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import cascade


class BadAtsUtilityUrlNewPatternsTests(unittest.TestCase):
    """Patterns added for the empty_body backlog (t_caf21635)."""

    # -- should be flagged as bad (utility/error, not a board) ---------------

    def test_paylocity_getlogofilebyid(self):
        self.assertTrue(cascade._bad_ats_utility_url(
            "https://recruiting.paylocity.com/Recruiting/Jobs/GetLogoFileById?logoFileStoreId=45880327&moduleId=24130"
        ))

    def test_paylocity_getlogofile_lowercase(self):
        self.assertTrue(cascade._bad_ats_utility_url(
            "https://recruiting.paylocity.com/recruiting/jobs/GetLogoFile?moduleId=38192"
        ))

    def test_ultipro_postlogin(self):
        self.assertTrue(cascade._bad_ats_utility_url(
            "https://recruiting.ultipro.com/AuthCode/PostLogin?error_description=The+request+requires+some+interaction&error=interaction_required"
        ))

    def test_ultipro_postlogin_numbered_subdomain(self):
        self.assertTrue(cascade._bad_ats_utility_url(
            "https://recruiting2.ultipro.com/AuthCode/PostLogin?state=something"
        ))

    def test_teamtailor_map_details(self):
        self.assertTrue(cascade._bad_ats_utility_url(
            "https://teambluefinland.teamtailor.com/locations/map_details?editor=false&location_id=1133846"
        ))

    def test_teamtailor_map_details_no_query(self):
        self.assertTrue(cascade._bad_ats_utility_url(
            "https://acme.teamtailor.com/locations/map_details"
        ))

    def test_oraclecloud_sitemap(self):
        self.assertTrue(cascade._bad_ats_utility_url(
            "https://fa-etvl-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/sitemaps/sitemapIndex"
        ))

    def test_oraclecloud_images(self):
        self.assertTrue(cascade._bad_ats_utility_url(
            "https://iawfqy.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/images?imageId=ABC"
        ))

    def test_oraclecloud_afr_blank(self):
        self.assertTrue(cascade._bad_ats_utility_url(
            "https://eiqg.fa.us2.oraclecloud.com/hcmUI/afr/blank.html"
        ))

    def test_workable_oops(self):
        self.assertTrue(cascade._bad_ats_utility_url(
            "https://apply.workable.com/oops"
        ))

    def test_workable_login(self):
        self.assertTrue(cascade._bad_ats_utility_url(
            "https://jobs.workable.com/login"
        ))

    # -- should NOT be flagged (real boards) ----------------------------------

    def test_paylocity_real_board_kept(self):
        self.assertFalse(cascade._bad_ats_utility_url(
            "https://recruiting.paylocity.com/Recruiting/Jobs/All/3034"
        ))

    def test_teamtailor_real_board_kept(self):
        self.assertFalse(cascade._bad_ats_utility_url(
            "https://acme.teamtailor.com/jobs/12345-senior-engineer"
        ))

    def test_teamtailor_root_board_kept(self):
        self.assertFalse(cascade._bad_ats_utility_url(
            "https://acme.teamtailor.com/"
        ))

    def test_oraclecloud_real_board_kept(self):
        self.assertFalse(cascade._bad_ats_utility_url(
            "https://eeho.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1"
        ))

    def test_workable_real_board_kept(self):
        self.assertFalse(cascade._bad_ats_utility_url(
            "https://apply.workable.com/acmecorp/"
        ))

    def test_workable_job_listing_kept(self):
        self.assertFalse(cascade._bad_ats_utility_url(
            "https://apply.workable.com/acmecorp/j/ABC123/"
        ))

    def test_ultipro_real_board_kept(self):
        self.assertFalse(cascade._bad_ats_utility_url(
            "https://recruiting.ultipro.com/ACME0001/JobBoard/abc123"
        ))


if __name__ == "__main__":
    unittest.main()
