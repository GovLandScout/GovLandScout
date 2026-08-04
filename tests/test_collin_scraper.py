"""
Tests for collin_scraper.py's pure parsing helpers, using real text drawn
from collincountytx.gov's constable sale notices (see the docstring in
collin_scraper.py for how this site's data is shaped).
"""

import unittest

from collin_scraper import GEO_PATTERN, extract_address, is_cancelled, parse_listing_page

# Trimmed from a real response: one normal upcoming row, one cancelled row,
# and one row dated in the past -- parse_listing_page must keep all three
# (with is_cancelled set correctly) so collin_archive_scraper.py's
# historical archive doesn't silently lose cancelled/past sales the way
# collin_scraper.py's own public-facing filtering deliberately does.
ACCORDION_HTML = """
<div class="advListTableWrap accordion-section">
  <div class="accordion-header">Precinct 2</div>
  <div class="accordion-content">
    <div class="advListDataRow">
      <div class="advListDataCell" data-col="Title">Ancelmo Ordonez</div>
      <div class="advListDataCell" data-col="Description">TRACT 1: GEO: R276200002001 BEING ALL...</div>
      <div class="advListDataCell" data-col="Date">7/1/2025</div>
      <div class="advListDataCell" data-col="Document">
        <a href="/docs/ordonez.pdf">Ordonez Notice of Constable Sale</a>
      </div>
    </div>
    <div class="advListDataRow">
      <div class="advListDataCell" data-col="Title">Bradley Haynes - ***Cancelled***</div>
      <div class="advListDataCell" data-col="Description">Lot 8, Block 15...</div>
      <div class="advListDataCell" data-col="Date">9/2/2025</div>
      <div class="advListDataCell" data-col="Document"></div>
    </div>
  </div>
</div>
"""


class ParseListingPageTests(unittest.TestCase):
    def test_normal_row_is_not_cancelled(self):
        rows = parse_listing_page(ACCORDION_HTML)
        ordonez = next(r for r in rows if "Ordonez" in r["account_number"] or r["account_number"] == "R276200002001")
        self.assertFalse(ordonez["is_cancelled"])

    def test_cancelled_row_is_kept_not_dropped(self):
        # This is the whole point of the archive scraper reusing this
        # function -- a cancelled row must survive, just flagged, not be
        # filtered out the way collin_scraper.py's own main() filters it.
        rows = parse_listing_page(ACCORDION_HTML)
        self.assertEqual(len(rows), 2)
        cancelled = [r for r in rows if r["is_cancelled"]]
        self.assertEqual(len(cancelled), 1)
        self.assertIn("Haynes", cancelled[0]["account_number"])

    def test_precinct_captured_per_row(self):
        rows = parse_listing_page(ACCORDION_HTML)
        self.assertTrue(all(r["precinct"] == "Precinct 2" for r in rows))


class IsCancelledTests(unittest.TestCase):
    def test_marker_in_defendant_name(self):
        self.assertTrue(is_cancelled("Bradley Haynes - ***Cancelled***", "Lot 8, Block 15..."))

    def test_marker_in_legal_description(self):
        self.assertTrue(is_cancelled("Fenia Fen Chang ***CANCELLED***", "***CANCELLED***"))

    def test_normal_listing_is_not_cancelled(self):
        self.assertFalse(is_cancelled("Ancelmo Ordonez", "TRACT 1: GEO: R276200002001 BEING ALL..."))


class ExtractAddressTests(unittest.TestCase):
    def test_commonly_known_as_phrasing(self):
        legal = (
            "of Preston Lakes, Phase Three, an Addition to the City of Plano, "
            "Collin County, Texas, according to the Plat thereof recorded in "
            "Volume Q, Page 256, more commonly known as 3205 Broken Bow way, Plano, Texas 75093."
        )
        self.assertEqual(extract_address(legal), "3205 Broken Bow way, Plano, Texas 75093")

    def test_located_at_with_curly_quotes(self):
        legal = (
            "AS RECORDED IN INSTRUMENT NO. 19910703000360100 OF THE COLLIN COUNTY "
            "DEED RECORDS and located at ‘1809 MACGREGOR DR, PLANO 75093’ "
            "per the Collin County Appraisal District."
        )
        self.assertEqual(extract_address(legal), "1809 MACGREGOR DR, PLANO 75093")

    def test_no_address_phrasing_returns_none(self):
        legal = "TRACT 1: GEO: R276200002001 BEING ALL THAT CERTAIN 2.01 ACRES..."
        self.assertIsNone(extract_address(legal))


class GeoPatternTests(unittest.TestCase):
    def test_standard_geo_labeled_id(self):
        legal = "TRACT 1:  GEO:  R276200002001\nBEING ALL THAT CERTAIN 2.01 ACRES..."
        self.assertEqual(GEO_PATTERN.findall(legal), ["R276200002001"])

    def test_unlabeled_id_with_embedded_letter(self):
        # No "GEO:" label at all, and the ID itself isn't pure digits (common-area tract).
        legal = "Tract I: R224000B006R1\nPLANO 75093, BEING LOT 6R (COMMON AREA)..."
        self.assertEqual(GEO_PATTERN.findall(legal), ["R224000B006R1"])

    def test_multiple_tracts_all_captured(self):
        legal = "TRACT 2:\xa0 GEO:\xa0 R039101001601 LOT 16... TRACT 3: GEO: R039101001602"
        self.assertEqual(GEO_PATTERN.findall(legal), ["R039101001601", "R039101001602"])

    def test_no_geo_id_present(self):
        legal = "LOT 5, BLOCK H, OF BETHANY CREEK ESTATES PHASE B, AN ADDITION TO THE CITY OF ALLEN"
        self.assertEqual(GEO_PATTERN.findall(legal), [])


if __name__ == "__main__":
    unittest.main()
