"""
Tests for find_deals.py's pure helper functions -- no DB or network
involved, just the parsing/formatting logic that's easy to get subtly
wrong (see safe_float's own docstring for a real example that slipped
through once already).
"""

import unittest

from find_deals import build_maps_url, safe_float


class SafeFloatTests(unittest.TestCase):
    def test_valid_number_string(self):
        self.assertEqual(safe_float("1234.56"), 1234.56)

    def test_none_is_none(self):
        self.assertIsNone(safe_float(None))

    def test_malformed_number_is_none_not_a_crash(self):
        # Real MVBA PDF typo: a stray period where a comma belongs.
        self.assertIsNone(safe_float("$20.285.28"))

    def test_empty_string_is_none(self):
        self.assertIsNone(safe_float(""))


class BuildMapsUrlTests(unittest.TestCase):
    def test_prefers_address_over_coordinates(self):
        url = build_maps_url("123 Main St, Houston, TX", 29.7604, -95.3698)
        self.assertIn("query=123", url)
        self.assertNotIn("29.7604", url)

    def test_falls_back_to_coordinates_without_address(self):
        url = build_maps_url(None, 29.7604, -95.3698)
        self.assertEqual(url, "https://www.google.com/maps?q=29.7604,-95.3698")

    def test_nothing_to_work_with_returns_none(self):
        self.assertIsNone(build_maps_url(None, None, None))

    def test_partial_coordinates_without_address_returns_none(self):
        self.assertIsNone(build_maps_url(None, 29.7604, None))


if __name__ == "__main__":
    unittest.main()
