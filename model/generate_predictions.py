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

# Matches build_dataset.py's/train_model.py's own copy of this list --
# see ZHVI_FEATURE_COLS in either for why it's a real, separate list, not
# FEATURE_COLS with a couple columns dropped at call time.
ZHVI_FEATURE_COLS = [
    "zhvi_mom_pct", "zhvi_yoy_pct", "inventory_mom_pct", "inventory_level",
    "mortgage_rate", "mortgage_rate_mom_change",
    "month_sin", "month_cos",
]

# Zillow's county names and the Census geometry's names match exactly
# except for these spelling variants -- same kind of one-off fix
# lgbs_scraper.py/mvba_scraper.py already use for their own county-name
# mismatches (see their own COUNTY_NAME_OVERRIDES). Shared across states
# since it's just a flat name->name lookup, not worth splitting up.
COUNTY_NAME_OVERRIDES = {"De Witt County": "DeWitt County"}


def latest_row_per_county(dataset: pd.DataFrame, feature_cols: list[str] = FEATURE_COLS) -> pd.DataFrame:
    # Only need the features to be present -- the target columns don't
    # matter here, there's nothing to predict *against* for the current
    # month, only forward from it.
    usable = dataset.dropna(subset=feature_cols)
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


def recent_history_by_county(dataset: pd.DataFrame, value_col: str = "price_cut_pct") -> dict[str, list[list]]:
    """value_col defaults to price_cut_pct (the primary model's own
    current-value/history unit) -- the ZHVI gap-filler passes
    "zhvi_mom_pct" instead, so a gap county's history chart is in the same
    units as its own current value and predictions, not mixed with the
    primary model's."""
    history = {}
    for county, group in dataset.groupby("county"):
        recent = group.sort_values("year_month").tail(HISTORY_MONTHS)
        history[county] = [
            [str(row["year_month"]), round(float(row[value_col]), 4)]
            for _, row in recent.iterrows()
        ]
    return history


def build_county_rows(
    state_key: str, dataset_suffix: str, model_suffix: str, feature_cols: list[str],
    current_value_col: str, metric_name: str, only_counties: set[str] | None = None,
) -> dict[str, dict]:
    """{county: output row} for one metric (see METRICS in train_model.py --
    duplicated here as explicit args rather than imported, same reasoning
    as predict_with_interval() already duplicating train_model.py's own
    logic: this module reads what train_model.py wrote, it doesn't need
    that module's training-time machinery, just its file-naming
    convention). only_counties restricts output to that set (used for the
    ZHVI gap-filler -- see main() -- so it only fills the specific
    counties the primary price-cut metric didn't already cover, rather
    than recomputing every county gap-filler-style even where the more
    directly relevant price-cut prediction already exists)."""
    dataset_path = DATA_DIR / f"{state_key}_county_month_dataset{dataset_suffix}.csv"
    dataset = pd.read_csv(dataset_path)
    dataset["year_month"] = pd.PeriodIndex(dataset["year_month"], freq="M")

    latest = latest_row_per_county(dataset, feature_cols)
    if only_counties is not None:
        latest = latest[latest["county"].isin(only_counties)].reset_index(drop=True)
    if latest.empty:
        return {}

    history = recent_history_by_county(dataset, current_value_col)
    calibration = json.loads(
        (Path(__file__).parent / f"county_distress_calibration_{state_key}{dataset_suffix}.json").read_text()
    )

    predictions: dict[str, dict] = {}
    for horizon in TARGET_HORIZONS:
        models = joblib.load(Path(__file__).parent / f"county_distress_model_{state_key}_{horizon}m{model_suffix}.joblib")
        gb_calibration = calibration[str(horizon)]["gb"]
        means, stds, stds95 = predict_with_interval(
            models, latest[feature_cols], gb_calibration["delta68"], gb_calibration["delta95"],
        )
        for county, mean, std, std95 in zip(latest["county"], means, stds, stds95):
            predictions.setdefault(county, {})[f"change_{horizon}m"] = round(float(mean), 4)
            predictions[county][f"change_{horizon}m_std"] = round(float(std), 4)
            predictions[county][f"change_{horizon}m_std95"] = round(float(std95), 4)

    rows = {}
    for _, row in latest.iterrows():
        county = row["county"]
        rows[county] = {
            "county": COUNTY_NAME_OVERRIDES.get(county, county),
            "metric": metric_name,
            "as_of": str(row["year_month"]),
            "current_value": round(float(row[current_value_col]), 4),
            "history": history.get(county, []),
            **predictions[county],
        }
    return rows


def main():
    state_key = sys.argv[1] if len(sys.argv) > 1 else "tx"
    state = STATES[state_key]

    # Primary model first -- price-cut share, whatever counties Zillow
    # publishes it for (see build_dataset.py's build_panel()). gb.delta68/
    # delta95 per horizon, fit by train_model.py's calibrate_gb_quantiles()
    # on walk-forward CV residuals -- the raw quantile-model interval is
    # measurably overconfident the same way RF's old tree-spread was (see
    # model/README.md's "Uncertainty calibration" results), so
    # predict_with_interval() widens it before shipping rather than
    # sending it as-is. rf.c68/c95 also live in that calibration file but
    # are no longer read here -- kept only as train_model.py's own
    # comparison-table reference now that gradient boosting, not the
    # random forest, is the production model.
    price_cut_rows = build_county_rows(
        state_key, dataset_suffix="", model_suffix="", feature_cols=FEATURE_COLS,
        current_value_col="price_cut_pct", metric_name="price_cut",
    )
    print(f"{len(price_cut_rows)} {state['name']} counties with a usable price-cut row.")

    # ZHVI gap-filler second, restricted to counties the price-cut pass
    # above didn't already produce a row for -- price-cut is the more
    # directly relevant signal ("did sellers actually cut asking prices")
    # wherever Zillow publishes it at all; ZHVI-decline (see
    # build_dataset.py's engineer_features()) only fills in the counties
    # it doesn't, not a second opinion on the ones it does. Silently
    # produces nothing if this state's ZHVI gap-filler was never trained
    # (no county_distress_calibration_{state}_zhvi.json on disk) --
    # train_model.py's own train_zhvi_gap_filler() prints a similar note
    # when there's no dataset to train it from in the first place.
    zhvi_calibration_path = Path(__file__).parent / f"county_distress_calibration_{state_key}_zhvi.json"
    gap_filler_rows: dict[str, dict] = {}
    if zhvi_calibration_path.exists():
        all_zhvi_counties = set(
            pd.read_csv(DATA_DIR / f"{state_key}_county_month_dataset_zhvi.csv", usecols=["county"])["county"]
        )
        gap_counties = all_zhvi_counties - set(price_cut_rows.keys())
        gap_filler_rows = build_county_rows(
            state_key, dataset_suffix="_zhvi", model_suffix="_zhvi", feature_cols=ZHVI_FEATURE_COLS,
            current_value_col="zhvi_mom_pct", metric_name="zhvi_decline", only_counties=gap_counties,
        )
        print(f"{len(gap_filler_rows)} more {state['name']} counties filled in from the ZHVI gap-filler "
              f"(no price-cut data available for these -- see build_dataset.py's build_panel()).")
    else:
        print(f"(No {zhvi_calibration_path.name} found -- skipping the ZHVI gap-filler. "
              f"Run train_model.py {state_key} to produce it.)")

    all_rows = {**price_cut_rows, **gap_filler_rows}
    output = list(all_rows.values())

    dest = PUBLIC_DIR / f"{state_key}_county_predictions.json"
    dest.write_text(json.dumps(output, indent=None, separators=(",", ":")))
    print(f"Wrote {len(output)} counties' predictions to {dest} ({dest.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
