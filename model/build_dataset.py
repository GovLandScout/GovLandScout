"""
GovLandScout model (Phase 1) - Dataset build

Joins the four raw sources fetch_data.py cached (Zillow's ZHVI,
price-cut share, and for-sale inventory; FRED's derived unemployment
rate) into one county-month panel, then engineers the lagged/rolling
features the model trains on.

Zillow's own CSVs are wide (one column per month-end date) and cover
every state at once; FRED's per-state unemployment file (see
fetch_data.py) is already long (one row per county-date). Both get
reshaped to the same long (county, year_month, value) shape before
merging, using year-month rather than the exact date since Zillow
stamps month-*end* dates ("2018-03-31") and FRED stamps month-*start*
("2018-03-01") for what's conceptually the same observation period.

Writes two output datasets, not one: `{state}_county_month_dataset.csv`
(unchanged -- the original price-cut-share model, 207 of 254 TX counties
/ 66 of 67 PA ones, whatever Zillow itself publishes price-cut data for)
and `{state}_county_month_dataset_zhvi.csv` (a gap-filler covering every
county Zillow has ZHVI data for instead -- 243 TX / 67 PA, all but the
handful its price-cut coverage doesn't reach). See engineer_features()
and build_panel()'s own comments for why price-cut coverage is narrower
in the first place and how the second dataset fills the gap without
touching the first.

Run with a state key from states.py as the only CLI arg, e.g.
`python3 build_dataset.py pa` (defaults to tx).
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from states import STATES

DATA_DIR = Path(__file__).parent / "data"

LOOKBACK_MONTHS = 6  # a county needs this much history before its rows are usable as training examples
TARGET_HORIZONS = [1, 3, 6]  # months ahead -- see engineer_features()'s target comment


def load_wide_zillow(filename: str, value_name: str, state_abbrev: str) -> pd.DataFrame:
    df = pd.read_csv(DATA_DIR / filename)
    df = df[df["State"] == state_abbrev]
    date_cols = [c for c in df.columns if c[:4].isdigit()]
    long = df.melt(
        id_vars=["RegionName"], value_vars=date_cols,
        var_name="date", value_name=value_name,
    )
    long["year_month"] = pd.to_datetime(long["date"]).dt.to_period("M")
    return long[["RegionName", "year_month", value_name]]


def load_unemployment(state_key: str) -> pd.DataFrame:
    df = pd.read_csv(DATA_DIR / f"unemployment_{state_key}.csv")
    df["year_month"] = pd.to_datetime(df["observation_date"]).dt.to_period("M")
    return df[["RegionName", "year_month", "unemployment_rate"]]


def load_mortgage_rate() -> pd.DataFrame:
    """National, not per-county -- fetch_data.py's fetch_mortgage_rate()
    caches Freddie Mac's weekly (Thursday) series as-is; averaged here to
    one value per month (rather than, say, just the last weekly reading)
    so a single unusual week doesn't dominate a whole month's feature
    value the way picking one reading could."""
    df = pd.read_csv(DATA_DIR / "mortgage_rate.csv")
    df["year_month"] = pd.to_datetime(df["observation_date"]).dt.to_period("M")
    monthly = df.groupby("year_month")["MORTGAGE30US"].mean().reset_index()
    return monthly.rename(columns={"MORTGAGE30US": "mortgage_rate"})


def build_panel(state_key: str, state_abbrev: str) -> pd.DataFrame:
    """
    Outer-joins ZHVI/price-cut/inventory rather than anchoring on one of
    them -- confirmed directly against the raw CSVs before writing this:
    Zillow publishes ZHVI and inventory for 243 of 254 TX counties but
    price-cut share for only 208 (the ~35 missing are recognizably its
    smallest, most rural counties -- evidently below whatever minimum
    active-listing volume Zillow needs to publish a stable price-cut
    figure at all, not a gap in what this project fetches). Anchoring on
    price_cut here (the old behavior) silently dropped every one of those
    ZHVI-covered counties from the panel before engineer_features() ever
    ran. Outer-joining keeps them in the panel; engineer_features() below
    is what actually decides which output dataset(s) a given county ends
    up usable for, based on which columns it has real data in.
    """
    zhvi = load_wide_zillow("zhvi_county.csv", "zhvi", state_abbrev)
    price_cut = load_wide_zillow("price_cut_county.csv", "price_cut_pct", state_abbrev)
    inventory = load_wide_zillow("inventory_county.csv", "inventory", state_abbrev)
    unemployment = load_unemployment(state_key)
    mortgage_rate = load_mortgage_rate()

    panel = zhvi.merge(price_cut, on=["RegionName", "year_month"], how="outer")
    panel = panel.merge(inventory, on=["RegionName", "year_month"], how="outer")
    panel = panel.merge(unemployment, on=["RegionName", "year_month"], how="left")
    # year_month only, not RegionName -- every county in the state shares
    # the same national mortgage rate for a given month, unlike the
    # per-county sources above.
    panel = panel.merge(mortgage_rate, on="year_month", how="left")
    return panel.sort_values(["RegionName", "year_month"]).reset_index(drop=True)


# The price-cut model's original 15 features -- unchanged from before the
# ZHVI gap-filler existed, still what county_month_dataset.csv's own 207
# (TX) / 66 (PA) counties train on.
PRICE_CUT_FEATURE_COLS = [
    "price_cut_pct", "price_cut_pct_lag1", "price_cut_pct_lag3", "price_cut_pct_lag6",
    "price_cut_pct_roll3", "zhvi_mom_pct", "zhvi_yoy_pct", "inventory_mom_pct",
    "inventory_level", "unemployment_rate", "unemployment_rate_mom_change",
    "mortgage_rate", "mortgage_rate_mom_change",
    "month_sin", "month_cos",
]

# Everything price_cut_pct-derived removed -- the whole reason this
# feature set exists is to cover counties Zillow never publishes
# price_cut_pct for at all (see build_panel()'s own comment), so
# requiring those columns would defeat the purpose immediately.
#
# unemployment_rate/unemployment_rate_mom_change removed too, for the
# same reason but a different data source: FRED's own county-level
# employment/labor-force series (see fetch_data.py's
# fetch_county_unemployment(), which unemployment_rate is derived from)
# simply doesn't exist for many of the same small, rural counties Zillow
# skips for price_cut_pct -- confirmed directly on Motley County, TX
# before writing this: real ZHVI and inventory history, unemployment_rate
# null for literally every month. Requiring it here would silently
# reintroduce most of the coverage gap this dataset exists to close.
# Losing it is a real, deliberate tradeoff, not a free one -- but
# unemployment features were already the least important block in every
# version of this model's feature-importance results (see
# model/README.md's Results sections), so trading them away specifically
# for the gap-filler model, whose one job is maximizing county coverage,
# is the right side of that tradeoff. Verified directly: keeping
# unemployment would leave this dataset at 207 usable TX counties, barely
# better than price-cut's own 203 and defeating the point of building it;
# dropping it reaches 238 (of 243 Zillow publishes ZHVI for at all).
ZHVI_FEATURE_COLS = [
    "zhvi_mom_pct", "zhvi_yoy_pct", "inventory_mom_pct", "inventory_level",
    "mortgage_rate", "mortgage_rate_mom_change",
    "month_sin", "month_cos",
]


def engineer_features(panel: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (price_cut_dataset, zhvi_dataset) -- two different row
    filters and target columns over the *same* underlying per-county
    feature engineering, not two separate passes over the data. See
    PRICE_CUT_FEATURE_COLS/ZHVI_FEATURE_COLS above for why they need
    different feature sets, and each target loop's own comment for why
    they need different targets too."""
    out = []
    for county, group in panel.groupby("RegionName"):
        g = group.sort_values("year_month").copy()

        # Price-cut target: *change* in price-cut share over the next H
        # months, not the raw next-month level. Predicting the level let a
        # model just copy this month's value forward and call it a
        # prediction -- price_cut_pct is highly autocorrelated month to
        # month, so that alone got most of the way to a good-looking score
        # without actually explaining any movement. Predicting change
        # removes that shortcut: naive "predict zero change" is the honest
        # baseline now, and doing better than it means a horizon's features
        # are actually carrying signal about which direction things are
        # headed.
        #
        # Three horizons rather than one so short vs. longer-term dynamics
        # can be compared directly -- current momentum is expected to
        # matter more at 1 month, macro trends (unemployment, ZHVI) more at
        # 6, and train_model.py reports both instead of assuming either.
        for horizon in TARGET_HORIZONS:
            g[f"target_change_{horizon}m"] = g["price_cut_pct"].shift(-horizon) - g["price_cut_pct"]

        g["price_cut_pct_lag1"] = g["price_cut_pct"].shift(1)
        g["price_cut_pct_lag3"] = g["price_cut_pct"].shift(3)
        g["price_cut_pct_lag6"] = g["price_cut_pct"].shift(6)
        g["price_cut_pct_roll3"] = g["price_cut_pct"].rolling(3).mean()

        g["zhvi_mom_pct"] = g["zhvi"].pct_change(1) * 100
        g["zhvi_yoy_pct"] = g["zhvi"].pct_change(12) * 100

        # ZHVI-decline target (the gap-filler model's own, for counties
        # with no price_cut_pct at all): *change in the rate* of home-value
        # appreciation over the next H months, negated so the sign
        # convention matches the price-cut target above -- positive means
        # "getting worse" for both (more price cuts there, decelerating/
        # declining values here), not opposite signs for what's shown as
        # the same red-toward-distress color on the map. Same "predict
        # change, not the level" reasoning as the price-cut target: raw
        # zhvi_mom_pct is itself fairly persistent month to month, so
        # predicting its future *level* would mostly just copy today's
        # reading forward again.
        for horizon in TARGET_HORIZONS:
            g[f"target_zhvi_decline_{horizon}m"] = -(g["zhvi_mom_pct"].shift(-horizon) - g["zhvi_mom_pct"])

        g["inventory_mom_pct"] = g["inventory"].pct_change(1) * 100
        g["inventory_level"] = g["inventory"]

        g["unemployment_rate_mom_change"] = g["unemployment_rate"].diff(1)

        # mortgage_rate itself is already a column here (see
        # build_panel()'s merge) -- just adding its month-over-month
        # change, same pairing every other macro feature already gets.
        # It's identical across every county for a given month, so
        # .diff(1) is really tracking the *national* rate's own movement,
        # not anything county-specific.
        g["mortgage_rate_mom_change"] = g["mortgage_rate"].diff(1)

        # Not the raw month number (1-12) -- that encoding puts December
        # and January, adjacent in reality, about as far apart as two
        # months can be numerically, which actively fights the model on
        # its own single most important feature (43-61% of importance
        # across every horizon/state per model/README.md's Results
        # section, before this fix). Sin/cos together place every month on
        # a circle, so November sits next to both October and December the
        # way it actually does -- one of the two alone can't do this (sine
        # alone maps e.g. month 3 and month 9 to the same value).
        month_angle = 2 * np.pi * g["year_month"].dt.month / 12
        g["month_sin"] = np.sin(month_angle)
        g["month_cos"] = np.cos(month_angle)
        g["county"] = county

        out.append(g)

    full = pd.concat(out, ignore_index=True)

    # Every feature has to be present -- mostly the first LOOKBACK_MONTHS
    # of each county, not enough history yet for the lag/rolling features.
    # Targets are handled separately: the 6-month target needs 6 more
    # months of future data than the 1-month one does, so requiring all
    # three at once would drop rows near the end of each county's series
    # that are perfectly usable for the shorter horizons. train_model.py
    # drops NaNs on whichever single target column it's training against.
    price_cut_targets = [f"target_change_{h}m" for h in TARGET_HORIZONS]
    price_cut_keep = ["county", "year_month"] + price_cut_targets + PRICE_CUT_FEATURE_COLS
    price_cut_dataset = full[price_cut_keep].dropna(subset=PRICE_CUT_FEATURE_COLS)

    zhvi_targets = [f"target_zhvi_decline_{h}m" for h in TARGET_HORIZONS]
    zhvi_keep = ["county", "year_month"] + zhvi_targets + ZHVI_FEATURE_COLS
    zhvi_dataset = full[zhvi_keep].dropna(subset=ZHVI_FEATURE_COLS)

    return price_cut_dataset, zhvi_dataset


def main():
    state_key = sys.argv[1] if len(sys.argv) > 1 else "tx"
    state = STATES[state_key]

    print(f"Building {state['name']} county-month panel ...")
    panel = build_panel(state_key, state["abbrev"])
    print(f"  {len(panel):,} raw county-month rows, {panel['RegionName'].nunique()} counties")

    print("Engineering features ...")
    price_cut_dataset, zhvi_dataset = engineer_features(panel)
    print(f"  price-cut dataset: {len(price_cut_dataset):,} usable rows, "
          f"{price_cut_dataset['county'].nunique()} counties, "
          f"{price_cut_dataset['year_month'].min()} to {price_cut_dataset['year_month'].max()}")
    print(f"  zhvi-decline dataset: {len(zhvi_dataset):,} usable rows, "
          f"{zhvi_dataset['county'].nunique()} counties, "
          f"{zhvi_dataset['year_month'].min()} to {zhvi_dataset['year_month'].max()}")

    dest = DATA_DIR / f"{state_key}_county_month_dataset.csv"
    price_cut_dataset.to_csv(dest, index=False)
    print(f"Wrote {dest}")

    zhvi_dest = DATA_DIR / f"{state_key}_county_month_dataset_zhvi.csv"
    zhvi_dataset.to_csv(zhvi_dest, index=False)
    print(f"Wrote {zhvi_dest}")


if __name__ == "__main__":
    main()
