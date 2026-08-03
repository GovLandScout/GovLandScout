"""
Tests for govease_scraper.py's HTML table parsing, using a trimmed-down
but real-shaped fixture matching what liveauctions.govease.com actually
serves (see govease_scraper.py's docstring/investigation notes -- column
labels like the bid column vary by county, which is exactly why
parse_county_grid reads the real header instead of assuming fixed
positions).
"""

import unittest

from govease_scraper import clean_money, parse_county_grid

# Trimmed from a real Denton County response: header labels, one normal
# row, and one row with no address ("N/A", as Wichita's real data has).
DENTON_HTML = """
<table id="dt-auctions">
  <thead>
    <tr>
      <th></th><th>Watch</th><th>Unique #</th><th>Parcel #</th><th>Owner Name</th>
      <th>Minimum Bid</th><th>Parcel Address:</th><th>Auction Name:</th>
      <th>Auction Type:</th><th>Bidding</th><th>My Bid</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td></td><td></td>
      <td><a href="/tx/txdenton/1355/openparcel/2087955/19-7161-16">1</a></td>
      <td>19-7161-16</td>
      <td><span>JESSIE MAE TYLER</span></td>
      <td class="alignDollar">$24,174.74</td>
      <td>1205 MORSE ST</td>
      <td>2026 Denton County TX August Sheriffs Sale</td>
      <td>Redeemable Tax Deed</td>
      <td></td><td></td>
    </tr>
    <tr>
      <td></td><td></td>
      <td><a href="/tx/txdenton/1355/openparcel/2087960/25-3822-431">6</a></td>
      <td>25-3822-431</td>
      <td><span>KRISTIN HADDAD</span></td>
      <td class="alignDollar">$7,526.00</td>
      <td>N/A</td>
      <td>2026 Denton County TX August Sheriffs Sale</td>
      <td>Redeemable Tax Deed</td>
      <td></td><td></td>
    </tr>
  </tbody>
</table>
"""

# A different auction's response, matching Grayson's real "Face Value"
# bid-column label instead of Denton's "Minimum Bid".
GRAYSON_HTML = """
<table id="dt-auctions">
  <thead>
    <tr>
      <th></th><th>Watch</th><th>Unique #</th><th>Parcel #</th><th>Owner Name</th>
      <th>Face Value</th><th>Parcel Address:</th><th>Auction Name:</th>
      <th>Auction Type:</th><th>Bidding</th><th>My Bid</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td></td><td></td>
      <td><a href="/tx/txgrayson/1280/openparcel/2082785/T-20-3168">2</a></td>
      <td>T-20-3168</td>
      <td><span>THE ESTATE OF T E JONES</span></td>
      <td class="alignDollar">$3,467.75</td>
      <td>E HINTON ST, TIOGA</td>
      <td>2026 Grayson County TX August Sheriffs Sale</td>
      <td>Redeemable Tax Deed</td>
      <td></td><td></td>
    </tr>
  </tbody>
</table>
"""

EMPTY_AUCTION_HTML = """
<table id="dt-auctions">
  <thead>
    <tr>
      <th></th><th>Watch</th><th>Unique #</th><th>Parcel #</th><th>Owner Name</th>
      <th>Minimum Bid</th><th>Parcel Address:</th><th>Auction Name:</th>
      <th>Auction Type:</th><th>Bidding</th><th>My Bid</th>
    </tr>
  </thead>
  <tbody></tbody>
</table>
"""


class CleanMoneyTests(unittest.TestCase):
    def test_strips_dollar_sign_and_commas(self):
        self.assertEqual(clean_money("$24,174.74"), "24174.74")

    def test_blank_is_none(self):
        self.assertIsNone(clean_money(""))


class ParseCountyGridTests(unittest.TestCase):
    def test_normal_row_parses_all_fields(self):
        listings = parse_county_grid(DENTON_HTML, "Denton")
        first = listings[0]
        self.assertEqual(first["account_number"], "19-7161-16")
        self.assertEqual(first["minimum_bid"], "24174.74")
        self.assertEqual(first["address"], "1205 MORSE ST")
        self.assertEqual(
            first["source_url"],
            "https://liveauctions.govease.com/tx/txdenton/1355/openparcel/2087955/19-7161-16",
        )
        self.assertIn("Redeemable Tax Deed", first["description"])

    def test_na_address_becomes_none(self):
        listings = parse_county_grid(DENTON_HTML, "Denton")
        self.assertIsNone(listings[1]["address"])

    def test_different_bid_column_label_still_found(self):
        listings = parse_county_grid(GRAYSON_HTML, "Grayson")
        self.assertEqual(listings[0]["minimum_bid"], "3467.75")

    def test_county_is_passed_through_not_scraped_from_html(self):
        listings = parse_county_grid(GRAYSON_HTML, "Grayson")
        self.assertEqual(listings[0]["county"], "Grayson")

    def test_empty_auction_returns_no_listings(self):
        self.assertEqual(parse_county_grid(EMPTY_AUCTION_HTML, "Wichita"), [])

    def test_no_table_present_returns_no_listings(self):
        self.assertEqual(parse_county_grid("<html><body>nothing here</body></html>", "Denton"), [])


if __name__ == "__main__":
    unittest.main()
