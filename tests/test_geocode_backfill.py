"""
Tests for geocode_backfill.py's parse_address() -- the function that
decides whether an address is usable enough to hand to the Census batch
geocoder. It used to reject 82 of 131 real ungeocoded listings outright
over formatting rather than missing content; these cases are drawn
directly from that investigation (see the "Recovering missing data"
commit), not synthetic examples, so a regression here means a real
county's listings silently stop getting map pins again.
"""

import unittest

from geocode_backfill import is_within_state_bounds, parse_address


class ParseAddressTests(unittest.TestCase):
    def test_well_formed_address_with_zip(self):
        self.assertEqual(
            parse_address("1027 W King St, Dallas, TX 75208", "TX"),
            ("1027 W King St", "Dallas", "TX", "75208"),
        )

    def test_three_parts_no_zip_still_usable(self):
        # MVBA's Bastrop listings end "..., Bastrop, Texas" with no zip at all.
        self.assertEqual(
            parse_address("276 Laura Ln, Bastrop, Texas", "TX"),
            ("276 Laura Ln", "Bastrop", "TX", ""),
        )

    def test_three_parts_state_only_no_digits_in_third_segment(self):
        self.assertEqual(
            parse_address("400 S. Lucy Street, Bartlett, TX", "TX"),
            ("400 S. Lucy Street", "Bartlett", "TX", ""),
        )

    def test_two_parts_street_and_city_no_state_or_zip(self):
        # GovEase's Grayson listings never give more than "<street>, <city>".
        self.assertEqual(
            parse_address("1809 E ALMA AVE, SHERMAN", "TX"),
            ("1809 E ALMA AVE", "SHERMAN", "TX", ""),
        )

    def test_no_comma_falls_back_to_county_as_city(self):
        # GovEase's Denton listings are a bare street with no city at all.
        self.assertEqual(
            parse_address("1205 MORSE ST", "TX", county_fallback="Denton"),
            ("1205 MORSE ST", "Denton", "TX", ""),
        )

    def test_no_comma_and_no_county_fallback_is_unusable(self):
        self.assertIsNone(parse_address("1205 MORSE ST", "TX"))

    def test_dangling_state_token_is_not_a_city(self):
        # "<street>, Texas <zip>" -- no real city was ever given, don't guess.
        self.assertIsNone(parse_address("E Oak St, Texas 76853", "TX"))

    def test_place_comma_texas_with_no_street_is_unusable(self):
        self.assertIsNone(parse_address("Lampasas, Texas", "TX"))

    def test_empty_address_is_unusable(self):
        self.assertIsNone(parse_address("", "TX"))

    def test_county_fallback_ignored_when_address_already_has_a_city(self):
        # The fallback should only kick in when there's truly no comma --
        # a well-formed address shouldn't have its real city overridden.
        parsed = parse_address("123 Main St, Austin, TX 78701", "TX", county_fallback="Travis")
        self.assertEqual(parsed[1], "Austin")

    def test_pennsylvania_address_geocodes_against_pa_not_texas(self):
        # GovEase's Beaver County (PA) listings are a bare street, same
        # shape as Denton's TX ones -- but must come back tagged PA, not
        # hardcoded TX, or the Census geocoder resolves it against the
        # wrong state entirely.
        self.assertEqual(
            parse_address("828 2ND AVE", "PA", county_fallback="Beaver"),
            ("828 2ND AVE", "Beaver", "PA", ""),
        )

    def test_dangling_pennsylvania_state_token_is_not_a_city(self):
        self.assertIsNone(parse_address("E Oak St, Pennsylvania 16001", "PA"))

    def test_dangling_state_falls_back_to_county_when_available(self):
        # As of 2026-08-06: Bid4Assets' Berks and Fayette County listings
        # are 100% "<street>, PA" with no city at all (confirmed against
        # 1,467 real Berks listings) -- previously rejected outright,
        # leaving them with zero geocoding coverage. Falling back to the
        # county (same treatment the no-comma case already got) recovers
        # them the same way it already recovered GovEase's Denton listings.
        self.assertEqual(
            parse_address("78 RIEGEL LN, PA", "PA", county_fallback="Berks"),
            ("78 RIEGEL LN", "Berks", "PA", ""),
        )

    def test_dangling_state_with_no_fallback_still_unusable(self):
        # Same shape as above, but with no county_fallback available --
        # must still decline rather than guess at nothing.
        self.assertIsNone(parse_address("78 RIEGEL LN, PA", "PA"))


class IsWithinStateBoundsTests(unittest.TestCase):
    def test_real_pa_coordinate_is_within_bounds(self):
        # Camp Hill Borough, Cumberland County, PA.
        self.assertTrue(is_within_state_bounds("PA", 40.2377, -76.9280))

    def test_texas_mismatch_for_a_pa_listing_is_rejected(self):
        # Real 2026-08-06 case: a Cumberland County, PA listing's address
        # ("1605 MAIN STREET, LOWER ALLEN TOWNSHIP, PA", no zip) came back
        # from the Census geocoder matched near Dallas, TX instead.
        self.assertFalse(is_within_state_bounds("PA", 33.100285, -96.62863))

    def test_new_york_mismatch_for_a_pa_listing_is_rejected(self):
        # Real 2026-08-06 case: "...UPPER FRANKFORD TOWNSHIP, PA" matched
        # to upstate New York.
        self.assertFalse(is_within_state_bounds("PA", 43.02646, -75.07558))

    def test_real_tx_coordinate_is_within_bounds(self):
        # Houston, TX.
        self.assertTrue(is_within_state_bounds("TX", 29.7604, -95.3698))

    def test_unknown_state_is_not_validated(self):
        # No bounding box defined yet -- can't reject what isn't checkable,
        # so this must not block a state this hasn't been taught about.
        self.assertTrue(is_within_state_bounds("OH", 0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
