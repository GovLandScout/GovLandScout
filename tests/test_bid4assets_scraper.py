"""
Tests for bid4assets_scraper.py's parsing helpers, using fixtures trimmed
from real bid4assets.com responses captured during development (see that
module's docstring for why title format and address shape both vary by
county rather than following one consistent template).
"""

import unittest

from bid4assets_scraper import (
    discover_pa_storefronts,
    extract_account_number,
    is_still_available,
    parse_storefront_collections,
)

# Trimmed from the real /county-tax-sales page -- a mix of PA and non-PA
# (CA, WA) auctions, confirming non-PA listings are correctly skipped.
COUNTY_SALES_HTML = """
<div class="row">
  <div class="col-4 p-3"><a href="/storefront/BerksPATaxSaleSep26">Berks County, PA Upset Sale</a></div>
  <div class="col-4 p-3"><a href="/storefront/MonroePATaxAug26">Monroe County, PA Repository August 2026</a></div>
  <div class="col-4 p-3"><a href="/storefront/ShastaFeb26">Shasta County, CA Tax Sale</a></div>
  <div class="col-4 p-3"><a href="/philataxsales">Philadelphia Tax Sales</a></div>
</div>
"""

# Trimmed from a real storefront page's inline Kendo ListView data-binding
# script (see bid4assets_scraper.py's module docstring).
STOREFRONT_HTML = """
<script>
    url: "/storefront/taxsales/getauctiondisplay/17954?storefrontCollectionId=" + id,
</script>
<script>
"data":{"Data":[{"PublishedStorefrontCollectionId":17385,"StorefrontCollectionId":10379,"CollectionName":"APNs 1 thru 2"}],"Total":1}
</script>
"""


class DiscoverPaStorefrontsTests(unittest.TestCase):
    def test_finds_every_pa_storefront(self):
        storefronts = discover_pa_storefronts(COUNTY_SALES_HTML)
        self.assertIn(("Berks", "BerksPATaxSaleSep26"), storefronts)
        self.assertIn(("Monroe", "MonroePATaxAug26"), storefronts)

    def test_non_pa_storefront_excluded(self):
        storefronts = discover_pa_storefronts(COUNTY_SALES_HTML)
        counties = [c for c, _ in storefronts]
        self.assertNotIn("Shasta", counties)

    def test_philadelphia_excluded_since_it_has_no_storefront_slug(self):
        # Philadelphia's link is /philataxsales, not /storefront/..., and its
        # label doesn't match "<County> County, PA" -- see module docstring.
        storefronts = discover_pa_storefronts(COUNTY_SALES_HTML)
        self.assertEqual([s for s in storefronts if "phila" in s[1].lower()], [])


class ParseStorefrontCollectionsTests(unittest.TestCase):
    def test_extracts_storefront_id_and_collection_ids(self):
        self.assertEqual(parse_storefront_collections(STOREFRONT_HTML), (17954, [10379]))

    def test_missing_data_returns_none(self):
        self.assertIsNone(parse_storefront_collections("<html>nothing here</html>"))


class ExtractAccountNumberTests(unittest.TestCase):
    # Every one of these title shapes was observed on a real, currently
    # listed county during development -- not synthetic.
    def test_pin_with_space_before_colon(self):
        self.assertEqual(extract_account_number("Berks County PA Tax: PIN: 21541800696512"), "21541800696512")

    def test_parcel_no_space_before_colon(self):
        self.assertEqual(extract_account_number("Fayette County PA Tax: Parcel:01-01-0012"), "01-01-0012")

    def test_apn_label_with_spaces_in_id(self):
        self.assertEqual(extract_account_number("Columbia County, PA Tax: APN:01 04 03400000"), "01 04 03400000")

    def test_no_label_at_all(self):
        self.assertEqual(extract_account_number("Cumberland County, PA Tax: 01-20-1852-013."), "01-20-1852-013.")

    def test_parcel_label_with_space_after_colon(self):
        self.assertEqual(extract_account_number("Schuylkill County PA Tax: Parcel: 05-05-0037.000"), "05-05-0037.000")

    def test_unrecognized_format_falls_back_to_raw_title(self):
        # Must never drop a listing just because a future title doesn't
        # match any known shape.
        self.assertEqual(extract_account_number("Some Unexpected New Format XYZ"), "Some Unexpected New Format XYZ")


class IsStillAvailableTests(unittest.TestCase):
    def test_in_preview_is_available(self):
        self.assertTrue(is_still_available({"remaining": "In Preview"}))

    def test_countdown_is_available(self):
        self.assertTrue(is_still_available({"remaining": "<B>8 <I>days</I></B>"}))

    def test_sold_is_not_available(self):
        self.assertFalse(is_still_available({"remaining": "Sold"}))

    def test_closed_is_not_available(self):
        self.assertFalse(is_still_available({"remaining": "Closed"}))

    def test_missing_remaining_field_defaults_to_available(self):
        self.assertTrue(is_still_available({}))


if __name__ == "__main__":
    unittest.main()
