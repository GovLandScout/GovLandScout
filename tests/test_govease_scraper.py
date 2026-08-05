"""
Tests for govease_scraper.py's HTML table parsing, using a trimmed-down
but real-shaped fixture matching what liveauctions.govease.com actually
serves (see govease_scraper.py's docstring/investigation notes -- column
labels like the bid column vary by county, which is exactly why
parse_county_grid reads the real header instead of assuming fixed
positions).
"""

import unittest

from govease_scraper import COUNTIES, clean_money, parse_county_grid

# Trimmed from a real Beaver County (PA) response -- same shape as Denton's
# TX fixture below, confirming the PA rollout didn't need any parser
# changes (GovEase's grid format is state-agnostic; only the COUNTIES list
# changed). PA's bid column is labeled "Face Value", like Grayson's TX one.
BEAVER_HTML = """
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
      <td><a href="/pa/pabeaverupset/1533/openparcel/2087955/01-003-0403-000">1</a></td>
      <td>01-003-0403-000</td>
      <td><span>CLECKLEY,TOMIYA</span></td>
      <td class="alignDollar">$2,993.84</td>
      <td>828 2ND AVE</td>
      <td>2026 Beaver County Upset Sale</td>
      <td>Tax Lien</td>
      <td></td><td></td>
    </tr>
  </tbody>
</table>
"""

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

    def test_pennsylvania_county_parses_the_same_way_as_texas(self):
        listings = parse_county_grid(BEAVER_HTML, "Beaver")
        self.assertEqual(listings[0]["account_number"], "01-003-0403-000")
        self.assertEqual(listings[0]["minimum_bid"], "2993.84")
        self.assertEqual(listings[0]["address"], "828 2ND AVE")


class CountiesTableTests(unittest.TestCase):
    """
    Data-integrity checks on the COUNTIES list itself, not the HTML parser
    -- catches a bad edit (e.g. a typo'd auction_id, or Erie's two sale
    types losing their disambiguating sale_type) without needing a live
    request.
    """

    def test_every_county_has_a_two_letter_state(self):
        for county, state, slug, auction_id, sale_type in COUNTIES:
            self.assertEqual(len(state), 2, f"{county}: state {state!r} isn't a 2-letter code")

    def test_no_duplicate_slugs_or_auction_ids(self):
        slugs = [c[2] for c in COUNTIES]
        auction_ids = [c[3] for c in COUNTIES]
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertEqual(len(auction_ids), len(set(auction_ids)))

    def test_texas_counties_have_no_sale_type_suffix(self):
        # Must stay None -- these keys are already live in production
        # (see COUNTIES's own comment); adding a suffix would orphan them.
        for county, state, slug, auction_id, sale_type in COUNTIES:
            if state == "tx":
                self.assertIsNone(sale_type, f"{county}: TX counties must not get a sale_type suffix")

    def test_erie_pa_has_two_distinct_sale_types(self):
        erie_sale_types = {sale_type for county, state, slug, auction_id, sale_type in COUNTIES
                            if state == "pa" and county == "Erie"}
        self.assertEqual(erie_sale_types, {"Judicial", "Upset"})


if __name__ == "__main__":
    unittest.main()
