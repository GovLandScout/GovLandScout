"""
Tests for pa_assessment_backfill.py's pure logic (id normalization, batch
matching, CLR table integrity). query_batch's actual HTTP behavior is
exercised live in development (see the module docstring) rather than
mocked here -- these tests cover the parts that don't need a network call.
"""

import unittest

from pa_assessment_backfill import (
    COUNTY_CONFIGS,
    STATE_CLR_FACTORS,
    strip_dashes,
    strip_trailing_period,
)


class StripTrailingPeriodTests(unittest.TestCase):
    def test_strips_trailing_period(self):
        # Real Cumberland account_number shape from bid4assets_scraper.py.
        self.assertEqual(strip_trailing_period("01-20-1852-013."), "01-20-1852-013")

    def test_no_trailing_period_unchanged(self):
        self.assertEqual(strip_trailing_period("1-2-63"), "1-2-63")

    def test_strips_surrounding_whitespace_too(self):
        self.assertEqual(strip_trailing_period("  1-2-63.  "), "1-2-63")


class StripDashesTests(unittest.TestCase):
    def test_strips_all_dashes(self):
        # Real Montgomery account_number -> the format its own GIS PARCEL field uses.
        self.assertEqual(strip_dashes("01-00-01606-02-2"), "010001606022")

    def test_no_dashes_unchanged(self):
        self.assertEqual(strip_dashes("010001606022"), "010001606022")


class StateClrFactorsTests(unittest.TestCase):
    def test_every_configured_county_has_a_clr_factor(self):
        # A county in COUNTY_CONFIGS with no matching CLR factor would
        # KeyError at runtime in backfill_county -- catch that here instead.
        for county in COUNTY_CONFIGS:
            self.assertIn(county, STATE_CLR_FACTORS, f"{county} is configured but has no CLR factor")

    def test_known_factors_match_the_published_2025_table(self):
        # Spot-checked against the real PA Dept. of Revenue PDF during
        # development -- a regression here means the table got edited wrong.
        self.assertEqual(STATE_CLR_FACTORS["Chester"], 3.27)
        self.assertEqual(STATE_CLR_FACTORS["Montgomery"], 3.36)
        self.assertEqual(STATE_CLR_FACTORS["Cumberland"], 1.56)

    def test_philadelphia_deliberately_excluded(self):
        # Two different factors depending on transaction date -- see
        # module docstring for why this is left out rather than guessed at.
        self.assertNotIn("Philadelphia", STATE_CLR_FACTORS)

    def test_all_factors_are_positive(self):
        for county, factor in STATE_CLR_FACTORS.items():
            self.assertGreater(factor, 0, f"{county}'s CLR factor must be positive")


class CountyConfigsTests(unittest.TestCase):
    def test_every_config_has_required_keys(self):
        required = {"query_url", "id_field", "value_field", "normalize_id"}
        for county, config in COUNTY_CONFIGS.items():
            missing = required - config.keys()
            self.assertFalse(missing, f"{county} config missing: {missing}")

    def test_query_urls_are_unique(self):
        urls = [c["query_url"] for c in COUNTY_CONFIGS.values()]
        self.assertEqual(len(urls), len(set(urls)))


if __name__ == "__main__":
    unittest.main()
