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

from geocode_backfill import parse_address


class ParseAddressTests(unittest.TestCase):
    def test_well_formed_address_with_zip(self):
        self.assertEqual(
            parse_address("1027 W King St, Dallas, TX 75208"),
            ("1027 W King St", "Dallas", "TX", "75208"),
        )

    def test_three_parts_no_zip_still_usable(self):
        # MVBA's Bastrop listings end "..., Bastrop, Texas" with no zip at all.
        self.assertEqual(
            parse_address("276 Laura Ln, Bastrop, Texas"),
            ("276 Laura Ln", "Bastrop", "TX", ""),
        )

    def test_three_parts_state_only_no_digits_in_third_segment(self):
        self.assertEqual(
            parse_address("400 S. Lucy Street, Bartlett, TX"),
            ("400 S. Lucy Street", "Bartlett", "TX", ""),
        )

    def test_two_parts_street_and_city_no_state_or_zip(self):
        # GovEase's Grayson listings never give more than "<street>, <city>".
        self.assertEqual(
            parse_address("1809 E ALMA AVE, SHERMAN"),
            ("1809 E ALMA AVE", "SHERMAN", "TX", ""),
        )

    def test_no_comma_falls_back_to_county_as_city(self):
        # GovEase's Denton listings are a bare street with no city at all.
        self.assertEqual(
            parse_address("1205 MORSE ST", county_fallback="Denton"),
            ("1205 MORSE ST", "Denton", "TX", ""),
        )

    def test_no_comma_and_no_county_fallback_is_unusable(self):
        self.assertIsNone(parse_address("1205 MORSE ST"))

    def test_dangling_state_token_is_not_a_city(self):
        # "<street>, Texas <zip>" -- no real city was ever given, don't guess.
        self.assertIsNone(parse_address("E Oak St, Texas 76853"))

    def test_place_comma_texas_with_no_street_is_unusable(self):
        self.assertIsNone(parse_address("Lampasas, Texas"))

    def test_empty_address_is_unusable(self):
        self.assertIsNone(parse_address(""))

    def test_county_fallback_ignored_when_address_already_has_a_city(self):
        # The fallback should only kick in when there's truly no comma --
        # a well-formed address shouldn't have its real city overridden.
        parsed = parse_address("123 Main St, Austin, TX 78701", county_fallback="Travis")
        self.assertEqual(parsed[1], "Austin")


if __name__ == "__main__":
    unittest.main()
