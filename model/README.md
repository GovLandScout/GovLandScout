# GovLandScout - County Distress Trend Model (Phase 1)

A gradient boosting model predicting next-month change in county housing
distress, using Zillow Research + FRED historical data rather than
GovLandScout's own scraped listings -- our own history is only a few
weeks old, nowhere near enough to train on yet. This is deliberately
kept separate from the scraper/web app: different dependencies (pandas,
scikit-learn), different deploy target (none -- this doesn't run on
Render or in the daily scrape workflow), different audience (research,
not the live site).

Was a random forest through most of this project's history; switched to
gradient boosting once two separate hyperparameter-tuning passes on both
models (see "Hyperparameter search" below) showed GB fairly beating RF at
every Texas horizon and most Pennsylvania ones -- a real, earned result,
not an artifact of only one side getting attention. See git history for
the RF-era version of this README if you need it; this version documents
the current GB architecture, including the uncertainty-quantification
redesign that switch required (see "Uncertainty, not just a point
estimate" below).

Runs per state (see `states.py`) -- Texas, Pennsylvania, and California
currently, each with its own trained models, GeoJSON, and predictions
file, served on their own `/market-trends` pages. Nothing about the
modeling code is state-specific; adding a state is a config entry plus
running the
pipeline for it (see "Running it" below), not new code.

## Scope (Phase 1)

- **Target**: *change* in `perc_listings_price_cut` (% of a county's
  active listings that took a price cut) over the next 1, 3, and 6
  months -- not the raw next-period level. An earlier version predicted
  the level directly and the model mostly just learned to copy this
  month's value forward, since price_cut_pct is highly autocorrelated;
  predicting change forces it to actually explain movement instead (see
  Results below).
- **Geography**: every county Zillow has price-cut data for, per state,
  plus a ZHVI-decline gap-filler for counties it doesn't publish
  price-cut data for at all (see "ZHVI gap-filler" below) -- **238 of
  254 Texas counties** (203 price-cut + 35 gap-filler), **all 67 of 67
  Pennsylvania counties** (64 price-cut + 3 gap-filler; PA's price-cut-only
  ratio was already better before the gap-filler, being a much smaller
  state). Price-cut-only coverage (203 TX, 64 PA as of this run) is down
  slightly from an earlier version of this README's 207/66 -- confirmed
  directly against Zillow's raw CSVs that this is real drift in Zillow's
  own published coverage since then, not a regression introduced here.
- **Features**: lagged/rolling price-cut history, ZHVI trend
  (month-over-month and year-over-year % change), for-sale inventory
  and its trend, county unemployment rate (from FRED) and its trend,
  the national 30-year fixed mortgage rate (from FRED, level and
  month-over-month change) and its trend, month-of-year seasonality --
  as a sine/cosine pair (`month_sin`/`month_cos`), not the raw 1-12
  month number. That raw encoding put December and January, adjacent in
  reality, about as numerically far apart as two months can be, actively
  fighting the model on what turned out to be its single most important
  feature. The mortgage rate is national, not per-county, like Zillow's
  ZHVI/price-cut/inventory data itself -- it helped both states' random
  forests once their hyperparameters were tuned (see "Hyperparameter
  search" and each state's Results below), though it genuinely hurt
  Texas's 6-month model before that tuning existed -- see "Next steps"
  for that history and why it's no longer an open production concern.
- **Model**: scikit-learn `HistGradientBoostingRegressor` (the production
  model `generate_predictions.py` actually serves), evaluated in
  `train_model.py` against a naive "predict zero change" baseline, a
  LinearRegression benchmark, and a RandomForestRegressor (kept as a
  comparison point in the CV table now, not production -- see git
  history for when the roles were reversed), on walk-forward time-based
  cross-validation (several rolling folds, not one fixed split -- see
  Results below for why that matters). Neither model's own
  hyperparameters (tree count, depth, leaf/split sizes, learning rate,
  features considered per split) are one hand-picked config reused
  everywhere -- `random_search_rf()` and `random_search_gb()` each
  search per horizon per state, scored on the same walk-forward folds,
  before every other number in this README is computed. See
  "Hyperparameter search" below.
- **Uncertainty, not just a point estimate**: alongside the mean-loss
  production model, `train_model.py`'s `fit_gb_production_model()` fits
  four more gradient boosting models per horizon at fixed quantiles
  (`GB_QUANTILES`: 0.16/0.84 for a 68% interval, 0.025/0.975 for 95%) --
  real quantile regression, not a proxy for one. HistGradientBoostingRegressor's
  sequential trees can't produce RandomForestRegressor's old tree-spread
  estimate (there's no ensemble of independent trees to disagree with
  each other), so this is a genuinely different technique, not a
  drop-in replacement -- see generate_predictions.py's
  `predict_with_interval()`. Surfaced on `/market-trends` as reduced
  opacity for low-confidence counties and a plain-language `±` range in
  the tooltip/detail panel, plus (as of this version) a green/blue
  whisker pair on the drill-down chart itself. **Checked, not just
  asserted**: train_model.py's own copy of the quantile fit feeds a
  coverage check in its CV loop (does the true outcome actually fall
  inside the predicted interval as often as it claims?) -- see Results
  below. The raw quantile interval turned out to be measurably
  overconfident, same as the old tree-spread was, so it's not shipped
  as-is: `train_model.py`'s `calibrate_gb_quantiles()` widens both
  interval edges by a delta fit on those same walk-forward-CV residuals
  (a held-out set, not the production model's own training data) using
  Conformalized Quantile Regression (Romano, Patterson & Candès 2019 --
  a principled, published technique, not this project's own invention),
  and saves the deltas to `county_distress_calibration_{state}.json`;
  `generate_predictions.py` applies them before writing the `±` band.
  See "Uncertainty calibration" below for the before/after numbers.
- **Not in scope yet**: per-property predictions, states beyond TX/PA/CA,
  GovLandScout's own scraped history as a feature (revisit once it has
  enough months behind it to matter).
- **Now served on the live site, clearly labeled as experimental**: the
  `/market-trends` (Texas) and `/market-trends-pa` (Pennsylvania) pages
  each render a county choropleth from this model's current output,
  locked to a fitted view of the whole state (no pan/zoom -- see
  web.py's `render_market_trends_page()`). That's a deliberate exception
  to "not served on the live site" above -- it's framed throughout as a
  research demo, not a feature of the property-listing product, with the
  same kind of disclaimer language the Investment Info page already
  uses. See "Publishing current predictions" below.

## Pipeline

Each script takes a state key from `states.py` as its only CLI arg
(`tx` or `pa`; defaults to `tx` if omitted):

1. `fetch_data.py <state>` -- downloads and caches the raw Zillow CSVs and
   FRED's national 30-year mortgage rate series (all three nationwide,
   not state-specific -- shared across every state this pipeline runs
   for, downloaded once), plus, per county in that state, FRED's
   unemployment/employment level series (used to compute a monthly
   unemployment rate; FRED's own monthly county unemployment *rate*
   series isn't uniformly published, but the level series behind it
   are). Caches everything in `data/` (gitignored -- this is
   multi-hundred-county raw data, not something to commit) so re-runs
   don't re-hit either source.
2. `build_dataset.py <state>` -- joins all of it into one county-month
   panel for that state (an outer join across ZHVI/price-cut/inventory,
   not anchored on whichever dataset happens to have the narrowest
   coverage -- see "ZHVI gap-filler" below for why that matters),
   engineers the lagged/rolling features described above plus the
   gap-filler's own ZHVI-decline target, and writes both
   `data/{state}_county_month_dataset.csv` (the price-cut dataset) and
   `data/{state}_county_month_dataset_zhvi.csv` (the broader-coverage
   ZHVI gap-filler dataset).
3. `train_model.py <state>` -- for each horizon, first a random search
   over both the random forest's and the gradient boosting's own
   hyperparameters (see "Hyperparameter search" below), then walk-forward
   cross-validation with each model's winning config (gradient boosting
   vs. random forest vs. linear regression vs. a naive baseline, plus a
   check on whether GB's quantile-regression uncertainty interval is
   actually calibrated), then fits the final production models on all
   available data using GB's winning config: one mean-loss model plus
   four quantile-loss models per horizon (see "Uncertainty, not just a
   point estimate" above), bundled into one dict and saved as
   `county_distress_model_{state}_{h}m.joblib` (gitignored -- regenerated
   by re-running this script, not something to commit). Also saves
   `county_distress_calibration_{state}.json`, a per-horizon `{"rf":
   {...}, "gb": {...}}` of calibration values fit on the same CV
   residuals (also gitignored, same reasoning as the `.joblib` files --
   both regenerate together and `generate_predictions.py` needs them run
   in the same pass); only the `"gb"` half (`delta68`/`delta95`) is
   actually used in production, `"rf"` is kept for the comparison table's
   own reference. Then, if `data/{state}_county_month_dataset_zhvi.csv`
   exists, repeats a leaner GB-only version of the same process for the
   ZHVI gap-filler (see "ZHVI gap-filler" below for why it's
   deliberately leaner), saving
   `county_distress_model_{state}_{h}m_zhvi.joblib` and
   `county_distress_calibration_{state}_zhvi.json`.
4. `generate_predictions.py <state>` -- runs that state's three trained
   price-cut horizon bundles against each county's latest available row,
   widens each prediction's quantile interval by that horizon's
   calibration deltas, then does the same against the ZHVI gap-filler
   models but restricted to counties the price-cut pass didn't already
   cover (`build_county_rows()`'s `only_counties` -- see "ZHVI
   gap-filler" below), merges both into one list tagged with a `metric`
   field (`"price_cut"` or `"zhvi_decline"`) per county, and writes
   `public/{state}_county_predictions.json`.

## Publishing current predictions

`public/` (unlike `data/`) is **not** gitignored -- it holds the small
static files each `/market-trends*` page actually reads, per state:
`{state}_county_predictions.json` (from generate_predictions.py, above)
and `{state}_counties.geojson` (county boundaries, sourced once per
state from the Census Bureau's TIGERweb API and simplified with shapely
to ~35-110KB depending on the state's size and county count -- see git
history if that ever needs regenerating, it doesn't change). web.py's
own environment (Render) never installs pandas/scikit-learn; it just
reads these committed JSON files at import time. This is static, not
live -- there's no scheduled job re-running the model, so after
retraining, re-run `generate_predictions.py <state>` and commit the
updated `{state}_county_predictions.json` by hand.

## Running it

```bash
cd model
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3 fetch_data.py tx       # takes a few minutes -- ~200-400 small requests to FRED, depending on the state
python3 build_dataset.py tx
python3 train_model.py tx
python3 generate_predictions.py tx   # updates public/tx_county_predictions.json for /market-trends

# repeat with "pa" (or any other key added to states.py) for another state
```

## Hyperparameter search

`train_model.py`'s `random_search_rf()` runs before every other number in
this README is computed. Per horizon (and per state, since `main()` runs
per state), it samples `RANDOM_SEARCH_ITER` (30) random combinations of
`n_estimators`/`max_depth`/`min_samples_leaf`/`min_samples_split`/
`max_features` from `RF_SEARCH_SPACE`, plus the old hand-picked config
(`n_estimators=300, max_depth=10, min_samples_leaf=5`) as a guaranteed
31st candidate, and scores each by mean MAE across the *same*
walk-forward folds used everywhere else in this file -- not sklearn's own
`RandomizedSearchCV`, whose default k-fold would shuffle a county's later
months into the same fold as its earlier ones, the exact future-leak
walk-forward CV exists to avoid (see train_model.py's module docstring).
The winning config feeds every RF fit downstream: the comparison table
below and its own coverage check. RF is no longer the production model
(see git history for when it was) -- kept as the CV comparison's second
tree-based reference point alongside GB, still tuned just as seriously
as GB is, so the comparison stays honest.

This turned out to matter well beyond the Texas 6-month weakness that
originally motivated it (see git history for the first version of this
section -- that model beat naive by only 36.1% there, worst of any
horizon/state at the time). The table below is this search's *second*
pass: the first (`RANDOM_SEARCH_ITER=15`, a coarser version of
`RF_SEARCH_SPACE`) found real gains everywhere, documented in an earlier
version of this section; this pass doubled the candidate count and
filled in the gaps between the original grid's values (same range, denser
steps -- see `RF_SEARCH_SPACE`'s own comment for why it wasn't also
narrowed around the first pass's winners). The honest result: **mixed,
not uniformly better**. Random search with a different-sized space draws
different specific candidates even from the same seed, so a wider search
isn't guaranteed to beat a narrower one on every horizon it's only
guaranteed to have at least as good a chance -- and that's what happened:
three of six horizon/state combinations improved further, three came out
marginally worse than the first pass's winner (never worse than the
original hand-picked default, which every candidate is still checked
against):

| State | Horizon | Winning config | MAE vs. default | vs. first pass's winner |
|---|---|---|---|---|
| TX | 1 month | `n_estimators=100, max_depth=15, min_samples_leaf=15, min_samples_split=10, max_features='log2'` | 6.0% lower | worse (was 6.8%) |
| TX | 3 months | `n_estimators=300, max_depth=25, min_samples_leaf=5, min_samples_split=5, max_features=0.5` | 4.0% lower | better (was 3.7%) |
| TX | 6 months | `n_estimators=150, max_depth=18, min_samples_leaf=2, min_samples_split=15, max_features=0.5` | 16.7% lower | worse (was 17.1%) |
| PA | 1 month | `n_estimators=300, max_depth=25, min_samples_leaf=5, min_samples_split=5, max_features=0.5` | 2.2% lower | better (was 1.1%) |
| PA | 3 months | `n_estimators=300, max_depth=25, min_samples_leaf=1, min_samples_split=4, max_features=1.0` | 1.2% lower | worse (was 1.5%) |
| PA | 6 months | `n_estimators=300, max_depth=25, min_samples_leaf=1, min_samples_split=4, max_features=1.0` | 2.2% lower | worse (was 2.8%) |

A pattern still holds across both passes: every winning config keeps
`max_depth` at 12 or deeper and leans on `min_samples_split`/
`max_features` for regularization instead -- the old default's
`max_depth=10` remains the clearest single wrong lever this search has
found. But the swings between passes here (all under 1 percentage point)
are smaller than the run-to-run noise this project's own walk-forward
folds already show in the Results tables below (± up to 0.0151 on a
single model) -- a real sign this particular search has roughly found
its ceiling for this feature set, not that a fourth pass would keep
finding more.

`random_search_gb()` does the same thing for gradient boosting, over its
own search space (`max_iter`/`learning_rate`/`max_depth`/
`max_leaf_nodes`/`min_samples_leaf`/`l2_regularization`), also widened
this same pass. Unlike RF's winning config, GB's now feeds more than a
comparison table: it's what `fit_gb_production_model()` actually deploys
(the mean-loss model), and what every one of the four quantile models
alongside it reuses too (see "Uncertainty, not just a point estimate"
above for why sharing one config across five models is a deliberate
simplification, not an oversight):

| State | Horizon | Winning config | MAE vs. default | vs. first pass's winner |
|---|---|---|---|---|
| TX | 1 month | `max_iter=500, learning_rate=0.07, max_depth=10, max_leaf_nodes=7, min_samples_leaf=25, l2_regularization=0.3` | 2.2% lower | better (was 1.8%) |
| TX | 3 months | `max_iter=400, learning_rate=0.15, max_depth=10, max_leaf_nodes=15, min_samples_leaf=15, l2_regularization=0.5` | 0.8% lower | worse (was 1.0%) |
| TX | 6 months | `max_iter=500, learning_rate=0.07, max_depth=10, max_leaf_nodes=7, min_samples_leaf=25, l2_regularization=0.3` | **6.4% lower** | better (was 2.8%) |
| PA | 1 month | `max_iter=400, learning_rate=0.15, max_depth=10, max_leaf_nodes=15, min_samples_leaf=15, l2_regularization=0.5` | 2.9% lower | better (was 0.9%) |
| PA | 3 months | `max_iter=500, learning_rate=0.07, max_depth=10, max_leaf_nodes=7, min_samples_leaf=25, l2_regularization=0.3` | 0.6% lower | worse (was 1.3%) |
| PA | 6 months | `max_iter=200, learning_rate=0.15, max_depth=4, max_leaf_nodes=47, min_samples_leaf=40, l2_regularization=0.5` | 1.5% lower | better (was 0.7%) |

GB's own pattern across both passes: every winning config uses
`max_depth` in the 4-10 range (never the unbounded end RF favors) and a
non-default, usually non-trivial `learning_rate` (0.07-0.15 vs. the
default's 0.05) -- a real, different-shaped lever than RF's. Unlike RF,
GB improved further at four of six horizons this pass, most notably
Texas's 6-month result (50.5% -> 52.3% vs. naive, see Results below) --
worth noting since it's the same horizon where RF's own second pass
happened to land slightly worse, widening the gap between the two
models specifically at that horizon. See each state's Results section
below for what tuning both sides across two passes changed about which
model actually wins where.

## Results: Texas (walk-forward cross-validation, 2026-08-14)

203 TX counties with price-cut data, ~16,300 county-month training
examples spanning 2019-03 through 2026-06 (see the Scope section above
for why this is 203, not the 207 an earlier version of this README
reported -- Zillow's own coverage drift, not a regression here).
Evaluated on 4 rolling walk-forward folds per horizon (3-month test
windows each, all in 2025-04 through 2026-05) rather than one fixed
split -- see train_model.py's module docstring for why a single split
understates how much performance actually varies month to month. RF and
GB here each use their own horizon's winning config from "Hyperparameter
search" above, not the old fixed defaults.

| Horizon | Naive MAE | Linear MAE | RF MAE | GB MAE | RF vs. naive | Linear vs. naive | GB vs. naive |
|---|---|---|---|---|---|---|---|
| 1 month | 0.0162 ± 0.0013 | 0.0128 ± 0.0010 | 0.0128 ± 0.0004 | **0.0126 ± 0.0005** | 20.7% | 20.8% | **22.3%** |
| 3 months | 0.0378 ± 0.0067 | 0.0248 ± 0.0010 | 0.0220 ± 0.0023 | **0.0215 ± 0.0020** | 41.7% | 34.4% | **43.0%** |
| 6 months | 0.0505 ± 0.0059 | 0.0256 ± 0.0018 | 0.0263 ± 0.0076 | **0.0236 ± 0.0034** | 47.9% | 49.3% | **53.3%** |

(± is one standard deviation across the 4 folds -- how consistent each
model's error was across different stretches of time, not the accuracy
of a single number. These are a re-run of the same tuned pipeline
(see "Hyperparameter search" above); the small shifts from the previous
version of this table -- all well under one fold's own standard
deviation -- come from Zillow's data having moved a couple more months
forward between runs, not a methodology change.)

**Gradient boosting wins outright at every horizon in Texas, and is now
what `/market-trends` actually serves, not a comparison point held out
of production for architectural reasons** -- 1 month held (GB 22.3% vs.
RF's 20.7%), and 6 months widened noticeably (GB reaching 53.3% vs. RF's
47.9%). That's a real, fairly-earned result, not an artifact of only
one side of the comparison getting attention -- both models went through
the same two-pass search. Adopting GB required solving the uncertainty
problem that had kept RF in production despite losing this comparison --
see below for how, and "Next steps" for what's still approximate about
that solution.

Feature importances (permutation MAE-increase on the production mean
model -- not comparable percentage-for-percentage to the RF-era table
this replaced, see Scope's "Uncertainty" bullet and
`fit_gb_production_model()`'s own docstring for why):

- **`price_cut_pct` (current level, or its `_roll3` rolling-average
  variant) dominates every horizon here, a real shift from RF's
  seasonality-first story.** At 1 month, `price_cut_pct_roll3` alone
  (0.0068 MAE-increase) is more than 3x the next feature; at 3 and 6
  months it's the bare `price_cut_pct` level instead (0.0165, 0.0257 --
  each roughly the size of the *entire model's* MAE at that horizon,
  meaning shuffling this one feature alone destroys most of what the
  model knows). `month_sin`/`month_cos` are still present and real
  (ranked 2nd-3rd at every horizon) but clearly secondary to GB, unlike
  RF where they were consistently the single largest block.
- **`mortgage_rate` is a real, consistent presence** -- 4th-6th ranked at
  every horizon (0.0008-0.0053), joined by `mortgage_rate_mom_change` more
  prominently at 6 months (0.0033, 7th) than at 1 or 3.
- `price_cut_pct`'s dominance here is itself a mean-reversion signal --
  see the Clay County case study below for what a sharp recent spike in
  this exact feature does to GB's own prediction, a much bigger call
  than RF ever made for the same county.
- Unemployment features stay the least important block at every horizon,
  same conclusion as the RF-era table even though the underlying numbers
  aren't comparable.

**Uncertainty calibration**: before applying any calibration, 66% (1
month) down to 56-58% (3, 6 months) of actual outcomes fall within the
raw `[lower68, upper68]` quantile interval (target for a well-calibrated
68% interval: ~68%) -- overconfident at the longer horizons, the same
direction RF's raw tree-spread was. 89-93% land within the raw
`[lower95, upper95]` interval (target: ~95%), closer but still short.

`train_model.py`'s `calibrate_gb_quantiles()` widens both edges of the
interval by a delta fit on these same held-out walk-forward-CV residuals
(Conformalized Quantile Regression -- see that function's own docstring),
which by construction brings coverage on this data to exactly the target
rate. Texas's fitted deltas: `delta68` of 0.0006 (1-month), 0.0048
(3-month), 0.0055 (6-month) -- all small relative to the point estimates
themselves, and `delta95` of 0.0025, 0.0099, 0.0121 respectively (roughly
2-4x `delta68`, similar to RF's old `c95`/`c68` ratio staying near 2).
`generate_predictions.py` applies both before writing the ± band
`/market-trends` shows -- see `predict_with_interval()`'s own docstring
for how an asymmetric calibrated interval becomes the single symmetric ±
number the UI actually draws (a real, documented simplification, not
hidden away).

## Results: Pennsylvania (walk-forward cross-validation, 2026-08-14)

64 PA counties with price-cut data, ~5,300-5,700 county-month training
examples (fewer than Texas simply because Pennsylvania has far fewer
counties -- 67 total vs. 254; see the Scope section above for why 64,
not the 66 an earlier version of this README reported), same 2019-03
through 2026-06 span and same 4-fold walk-forward setup. RF and GB here
each use their own horizon's winning config from "Hyperparameter search"
above, not the old fixed defaults.

| Horizon | Naive MAE | Linear MAE | RF MAE | GB MAE | RF vs. naive | Linear vs. naive | GB vs. naive |
|---|---|---|---|---|---|---|---|
| 1 month | 0.0184 ± 0.0028 | 0.0118 ± 0.0004 | 0.0124 ± 0.0005 | 0.0121 ± 0.0003 | 32.6% | **35.9%** | 34.5% |
| 3 months | 0.0379 ± 0.0032 | 0.0226 ± 0.0014 | 0.0216 ± 0.0009 | 0.0208 ± 0.0012 | 43.2% | 40.3% | **45.3%** |
| 6 months | 0.0518 ± 0.0151 | 0.0217 ± 0.0003 | 0.0222 ± 0.0016 | 0.0216 ± 0.0020 | 57.1% | 58.2% | **58.4%** |

**Gradient boosting doesn't win outright in Pennsylvania -- linear
regression still wins at 1 month (35.9% vs. GB's 34.5%)** -- but it's the
production model here too, the same as Texas, for consistency (one
pipeline, one production model class per horizon across both states, not
a per-horizon "whichever wins" selection this project has never done for
any other model). GB does win clearly at 3 and 6 months (45.3%, 58.4%,
the latter a much tighter margin over RF/linear than the earlier version
of this table showed). With a third as many counties and a smaller, more
geographically compact state, PA's 6-month fold error is also still the
least consistent of any horizon/state combination here (± up to 0.0151)
-- a smaller, noisier dataset remains a plausible reason the more
flexible models have a harder time earning their extra complexity back
here specifically.

Feature importances (permutation MAE-increase on the production mean
model -- see the Texas section above for why these aren't comparable to
the old RF-era percentages):

- **`price_cut_pct`/`price_cut_pct_roll3` dominate here too, same shift
  from RF's seasonality-first story as Texas.** 1 month: `price_cut_pct_roll3`
  leads (0.0193, more than 2x the next feature); 3 and 6 months: bare
  `price_cut_pct` leads (0.0152, 0.0250). `month_cos`/`month_sin` are
  still real and closely ranked 2nd at every horizon, closer behind the
  leader here than in Texas.
- `mortgage_rate` is a consistent, if secondary, presence (3rd-4th ranked
  at every horizon, 0.0018-0.0074), similar magnitude to Texas.

**Uncertainty calibration**: before applying any calibration, 48-54% of
actual outcomes fall within the raw `[lower68, upper68]` quantile
interval (target ~68%) -- more overconfident than Texas's own raw
interval, and 77-85% within `[lower95, upper95]` (target ~95%), also
further short. Pennsylvania's smaller, noisier dataset (see the 6-month
fold-consistency point above) is a plausible reason its raw quantile
models fit the tails less precisely than Texas's.

`calibrate_gb_quantiles()` widens Pennsylvania's interval more than
Texas's needed, consistent with the larger raw-coverage gap above --
`delta68` of 0.0046 (1-month), 0.0085 (3-month), 0.0064 (6-month), and
`delta95` of 0.0109, 0.0202, 0.0176 respectively (roughly 2-2.5x
`delta68`, similar ratio to Texas's). `generate_predictions.py` applies
these the same way as Texas's -- a real, if larger, calibration fix,
following the same "widen the raw interval by a delta fit on held-out
data" logic throughout this section.

## ZHVI gap-filler

Zillow's own `price_cut_county.csv` methodology excludes a real chunk of
small/rural counties outright -- 51 of Texas's 254 counties have no
price-cut data at all, ever, at any date (Pennsylvania's gap is much
smaller: 3 of 67). That's not a scraping or pipeline problem to fix,
it's a real hole in the source data this model has always been built
on. Rather than leave those counties permanently off the map, or swap
the whole model's target to a broader-but-coarser metric everywhere
(rejected -- it would have thrown away the price-cut model's real,
demonstrated edge over naive/linear at every horizon in both states, see
Results above), this adds a second, narrower model that only fills in
the specific counties the first one can't reach.

**The gap-filler's target**: `target_zhvi_decline_{h}m` in
`build_dataset.py`'s `engineer_features()`, defined as
`-(zhvi_mom_pct.shift(-h) - zhvi_mom_pct)` -- the *decline* in ZHVI's own
month-over-month growth rate, negated so positive still means "more
distress" the same direction as the price-cut target, for one consistent
red/green convention on the map (see web.py's `distressColor()`, unchanged
by any of this). ZHVI (Zillow's home value index) is published far more
broadly than the price-cut series -- this is a proxy for "is the local
market cooling," not a direct read of seller behavior the way price-cut
share is, which is exactly why it's kept as a fallback rather than the
primary signal.

**Feature set**: `ZHVI_FEATURE_COLS` in `build_dataset.py`/`train_model.py`/
`generate_predictions.py` (kept as three separate copies, same reasoning
as this project's existing duplicated constants elsewhere) drops
`unemployment_rate`/`unemployment_rate_mom_change` from the primary
model's feature list -- a deliberate coverage trade, not an oversight.
FRED simply has no unemployment series at all for many small rural
counties (confirmed directly: Motley County, TX has zero
`unemployment_rate` rows despite full ZHVI/inventory data). Verified via
a direct A/B on the same build: keeping those two features in caps TX's
ZHVI dataset at 207 counties (matching FRED's own unemployment
coverage); dropping them raises it to all 238 the merged output actually
ships. Unemployment was already the least-important feature block in
every price-cut Results table above, so this is trading away a
consistently marginal signal for real coverage, not a costly one.

**Merge logic**: `build_dataset.py` writes the gap-filler's own dataset
to `data/{state}_county_month_dataset_zhvi.csv` alongside the existing
price-cut one (an outer join across ZHVI/price-cut/inventory now, not
anchored on price-cut's own narrower set -- otherwise counties missing
price-cut data would have been silently dropped before the gap-filler
ever got a chance to compute features for them). `train_model.py`'s
`train_zhvi_gap_filler()` trains it the same way as the primary model,
just leaner (see below), saving
`county_distress_model_{state}_{h}m_zhvi.joblib` and
`county_distress_calibration_{state}_zhvi.json` alongside the existing
files. `generate_predictions.py`'s `build_county_rows()` runs the
price-cut pass first, then the gap-filler pass restricted to counties
the price-cut pass didn't already produce a row for (`only_counties` --
price-cut is the more directly relevant signal everywhere Zillow
publishes it, not a second opinion competing with it), and merges both
into one list, each row tagged `"metric": "price_cut"` or
`"metric": "zhvi_decline"` with a metric-agnostic `current_value` field
in place of the old `current_price_cut_pct`.

**Deliberately leaner evaluation**: `evaluate_gb_fold()`/
`cross_validate_gb()` in `train_model.py` run gradient boosting only --
naive baseline and GB, no random forest, no linear regression. The
RF-vs-GB question this simplification skips was already settled for the
primary model (see "Hyperparameter search" and both Results sections
above); the gap-filler's whole job is coverage, not re-earning that same
comparison a second time on a narrower, noisier proxy target. Everything
else -- walk-forward CV, random hyperparameter search over GB's own
space, quantile regression at the same `GB_QUANTILES`, Conformalized
Quantile Regression calibration -- is identical machinery to the primary
model, just not run through RF/linear at all.

**Results, Texas** (35 gap-filler counties, on top of the 203 price-cut
ones -- 238 of 254 total):

| Horizon | Naive MAE | GB MAE | GB vs. naive | Raw 68%/95% coverage | delta68 | delta95 |
|---|---|---|---|---|---|---|
| 1 month | 0.3129 | 0.3170 | **-1.3%** | 69% / 95% | -0.0087 | 0.0045 |
| 3 months | 0.7079 | 0.5908 | **16.5%** | 68% / 94% | 0.0041 | 0.0546 |
| 6 months | 0.8751 | 0.6087 | **30.4%** | 62% / 93% | 0.0872 | 0.1350 |

**Texas's 1-month gap-filler is honestly close to a wash against naive
(-1.3%)** -- reported as-is, not hidden, the same "checked, not just
asserted" standard the price-cut model's own calibration is held to
above. ZHVI's own month-over-month growth rate is a noisier, more
proxy-once-removed signal than price-cut share at short horizons, and
that shows up directly in the point-estimate accuracy here. The raw 68%
interval was already essentially at its 69% target before any
correction, though, which is *why* `delta68` comes out slightly
negative (a small narrowing, not a widening) -- `predict_with_interval()`
in `generate_predictions.py` already handles a negative delta safely
with plain arithmetic, no special-casing needed (verified directly).
3 and 6 months are real, clear wins over naive (16.5%, 30.4%) -- close
to the price-cut model's own longer-horizon margins. Feature importances
flip a striking pattern relative to the price-cut model: `mortgage_rate`
edges out `zhvi_mom_pct` itself at 1 month (0.0373 vs. 0.0286), then
`zhvi_mom_pct` pulls sharply ahead at 3 and 6 months (0.3327, 0.4924 --
each easily the single largest feature by that point). `price_cut_pct`'s
roll3-dominated story above doesn't repeat here because it isn't in this
feature set at all.

**Results, Pennsylvania** (3 gap-filler counties -- Cameron, Fulton,
Sullivan -- on top of the 64 price-cut ones, for **all 67 of 67 total**):

| Horizon | Naive MAE | GB MAE | GB vs. naive | Raw 68%/95% coverage | delta68 | delta95 |
|---|---|---|---|---|---|---|
| 1 month | 0.2368 | 0.2294 | **3.1%** | 70% / 96% | -0.0156 | -0.0435 |
| 3 months | 0.4797 | 0.3599 | **25.0%** | 75% / 95% | -0.0602 | -0.0771 |
| 6 months | 0.5026 | 0.3506 | **30.2%** | 74% / 95% | -0.0467 | 0.0266 |

Pennsylvania's gap-filler beats naive at every horizon, unlike Texas's
1-month result -- plausibly just that Cameron/Fulton/Sullivan happen to
be less noisy than Texas's 35, on a sample this small (3 counties) that
could easily be variance rather than a real state-level difference,
which is why this isn't claimed as one. **The calibration deltas are the
more interesting difference**: nearly every one is negative here, the
opposite pattern from Texas's mostly-positive deltas -- raw coverage
(70-75%/95-96%) is already *at or above* every target here, so
`calibrate_gb_quantiles()` correctly *narrows* the raw interval instead
of widening it. Same function, same "fit a delta on held-out CV
residuals" logic as everywhere else in this document; it just happens
that Pennsylvania's raw quantile fit here is already slightly
over-cautious rather than overconfident, and the calibration corrects in
whichever direction the data actually calls for -- not a asymmetry the
code treats specially, see `predict_with_interval()`'s plain
`lower - delta` / `upper + delta` arithmetic. Feature importances follow
the same mortgage-rate-then-ZHVI shape as Texas: `mortgage_rate` leads at
1 month (0.0386 vs. `zhvi_mom_pct`'s 0.0277), `zhvi_mom_pct` takes over
decisively by 6 months (0.5221).

**Surfaced honestly, not blended in**: gap-filled counties are a
genuinely different observation (home-value growth trend, not seller
price-cut behavior) on a genuinely different numeric scale, so
`/market-trends` doesn't just drop them into the existing choropleth as
if they were more price-cut data. `web.py`'s `renderChoropleth()`
computes separate `maxAbs`/`maxStd` color-scale normalization per
`metric` group (mixing scales into one linear color intensity would make
one group look artificially muted or extreme next to the other), draws
gap-filled counties with a dashed, darker county border, and both the
map tooltip and the county detail panel show an explicit "estimated from
home values, no price-cut data" disclosure alongside a metric-appropriate
label ("Current home-value growth rate" instead of "Current price-cut
share") -- the same kind of plain-language honesty this model's
uncertainty bands are already held to.

## Case study: Clay County

Clay County's 6-month horizon is still the single largest predicted move
in the current output (see `public/tx_county_predictions.json`), and
**switching to gradient boosting made it a much bigger call than RF ever
made for this county** -- a projected **-13.4 point** drop in price-cut
share, from a current 29.7% down toward roughly 16%, versus RF's own
largest version of this same call, -8.3 points. Andrews County's own
6-month call grew similarly (-7.5 -> -10.8 points) but stays behind
Clay's.

Its actual history explains why either model calls reversion here: Clay
sat in a 22-25% band through most of 2025, dropped to a recent low of
10.6% in January 2026 -- then spiked hard: 13.0% (Feb) -> 14.6% (Mar) ->
21.3% (Apr) -> 25.7% (May) -> 29.7% (Jun 2026), nearly tripling in five
months. That's a sharp, unusual move for a county that had otherwise been
comparatively range-bound -- and, per the Results section above,
`price_cut_pct`'s current level is GB's single most important feature at
this horizon, more so than it ever was for RF. A model that leans harder
on "how extreme is the current level" for exactly the input feature this
county's own history is currently most extreme on plausibly explains why
GB's call moved further from zero than RF's, not just a random
difference between two comparably-accurate models.

The model isn't predicting Clay keeps climbing -- it's betting on
reversion, gradually: a real pullback shows up even at 1 month (-1.1
points) and 3 months (-9.6 points) now, not just 6 (-13.4 points) --
GB's calls front-load more of the reversion into earlier horizons than
RF's did. The uncertainty band on the 6-month number is ±6.6 points
(68%) / ±15.2 points (95%) -- both meaningfully wider in absolute terms
than RF's final ±3.8 points, consistent with GB's raw quantile interval
needing a real conformal widening at this horizon (`delta68`=0.0055,
see Results above) where RF's tuned tree-spread had gotten close to
self-calibrated. A bigger point estimate paired with a wider,
honestly-calibrated band is the right shape for "the model is confident
*something* unusual is happening here, less confident exactly how much
of it reverses" -- not a contradiction. Whether the call is *right*
isn't knowable yet (2026-12 data doesn't exist yet); what's checkable
today is that the prediction is legible, tied to a real, visible pattern
in the underlying data, and paired with an honestly-calibrated
uncertainty range -- which is the whole point of shipping the drill-down
chart, now with both the 68% (green) and 95% (blue) bands drawn directly
on it, on `/market-trends`
alongside the map.

## Considered but not built: private mortgage foreclosure statistics

A natural next feature to ask about, and worth documenting why it isn't
here: granular (county-level, monthly) private-lender foreclosure data
is a genuinely commercial product. ATTOM Data Solutions and CoreLogic
both maintain exactly this kind of dataset (ATTOM specifically covers
~3,000 counties), but both are paid/licensed -- no free tier, no public
bulk download, no API key away from a sales conversation, checked
directly against ATTOM's own site before writing this. That rules out
the "just another FRED/Zillow CSV" pattern every other feature in this
pipeline uses.

A free option does exist -- FRED's `DRSFRMACBS` ("Delinquency Rate on
Single-Family Residential Mortgages, Booked in Domestic Offices, All
Commercial Banks"), same no-API-key `fredgraph.csv` pattern as the
mortgage rate feature above -- but it's **national only and quarterly**,
not county-level and not monthly. Given this model already has one
national-only feature (mortgage rate) whose value just turned out to be
genuinely mixed by state, and every other feature here is county-level
and monthly, a second coarse national series is unlikely to earn its
keep without evidence otherwise; not added without that evidence.

The genuinely useful version -- granular, county-level, monthly, free --
doesn't appear to exist as a ready-made dataset. The closest realistic
path is public records: foreclosure filings are public court/recording
documents in both Texas and Pennsylvania, so county-by-county scraping
is *possible* in principle, the same category of work as this project's
own tax-sale scrapers (see the main project's `realauction_scraper.py`
and `bid4assets_scraper.py`, and note Allegheny County, PA's own Sheriff
Sale data already mixes "Mortgage Foreclosure" and tax-lien sale types
in one feed, confirmed while researching PA tax sale sources for the
scraper side of this project). That's real, standalone scraper-building
work per county, not a quick model feature the way mortgage rate was --
a candidate for its own future project, not a follow-up to this one.

## Next steps

GovLandScout's own scraped listing history now spans several months and
multiple states (Texas and, as of this month, Pennsylvania across six
different scraped sources -- see the main project README's commit
history). Once it has enough months behind it to compute county-level
rolling features from (mirroring what this model already does with
Zillow's price-cut data), it becomes a candidate to fold in as an
additional feature -- or, further out, a target in its own right.

Retuning the random forest's hyperparameters (see "Hyperparameter
search" above) has now gone through two passes -- `RANDOM_SEARCH_ITER`
15 then 30, the second also with a denser `RF_SEARCH_SPACE` grid over
the same range -- and the second pass's own result is itself the answer
to whether a third pass is worth running soon: three of six
horizon/state combinations improved further, three came out marginally
worse, all swings under 1 percentage point, smaller than this project's
own walk-forward fold-to-fold noise. That's a genuine sign of
diminishing returns from more random search alone on this feature set,
not a reason to stop tuning forever -- but the next real gain here is
more likely to come from changing what's being searched (e.g. feature
engineering, or the per-county/subdivided-tract calibration idea below)
than from a fourth pass over the same kind of space.

Tuning gradient boosting's own hyperparameters (`random_search_gb()`,
same section above) went through the same two passes, with a more
consistent payoff: GB improved further at four of six horizon/state
combinations in the second pass (most notably Texas's 6-month result,
50.5% -> 52.3%), and it now wins at **every** Texas horizon and 2 of 3
Pennsylvania ones -- a real, fairly-earned result, not an artifact of
only one side of the comparison getting attention. That's no longer an
open decision to make -- it's why GB is the production model as of this
version of this README (see git history for the RF-era version). What
that switch actually required is now real, shipped work rather than a
future blocker: `fit_gb_production_model()`'s four quantile models plus
`calibrate_gb_quantiles()`'s conformal widening (see "Uncertainty, not
just a point estimate" above), not the "quantile regression and
conformal prediction... are the natural candidates" hand-wave this
section used to make.

That real implementation has its own real, documented simplifications
worth revisiting, though, not a finished, closed topic:

- **The four quantile models share the mean model's hyperparameters**
  (see `make_gb_quantile_model()`'s own docstring) rather than each being
  independently tuned. A quantile loss surface isn't the same shape as
  squared-error's, so the mean model's regularization strength is a
  reasonable starting assumption, not a verified-optimal one for the
  tails specifically -- an independent `random_search_gb()` pass per
  quantile (4x the search cost already run twice for the mean model)
  is the natural next step if the calibration deltas above ever look too
  large to just be measurement noise.
- **The calibration deltas are a single number per horizon per state**,
  same limitation the old RF `c68`/`c95` scalars had -- correcting the
  *average* overconfidence, not per-county variation in how well-fit the
  raw interval already is. A learned, feature-dependent conformity score
  (rather than a flat delta) would be more precise, at the cost of
  needing more held-out data than a single scalar does per horizon --
  same "worth revisiting once there's more history" conclusion the
  RF-era version of this section reached, still true here.
- **The shipped ± band is a symmetric approximation of a genuinely
  asymmetric calibrated interval** (see `predict_with_interval()`'s own
  docstring for the exact tradeoff) -- `/market-trends` could show the
  true asymmetric `[lower, upper]` bounds directly instead, now that the
  green/blue whisker pair already breaks from a single flat ± number;
  not done yet because it's a real UI redesign (two edges instead of one
  symmetric radius), not a data change.

Investigating why mortgage rate helped Pennsylvania's random forest
cleanly but originally hurt Texas's 6-month one (a real, documented
finding from before RF was tuned at all -- see git history for that
version of this section) is still an open question about *why* the two
states diverged, but no longer a production accuracy problem riding on
the answer -- RF isn't production anymore, and GB never showed the same
mortgage-rate weakness RF once did.
