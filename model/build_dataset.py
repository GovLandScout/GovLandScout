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

Run with a state key from states.py as the only CLI arg, e.g.
`python3 build_dataset.py pa` (defaults to tx).
"""

import sys
from pathlib import Path

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


def build_panel(state_key: str, state_abbrev: str) -> pd.DataFrame:
    zhvi = load_wide_zillow("zhvi_county.csv", "zhvi", state_abbrev)
    price_cut = load_wide_zillow("price_cut_county.csv", "price_cut_pct", state_abbrev)
    inventory = load_wide_zillow("inventory_county.csv", "inventory", state_abbrev)
    unemployment = load_unemployment(state_key)

    panel = price_cut.merge(zhvi, on=["RegionName", "year_month"], how="left")
    panel = panel.merge(inventory, on=["RegionName", "year_month"], how="left")
    panel = panel.merge(unemployment, on=["RegionName", "year_month"], how="left")
    return panel.sort_values(["RegionName", "year_month"]).reset_index(drop=True)


def engineer_features(panel: pd.DataFrame) -> pd.DataFrame:
    out = []
    for county, group in panel.groupby("RegionName"):
        g = group.sort_values("year_month").copy()

        # Targets: *change* in price-cut share over the next H months, not
        # the raw next-month level. Predicting the level let a model just
        # copy this month's value forward and call it a prediction --
        # price_cut_pct is highly autocorrelated month to month, so that
        # alone got most of the way to a good-looking score without
        # actually explaining any movement. Predicting change removes that
        # shortcut: naive "predict zero change" is the honest baseline now,
        # and doing better than it means a horizon's features are actually
        # carrying signal about which direction things are headed.
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

        g["inventory_mom_pct"] = g["inventory"].pct_change(1) * 100
        g["inventory_level"] = g["inventory"]

        g["unemployment_rate_mom_change"] = g["unemployment_rate"].diff(1)

        g["month_of_year"] = g["year_month"].dt.month
        g["county"] = county

        out.append(g)

    full = pd.concat(out, ignore_index=True)

    feature_cols = [
        "price_cut_pct", "price_cut_pct_lag1", "price_cut_pct_lag3", "price_cut_pct_lag6",
        "price_cut_pct_roll3", "zhvi_mom_pct", "zhvi_yoy_pct", "inventory_mom_pct",
        "inventory_level", "unemployment_rate", "unemployment_rate_mom_change", "month_of_year",
    ]
    target_cols = [f"target_change_{h}m" for h in TARGET_HORIZONS]
    keep_cols = ["county", "year_month"] + target_cols + feature_cols

    # Every feature has to be present -- mostly the first LOOKBACK_MONTHS
    # of each county, not enough history yet for the lag/rolling features.
    # Targets are handled separately: the 6-month target needs 6 more
    # months of future data than the 1-month one does, so requiring all
    # three at once would drop rows near the end of each county's series
    # that are perfectly usable for the shorter horizons. train_model.py
    # drops NaNs on whichever single target column it's training against.
    clean = full[keep_cols].dropna(subset=feature_cols)
    return clean


def main():
    state_key = sys.argv[1] if len(sys.argv) > 1 else "tx"
    state = STATES[state_key]

    print(f"Building {state['name']} county-month panel ...")
    panel = build_panel(state_key, state["abbrev"])
    print(f"  {len(panel):,} raw county-month rows, {panel['RegionName'].nunique()} counties")

    print("Engineering features ...")
    dataset = engineer_features(panel)
    print(f"  {len(dataset):,} usable rows after requiring full feature/target history")
    print(f"  date range: {dataset['year_month'].min()} to {dataset['year_month'].max()}")

    dest = DATA_DIR / f"{state_key}_county_month_dataset.csv"
    dataset.to_csv(dest, index=False)
    print(f"Wrote {dest}")


if __name__ == "__main__":
    main()
