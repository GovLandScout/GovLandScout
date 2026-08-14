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

Before that comparison runs, random_search_rf() tunes the random forest's
own hyperparameters per horizon -- n_estimators/max_depth/min_samples_leaf/
min_samples_split/max_features, sampled from RF_SEARCH_SPACE below and
scored by mean MAE across the *same* walk-forward folds, not sklearn's
own RandomizedSearchCV. That matters here specifically because
RandomizedSearchCV's default k-fold shuffles a county's later months into
the same fold as its earlier ones -- the exact future-information leak
walk-forward CV above exists to avoid, so reusing sklearn's random-search
machinery on top of hand-rolled walk-forward folds would quietly undo the
whole reason walk-forward was chosen. The winning config feeds every RF
fit downstream (the comparison table, the coverage check, and the
production model itself), not just a tuning side-quest reported and
ignored -- see model/README.md's "Hyperparameter search" section for
whether this actually fixed the Texas 6-month weakness that section had
previously documented (RF beating naive by only 36.1%, worst of any
horizon/state).

random_search_gb() does the same thing for the gradient boosting
comparison point, over its own GB_SEARCH_SPACE (max_iter/learning_rate/
max_depth/max_leaf_nodes/min_samples_leaf/l2_regularization) -- originally
tuned only for an honest CV comparison, back when GB wasn't a production
candidate. Two full hyperparameter-search passes later (see
model/README.md's "Hyperparameter search" for both), gradient boosting
won at every Texas horizon and most Pennsylvania ones on a fairly-tuned
comparison against an equally-tuned random forest -- a real, earned
result, not an artifact of only one side getting attention. GB is now
the production model.

That required actually solving the problem the random forest's
production role had been quietly resting on: GB's sequential trees can't
produce RandomForestRegressor's tree-spread uncertainty estimate (see
make_gradient_boosting()'s docstring). fit_gb_production_model() below
uses quantile regression instead -- HistGradientBoostingRegressor
supports loss="quantile", so fitting four extra models per horizon at
GB_QUANTILES' quantiles (0.16/0.84 for a 68% interval, 0.025/0.975 for
95%) alongside the usual mean-loss model gives a real prediction
interval, not a proxy for one. calibrate_gb_quantiles() then checks
those intervals the same way calibrate_uncertainty() checked RF's tree
spread -- via held-out walk-forward CV residuals -- and widens them by
whatever a conformal calibration set says is needed to actually hit the
target coverage (Conformalized Quantile Regression, Romano et al. 2019:
principled, not this project's own invention). Same "checked, not just
asserted" standard the tree-spread approach was held to.

One real gap this leaves: HistGradientBoostingRegressor has no
`feature_importances_` (unlike RandomForestRegressor's impurity-based
one) -- fit_gb_production_model() uses sklearn's permutation_importance
instead, a different technique (how much does shuffling one feature
hurt predictions, measured on the training data) that isn't numerically
comparable to the old RF importances title-for-title, just the closest
available equivalent question for this model class.

After cross-validation, the final production models -- one mean-loss GB
plus four quantile-loss GB per horizon, all fit on *all* available data
(there's no held-out test set to protect at deployment time -- more real
data only helps) -- are bundled into one joblib file per horizon for
generate_predictions.py to use.

Run with a state key from states.py as the only CLI arg, e.g.
`python3 train_model.py pa` (defaults to tx).
"""

import json
import random
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.inspection import permutation_importance
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


# The hand-picked config every RF fit used before random_search_rf()
# existed -- also candidate zero in every search below, so tuning can
# only ever match or beat this, never do worse by bad luck.
DEFAULT_RF_PARAMS = {"n_estimators": 300, "max_depth": 10, "min_samples_leaf": 5}

# Deliberately doesn't touch max_features's sklearn default (1.0, i.e.
# every feature considered at every split) as its own baseline value below
# 1.0 -- with only 15 features to begin with (see FEATURE_COLS), limiting
# a split to a random subset of them is a real, testable regularization
# lever, not a value nobody would reasonably pick.
#
# Densened (model/README.md's "Hyperparameter search" section originally
# flagged this as the natural follow-up) from the original 5/7/6/4/6-value
# grids -- every value that was already here stays, this only fills in the
# gaps between them, so it can only ever find something the coarser
# version couldn't, not lose a candidate the original search had access
# to. Left deliberately still spanning the same full range rather than
# narrowed around the winning configs found with the coarser grid -- doing
# that would bias this search toward confirming the last one's answer
# instead of honestly re-checking it.
RF_SEARCH_SPACE = {
    "n_estimators": [100, 150, 200, 250, 300, 350, 400, 450, 500],
    "max_depth": [4, 6, 8, 10, 12, 15, 18, 20, 25, None],
    "min_samples_leaf": [1, 2, 3, 5, 8, 10, 15, 20, 25],
    "min_samples_split": [2, 4, 5, 8, 10, 15, 20, 25],
    "max_features": ["sqrt", "log2", 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
}

RANDOM_SEARCH_ITER = 30  # doubled from 15 alongside the denser grids above -- same reasoning, more thorough coverage of a now-larger space
RANDOM_SEARCH_SEED = 42  # same seed as every other random_state here -- a rerun reproduces the same search


def make_random_forest(params: dict | None = None) -> RandomForestRegressor:
    return RandomForestRegressor(**(params or DEFAULT_RF_PARAMS), random_state=42, n_jobs=-1)


def sample_rf_params(rng: random.Random) -> dict:
    return {name: rng.choice(values) for name, values in RF_SEARCH_SPACE.items()}


# The hand-picked config every GB fit used before random_search_gb()
# existed -- also candidate zero in every GB search below, same reasoning
# as DEFAULT_RF_PARAMS.
DEFAULT_GB_PARAMS = {"max_depth": 6, "learning_rate": 0.05, "max_iter": 300}

# max_leaf_nodes/min_samples_leaf/l2_regularization left untouched by the
# original hand-picked config (sklearn defaults: 31, 20, 0.0) -- included
# here since they're the standard levers for controlling how much an
# individual boosting stage can overfit, the same kind of regularization
# RF_SEARCH_SPACE's min_samples_leaf/min_samples_split/max_features tune
# on the forest side. Densened the same way and for the same reason as
# RF_SEARCH_SPACE above -- see its own comment.
GB_SEARCH_SPACE = {
    "max_iter": [100, 150, 200, 250, 300, 350, 400, 450, 500],
    "learning_rate": [0.01, 0.02, 0.03, 0.05, 0.07, 0.1, 0.15, 0.2],
    "max_depth": [3, 4, 5, 6, 8, 10, 12, None],
    "max_leaf_nodes": [7, 15, 31, 47, 63, 95, 127],
    "min_samples_leaf": [5, 10, 15, 20, 25, 30, 40, 50],
    "l2_regularization": [0.0, 0.05, 0.1, 0.3, 0.5, 0.7, 1.0],
}


def make_gradient_boosting(params: dict | None = None) -> HistGradientBoostingRegressor:
    # Mean-loss (squared_error, sklearn's default) point estimate. GB's
    # sequential trees can't produce RandomForestRegressor's tree-spread
    # uncertainty estimate the way predict_with_uncertainty() gets it from
    # independently-bagged trees -- see GB_QUANTILES and
    # fit_gb_production_model() below for what actually replaces it now
    # that GB is the production model, not just a CV comparison point.
    return HistGradientBoostingRegressor(**(params or DEFAULT_GB_PARAMS), random_state=42)


def sample_gb_params(rng: random.Random) -> dict:
    return {name: rng.choice(values) for name, values in GB_SEARCH_SPACE.items()}


# (lower, upper) pairs bracketing a 68%- and 95%-coverage interval around
# the median, the same two coverage levels RF's tree-spread calibration
# targeted (see calibrate_uncertainty()) -- e.g. a well-calibrated
# [0.16, 0.84] interval contains the true value 84%-16% = 68% of the time.
GB_QUANTILES = {"lower68": 0.16, "upper68": 0.84, "lower95": 0.025, "upper95": 0.975}


def make_gb_quantile_model(quantile: float, params: dict | None = None) -> HistGradientBoostingRegressor:
    """Same hyperparameters random_search_gb() found for the mean-loss
    model (n_estimators/max_depth/etc.), just swapped to quantile loss --
    a deliberate simplification, not an oversight: independently
    re-tuning each of the four quantile models would need its own random
    search per quantile per horizon per state (4x the search cost this
    project has already run twice), and the mean model's regularization
    strength is a reasonable starting assumption for nearby quantiles of
    the same target. calibrate_gb_quantiles() below is what actually
    corrects for this being an approximation, not this function."""
    config = dict(params or DEFAULT_GB_PARAMS)
    return HistGradientBoostingRegressor(**config, loss="quantile", quantile=quantile, random_state=42)


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


def evaluate_fold(
    train_df: pd.DataFrame, test_df: pd.DataFrame, target_col: str,
    rf_params: dict | None = None, gb_params: dict | None = None,
) -> dict:
    X_train, y_train = train_df[FEATURE_COLS], train_df[target_col]
    X_test, y_test = test_df[FEATURE_COLS], test_df[target_col]

    naive_mae = mean_absolute_error(y_test, pd.Series(0.0, index=y_test.index))

    rf = make_random_forest(rf_params)
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

    gb = make_gradient_boosting(gb_params)
    gb.fit(X_train, y_train)
    gb_mae = mean_absolute_error(y_test, gb.predict(X_test))

    # GB's own uncertainty interval -- one quantile model per GB_QUANTILES
    # entry, all sharing gb_params (see make_gb_quantile_model's docstring
    # for why), fit here rather than only in fit_gb_production_model() so
    # calibrate_gb_quantiles() below has real held-out predictions to
    # check coverage against, the same held-out-CV standard
    # calibrate_uncertainty() already holds RF's tree spread to.
    gb_quantile_preds = {
        name: make_gb_quantile_model(q, gb_params).fit(X_train, y_train).predict(X_test)
        for name, q in GB_QUANTILES.items()
    }
    gb_score_68 = np.maximum(
        gb_quantile_preds["lower68"] - y_test.to_numpy(), y_test.to_numpy() - gb_quantile_preds["upper68"]
    )
    gb_score_95 = np.maximum(
        gb_quantile_preds["lower95"] - y_test.to_numpy(), y_test.to_numpy() - gb_quantile_preds["upper95"]
    )
    gb_coverage_68 = float(np.mean(gb_score_68 <= 0))
    gb_coverage_95 = float(np.mean(gb_score_95 <= 0))

    lr = LinearRegression()
    lr.fit(X_train, y_train)
    lr_mae = mean_absolute_error(y_test, lr.predict(X_test))

    return {
        "naive_mae": naive_mae, "rf_mae": rf_mae, "lr_mae": lr_mae, "gb_mae": gb_mae,
        "rf_coverage_1std": coverage_1std, "rf_coverage_2std": coverage_2std,
        "gb_coverage_68": gb_coverage_68, "gb_coverage_95": gb_coverage_95,
        "test_rows": len(test_df),
        # Raw per-row arrays, not just the aggregated coverage rates above --
        # calibrate_uncertainty()/calibrate_gb_quantiles() below need the
        # individual per-row values pooled across every fold to fit their
        # calibration factors, and refitting a second time just to get them
        # back would waste this fold's work for no reason.
        "abs_residuals": abs_residuals, "pred_std": rf_pred_std,
        # CQR's own conformity score (see calibrate_gb_quantiles()) is
        # exactly "how far outside the raw interval did the true value
        # land" -- positive means outside, so these are what that
        # calibration step needs, not the raw quantile predictions
        # themselves.
        "gb_score_68": gb_score_68, "gb_score_95": gb_score_95,
    }


def cross_validate(
    dataset: pd.DataFrame, horizon: int,
    rf_params: dict | None = None, gb_params: dict | None = None,
) -> list[dict]:
    target_col = f"target_change_{horizon}m"
    usable = dataset.dropna(subset=[target_col])
    months = sorted(usable["year_month"].unique())

    fold_results = []
    for train_months, test_months in walk_forward_folds(months, N_CV_FOLDS, CV_TEST_WINDOW_MONTHS):
        train_df = usable[usable["year_month"].isin(train_months)]
        test_df = usable[usable["year_month"].isin(test_months)]
        if train_df.empty or test_df.empty:
            continue
        result = evaluate_fold(train_df, test_df, target_col, rf_params, gb_params)
        result["test_range"] = f"{test_months[0]}..{test_months[-1]}"
        fold_results.append(result)
    return fold_results


def random_search_rf(dataset: pd.DataFrame, horizon: int, n_iter: int = RANDOM_SEARCH_ITER) -> dict:
    """Randomly samples n_iter hyperparameter combinations from
    RF_SEARCH_SPACE (plus DEFAULT_RF_PARAMS itself as a guaranteed
    candidate) and scores each by mean RF MAE across the same walk-forward
    folds cross_validate() uses -- see module docstring for why this is
    hand-rolled rather than sklearn's own RandomizedSearchCV. Only fits
    the random forest per candidate, not gradient boosting or linear
    regression too -- those aren't being tuned, so refitting them n_iter
    times over would just be wasted work with nothing to show for it."""
    target_col = f"target_change_{horizon}m"
    usable = dataset.dropna(subset=[target_col])
    months = sorted(usable["year_month"].unique())
    folds = walk_forward_folds(months, N_CV_FOLDS, CV_TEST_WINDOW_MONTHS)

    rng = random.Random(RANDOM_SEARCH_SEED)
    candidates = [DEFAULT_RF_PARAMS] + [sample_rf_params(rng) for _ in range(n_iter)]

    results = []
    for params in candidates:
        fold_maes = []
        for train_months, test_months in folds:
            train_df = usable[usable["year_month"].isin(train_months)]
            test_df = usable[usable["year_month"].isin(test_months)]
            if train_df.empty or test_df.empty:
                continue
            X_train, y_train = train_df[FEATURE_COLS], train_df[target_col]
            X_test, y_test = test_df[FEATURE_COLS], test_df[target_col]
            rf = make_random_forest(params)
            rf.fit(X_train, y_train)
            fold_maes.append(mean_absolute_error(y_test, rf.predict(X_test)))
        if not fold_maes:
            continue
        maes = pd.Series(fold_maes)
        results.append({"params": params, "mean_mae": maes.mean(), "std_mae": maes.std()})

    results.sort(key=lambda r: r["mean_mae"])
    baseline = next(r for r in results if r["params"] == DEFAULT_RF_PARAMS)
    return {"best": results[0], "baseline": baseline, "all": results}


def random_search_gb(dataset: pd.DataFrame, horizon: int, n_iter: int = RANDOM_SEARCH_ITER) -> dict:
    """Same approach as random_search_rf() (see its own docstring for why
    this is hand-rolled instead of sklearn's RandomizedSearchCV), applied
    to GB_SEARCH_SPACE instead: only fits gradient boosting per candidate,
    not the random forest or linear regression too."""
    target_col = f"target_change_{horizon}m"
    usable = dataset.dropna(subset=[target_col])
    months = sorted(usable["year_month"].unique())
    folds = walk_forward_folds(months, N_CV_FOLDS, CV_TEST_WINDOW_MONTHS)

    rng = random.Random(RANDOM_SEARCH_SEED)
    candidates = [DEFAULT_GB_PARAMS] + [sample_gb_params(rng) for _ in range(n_iter)]

    results = []
    for params in candidates:
        fold_maes = []
        for train_months, test_months in folds:
            train_df = usable[usable["year_month"].isin(train_months)]
            test_df = usable[usable["year_month"].isin(test_months)]
            if train_df.empty or test_df.empty:
                continue
            X_train, y_train = train_df[FEATURE_COLS], train_df[target_col]
            X_test, y_test = test_df[FEATURE_COLS], test_df[target_col]
            gb = make_gradient_boosting(params)
            gb.fit(X_train, y_train)
            fold_maes.append(mean_absolute_error(y_test, gb.predict(X_test)))
        if not fold_maes:
            continue
        maes = pd.Series(fold_maes)
        results.append({"params": params, "mean_mae": maes.mean(), "std_mae": maes.std()})

    results.sort(key=lambda r: r["mean_mae"])
    baseline = next(r for r in results if r["params"] == DEFAULT_GB_PARAMS)
    return {"best": results[0], "baseline": baseline, "all": results}


def calibrate_uncertainty(fold_results: list[dict]) -> dict:
    """The coverage check in evaluate_fold() showed the raw tree-spread std
    is overconfident (fewer outcomes fall inside +/-1 std than the ~68% a
    well-calibrated band should catch -- see model/README.md's "Uncertainty
    calibration" results). Rather than changing the forest itself, scale its
    std by a factor fit on these same walk-forward-CV residuals: pool every
    fold's (|residual|, std) pairs -- each from a model that never saw its
    own test rows during training, so this is a fair, held-out calibration
    set, not circular -- and take the 68th/95th percentile of |residual|/std
    across all of them. Multiplying future std predictions by c68 then
    guarantees (on this historical data, at least) that 68% of outcomes
    would have landed inside the scaled band, by construction: this is
    exactly what "68th percentile of the ratio" means. This is standard
    split-conformal-style variance calibration, not a new model."""
    abs_residuals = np.concatenate([r["abs_residuals"] for r in fold_results])
    pred_std = np.concatenate([r["pred_std"] for r in fold_results])
    # Rows where every tree in that fold's forest agreed exactly (std == 0)
    # have an undefined ratio, not a near-infinite one -- exclude them from
    # fitting the factor rather than dividing by zero.
    nonzero = pred_std > 0
    ratios = abs_residuals[nonzero] / pred_std[nonzero]
    return {
        "c68": float(np.quantile(ratios, 0.68)),
        "c95": float(np.quantile(ratios, 0.95)),
        "n": int(nonzero.sum()),
        "ratios": ratios,  # kept off the saved JSON by main() -- just for its own post-fit coverage check
    }


def calibrate_gb_quantiles(fold_results: list[dict]) -> dict:
    """Conformalized Quantile Regression (CQR; Romano, Patterson & Candès
    2019) -- the same "held-out CV residuals, not a new model" spirit as
    calibrate_uncertainty() above, adapted for an interval with two edges
    instead of a single std. Each fold's gb_score_68/95 (see
    evaluate_fold()) is already "how far past the raw interval's edge did
    the true value land" -- positive means it landed outside, so the 68th/
    95th percentile of these *held-out* scores is exactly the amount both
    edges need to widen by for the interval to hit that coverage rate on
    this same data, by construction (same "by construction" logic as
    c68/c95 above, just solved from the other direction: widening a gap
    instead of scaling a std). Applying delta68/delta95 to a future
    prediction's raw interval (widening it, never narrowing -- see
    generate_predictions.py) is the whole calibration step; there's no
    model retraining involved."""
    score_68 = np.concatenate([r["gb_score_68"] for r in fold_results])
    score_95 = np.concatenate([r["gb_score_95"] for r in fold_results])
    return {
        "delta68": float(np.quantile(score_68, 0.68)),
        "delta95": float(np.quantile(score_95, 0.95)),
        "n": int(len(score_68)),
        "scores_68": score_68,  # kept off the saved JSON by main() -- just for its own post-fit coverage check
        "scores_95": score_95,
    }


def fit_gb_production_model(
    dataset: pd.DataFrame, horizon: int, state_key: str, gb_params: dict | None = None,
) -> dict:
    """The model generate_predictions.py actually uses -- one mean-loss GB
    plus four quantile-loss GB (see GB_QUANTILES), all trained on every
    row available, not held back from a test split, since there's no
    accuracy claim being protected here, just the best model deployable
    today. Bundled into one dict/joblib file per horizon rather than five
    separate files -- they're only ever loaded and used together."""
    target_col = f"target_change_{horizon}m"
    usable = dataset.dropna(subset=[target_col])
    X, y = usable[FEATURE_COLS], usable[target_col]

    mean_model = make_gradient_boosting(gb_params)
    mean_model.fit(X, y)
    models = {"mean": mean_model}
    for name, q in GB_QUANTILES.items():
        models[name] = make_gb_quantile_model(q, gb_params).fit(X, y)

    model_path = Path(__file__).parent / f"county_distress_model_{state_key}_{horizon}m.joblib"
    joblib.dump(models, model_path)

    # permutation_importance, not feature_importances_ -- HistGradientBoostingRegressor
    # doesn't expose the latter at all (see module docstring). n_repeats=5
    # on the mean model only; the four quantile models exist to bracket an
    # interval, not to be individually interpreted. scoring="neg_mean_absolute_error"
    # explicitly rather than the estimator's own default score (R²) --
    # these values are then directly "how much MAE gets worse when this
    # feature is shuffled," in the same units as every MAE already printed
    # elsewhere, not an unnormalized, harder-to-read R²-decrease number.
    # Not comparable to RF's old feature_importances_ percentages, which
    # summed to 1 by construction -- these don't and aren't meant to.
    importance_result = permutation_importance(
        mean_model, X, y, n_repeats=5, random_state=42, scoring="neg_mean_absolute_error",
    )
    importances = pd.Series(importance_result.importances_mean, index=FEATURE_COLS).sort_values(ascending=False)

    return {"rows": len(usable), "importances": importances, "model_path": model_path}


def summarize(fold_results: list[dict], key: str) -> tuple[float, float]:
    values = pd.Series([r[key] for r in fold_results])
    return values.mean(), values.std()


def main():
    state_key = sys.argv[1] if len(sys.argv) > 1 else "tx"
    state = STATES[state_key]

    dataset = pd.read_csv(DATA_DIR / f"{state_key}_county_month_dataset.csv")
    dataset["year_month"] = pd.PeriodIndex(dataset["year_month"], freq="M")

    calibration_by_horizon = {}
    for horizon in TARGET_HORIZONS:
        print(f"\n{'=' * 60}\n{state['name'].upper()} -- {horizon}-MONTH HORIZON\n{'=' * 60}")

        search = random_search_rf(dataset, horizon)
        best, baseline = search["best"], search["baseline"]
        if best["params"] == baseline["params"]:
            print(f"\nRandom search over {RANDOM_SEARCH_ITER} candidate(s): the hand-picked default "
                  f"{DEFAULT_RF_PARAMS} was already the best of them (MAE {best['mean_mae']:.4f}).")
        else:
            print(f"\nRandom search over {RANDOM_SEARCH_ITER} candidate(s): {best['params']} "
                  f"reached MAE {best['mean_mae']:.4f}±{best['std_mae']:.4f}, vs. the hand-picked default's "
                  f"{baseline['mean_mae']:.4f}±{baseline['std_mae']:.4f} "
                  f"({1 - best['mean_mae'] / baseline['mean_mae']:.1%} lower) -- used for every RF fit below.")
        rf_params = best["params"]

        gb_search = random_search_gb(dataset, horizon)
        gb_best, gb_baseline = gb_search["best"], gb_search["baseline"]
        if gb_best["params"] == gb_baseline["params"]:
            print(f"Random search over {RANDOM_SEARCH_ITER} GB candidate(s): the hand-picked default "
                  f"{DEFAULT_GB_PARAMS} was already the best of them (MAE {gb_best['mean_mae']:.4f}).")
        else:
            print(f"Random search over {RANDOM_SEARCH_ITER} GB candidate(s): {gb_best['params']} "
                  f"reached MAE {gb_best['mean_mae']:.4f}±{gb_best['std_mae']:.4f}, vs. the hand-picked default's "
                  f"{gb_baseline['mean_mae']:.4f}±{gb_baseline['std_mae']:.4f} "
                  f"({1 - gb_best['mean_mae'] / gb_baseline['mean_mae']:.1%} lower) -- used for every GB fit below.")
        gb_params = gb_best["params"]

        folds = cross_validate(dataset, horizon, rf_params, gb_params)
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
        print(f"\nRandom forest uncertainty calibration (see predict_with_uncertainty()), "
              f"raw tree-spread std, before calibration: "
              f"{cov1_mean:.0%} of actual outcomes fell within the predicted ±1 std band "
              f"(well-calibrated target: ~68%), {cov2_mean:.0%} within ±2 std (target: ~95%). "
              f"Kept for comparison only -- RF is no longer the production model, see below.")

        rf_calibration = calibrate_uncertainty(folds)
        rf_calibration.pop("ratios")

        gb_cov68_mean, _ = summarize(folds, "gb_coverage_68")
        gb_cov95_mean, _ = summarize(folds, "gb_coverage_95")
        print(f"\nGradient boosting uncertainty calibration (see GB_QUANTILES), "
              f"raw quantile interval, before calibration: "
              f"{gb_cov68_mean:.0%} of actual outcomes fell within the raw [lower68, upper68] interval "
              f"(well-calibrated target: ~68%), {gb_cov95_mean:.0%} within [lower95, upper95] (target: ~95%).")

        gb_calibration = calibrate_gb_quantiles(folds)
        scores_68, scores_95 = gb_calibration.pop("scores_68"), gb_calibration.pop("scores_95")
        cov68_after = float(np.mean(scores_68 <= gb_calibration["delta68"]))
        cov95_after = float(np.mean(scores_95 <= gb_calibration["delta95"]))
        print(f"Calibration deltas fit on these {gb_calibration['n']:,} held-out CV rows: "
              f"delta68={gb_calibration['delta68']:.4f}, delta95={gb_calibration['delta95']:.4f} -- "
              f"widening the raw interval by delta68 on each side gives {cov68_after:.0%} coverage, "
              f"{cov95_after:.0%} at the delta95-widened interval, by construction on this same "
              f"held-out CV data (see calibrate_gb_quantiles()'s docstring for why that's a fair, "
              f"non-circular check rather than a fit-your-own-test-set number).")
        calibration_by_horizon[horizon] = {"rf": rf_calibration, "gb": gb_calibration}

        production = fit_gb_production_model(dataset, horizon, state_key, gb_params)
        print(f"\nProduction model (gradient boosting, mean + 4 quantile) trained on all "
              f"{production['rows']:,} available rows (saved to {production['model_path'].name}).")
        print("Feature importances (permutation MAE-increase, mean model only -- see module docstring):")
        for feature, importance in production["importances"].items():
            print(f"  {feature:<32} {importance:.4f}")

    calibration_path = Path(__file__).parent / f"county_distress_calibration_{state_key}.json"
    calibration_path.write_text(json.dumps(calibration_by_horizon, indent=2))
    print(f"\nSaved uncertainty calibration factors to {calibration_path.name} "
          f"(generate_predictions.py applies gb.delta68/delta95 to widen the shipped ± band; "
          f"rf.c68/c95 kept for reference only, no longer used in production).")


if __name__ == "__main__":
    main()
