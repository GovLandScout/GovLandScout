"""
Tests for collin_scraper.py's pure parsing helpers, using real text drawn
from collincountytx.gov's constable sale notices (see the docstring in
collin_scraper.py for how this site's data is shaped).
"""

import unittest

from collin_scraper import GEO_PATTERN, extract_address, is_cancelled


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
