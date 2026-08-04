"""
GovLandScout model (Phase 1) - Generate current predictions

Runs the three trained horizon models (see train_model.py) against each
county's most recent usable row to produce "here's what the model
currently expects for this county over the next 1/3/6 months," and
writes it to public/county_predictions.json.

That file -- not this script, and not the trained .joblib models it
depends on -- is what web.py actually reads. web.py's own environment
(Render) never installs pandas/scikit-learn; this stays a manual/
periodic step run from here, same as fetch_data.py/build_dataset.py/
train_model.py, with its small JSON output committed to the repo.

Static, not live: this doesn't run on a schedule, so the map reflects
whenever this was last run, not today's date. Good enough for a
research/portfolio feature -- re-run by hand after retraining.
"""

import json
from pathlib import Path

import joblib
import pandas as pd

DATA_DIR = Path(__file__).parent / "data"
PUBLIC_DIR = Path(__file__).parent / "public"
TARGET_HORIZONS = [1, 3, 6]

FEATURE_COLS = [
    "price_cut_pct", "price_cut_pct_lag1", "price_cut_pct_lag3", "price_cut_pct_lag6",
    "price_cut_pct_roll3", "zhvi_mom_pct", "zhvi_yoy_pct", "inventory_mom_pct",
    "inventory_level", "unemployment_rate", "unemployment_rate_mom_change", "month_of_year",
]

# Zillow's county names and the Census geometry's names match exactly
# except for this one spelling variant -- same kind of one-off fix
# lgbs_scraper.py/mvba_scraper.py already use for their own county-name
# mismatches (see their own COUNTY_NAME_OVERRIDES).
COUNTY_NAME_OVERRIDES = {"De Witt County": "DeWitt County"}


def latest_row_per_county(dataset: pd.DataFrame) -> pd.DataFrame:
    dataset = dataset.copy()
    dataset["year_month"] = pd.PeriodIndex(dataset["year_month"], freq="M")
    # Only need the features to be present -- the target columns don't
    # matter here, there's nothing to predict *against* for the current
    # month, only forward from it.
    usable = dataset.dropna(subset=FEATURE_COLS)
    latest_idx = usable.groupby("county")["year_month"].idxmax()
    return usable.loc[latest_idx].reset_index(drop=True)


def main():
    dataset = pd.read_csv(DATA_DIR / "county_month_dataset.csv")
    latest = latest_row_per_county(dataset)
    print(f"{len(latest)} counties with a usable current row.")

    predictions = {}
    for horizon in TARGET_HORIZONS:
        model = joblib.load(Path(__file__).parent / f"county_distress_model_{horizon}m.joblib")
        preds = model.predict(latest[FEATURE_COLS])
        for county, pred in zip(latest["county"], preds):
            predictions.setdefault(county, {})[f"change_{horizon}m"] = round(float(pred), 4)

    output = []
    for _, row in latest.iterrows():
        county = row["county"]
        output.append({
            "county": COUNTY_NAME_OVERRIDES.get(county, county),
            "as_of": str(row["year_month"]),
            "current_price_cut_pct": round(float(row["price_cut_pct"]), 4),
            **predictions[county],
        })

    dest = PUBLIC_DIR / "county_predictions.json"
    dest.write_text(json.dumps(output, indent=None, separators=(",", ":")))
    print(f"Wrote {len(output)} counties' predictions to {dest}")


if __name__ == "__main__":
    main()
