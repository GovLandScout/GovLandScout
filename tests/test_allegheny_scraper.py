"""
Tests for allegheny_scraper.py -- fixtures trimmed from real word-position
data captured from the September 2026 Sheriff Sale PDF (see that module's
docstring for why Property/Municipality/Parcel are parsed positionally by
x0 rather than via plain text extraction).
"""

import unittest

from allegheny_scraper import (
    find_column_boundaries,
    find_sale_list_url,
    group_into_rows,
    parse_page,
    row_text,
)

# Trimmed from a real sheriffalleghenycounty.com/sheriffs-sales/ page --
# other, non-matching PDF links (a bidder packet, an old court order)
# included to confirm the pattern doesn't just grab the first PDF href.
SALES_PAGE_HTML = """
<a href="https://sheriffalleghenycounty.com/wp-content/uploads/2025/04/Sheriff-Sale-Packet.pdf">Sheriff Sale Packet</a>
<a href="https://sheriffalleghenycounty.com/wp-content/uploads/2022/10/AD20-140-VIRTUAL-SALE-CO.pdf">Court Order</a>
<a href="https://sheriffalleghenycounty.com/wp-content/uploads/2026/08/September-Sale-List.pdf">Sale Listings</a>
"""


def word(top, x0, text):
    return {"top": top, "x0": x0, "text": text}


# One full real record (a confirmed Mortgage Foreclosure case, page 10 of
# the real September 2026 list) followed by the start of the next record's
# value line -- confirms parse_page stops collecting Property/Municipality/
# Parcel at the right place rather than reading into the next record.
MORTGAGE_FORECLOSURE_RECORD_WORDS = [
    # value line
    word(48.3, 26.0, "16JUN25"), word(48.3, 82.5, "GD-24-009484"),
    word(48.3, 163.0, "Real"), word(48.3, 182.1, "Estate"), word(48.3, 208.4, "Sale"),
    word(48.3, 227.0, "-"), word(48.3, 231.9, "Mortgage"), word(48.3, 270.1, "Foreclosure"),
    word(48.1, 389.8, "Active"), word(48.1, 498.0, "1"), word(48.1, 580.5, "$3,715.71"),
    # field mini-header
    word(67.1, 17.4, "Plaintiff(s):"),
    word(67.1, 167.4, "Attorney"), word(67.1, 211.3, "for"), word(67.1, 227.4, "the"), word(67.1, 245.2, "Plaintiff:"),
    word(67.1, 308.4, "Defendant(s):"),
    word(67.1, 455.4, "Property"),
    word(67.1, 587.4, "Municipality"),
    word(67.1, 671.4, "Parcel/Tax"), word(67.1, 723.9, "ID:"),
    # data line
    word(82.2, 17.4, "STOCKTON"), word(82.2, 63.8, "MORTGAGE"),
    word(81.1, 167.4, "ROBERTSON"), word(81.1, 219.7, "ANSCHUTZ"), word(81.1, 265.5, "SCHNEID"),
    word(81.1, 308.4, "Williams,"), word(81.1, 342.6, "Lauren"), word(81.1, 369.7, "C."),
    word(82.2, 455.4, "1317"), word(82.2, 475.4, "NEW"), word(82.2, 496.2, "YORK"), word(82.2, 520.7, "Avenue"),
    word(81.1, 587.4, "Port"), word(81.1, 604.3, "Vue"),
    word(81.3, 671.4, "466-B-87"),
    # plaintiff continuation, then address city/zip continuation
    word(92.7, 17.4, "CORPORATION"),
    word(92.7, 455.4, "MCKEESPORT,"), word(92.7, 515.8, "PA"), word(92.7, 527.6, "15133"),
    # comments block -- must not be captured into any of the three columns
    word(122.1, 17.4, "Comments:"),
    word(121.1, 68.4, "NEED"), word(121.1, 92.8, "O/C"), word(121.1, 109.3, "FOR"), word(121.1, 128.4, "8-3-26"),
    # next record's value line -- confirms the loop stops before this
    word(145.6, 26.0, "68JUN25"), word(145.6, 82.5, "GD-24-008632"),
    word(145.6, 168.4, "Real"), word(145.6, 187.5, "Estate"), word(145.6, 213.8, "Sale"),
    word(145.6, 232.4, "-"),
]


class FakePage:
    def __init__(self, words):
        self._words = words

    def extract_words(self):
        return self._words


class FindSaleListUrlTests(unittest.TestCase):
    def test_finds_the_dated_sale_list_pdf_not_other_pdfs(self):
        self.assertEqual(
            find_sale_list_url(SALES_PAGE_HTML),
            "https://sheriffalleghenycounty.com/wp-content/uploads/2026/08/September-Sale-List.pdf",
        )

    def test_no_matching_link_returns_none(self):
        self.assertIsNone(find_sale_list_url("<html><body>nothing here</body></html>"))


class GroupIntoRowsTests(unittest.TestCase):
    def test_clusters_sub_pixel_top_differences_into_one_row(self):
        # Real data: words on the same visual line differ by up to ~1.2pt
        # in "top" (48.3 vs 48.1) -- must still land in one row.
        rows = group_into_rows(MORTGAGE_FORECLOSURE_RECORD_WORDS[:11])
        self.assertEqual(len(rows), 1)

    def test_separates_visually_distinct_lines(self):
        rows = group_into_rows(MORTGAGE_FORECLOSURE_RECORD_WORDS)
        # value line, mini-header, data line, plaintiff-cont/address-cont
        # (same top-cluster), comments label, comments text, next value line
        self.assertGreaterEqual(len(rows), 6)


class FindColumnBoundariesTests(unittest.TestCase):
    def test_recognizes_the_mini_header_row(self):
        header_row = [w for w in MORTGAGE_FORECLOSURE_RECORD_WORDS if abs(w["top"] - 67.1) < 0.5]
        self.assertEqual(find_column_boundaries(header_row), (455.4, 587.4, 671.4))

    def test_non_header_row_returns_none(self):
        value_row = [w for w in MORTGAGE_FORECLOSURE_RECORD_WORDS if abs(w["top"] - 48.3) < 0.5]
        self.assertIsNone(find_column_boundaries(value_row))


class ParsePageTests(unittest.TestCase):
    def test_parses_a_confirmed_mortgage_foreclosure_record(self):
        page = FakePage(MORTGAGE_FORECLOSURE_RECORD_WORDS)
        listings = parse_page(page, "https://sheriffalleghenycounty.com/.../September-Sale-List.pdf")
        self.assertEqual(len(listings), 1)  # the trailing partial record has no parcel/header, so it's dropped
        listing = listings[0]
        self.assertEqual(listing["county"], "Allegheny")
        self.assertEqual(listing["account_number"], "466-B-87_GD-24-009484")
        self.assertEqual(listing["minimum_bid"], "3715.71")
        self.assertEqual(listing["municipality"], "Port Vue")
        self.assertEqual(listing["description"], "Sheriff Sale - Mortgage Foreclosure -- Case GD-24-009484")

    def test_address_uses_the_mailing_city_not_the_legal_municipality(self):
        # Real quirk: the mailing city on the address line (MCKEESPORT) can
        # differ from the legal Municipality column (Port Vue) for the same
        # parcel -- address must reflect the former, not double up on both.
        page = FakePage(MORTGAGE_FORECLOSURE_RECORD_WORDS)
        listings = parse_page(page, "test-url")
        self.assertEqual(listings[0]["address"], "1317 NEW YORK Avenue MCKEESPORT, PA 15133")

    def test_comments_block_does_not_leak_into_any_column(self):
        page = FakePage(MORTGAGE_FORECLOSURE_RECORD_WORDS)
        listings = parse_page(page, "test-url")
        for field in ("address", "municipality", "description"):
            self.assertNotIn("NEED", listings[0][field] or "")

    def test_row_text_reassembles_left_to_right_regardless_of_input_order(self):
        header_row = [w for w in MORTGAGE_FORECLOSURE_RECORD_WORDS if abs(w["top"] - 67.1) < 0.5]
        self.assertIn("Property", row_text(list(reversed(header_row))))


if __name__ == "__main__":
    unittest.main()
