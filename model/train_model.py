"""
GovLandScout model (Phase 1) - Training and evaluation

Trains a RandomForestRegressor to predict next month's price-cut share
per Texas county, and reports it against a naive persistence baseline
(predict no change from this month) -- the real question for a first
pass isn't "is the RMSE some abstract good number," it's "does this
actually beat doing nothing."

Split is time-based, not a random shuffle: the last HOLDOUT_FRACTION of
months (by calendar order) are held out as the test set. A random split
would let the model train on some of a county's later months and get
tested on earlier ones from the same county -- leaking future
information backward and overstating how well this would really
predict the *next* unseen month.
"""

from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error

DATA_DIR = Path(__file__).parent / "data"
HOLDOUT_FRACTION = 0.2

FEATURE_COLS = [
    "price_cut_pct", "price_cut_pct_lag1", "price_cut_pct_lag3", "price_cut_pct_lag6",
    "price_cut_pct_roll3", "zhvi_mom_pct", "zhvi_yoy_pct", "inventory_mom_pct",
    "inventory_level", "unemployment_rate", "unemployment_rate_mom_change", "month_of_year",
]
TARGET_COL = "target_next_price_cut_pct"


def time_based_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    months = sorted(df["year_month"].unique())
    cutoff_idx = int(len(months) * (1 - HOLDOUT_FRACTION))
    cutoff_month = months[cutoff_idx]
    train = df[df["year_month"] < cutoff_month]
    test = df[df["year_month"] >= cutoff_month]
    return train, test, cutoff_month


def evaluate(y_true, y_pred, label: str) -> None:
    rmse = mean_squared_error(y_true, y_pred) ** 0.5
    mae = mean_absolute_error(y_true, y_pred)
    print(f"  {label}: RMSE={rmse:.3f}  MAE={mae:.3f}")


def main():
    dataset = pd.read_csv(DATA_DIR / "county_month_dataset.csv")
    dataset["year_month"] = pd.PeriodIndex(dataset["year_month"], freq="M")

    train, test, cutoff_month = time_based_split(dataset)
    print(f"Train: {len(train):,} rows through {train['year_month'].max()}")
    print(f"Test:  {len(test):,} rows from {cutoff_month} onward "
          f"({test['county'].nunique()} counties)\n")

    X_train, y_train = train[FEATURE_COLS], train[TARGET_COL]
    X_test, y_test = test[FEATURE_COLS], test[TARGET_COL]

    model = RandomForestRegressor(
        n_estimators=300, max_depth=10, min_samples_leaf=5,
        random_state=42, n_jobs=-1,
    )
    model.fit(X_train, y_train)
    predictions = model.predict(X_test)

    # Naive baseline: "next month looks like this month" -- price_cut_pct
    # *is* this month's value, already a feature, so it doubles as the
    # baseline prediction directly.
    naive_predictions = test["price_cut_pct"]

    print("Results on held-out months (lower is better):")
    evaluate(y_test, naive_predictions, "Naive (persistence) baseline")
    evaluate(y_test, predictions, "Random forest             ")

    improvement = 1 - (mean_absolute_error(y_test, predictions) / mean_absolute_error(y_test, naive_predictions))
    print(f"\n  Random forest improves MAE over the naive baseline by {improvement:.1%}")

    print("\nFeature importances:")
    importances = pd.Series(model.feature_importances_, index=FEATURE_COLS).sort_values(ascending=False)
    for feature, importance in importances.items():
        print(f"  {feature:<32} {importance:.3f}")

    model_path = Path(__file__).parent / "county_distress_model.joblib"
    joblib.dump(model, model_path)
    print(f"\nModel saved to {model_path}")


if __name__ == "__main__":
    main()
