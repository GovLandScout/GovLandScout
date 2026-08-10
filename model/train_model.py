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

Validation is walk-forward, not one fixed split: N_CV_FOLDS rolling
folds, each training on everything before its own test window and
testing on the CV_TEST_WINDOW_MONTHS right after -- fold 2's training
data includes fold 1's test months, etc., the same way a real
deployment would keep accumulating history. A single train/test split
only tells you how the model did on one particular stretch of months;
walking it forward across several stretches (and reporting the spread
across them, not just the mean) is a much more honest read on how
consistent that performance actually is. A random split would be worse
than either -- it'd let the model train on some of a county's later
months and get tested on earlier ones from the same county, leaking
future information backward.

LinearRegression is trained alongside the random forest in every fold
as a benchmark -- the point isn't "linear regression is competitive,"
it's showing the forest was actually compared against something rather
than being the only thing tried.

After cross-validation, one final "production" random forest per
horizon is fit on *all* available data (there's no held-out test set to
protect at deployment time -- more real data only helps) and saved for
generate_predictions.py to use.

Run with a state key from states.py as the only CLI arg, e.g.
`python3 train_model.py pa` (defaults to tx).
"""

import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error

from states import STATES

DATA_DIR = Path(__file__).parent / "data"
TARGET_HORIZONS = [1, 3, 6]
N_CV_FOLDS = 4
CV_TEST_WINDOW_MONTHS = 3

FEATURE_COLS = [
    "price_cut_pct", "price_cut_pct_lag1", "price_cut_pct_lag3", "price_cut_pct_lag6",
    "price_cut_pct_roll3", "zhvi_mom_pct", "zhvi_yoy_pct", "inventory_mom_pct",
    "inventory_level", "unemployment_rate", "unemployment_rate_mom_change",
    "mortgage_rate", "mortgage_rate_mom_change",
    "month_sin", "month_cos",
]


def make_random_forest() -> RandomForestRegressor:
    return RandomForestRegressor(n_estimators=300, max_depth=10, min_samples_leaf=5, random_state=42, n_jobs=-1)


def make_gradient_boosting() -> HistGradientBoostingRegressor:
    # Added as a third CV comparison point alongside the random forest and
    # linear regression, not a production-model swap: generate_predictions.py's
    # uncertainty estimate (see predict_with_uncertainty() below) depends on
    # averaging across many *independently* bagged trees, which is what
    # RandomForestRegressor's estimators_ actually are. HistGradientBoostingRegressor's
    # trees are sequential and each corrects the last, so a spread across
    # them wouldn't carry the same "how much do independent trees agree"
    # meaning -- swapping production models would mean redesigning
    # uncertainty quantification too, not just picking a different
    # regressor, so that's left as a separate decision for later.
    return HistGradientBoostingRegressor(max_depth=6, learning_rate=0.05, max_iter=300, random_state=42)


def predict_with_uncertainty(model: RandomForestRegressor, X: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Mean prediction plus the standard deviation across the forest's
    individual trees -- see generate_predictions.py, which uses this same
    approach in production. Duplicated here (rather than imported) because
    train_model.py needs it to check *whether that uncertainty estimate is
    actually trustworthy* -- see evaluate_fold()'s coverage calculation --
    which generate_predictions.py has no reason to do itself."""
    X_values = X.values
    tree_predictions = np.array([tree.predict(X_values) for tree in model.estimators_])
    return tree_predictions.mean(axis=0), tree_predictions.std(axis=0)


def walk_forward_folds(months: list, n_folds: int, test_window: int) -> list[tuple[list, list]]:
    """Fold i trains on every month before its own test window, tests on
    the test_window months right after -- later folds simply have more
    training history, same as a real deployment accumulating months."""
    first_test_start = len(months) - n_folds * test_window
    folds = []
    for i in range(n_folds):
        test_start = first_test_start + i * test_window
        test_end = test_start + test_window
        folds.append((months[:test_start], months[test_start:test_end]))
    return folds


def evaluate_fold(train_df: pd.DataFrame, test_df: pd.DataFrame, target_col: str) -> dict:
    X_train, y_train = train_df[FEATURE_COLS], train_df[target_col]
    X_test, y_test = test_df[FEATURE_COLS], test_df[target_col]

    naive_mae = mean_absolute_error(y_test, pd.Series(0.0, index=y_test.index))

    rf = make_random_forest()
    rf.fit(X_train, y_train)
    # mean/std together rather than a plain rf.predict(): the MAE below
    # only needs the mean, but the coverage check right after needs the
    # per-row std too, and fitting a second time just to get it back would
    # waste half the CV's tree-fitting work for no reason.
    rf_pred_mean, rf_pred_std = predict_with_uncertainty(rf, X_test)
    rf_mae = mean_absolute_error(y_test, rf_pred_mean)

    # Is the tree-spread uncertainty generate_predictions.py ships to
    # /market-trends actually trustworthy, or just a number that looks
    # sciencey? A well-calibrated Gaussian-shaped spread should cover the
    # true outcome within +/-1 std about 68% of the time and +/-2 std
    # about 95% -- rates well below that mean the band is overconfident
    # (too narrow), well above means it's overcautious (too wide).
    residuals = (y_test.to_numpy() - rf_pred_mean)
    abs_residuals = np.abs(residuals)
    # A handful of rows can have rf_pred_std == 0 (every tree in the
    # forest happened to agree exactly) -- coverage at that row is just
    # whether the prediction was exact, not a divide-by-zero.
    coverage_1std = float(np.mean(abs_residuals <= rf_pred_std))
    coverage_2std = float(np.mean(abs_residuals <= 2 * rf_pred_std))

    gb = make_gradient_boosting()
    gb.fit(X_train, y_train)
    gb_mae = mean_absolute_error(y_test, gb.predict(X_test))

    lr = LinearRegression()
    lr.fit(X_train, y_train)
    lr_mae = mean_absolute_error(y_test, lr.predict(X_test))

    return {
        "naive_mae": naive_mae, "rf_mae": rf_mae, "lr_mae": lr_mae, "gb_mae": gb_mae,
        "rf_coverage_1std": coverage_1std, "rf_coverage_2std": coverage_2std,
        "test_rows": len(test_df),
    }


def cross_validate(dataset: pd.DataFrame, horizon: int) -> list[dict]:
    target_col = f"target_change_{horizon}m"
    usable = dataset.dropna(subset=[target_col])
    months = sorted(usable["year_month"].unique())

    fold_results = []
    for train_months, test_months in walk_forward_folds(months, N_CV_FOLDS, CV_TEST_WINDOW_MONTHS):
        train_df = usable[usable["year_month"].isin(train_months)]
        test_df = usable[usable["year_month"].isin(test_months)]
        if train_df.empty or test_df.empty:
            continue
        result = evaluate_fold(train_df, test_df, target_col)
        result["test_range"] = f"{test_months[0]}..{test_months[-1]}"
        fold_results.append(result)
    return fold_results


def fit_production_model(dataset: pd.DataFrame, horizon: int, state_key: str) -> dict:
    """The model generate_predictions.py actually uses -- trained on every
    row available, not held back from a test split, since there's no
    accuracy claim being protected here, just the best model deployable
    today."""
    target_col = f"target_change_{horizon}m"
    usable = dataset.dropna(subset=[target_col])

    model = make_random_forest()
    model.fit(usable[FEATURE_COLS], usable[target_col])

    model_path = Path(__file__).parent / f"county_distress_model_{state_key}_{horizon}m.joblib"
    joblib.dump(model, model_path)

    return {
        "rows": len(usable),
        "importances": pd.Series(model.feature_importances_, index=FEATURE_COLS).sort_values(ascending=False),
        "model_path": model_path,
    }


def summarize(fold_results: list[dict], key: str) -> tuple[float, float]:
    values = pd.Series([r[key] for r in fold_results])
    return values.mean(), values.std()


def main():
    state_key = sys.argv[1] if len(sys.argv) > 1 else "tx"
    state = STATES[state_key]

    dataset = pd.read_csv(DATA_DIR / f"{state_key}_county_month_dataset.csv")
    dataset["year_month"] = pd.PeriodIndex(dataset["year_month"], freq="M")

    for horizon in TARGET_HORIZONS:
        print(f"\n{'=' * 60}\n{state['name'].upper()} -- {horizon}-MONTH HORIZON\n{'=' * 60}")

        folds = cross_validate(dataset, horizon)
        print(f"\n{N_CV_FOLDS}-fold walk-forward cross-validation "
              f"({CV_TEST_WINDOW_MONTHS}-month test windows each):\n")
        print(f"{'Test months':<20}{'Rows':<8}{'Naive MAE':<12}{'Linear MAE':<13}{'RF MAE':<10}{'GB MAE':<10}")
        for r in folds:
            print(f"{r['test_range']:<20}{r['test_rows']:<8}{r['naive_mae']:<12.4f}"
                  f"{r['lr_mae']:<13.4f}{r['rf_mae']:<10.4f}{r['gb_mae']:<10.4f}")

        naive_mean, naive_std = summarize(folds, "naive_mae")
        lr_mean, lr_std = summarize(folds, "lr_mae")
        rf_mean, rf_std = summarize(folds, "rf_mae")
        gb_mean, gb_std = summarize(folds, "gb_mae")
        print(f"\n{'Mean ± std':<20}{'':<8}{naive_mean:.4f}±{naive_std:.4f}  "
              f"{lr_mean:.4f}±{lr_std:.4f}  {rf_mean:.4f}±{rf_std:.4f}  {gb_mean:.4f}±{gb_std:.4f}")
        print(f"\nRandom forest beats naive by {1 - rf_mean / naive_mean:.1%} on average across folds, "
              f"linear regression by {1 - lr_mean / naive_mean:.1%}, "
              f"gradient boosting by {1 - gb_mean / naive_mean:.1%}.")

        cov1_mean, _ = summarize(folds, "rf_coverage_1std")
        cov2_mean, _ = summarize(folds, "rf_coverage_2std")
        print(f"\nRandom forest uncertainty calibration (see predict_with_uncertainty()): "
              f"{cov1_mean:.0%} of actual outcomes fell within the predicted ±1 std band "
              f"(well-calibrated target: ~68%), {cov2_mean:.0%} within ±2 std (target: ~95%).")

        production = fit_production_model(dataset, horizon, state_key)
        print(f"\nProduction model trained on all {production['rows']:,} available rows "
              f"(saved to {production['model_path'].name}).")
        print("Feature importances:")
        for feature, importance in production["importances"].items():
            print(f"  {feature:<32} {importance:.3f}")


if __name__ == "__main__":
    main()
