"""
Tests for chester_scraper.py -- fixtures trimmed from the real 2026
chesco.org page and Advertising List spreadsheet captured during
development. Unlike montco_scraper.py's PDF, this one's a spreadsheet
(openpyxl), so the fixtures build small in-memory workbooks rather than
mocking pdfplumber.
"""

import unittest
from io import BytesIO

import openpyxl

from chester_scraper import find_advertising_list_url, parse_money, parse_workbook

# Trimmed from the real Upset Tax Sale Information page.
UPSET_SALE_PAGE_HTML = """
<p><a href="/DocumentCenter/View/85423" target="_blank" rel="noopener">2026 Upset Sale Registration Form &amp; Conditions of Sale</a></p>
<p><a href="/DocumentCenter/View/85421" target="_blank" rel="noopener">2026 Advertising List</a></p>
"""


def build_xlsx_bytes(rows: list[tuple]) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    buf = BytesIO()
    workbook.save(buf)
    return buf.getvalue()


class FindAdvertisingListUrlTests(unittest.TestCase):
    def test_finds_the_advertising_list_not_the_registration_form(self):
        self.assertEqual(find_advertising_list_url(UPSET_SALE_PAGE_HTML), "/DocumentCenter/View/85421")

    def test_no_matching_link_returns_none(self):
        self.assertIsNone(find_advertising_list_url("<html><body>nothing here</body></html>"))


class ParseMoneyTests(unittest.TestCase):
    def test_numeric_cell_value(self):
        self.assertEqual(parse_money(5753.97), "5753.97")

    def test_zero_is_treated_as_missing(self):
        self.assertIsNone(parse_money(0))

    def test_string_with_dollar_and_commas(self):
        self.assertEqual(parse_money("$21,117.98"), "21117.98")

    def test_none_input_is_none(self):
        self.assertIsNone(parse_money(None))


class ParseWorkbookTests(unittest.TestCase):
    HEADER = ("Customer", "ALTID", "Name", "Name 2", "LEGAL1", "LEGAL2", "APPROXIMATE UPSET SALE PRICE")

    def test_real_row_shape_parses_correctly(self):
        rows = [
            self.HEADER,
            ("0102_00630000", "1-2-63", "DEANGELO JAMES CHRISTOPHER", None, "WS OF HILLSIDE DR", "LOT 56 & DWG", 5753.97),
        ]
        listings = parse_workbook(build_xlsx_bytes(rows))
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0]["account_number"], "1-2-63")
        self.assertEqual(listings[0]["minimum_bid"], "5753.97")
        self.assertEqual(listings[0]["description"], "WS OF HILLSIDE DR -- LOT 56 & DWG")
        self.assertEqual(listings[0]["county"], "Chester")

    def test_header_row_is_skipped(self):
        rows = [self.HEADER]
        self.assertEqual(parse_workbook(build_xlsx_bytes(rows)), [])

    def test_row_with_no_altid_is_skipped(self):
        rows = [
            self.HEADER,
            ("0102_00630000", None, "SOME OWNER", None, "WS OF HILLSIDE DR", "LOT 56 & DWG", 5753.97),
        ]
        self.assertEqual(parse_workbook(build_xlsx_bytes(rows)), [])

    def test_missing_legal2_still_builds_a_description(self):
        rows = [
            self.HEADER,
            ("0106_00480000", "1-6-48", "MERCEDES JUANA", None, "ES S WORTHINGTON ST", None, 4920.09),
        ]
        listings = parse_workbook(build_xlsx_bytes(rows))
        self.assertEqual(listings[0]["description"], "ES S WORTHINGTON ST")


if __name__ == "__main__":
    unittest.main()
