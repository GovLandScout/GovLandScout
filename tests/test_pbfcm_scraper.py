"""
Tests for pbfcm_scraper.py -- the most format-fragile scraper in this
project (8 different table layouts across counties, detected from each
document's own header rather than assumed). Header/row fixtures below are
trimmed from real PDFs fetched during the "Recovering missing data"
investigation, not invented, since the whole point of this scraper is
correctly handling PBFCM's actual real-world inconsistency.
"""

import unittest
from unittest.mock import patch

import pbfcm_scraper as pbfcm


class ParseMoneyTests(unittest.TestCase):
    def test_strips_dollar_and_commas(self):
        self.assertEqual(pbfcm.parse_money("$1,234.56"), "1234.56")

    def test_non_numeric_placeholder_is_none(self):
        self.assertIsNone(pbfcm.parse_money("TBD"))

    def test_none_input_is_none(self):
        self.assertIsNone(pbfcm.parse_money(None))

    def test_empty_string_is_none(self):
        self.assertIsNone(pbfcm.parse_money(""))

    def test_trailing_text_on_the_same_cell_does_not_block_the_amount(self):
        # Real Brazoria/Hays cells: the dollar figure is followed by more
        # text on later lines of the same cell -- this used to make the
        # whole cell fail a full-string match and silently store no bid at
        # all, even though the actual number was right there.
        self.assertEqual(pbfcm.parse_money("$17,684.51\n2025 Taxes\nDue"), "17684.51")
        self.assertEqual(pbfcm.parse_money("$34,588.55\nSubject to\n2026 taxes"), "34588.55")


class ExtractCountyTests(unittest.TestCase):
    def test_plain_county_sales_for(self):
        self.assertEqual(
            pbfcm.extract_county("HARRIS COUNTY SALES FOR AUGUST 5, 2025"),
            ("Harris", None),
        )

    def test_county_with_precinct(self):
        self.assertEqual(
            pbfcm.extract_county("FORT BEND COUNTY PCT 1 SALES FOR AUGUST 5, 2025"),
            ("Fort Bend", "PCT 1"),
        )

    def test_extra_words_between_county_and_sales(self):
        self.assertEqual(
            pbfcm.extract_county("SMITH COUNTY TAX SALE FOR AUGUST 5, 2025")[0],
            "Smith",
        )

    def test_banner_with_no_county_named_returns_none(self):
        # Real Austin/Fort Bend page-1 text -- the county only appears in
        # the filename, not this banner. extract_county_from_filename is
        # the fallback for exactly this case.
        self.assertEqual(
            pbfcm.extract_county("SALES FOR AUGUST 4, 2026\nLOCATION: Official Door of the Courthouse"),
            (None, None),
        )


class ExtractCountyFromFilenameTests(unittest.TestCase):
    def test_plain_county_filename(self):
        self.assertEqual(
            pbfcm.extract_county_from_filename(".../08-2026austincountytaxsale.pdf"),
            ("Austin", None),
        )

    def test_abbreviated_county_needs_override(self):
        self.assertEqual(
            pbfcm.extract_county_from_filename(".../08-2026ftbendpct1taxsale.pdf"),
            ("Fort Bend", "PCT 1"),
        )

    def test_precinct_number_extracted(self):
        self.assertEqual(
            pbfcm.extract_county_from_filename(".../08-2026ftbendpct4taxsale.pdf")[1],
            "PCT 4",
        )

    def test_isd_filename_is_not_a_county(self):
        self.assertEqual(
            pbfcm.extract_county_from_filename(".../08-2026whitneyisdtaxsale.pdf"),
            (None, None),
        )

    def test_non_taxsale_filename_has_nothing_to_extract(self):
        self.assertEqual(
            pbfcm.extract_county_from_filename(".../JackSaleRulesNew.pdf"),
            (None, None),
        )


class ParseLegalAddressCellTests(unittest.TestCase):
    def test_trailing_lines_form_a_valid_address(self):
        cell = "LOT 3-3B, BLOCK H, PEARLAND\n1029 JACKSON RD,\nBELLVILLE, TX 77418"
        legal, addr = pbfcm.parse_legal_address_cell(cell)
        self.assertEqual(addr, "1029 JACKSON RD, BELLVILLE, TX 77418")
        self.assertEqual(legal, "LOT 3-3B, BLOCK H, PEARLAND")

    def test_no_address_shaped_lines_returns_none_address(self):
        cell = "LOT 3-3B, BLOCK H, PEARLAND, IN BRAZORIA COUNTY, TEXAS."
        legal, addr = pbfcm.parse_legal_address_cell(cell)
        self.assertIsNone(addr)
        self.assertEqual(legal, cell)


class DetectFormatTests(unittest.TestCase):
    def test_7col_harris(self):
        header = ["Idx", "Cause No/Court/Date", "Style", "Legal/Address", "Adjudged Value", "Minimum Bid", "Account #"]
        self.assertEqual(pbfcm.detect_format(header)["name"], "7col-harris")

    def test_6col_cameron(self):
        header = ["Cause No:", "Style of Case:", "Legal Description:", "Adjudged\nValue:", "Estimated\nMinimum:", "Cad Account #"]
        self.assertEqual(pbfcm.detect_format(header)["name"], "6col-cameron")

    def test_5col_taxpayer(self):
        header = ["Cause No", "Legal Description", "Minimum Bid", "Account", "Taxpayer Name"]
        self.assertEqual(pbfcm.detect_format(header)["name"], "5col-taxpayer")

    def test_4col_embedded_account(self):
        header = ["Case No", "Legal Description", "Adjudged Value", "Minimum Bid"]
        self.assertEqual(pbfcm.detect_format(header)["name"], "4col-embedded-account")

    def test_case_legal_bid(self):
        header = ["Case No.", "Legal Description/Address (if available)", "Estimated\nMinimum Bid", "GEO CODE"]
        self.assertEqual(pbfcm.detect_format(header)["name"], "case-legal-bid")

    def test_item_suit_legal_bid(self):
        header = ["Item\n#", "Tax Suit No.", "Legal Description / Address (if available)", "Estimated\nMinimum Bid"]
        self.assertEqual(pbfcm.detect_format(header)["name"], "item-suit-legal-bid")

    def test_brazoria(self):
        header = ["Item\nNo.", "Cause No.", None, None, "Legal Description", None, None, "Geographic\nID", "Minimum\nBid"]
        self.assertEqual(pbfcm.detect_format(header)["name"], "brazoria")

    def test_unrecognized_header_returns_none(self):
        header = ["Accepted payment methods are CASH, MONEY ORDER OR CASHIER'S CHECK"]
        self.assertIsNone(pbfcm.detect_format(header))

    def test_banner_row_is_not_mistaken_for_a_header(self):
        # Fort Bend's real row 0 -- a section title, not a header.
        header = ["Fort Bend Constable Precinct 1", None, None, None, None, None]
        self.assertIsNone(pbfcm.detect_format(header))


class BindParserTests(unittest.TestCase):
    def test_geo_column_binds_geo_kind(self):
        fmt = {"name": "case-legal-bid", "parser": pbfcm.parse_row_case_legal_bid}
        parser = pbfcm.bind_parser(fmt, ["Case No.", "Legal", "Minimum Bid", "GEO CODE"])
        row = ["2024-1", "Legal text", "$1,234.56", "R123456"]
        self.assertEqual(parser(row)["account_number"], "R123456")

    def test_value_column_binds_value_kind(self):
        fmt = {"name": "case-legal-bid", "parser": pbfcm.parse_row_case_legal_bid}
        parser = pbfcm.bind_parser(fmt, ["Case No.", "Legal", "Minimum Bid", "Appraised Value"])
        row = ["2024-1", "Legal text", "$1,234.56", "$50,000.00"]
        self.assertEqual(parser(row)["adjudged_value"], "50000.00")

    def test_notes_column_is_neither_kind(self):
        fmt = {"name": "case-legal-bid", "parser": pbfcm.parse_row_case_legal_bid}
        parser = pbfcm.bind_parser(fmt, ["Case No.", "Legal", "Minimum Bid", "Notes"])
        row = ["2024-1", "Legal text", "$1,234.56", "some note"]
        parsed = parser(row)
        self.assertIsNone(parsed["adjudged_value"])
        self.assertEqual(parsed["account_number"], "2024-1")  # falls back to cause_no

    def test_other_formats_pass_through_unchanged(self):
        fmt = {"name": "6col-cameron", "parser": pbfcm.parse_row_6col}
        self.assertIs(pbfcm.bind_parser(fmt, ["irrelevant"]), pbfcm.parse_row_6col)


class ParseRowCaseLegalBidTests(unittest.TestCase):
    def test_no_fourth_column_falls_back_to_cause_no_as_account(self):
        row = ["2024-1", "LOT 5, BLOCK H", "$1,234.56"]
        parsed = pbfcm.parse_row_case_legal_bid(row, value_kind=None)
        self.assertEqual(parsed["account_number"], "2024-1")
        self.assertEqual(parsed["minimum_bid"], "1234.56")

    def test_header_row_itself_is_rejected(self):
        row = ["Case No.", "Legal Description/Address (if available)", "Estimated Minimum Bid"]
        self.assertIsNone(pbfcm.parse_row_case_legal_bid(row, value_kind=None))

    def test_dead_link_placeholder_row_is_rejected(self):
        # Real Randall County row: no real data, just a link to their own site.
        row = ["", "http://randallcounty.com/293/Sheriff-Sale", "", "", ""]
        self.assertIsNone(pbfcm.parse_row_case_legal_bid(row, value_kind=None))


class ParseRowBrazoriaTests(unittest.TestCase):
    def test_real_row_shape(self):
        row = [
            "1",
            "122839-T\nPEARLAND\nINDEPENDENT\nSCHOOL DISTRICT,\nET AL VS.\nGARZA, JR.,\nPRAJEDIS JOSE, ET\nAL",
            None, None,
            "LOT 3-3B, BLOCK H, PEARLAND...\nAdjudged Value: $230,620.00",
            None, None,
            "7025-0592-000",
            "$17,684.51\n2025 Taxes\nDue",
        ]
        parsed = pbfcm.parse_row_brazoria(row)
        self.assertEqual(parsed["cause_no"], "122839-T")
        self.assertEqual(parsed["account_number"], "7025-0592-000")
        self.assertEqual(parsed["adjudged_value"], "230620.00")
        self.assertEqual(parsed["minimum_bid"], "17684.51")


class ParseRowItemSuitLegalBidTests(unittest.TestCase):
    def test_embedded_value_and_account_extracted(self):
        row = [
            "2",
            "TAX SUIT NO. 26-0129-\nDCA\nWIMBERLEY ISD\nVS.\nWOODCREEK RESORT,\nINC., ET AL.",
            "TRACT 2: Being 0.268 of an acre...(Tax Account No.\nR53102)\n"
            "Adjudged Value (at time of judgment): $74,980.00",
            "$34,588.55\nSubject to\n2026 taxes",
        ]
        parsed = pbfcm.parse_row_item_suit_legal_bid(row)
        self.assertEqual(parsed["cause_no"], "TAX SUIT NO. 26-0129-")
        self.assertEqual(parsed["account_number"], "R53102")
        self.assertEqual(parsed["adjudged_value"], "74980.00")
        self.assertEqual(parsed["minimum_bid"], "34588.55")


class FakePage:
    def __init__(self, text, tables):
        self._text = text
        self._tables = tables

    def extract_text(self):
        return self._text

    def extract_tables(self):
        return self._tables


class FakePdf:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ParsePdfRegressionTests(unittest.TestCase):
    """
    Regression coverage for the real crash this scraper hit on Fort Bend's
    documents: a section-title banner row ("Fort Bend Constable Precinct
    1") sits above the real header, and that banner happens to contain a
    stray digit (from "Precinct 1") that let it slip past parse_row_6col's
    own guard clause and crash on the next, blank, cell. parse_pdf() now
    tracks exactly which row the header was found on and skips everything
    up through it, rather than trusting each parser to reject rows it was
    never meant to see.
    """

    @patch("pbfcm_scraper.pdfplumber.open")
    def test_banner_row_above_header_does_not_crash_and_is_skipped(self, mock_open):
        table = [
            ["Fort Bend Constable Precinct 1", None, None, None, None, None],
            ["Cause No:\nDistrict Court:\nJudgment Date:", "Style of Case:",
             "Legal Description:\nProperty Address (Per Appraisal District):",
             "Adjudged\nValue:", "Estimated\nMinimum:", "Cad Account #\nOther Account"],
            ["2023V-0045\n155th District\nCourt\n14-Apr-26", "SOME PLAINTIFF vs. SOME DEFENDANT",
             "PERSONAL PROPERTY...\n1029 JACKSON RD\nBELLVILLE, TX 77418",
             "$44,061.00", "$4,570.78", "69408001"],
        ]
        mock_open.return_value = FakePdf([
            FakePage("SALES FOR AUGUST 4, 2026\nLOCATION: Gus George LEA", [table]),
        ])

        listings, skip_reason = pbfcm.parse_pdf(b"fake pdf bytes", ".../08-2026ftbendpct1taxsale.pdf")

        self.assertIsNone(skip_reason)
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0]["county"], "Fort Bend")
        self.assertEqual(listings[0]["precinct"], "PCT 1")
        self.assertEqual(listings[0]["account_number"], "69408001")


if __name__ == "__main__":
    unittest.main()
