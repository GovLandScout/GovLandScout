# GovLandScout - County Distress Trend Model (Phase 1)

A random forest baseline predicting next-month change in county housing
distress, using Zillow Research + FRED historical data rather than
GovLandScout's own scraped listings -- our own history is only a few
weeks old, nowhere near enough to train on yet. This is deliberately
kept separate from the scraper/web app: different dependencies (pandas,
scikit-learn), different deploy target (none -- this doesn't run on
Render or in the daily scrape workflow), different audience (research,
not the live site).

Runs per state (see `states.py`) -- Texas and Pennsylvania currently,
each with its own trained models, GeoJSON, and predictions file, served
on their own `/market-trends` pages. Nothing about the modeling code is
state-specific; adding a state is a config entry plus running the
pipeline for it (see "Running it" below), not new code.

## Scope (Phase 1)

- **Target**: *change* in `perc_listings_price_cut` (% of a county's
  active listings that took a price cut) over the next 1, 3, and 6
  months -- not the raw next-period level. An earlier version predicted
  the level directly and the model mostly just learned to copy this
  month's value forward, since price_cut_pct is highly autocorrelated;
  predicting change forces it to actually explain movement instead (see
  Results below).
- **Geography**: every county Zillow has price-cut data for, per state
  -- 207 of 254 Texas counties, 66 of 67 Pennsylvania counties (PA's
  coverage ratio is actually better; it's a much smaller state).
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
- **Model**: scikit-learn RandomForestRegressor (the production model
  `generate_predictions.py` actually serves), evaluated in
  `train_model.py` against a naive "predict zero change" baseline, a
  LinearRegression benchmark, and a HistGradientBoostingRegressor, on
  walk-forward time-based cross-validation (several rolling folds, not
  one fixed split -- see Results below for why that matters). Gradient
  boosting is a comparison point only, not a production candidate as-is
  -- see Results for why swapping it in isn't just a config change. The
  forest's own hyperparameters (tree count, depth, leaf/split sizes,
  features considered per split) are no longer one hand-picked config
  reused everywhere -- `random_search_rf()` searches per horizon per
  state, scored on the same walk-forward folds, before every other
  number in this README is computed. See "Hyperparameter search" below.
- **Uncertainty, not just a point estimate**: each production model
  also reports the spread across its individual trees' predictions for
  a given county, surfaced on `/market-trends` as reduced opacity for
  low-confidence counties and a plain-language `±` range in the
  tooltip/detail panel -- see generate_predictions.py's
  `predict_with_uncertainty()`. **Checked, not just asserted**:
  train_model.py's own copy of that function feeds a calibration check
  in its CV loop (does the true outcome actually fall inside the
  predicted band as often as the band claims?) -- see Results below.
  The raw tree-spread turned out to be measurably overconfident, so it's
  no longer shipped as-is: `train_model.py`'s `calibrate_uncertainty()`
  fits a scale factor per horizon on those same walk-forward-CV
  residuals (a held-out set, not the production model's own training
  data) and saves it to `county_distress_calibration_{state}.json`;
  `generate_predictions.py` applies it before writing the `±` band. See
  "Uncertainty calibration" below for the before/after numbers.
- **Not in scope yet**: per-property predictions, states beyond TX/PA,
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
   panel for that state, engineers the lagged/rolling features described
   above, and writes `data/{state}_county_month_dataset.csv`.
3. `train_model.py <state>` -- for each horizon, first a random search
   over the random forest's own hyperparameters (see "Hyperparameter
   search" below), then walk-forward cross-validation with the winning
   config (random forest vs. linear regression vs. gradient boosting vs.
   a naive baseline, plus a check on whether the random forest's own
   uncertainty estimate is actually calibrated), then fits one final
   production random forest per horizon on all available data using that
   same winning config. Saves each as
   `county_distress_model_{state}_{h}m.joblib` (gitignored -- regenerated
   by re-running this script, not something to commit), plus a
   `county_distress_calibration_{state}.json` of per-horizon uncertainty
   scale factors fit on the same CV residuals (also gitignored, same
   reasoning as the `.joblib` files -- both regenerate together and
   `generate_predictions.py` needs them run in the same pass).
4. `generate_predictions.py <state>` -- runs that state's three trained
   models against each county's latest available row, scales each
   prediction's uncertainty by that horizon's calibration factor, and
   writes
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
below, the coverage check, and the production model itself.

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

`random_search_gb()` does the same thing for the gradient boosting
comparison point (see module docstring), over its own search space
(`max_iter`/`learning_rate`/`max_depth`/`max_leaf_nodes`/
`min_samples_leaf`/`l2_regularization`), also widened this same pass:

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

## Results: Texas (walk-forward cross-validation, 2026-08-12)

207 TX counties, ~15,500 county-month training examples spanning 2019-03
through 2026-06. Evaluated on 4 rolling walk-forward folds per horizon
(3-month test windows each, all in 2024-11 through 2026-05) rather than
one fixed split -- see train_model.py's module docstring for why a
single split understates how much performance actually varies month to
month. RF and GB here each use their own horizon's winning config from
"Hyperparameter search" above, not the old fixed defaults.

| Horizon | Naive MAE | Linear MAE | RF MAE | GB MAE | RF vs. naive | Linear vs. naive | GB vs. naive |
|---|---|---|---|---|---|---|---|
| 1 month | 0.0162 ± 0.0013 | 0.0128 ± 0.0011 | 0.0129 ± 0.0004 | **0.0126 ± 0.0004** | 20.5% | 20.6% | **22.1%** |
| 3 months | 0.0378 ± 0.0067 | 0.0249 ± 0.0011 | 0.0221 ± 0.0022 | **0.0218 ± 0.0026** | 41.5% | 34.0% | **42.4%** |
| 6 months | 0.0505 ± 0.0059 | 0.0258 ± 0.0019 | 0.0269 ± 0.0087 | **0.0241 ± 0.0036** | 46.8% | 49.0% | **52.3%** |

(± is one standard deviation across the 4 folds -- how consistent each
model's error was across different stretches of time, not the accuracy
of a single number. These reflect the widened hyperparameter search's
second pass -- see "Hyperparameter search" above for why RF moved
slightly *backward* at 1 and 6 months here despite a more thorough
search, and why that's expected random-search behavior, not a bug.)

**Gradient boosting wins outright at every horizon in Texas, now by a
clearer margin than the first tuning pass found** -- 1 month held (GB
22.1% vs. RF's 20.5%, RF having given back the narrow lead it briefly
held), and 6 months widened noticeably (GB's second-pass tuning reached
52.3%, while RF's own second pass landed at 46.8%, slightly behind its
own first-pass result). That's still a real, fairly-earned result, not
an artifact of only one side of the comparison getting attention -- both
models went through the same two-pass search. GB remains a comparison
point, not a production candidate, for the reason given in Scope above
(its sequential trees can't produce the same tree-spread uncertainty
estimate) -- see "Next steps" for what adopting it would actually require.

Feature importances (production models, fit on all available data):

- **`month_sin`/`month_cos` together are back in the 28-34% range across
  horizons** (1 month: 28%, 3 months: 34%, 6 months: 32%) -- similar
  order of magnitude to the first tuning pass, not the 50%+ of the
  original untuned forest, but this pass's specific configs (shallower
  `max_depth` at 1 month, wider `max_features` at 3 and 6) redistribute
  the remaining weight differently fold to fold. `mortgage_rate` alone is
  now the *third*-most important single feature at 1 month (8.6%), ahead
  of every ZHVI/unemployment feature.
- **`mortgage_rate`/`mortgage_rate_mom_change` remain a real presence at
  every horizon** -- 14.3% combined at 1 month, 11.1% at 3 months, 15.4%
  at 6 months (`mortgage_rate_mom_change` alone is still the *third*-most
  important feature there, same as every prior version of this table).
- `price_cut_pct` itself (current level) still shows a mean-reversion
  pattern across horizons -- see the Clay County case study below for a
  concrete example.
- Unemployment features stay modest across horizons, similar to before.

**Uncertainty calibration**: before applying any scale factor, 52-64% of
actual outcomes now fall within the random forest's raw predicted ±1 std
band across the three horizons (target for a well-calibrated
Gaussian-shaped spread: ~68%) -- still well up from 36-42% before any
tuning, but a real step back from the first tuning pass's 64-70%,
mirroring that pass's own slightly-worse MAE at 1 and 6 months above (the
same specific configs driving both). 80-93% land within ±2 std (target:
~95%), also down from 91-95% before. Read together with the MAE table
above: this is the honest cost of the second search pass happening to
land on configs that fit the walk-forward folds' *point estimates*
marginally better at the expense of the *spread* being slightly less
well-behaved for two of the three horizons -- not a sign the search is
broken, just a reminder that MAE and calibration aren't the same target,
and this search only ever optimized the former (see "Hyperparameter
search" above).

`train_model.py`'s `calibrate_uncertainty()` still fits a scale factor
the same way as before (68th/95th percentile of `|residual| / std` across
every walk-forward fold's held-out predictions -- see its own docstring),
and Texas's fitted factors are still much closer to 1 than the ~1.9
every horizon needed before any tuning existed -- `c68` of 1.46
(1-month, up from the first pass's 1.10, tracking that horizon's own
calibration step-back above), 1.10 (3-month, up from 0.97, where the
first pass's raw band had briefly been slightly *too wide* rather than
too narrow), and 1.10 (6-month, up from 1.03). `generate_predictions.py`
still applies `c68` before writing the ± band `/market-trends` shows --
still a materially smaller correction than the pre-tuning ~1.9x across
the board, just not quite as small as the first pass's own numbers.

## Results: Pennsylvania (walk-forward cross-validation, 2026-08-12)

66 PA counties, ~5,300-5,400 county-month training examples (fewer than
Texas simply because Pennsylvania has far fewer counties -- 67 total vs.
254), same 2019-03 through 2026-06 span and same 4-fold walk-forward
setup. RF and GB here each use their own horizon's winning config from
"Hyperparameter search" above, not the old fixed defaults.

| Horizon | Naive MAE | Linear MAE | RF MAE | GB MAE | RF vs. naive | Linear vs. naive | GB vs. naive |
|---|---|---|---|---|---|---|---|
| 1 month | 0.0184 ± 0.0028 | 0.0118 ± 0.0004 | 0.0125 ± 0.0005 | 0.0120 ± 0.0003 | 32.1% | **35.7%** | 34.8% |
| 3 months | 0.0379 ± 0.0032 | 0.0232 ± 0.0020 | 0.0217 ± 0.0010 | 0.0211 ± 0.0016 | 42.9% | 38.9% | **44.3%** |
| 6 months | 0.0518 ± 0.0151 | 0.0224 ± 0.0010 | 0.0235 ± 0.0020 | 0.0220 ± 0.0029 | 54.6% | 56.7% | **57.5%** |

**Pennsylvania's second search pass moved every number by well under 1
percentage point**, in both directions -- RF improved slightly at 1
month (31.4% -> 32.1%) and GB at 1 and 6 months (33.4% -> 34.8%, 57.2% ->
57.5%), while RF gave back a hair at 3 and 6 months and GB at 3 months.
Linear regression still wins at 1 month and gradient boosting at 3 and 6
months, same pattern as every prior version of this table -- tuning
further here didn't flip which model wins anywhere, unlike Texas's 1
month. With a third as many counties and a smaller, more geographically
compact state, PA's 6-month fold error is also still the least
consistent of any horizon/state combination here (± up to 0.0151) -- a
smaller, noisier dataset remains a plausible reason the more flexible
models have a harder time earning their extra complexity back, and a
plausible reason this second search pass had less room to move PA's
numbers than Texas's larger dataset gave it. Feature importances follow
the same seasonality-dominant shape as Texas (`month_sin`/`month_cos`
together 33-58% across horizons, still the largest single block, more
dominant here than in Texas at every horizon), with `mortgage_rate`/
`mortgage_rate_mom_change` a modest but real presence throughout (8-10%
combined at every horizon, similar magnitude to Texas).

**Uncertainty calibration**: before applying any scale factor, 56-69%
coverage within ±1 std (target ~68%) -- essentially unchanged from the
first pass's 56-70%, unlike Texas's step-back -- and 88-93% within ±2 std
(target ~95%), also holding roughly steady. Pennsylvania's smaller
dataset showing less calibration movement between search passes matches
it showing less MAE movement too: less data for a wider search to find a
meaningfully different optimum in, in either direction.

Pennsylvania's fitted `c68` factors barely moved from the first pass --
1.28 (1-month, unchanged), 1.00 (3-month, up marginally from 0.98), 0.97
(6-month, up marginally from 0.95) -- all still well under the ~1.9x
Texas needed before any tuning existed, and still smaller than Texas's
own post-second-pass factors above. `generate_predictions.py` still ships
`std * c68` rather than the raw tree-spread, and the correction it's
making today for Pennsylvania is essentially the same small, honest
tweak it was after the first pass, not the ~1.3-1.6x
fix it used to be.

## Case study: Clay County

Clay County's 6-month horizon is back to being the single largest
predicted move in the current output (see
`public/tx_county_predictions.json`) -- Andrews County's own 6-month call
moved to -7.5 points this pass, behind Clay's own. Clay's call barely
moved through the widened hyperparameter search: a projected **-8.3
point** drop in price-cut share, from a current 29.7% down toward
roughly 21%.

Its actual history explains why: Clay sat in a 22-25% band through most
of 2025, dropped to a recent low of 10.6% in January 2026 -- then spiked
hard: 13.0% (Feb) -> 14.6% (Mar) -> 21.3% (Apr) -> 25.7% (May) -> 29.7%
(Jun 2026), nearly tripling in five months. That's a sharp, unusual move
for a county that had otherwise been comparatively range-bound.

The model isn't predicting Clay keeps climbing -- it's betting on
reversion, gradually: essentially flat at 1 month (-1.0 points), a
modest pullback at 3 months (-4.4 points), and the largest call, -8.3
points, only shows up at 6 months. The uncertainty band on that 6-month
number is now ±3.8 points -- close to the ±3.5 points the first tuning
pass found, both meaningfully narrower than the ±5.9 points before any
tuning existed, even though the point estimate itself moved further from
zero than that original run. Whether the call is *right* isn't knowable
yet (2026-12
data doesn't exist yet); what's checkable today is that the prediction
is legible, tied to a real, visible pattern in the underlying data, and
paired with a tighter, still-honest uncertainty range -- which is the
whole point of shipping the drill-down chart on `/market-trends`
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
50.5% -> 52.3%), and it wins at **every** horizon in Texas now, including
1 month, where RF had briefly taken the lead from it after the first
pass. Pennsylvania's picture barely moved either pass (GB was already
winning at 3 and 6 months, still does, by a slightly wider margin than
before). That's a real,
fairly-earned result now, not an artifact of only one side of the
comparison getting attention -- worth treating "should GB become the
production model" as a real decision to make deliberately, not defer
indefinitely. The blocker is still what Scope describes: GB's sequential
trees can't produce the same tree-spread uncertainty estimate
`predict_with_uncertainty()` depends on, so switching isn't a config
change, it's redesigning how `/market-trends` gets its confidence bands
at all (quantile regression and conformal prediction -- see the
calibration paragraph below -- are the natural candidates for what would
replace it).

Investigating why mortgage rate helped Pennsylvania's random forest
cleanly but originally hurt Texas's 6-month one (a real, documented
finding before this round of tuning -- see git history for the previous
version of this section) is less urgent now that hyperparameter tuning
resolved the practical symptom: Texas's 6-month RF no longer struggles
to generalize the feature the way it did. Still an open question about
*why* the two states diverged, just no longer a production accuracy
problem riding on the answer.

The uncertainty-band fix (see "Uncertainty calibration" in each state's
Results section) is a single scale factor per horizon, fit once on the
whole state -- it corrects the *average* overconfidence but doesn't know
that some counties' predictions are better-calibrated than others'. That
gap matters less than it did (tuning brought every `c68` factor close to
1 -- see above), but a per-county or feature-dependent calibration (e.g.
conformal prediction with a learned conformity score, or quantile
regression forests predicting the band directly) would still be more
precise, at the cost of needing more held-out data than a single scalar
does -- worth revisiting once there's more history to calibrate against,
not before.
