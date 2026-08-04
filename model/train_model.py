"""
GovLandScout model (Phase 1) - Training and evaluation

Trains a separate RandomForestRegressor per horizon (1, 3, and 6 months
ahead) predicting *change* in a Texas county's price-cut share, not the
raw next-period level -- see build_dataset.py's engineer_features() for
why: predicting the level let the model mostly just copy this month's
value forward, since price_cut_pct is highly autocorrelated month to
month. The honest baseline for a change target is "predict zero
change," and each horizon is reported against that baseline separately,
since 1-month and 6-month dynamics aren't the same prediction problem
-- short-term is expected to lean on current momentum, longer-term on
the macro features (unemployment, ZHVI trend) actually having time to
matter.

Split is time-based, not a random shuffle: the last HOLDOUT_FRACTION of
months (by calendar order) are held out as the test set. A random split
would let the model train on some of a county's later months and get
tested on earlier ones from the same county -- leaking future
information backward and overstating how well this would really
predict the *next* unseen month.
"""

from pathlib import Path

import joblib
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error

DATA_DIR = Path(__file__).parent / "data"
HOLDOUT_FRACTION = 0.2
TARGET_HORIZONS = [1, 3, 6]

FEATURE_COLS = [
    "price_cut_pct", "price_cut_pct_lag1", "price_cut_pct_lag3", "price_cut_pct_lag6",
    "price_cut_pct_roll3", "zhvi_mom_pct", "zhvi_yoy_pct", "inventory_mom_pct",
    "inventory_level", "unemployment_rate", "unemployment_rate_mom_change", "month_of_year",
]


def time_based_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, object]:
    months = sorted(df["year_month"].unique())
    cutoff_idx = int(len(months) * (1 - HOLDOUT_FRACTION))
    cutoff_month = months[cutoff_idx]
    train = df[df["year_month"] < cutoff_month]
    test = df[df["year_month"] >= cutoff_month]
    return train, test, cutoff_month


def train_and_evaluate(dataset: pd.DataFrame, horizon: int) -> dict:
    target_col = f"target_change_{horizon}m"
    usable = dataset.dropna(subset=[target_col])
    train, test, cutoff_month = time_based_split(usable)

    X_train, y_train = train[FEATURE_COLS], train[target_col]
    X_test, y_test = test[FEATURE_COLS], test[target_col]

    model = RandomForestRegressor(
        n_estimators=300, max_depth=10, min_samples_leaf=5,
        random_state=42, n_jobs=-1,
    )
    model.fit(X_train, y_train)
    predictions = model.predict(X_test)

    # "Predict zero change" -- the honest naive baseline once the target
    # is a change, not a level (see module docstring).
    naive_predictions = pd.Series(0.0, index=y_test.index)

    naive_mae = mean_absolute_error(y_test, naive_predictions)
    model_mae = mean_absolute_error(y_test, predictions)
    naive_rmse = mean_squared_error(y_test, naive_predictions) ** 0.5
    model_rmse = mean_squared_error(y_test, predictions) ** 0.5

    model_path = Path(__file__).parent / f"county_distress_model_{horizon}m.joblib"
    joblib.dump(model, model_path)

    return {
        "horizon": horizon,
        "train_rows": len(train),
        "test_rows": len(test),
        "test_from": cutoff_month,
        "naive_mae": naive_mae,
        "model_mae": model_mae,
        "naive_rmse": naive_rmse,
        "model_rmse": model_rmse,
        "improvement": 1 - (model_mae / naive_mae) if naive_mae else float("nan"),
        "importances": pd.Series(model.feature_importances_, index=FEATURE_COLS).sort_values(ascending=False),
        "model_path": model_path,
    }


def main():
    dataset = pd.read_csv(DATA_DIR / "county_month_dataset.csv")
    dataset["year_month"] = pd.PeriodIndex(dataset["year_month"], freq="M")

    results = [train_and_evaluate(dataset, h) for h in TARGET_HORIZONS]

    print("Results per horizon (change in price-cut share, lower error is better):\n")
    print(f"{'Horizon':<10}{'Train rows':<12}{'Test rows':<11}{'Naive MAE':<11}{'RF MAE':<9}{'Improvement':<12}")
    for r in results:
        print(
            f"{r['horizon']}mo{'':<7}{r['train_rows']:<12,}{r['test_rows']:<11,}"
            f"{r['naive_mae']:<11.4f}{r['model_mae']:<9.4f}{r['improvement']:<12.1%}"
        )

    for r in results:
        print(f"\n--- {r['horizon']}-month horizon: feature importances ---")
        for feature, importance in r["importances"].items():
            print(f"  {feature:<32} {importance:.3f}")
        print(f"  (model saved to {r['model_path'].name})")


if __name__ == "__main__":
    main()
