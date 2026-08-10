"""
GovLandScout model (Phase 1) - Generate current predictions

Runs the three trained horizon models (see train_model.py) against each
county's most recent usable row to produce "here's what the model
currently expects for this county over the next 1/3/6 months," and
writes it to public/county_predictions.json -- along with an
uncertainty estimate per prediction and each county's recent price-cut
history, both added specifically so /market-trends can show more than
a single bare number per county (see web.py's market_trends_page()).

That file -- not this script, and not the trained .joblib models it
depends on -- is what web.py actually reads. web.py's own environment
(Render) never installs pandas/scikit-learn; this stays a manual/
periodic step run from here, same as fetch_data.py/build_dataset.py/
train_model.py, with its small JSON output committed to the repo.

Static, not live: this doesn't run on a schedule, so the map reflects
whenever this was last run, not today's date. Good enough for a
research/portfolio feature -- re-run by hand after retraining.

Run with a state key from states.py as the only CLI arg, e.g.
`python3 generate_predictions.py pa` (defaults to tx).
"""

import json
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from states import STATES

DATA_DIR = Path(__file__).parent / "data"
PUBLIC_DIR = Path(__file__).parent / "public"
TARGET_HORIZONS = [1, 3, 6]
HISTORY_MONTHS = 24  # how much trailing history to ship per county for the drill-down chart

FEATURE_COLS = [
    "price_cut_pct", "price_cut_pct_lag1", "price_cut_pct_lag3", "price_cut_pct_lag6",
    "price_cut_pct_roll3", "zhvi_mom_pct", "zhvi_yoy_pct", "inventory_mom_pct",
    "inventory_level", "unemployment_rate", "unemployment_rate_mom_change",
    "mortgage_rate", "mortgage_rate_mom_change",
    "month_sin", "month_cos",  # see build_dataset.py's engineer_features() for why not a raw month number
]

# Zillow's county names and the Census geometry's names match exactly
# except for these spelling variants -- same kind of one-off fix
# lgbs_scraper.py/mvba_scraper.py already use for their own county-name
# mismatches (see their own COUNTY_NAME_OVERRIDES). Shared across states
# since it's just a flat name->name lookup, not worth splitting up.
COUNTY_NAME_OVERRIDES = {"De Witt County": "DeWitt County"}


def latest_row_per_county(dataset: pd.DataFrame) -> pd.DataFrame:
    # Only need the features to be present -- the target columns don't
    # matter here, there's nothing to predict *against* for the current
    # month, only forward from it.
    usable = dataset.dropna(subset=FEATURE_COLS)
    latest_idx = usable.groupby("county")["year_month"].idxmax()
    return usable.loc[latest_idx].reset_index(drop=True)


def predict_with_uncertainty(model, X: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Mean prediction (what model.predict() already gives) plus the
    standard deviation across the forest's individual trees -- a real,
    (almost) free uncertainty estimate a single-model forecast doesn't
    have on its own. Trees that broadly agree imply a more reliable
    prediction than trees that are all over the place for that row.
    Whether this spread is actually trustworthy (i.e. calibrated, not just
    a number that looks sciencey) is checked in train_model.py's own copy
    of this function -- see its evaluate_fold() coverage calculation and
    model/README.md's "Uncertainty calibration" results."""
    # .values, not the DataFrame itself -- each tree was fit as part of the
    # ensemble without its own column-name tracking, and predicting from a
    # named DataFrame directly against it is a harmless but noisy mismatch
    # sklearn warns about on every single tree otherwise.
    X_values = X.values
    tree_predictions = np.array([tree.predict(X_values) for tree in model.estimators_])
    return tree_predictions.mean(axis=0), tree_predictions.std(axis=0)


def recent_history_by_county(dataset: pd.DataFrame) -> dict[str, list[list]]:
    history = {}
    for county, group in dataset.groupby("county"):
        recent = group.sort_values("year_month").tail(HISTORY_MONTHS)
        history[county] = [
            [str(row["year_month"]), round(float(row["price_cut_pct"]), 4)]
            for _, row in recent.iterrows()
        ]
    return history


def main():
    state_key = sys.argv[1] if len(sys.argv) > 1 else "tx"
    state = STATES[state_key]

    dataset = pd.read_csv(DATA_DIR / f"{state_key}_county_month_dataset.csv")
    dataset["year_month"] = pd.PeriodIndex(dataset["year_month"], freq="M")

    latest = latest_row_per_county(dataset)
    print(f"{len(latest)} {state['name']} counties with a usable current row.")

    history = recent_history_by_county(dataset)

    predictions = {}
    for horizon in TARGET_HORIZONS:
        model = joblib.load(Path(__file__).parent / f"county_distress_model_{state_key}_{horizon}m.joblib")
        means, stds = predict_with_uncertainty(model, latest[FEATURE_COLS])
        for county, mean, std in zip(latest["county"], means, stds):
            predictions.setdefault(county, {})[f"change_{horizon}m"] = round(float(mean), 4)
            predictions[county][f"change_{horizon}m_std"] = round(float(std), 4)

    output = []
    for _, row in latest.iterrows():
        county = row["county"]
        output.append({
            "county": COUNTY_NAME_OVERRIDES.get(county, county),
            "as_of": str(row["year_month"]),
            "current_price_cut_pct": round(float(row["price_cut_pct"]), 4),
            "history": history.get(county, []),
            **predictions[county],
        })

    dest = PUBLIC_DIR / f"{state_key}_county_predictions.json"
    dest.write_text(json.dumps(output, indent=None, separators=(",", ":")))
    print(f"Wrote {len(output)} counties' predictions to {dest} ({dest.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
