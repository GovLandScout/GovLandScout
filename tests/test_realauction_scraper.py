"""
Tests for realauction_scraper.py's Pennsylvania sheriff-sale support --
fixtures trimmed from real butler/washington/lawrence.pa.realforeclose.com
AREA=W responses captured during development (see that module's docstring
for why PA gets its own field-parsing function and its own per-county
sale-date sourcing instead of reusing the Texas first-Tuesday rule).
"""

import unittest
from datetime import date
from unittest.mock import patch

from realauction_scraper import (
    parse_pa_waiting_items,
    pa_sale_dates,
    washington_sale_dates,
)

# Trimmed from a real butler.pa.realforeclose.com AREA=W response
# (09/18/2026 sale) -- confirmed identical field template on Washington's
# and Lawrence's own live responses too.
BUTLER_ITEM_HTML = """
<div class="AUCTION_ITEM PREVIEW" aid="9182">
  <div class="AUCTION_DETAILS">
    <table class="ad_tab">
      <tr><th>Case Status:</th><td>ACTIVE</td></tr>
      <tr><th>Case #:</th><td>2025-30042 (0)</td></tr>
      <tr><th>Final Judgment Amount:</th><td>$26,113.54</td></tr>
      <tr><th>Parcel ID:</th><td>130-S4-CU3024-0000</td></tr>
      <tr><th>Property Address:</th><td>301 BELLWOOD COURT</td></tr>
      <tr><th></th><td>CRANBERRY TOWNSHIP, 16066</td></tr>
      <tr><th>Opening Bid:</th><td>$2,618.40</td></tr>
    </table>
  </div>
</div>
"""

# A second real item, used to confirm multiple AUCTION_ITEMs on one page
# each parse independently.
BUTLER_SECOND_ITEM_HTML = """
<div class="AUCTION_ITEM PREVIEW" aid="9183">
  <div class="AUCTION_DETAILS">
    <table class="ad_tab">
      <tr><th>Case Status:</th><td>ACTIVE</td></tr>
      <tr><th>Case #:</th><td>2025-30138 (0)</td></tr>
      <tr><th>Final Judgment Amount:</th><td>$403,499.77</td></tr>
      <tr><th>Parcel ID:</th><td>040-S13-B202-0000</td></tr>
      <tr><th>Property Address:</th><td>108 SETTLERS COURT</td></tr>
      <tr><th></th><td>FREEPORT, 16229</td></tr>
      <tr><th>Opening Bid:</th><td>$7,487.89</td></tr>
    </table>
  </div>
</div>
"""

SALE_DATE = date(2026, 9, 18)
BASE_URL = "butler.pa.realforeclose.com"


class ParsePaWaitingItemsTests(unittest.TestCase):
    def test_parses_real_field_template(self):
        listings = parse_pa_waiting_items(BUTLER_ITEM_HTML, "Butler", BASE_URL, SALE_DATE)
        self.assertEqual(len(listings), 1)
        listing = listings[0]
        self.assertEqual(listing["county"], "Butler")
        self.assertEqual(listing["account_number"], "130-S4-CU3024-0000_2025-30042 (0)")
        self.assertEqual(listing["minimum_bid"], "2618.40")
        self.assertIsNone(listing["estimated_value"])  # Final Judgment Amount isn't an appraisal, see docstring
        self.assertEqual(listing["address"], "301 BELLWOOD COURT, CRANBERRY TOWNSHIP, 16066, PA")
        self.assertIn("2025-30042 (0)", listing["description"])
        self.assertIn("26113.54", listing["description"])
        self.assertIn("AUCTIONDATE=09/18/2026", listing["source_url"])

    def test_multiple_items_each_parse(self):
        html = BUTLER_ITEM_HTML + BUTLER_SECOND_ITEM_HTML
        listings = parse_pa_waiting_items(html, "Butler", BASE_URL, SALE_DATE)
        self.assertEqual(len(listings), 2)
        self.assertEqual(listings[1]["account_number"], "040-S13-B202-0000_2025-30138 (0)")

    def test_row_missing_parcel_id_is_skipped(self):
        html = BUTLER_ITEM_HTML.replace("130-S4-CU3024-0000", "")
        listings = parse_pa_waiting_items(html, "Butler", BASE_URL, SALE_DATE)
        self.assertEqual(listings, [])

    def test_no_items_returns_empty_list(self):
        self.assertEqual(parse_pa_waiting_items("<html><body>nothing here</body></html>", "Butler", BASE_URL, SALE_DATE), [])


class WashingtonSaleDatesTests(unittest.TestCase):
    """First Friday of every month except August -- Washington County's own
    published rule, computed rather than hardcoded (see module docstring)."""

    @patch("realauction_scraper.date")
    def test_skips_august_and_lands_on_fridays(self, mock_date):
        mock_date.today.return_value = date(2026, 6, 1)
        mock_date.side_effect = lambda *args, **kw: date(*args, **kw)
        dates = washington_sale_dates(3)
        self.assertNotIn(8, [d.month for d in dates])
        for d in dates:
            self.assertEqual(d.weekday(), 4)  # Friday

    @patch("realauction_scraper.date")
    def test_never_returns_a_date_before_today(self, mock_date):
        mock_date.today.return_value = date(2026, 8, 18)
        mock_date.side_effect = lambda *args, **kw: date(*args, **kw)
        dates = washington_sale_dates(3)
        self.assertTrue(all(d >= date(2026, 8, 18) for d in dates))


class PaSaleDatesTests(unittest.TestCase):
    def test_washington_uses_the_computed_rule(self):
        with patch("realauction_scraper.washington_sale_dates", return_value=[date(2026, 9, 4)]) as mock_fn:
            dates = pa_sale_dates("Washington")
        mock_fn.assert_called_once()
        self.assertEqual(dates, [date(2026, 9, 4)])

    @patch("realauction_scraper.date")
    def test_butler_uses_the_explicit_list_filtered_to_the_future(self, mock_date):
        mock_date.today.return_value = date(2026, 10, 1)
        mock_date.side_effect = lambda *args, **kw: date(*args, **kw)
        # Butler's explicit list includes both Sep 18 (already past 10/1)
        # and Nov 20 (still upcoming) -- only the latter should survive.
        dates = pa_sale_dates("Butler")
        self.assertEqual(dates, [date(2026, 11, 20)])

    def test_unknown_county_returns_empty_list(self):
        self.assertEqual(pa_sale_dates("NotARealCounty"), [])


if __name__ == "__main__":
    unittest.main()
