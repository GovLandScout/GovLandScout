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


def predict_with_interval(
    models: dict, X: pd.DataFrame, delta68: float, delta95: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(mean, std68, std95) from the bundled {"mean", "lower68", "upper68",
    "lower95", "upper95"} quantile models train_model.py's
    fit_gb_production_model() saves -- see that function and
    calibrate_gb_quantiles() for where this comes from. delta68/delta95
    widen the raw quantile predictions the same way train_model.py's own
    coverage check found necessary (Conformalized Quantile Regression,
    not a new model -- see calibrate_gb_quantiles()'s docstring).

    The calibrated interval [lower68, upper68] isn't guaranteed centered
    on the mean prediction -- quantile regression has no reason to be
    symmetric -- but /market-trends draws one symmetric ± band per
    horizon (see web.py's buildTrendSvg), not two separate edges. std68
    here is the *larger* of the two distances from mean to each edge, so
    the symmetric band drawn from it always fully contains the real,
    possibly-asymmetric calibrated interval rather than clipping
    whichever side happens to be farther from the mean -- conservative
    (occasionally wider than the tightest possible band) by design, not
    an oversight."""
    mean = models["mean"].predict(X)
    lower68 = models["lower68"].predict(X) - delta68
    upper68 = models["upper68"].predict(X) + delta68
    lower95 = models["lower95"].predict(X) - delta95
    upper95 = models["upper95"].predict(X) + delta95
    std68 = np.maximum(mean - lower68, upper68 - mean)
    std95 = np.maximum(mean - lower95, upper95 - mean)
    return mean, std68, std95


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

    # gb.delta68/delta95 per horizon, fit by train_model.py's
    # calibrate_gb_quantiles() on walk-forward CV residuals -- the raw
    # quantile-model interval below is measurably overconfident the same
    # way RF's old tree-spread was (see model/README.md's "Uncertainty
    # calibration" results), so it's widened here before shipping rather
    # than sent as-is. See that function's docstring for why a delta fit
    # on held-out CV rows is a fair calibration, not a circular one.
    # rf.c68/c95 also live in this same file but are no longer read here --
    # kept only as train_model.py's own comparison-table reference now
    # that gradient boosting, not the random forest, is the production
    # model (see train_model.py's module docstring for that history).
    calibration = json.loads((Path(__file__).parent / f"county_distress_calibration_{state_key}.json").read_text())

    predictions = {}
    for horizon in TARGET_HORIZONS:
        models = joblib.load(Path(__file__).parent / f"county_distress_model_{state_key}_{horizon}m.joblib")
        gb_calibration = calibration[str(horizon)]["gb"]
        means, stds, stds95 = predict_with_interval(
            models, latest[FEATURE_COLS], gb_calibration["delta68"], gb_calibration["delta95"],
        )
        for county, mean, std, std95 in zip(latest["county"], means, stds, stds95):
            predictions.setdefault(county, {})[f"change_{horizon}m"] = round(float(mean), 4)
            # Calibrated to ~68%/~95% historical coverage, not the raw
            # quantile-model interval -- see the calibration comment above.
            predictions[county][f"change_{horizon}m_std"] = round(float(std), 4)
            predictions[county][f"change_{horizon}m_std95"] = round(float(std95), 4)

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
