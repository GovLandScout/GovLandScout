"""
Tests for montco_scraper.py -- fixtures trimmed from real
montgomerycountypa.gov responses captured during development (see that
module's docstring for the two-hop discovery: the Upset Sale page links to
an "Archival Document" viewer page, which in turn embeds the real PDF
asset URL).
"""

import unittest
from unittest.mock import patch

from montco_scraper import (
    collapse_whitespace,
    find_archival_document_url,
    find_pdf_asset_url,
    parse_money,
    parse_pdf,
)

# Trimmed from the real Upset Sale page -- other unrelated links
# (Registration forms) included to confirm the aria-label match doesn't
# just grab the first /archival-document link it sees.
UPSET_SALE_PAGE_HTML = """
<h4><a class="button" tabindex="0" target="_blank" aria-label="2026 Individual Bidder Registration" href="/archival-document?id=19400"><i></i>2026 Individual Bidder Registration</a></h4>
<h4><a class="button" tabindex="0" target="_blank" aria-label="2026 Sale List" href="/archival-document?id=19406"><i></i>2026 Sale List</a></h4>
<h6><a class="button" tabindex="0" target="_blank" aria-label="2026 Business Bidder Registration" href="/archival-document?id=19401"><i></i>2026 Business Bidder Registration</a></h6>
"""

# Trimmed from the real archival-document viewer page's embedded JS state.
ARCHIVAL_VIEWER_HTML = """
<script>{"title":"2026 Upset Sale List 8.07.26.pdf","url":"https://assets.montgomerycountypa.gov/files/2026-08/2026%20Upset%20Sale%20List%208.07.26.pdf"}</script>
"""


class FindArchivalDocumentUrlTests(unittest.TestCase):
    def test_finds_the_sale_list_link_not_registration_links(self):
        self.assertEqual(find_archival_document_url(UPSET_SALE_PAGE_HTML), "/archival-document?id=19406")

    def test_no_matching_link_returns_none(self):
        self.assertIsNone(find_archival_document_url("<html><body>nothing here</body></html>"))


class FindPdfAssetUrlTests(unittest.TestCase):
    def test_extracts_the_real_pdf_url(self):
        self.assertEqual(
            find_pdf_asset_url(ARCHIVAL_VIEWER_HTML),
            "https://assets.montgomerycountypa.gov/files/2026-08/2026%20Upset%20Sale%20List%208.07.26.pdf",
        )

    def test_no_asset_url_returns_none(self):
        self.assertIsNone(find_pdf_asset_url("<html><body>nothing here</body></html>"))


class ParseMoneyTests(unittest.TestCase):
    def test_strips_dollar_and_commas(self):
        self.assertEqual(parse_money("$21,853.24"), "21853.24")

    def test_none_input_is_none(self):
        self.assertIsNone(parse_money(None))


class CollapseWhitespaceTests(unittest.TestCase):
    def test_joins_wrapped_lines(self):
        self.assertEqual(collapse_whitespace("HILLTOP GENERAL INDUSTRIES\nINCORPORATED"), "HILLTOP GENERAL INDUSTRIES INCORPORATED")

    def test_none_input_is_none(self):
        self.assertIsNone(collapse_whitespace(None))

    def test_empty_string_is_none(self):
        self.assertIsNone(collapse_whitespace(""))


class FakePage:
    def __init__(self, tables):
        self._tables = tables

    def extract_tables(self):
        return self._tables


class FakePdf:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ParsePdfTests(unittest.TestCase):
    """
    Real 2026 data confirmed every row is exactly 6 columns and every
    Parcel is unique across all 30 pages (712 rows, 712 distinct parcels)
    -- these fixtures mirror that shape, plus the one real wrinkle: the
    header row only appears once, as page 1's first row, with every later
    page's table starting directly with data (no repeated header).
    """

    @patch("montco_scraper.pdfplumber.open")
    def test_page_one_header_is_skipped(self, mock_open):
        table = [
            ["Municipality", "Sale Number", "Parcel", "BOA Owner Name", "BOA: Location", "Approx. Sale Price"],
            ["Ambler", "U26-0002", "01-00-01606-02-2", "CARRIAGE HOUSE EAST LLC", "224 -228 FOREST AVE", "$2,708.65"],
        ]
        mock_open.return_value = FakePdf([FakePage([table])])
        listings = parse_pdf(b"fake pdf bytes", "https://assets.montgomerycountypa.gov/files/list.pdf")
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0]["account_number"], "01-00-01606-02-2")
        self.assertEqual(listings[0]["minimum_bid"], "2708.65")
        self.assertEqual(listings[0]["address"], "224 -228 FOREST AVE, Ambler, PA")
        self.assertEqual(listings[0]["precinct"], "Ambler")
        self.assertIn("U26-0002", listings[0]["description"])

    @patch("montco_scraper.pdfplumber.open")
    def test_later_page_with_no_repeated_header_still_parses(self, mock_open):
        # Real Montgomery behavior: page 2 onward starts directly with a
        # data row, never repeating the "Municipality" header row.
        page1_table = [
            ["Municipality", "Sale Number", "Parcel", "BOA Owner Name", "BOA: Location", "Approx. Sale Price"],
            ["Ambler", "U26-0002", "01-00-01606-02-2", "CARRIAGE HOUSE EAST LLC", "224 -228 FOREST AVE", "$2,708.65"],
        ]
        page2_table = [
            ["Jenkintown", "U26-0048", "10-00-04693-80-5", "PHAM-TO JEANNIE T", "309 FLORENCE AVE", "$7,766.51"],
        ]
        mock_open.return_value = FakePdf([FakePage([page1_table]), FakePage([page2_table])])
        listings = parse_pdf(b"fake pdf bytes", "https://assets.montgomerycountypa.gov/files/list.pdf")
        self.assertEqual(len(listings), 2)
        self.assertEqual(listings[1]["account_number"], "10-00-04693-80-5")
        self.assertEqual(listings[1]["precinct"], "Jenkintown")

    @patch("montco_scraper.pdfplumber.open")
    def test_wrapped_multiline_cells_collapse_to_one_line(self, mock_open):
        table = [
            ["Municipality", "Sale Number", "Parcel", "BOA Owner Name", "BOA: Location", "Approx. Sale Price"],
            ["Hatfield\nTownship", "U26-0644", "35-00-00427-00-6", "HILLTOP GENERAL INDUSTRIES\nINCORPORATED",
             "2544 BETHLEHEM PIKE", "$26,802.52"],
        ]
        mock_open.return_value = FakePdf([FakePage([table])])
        listings = parse_pdf(b"fake pdf bytes", "https://assets.montgomerycountypa.gov/files/list.pdf")
        self.assertEqual(listings[0]["precinct"], "Hatfield Township")
        self.assertEqual(listings[0]["address"], "2544 BETHLEHEM PIKE, Hatfield Township, PA")

    @patch("montco_scraper.pdfplumber.open")
    def test_row_with_wrong_column_count_is_skipped(self, mock_open):
        # A stray box or footer row pdfplumber might misread as a table.
        table = [
            ["Municipality", "Sale Number", "Parcel", "BOA Owner Name", "BOA: Location", "Approx. Sale Price"],
            ["Some footer note spanning fewer columns"],
        ]
        mock_open.return_value = FakePdf([FakePage([table])])
        listings = parse_pdf(b"fake pdf bytes", "https://assets.montgomerycountypa.gov/files/list.pdf")
        self.assertEqual(listings, [])

    @patch("montco_scraper.pdfplumber.open")
    def test_row_with_no_parcel_is_skipped(self, mock_open):
        table = [
            ["Municipality", "Sale Number", "Parcel", "BOA Owner Name", "BOA: Location", "Approx. Sale Price"],
            ["Ambler", "U26-0002", "", "CARRIAGE HOUSE EAST LLC", "224 -228 FOREST AVE", "$2,708.65"],
        ]
        mock_open.return_value = FakePdf([FakePage([table])])
        listings = parse_pdf(b"fake pdf bytes", "https://assets.montgomerycountypa.gov/files/list.pdf")
        self.assertEqual(listings, [])


if __name__ == "__main__":
    unittest.main()
