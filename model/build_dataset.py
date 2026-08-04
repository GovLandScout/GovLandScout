"""
GovLandScout model (Phase 1) - Dataset build

Joins the four raw sources fetch_data.py cached (Zillow's ZHVI,
price-cut share, and for-sale inventory; FRED's derived unemployment
rate) into one county-month panel, then engineers the lagged/rolling
features the model trains on.

Zillow's own CSVs are wide (one column per month-end date); FRED's is
already long (one row per county-date). Both get reshaped to the same
long (county, year_month, value) shape before merging, using year-month
rather than the exact date since Zillow stamps month-*end* dates
("2018-03-31") and FRED stamps month-*start* ("2018-03-01") for what's
conceptually the same observation period.
"""

from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).parent / "data"

LOOKBACK_MONTHS = 6  # a county needs this much history before its rows are usable as training examples


def load_wide_zillow(filename: str, value_name: str) -> pd.DataFrame:
    df = pd.read_csv(DATA_DIR / filename)
    df = df[df["State"] == "TX"]
    date_cols = [c for c in df.columns if c[:4].isdigit()]
    long = df.melt(
        id_vars=["RegionName"], value_vars=date_cols,
        var_name="date", value_name=value_name,
    )
    long["year_month"] = pd.to_datetime(long["date"]).dt.to_period("M")
    return long[["RegionName", "year_month", value_name]]


def load_unemployment() -> pd.DataFrame:
    df = pd.read_csv(DATA_DIR / "unemployment_county.csv")
    df["year_month"] = pd.to_datetime(df["observation_date"]).dt.to_period("M")
    return df[["RegionName", "year_month", "unemployment_rate"]]


def build_panel() -> pd.DataFrame:
    zhvi = load_wide_zillow("zhvi_county.csv", "zhvi")
    price_cut = load_wide_zillow("price_cut_county.csv", "price_cut_pct")
    inventory = load_wide_zillow("inventory_county.csv", "inventory")
    unemployment = load_unemployment()

    panel = price_cut.merge(zhvi, on=["RegionName", "year_month"], how="left")
    panel = panel.merge(inventory, on=["RegionName", "year_month"], how="left")
    panel = panel.merge(unemployment, on=["RegionName", "year_month"], how="left")
    return panel.sort_values(["RegionName", "year_month"]).reset_index(drop=True)


def engineer_features(panel: pd.DataFrame) -> pd.DataFrame:
    out = []
    for county, group in panel.groupby("RegionName"):
        g = group.sort_values("year_month").copy()

        # Target: next month's price-cut share -- what the model is
        # actually trying to predict. The last row of every county has
        # no "next month" yet, dropped below.
        g["target_next_price_cut_pct"] = g["price_cut_pct"].shift(-1)

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
    keep_cols = ["county", "year_month", "target_next_price_cut_pct"] + feature_cols

    # A row is only usable once every feature and the target are actually
    # present -- mostly the first LOOKBACK_MONTHS of each county (not
    # enough history for the lag/rolling features yet) and the last row
    # of each county (no next month to form the target from).
    clean = full[keep_cols].dropna()
    return clean


def main():
    print("Building county-month panel ...")
    panel = build_panel()
    print(f"  {len(panel):,} raw county-month rows, {panel['RegionName'].nunique()} counties")

    print("Engineering features ...")
    dataset = engineer_features(panel)
    print(f"  {len(dataset):,} usable rows after requiring full feature/target history")
    print(f"  date range: {dataset['year_month'].min()} to {dataset['year_month'].max()}")

    dest = DATA_DIR / "county_month_dataset.csv"
    dataset.to_csv(dest, index=False)
    print(f"Wrote {dest}")


if __name__ == "__main__":
    main()
