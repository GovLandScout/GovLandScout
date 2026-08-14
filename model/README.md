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
per state), it samples `RANDOM_SEARCH_ITER` (15) random combinations of
`n_estimators`/`max_depth`/`min_samples_leaf`/`min_samples_split`/
`max_features` from `RF_SEARCH_SPACE`, plus the old hand-picked config
(`n_estimators=300, max_depth=10, min_samples_leaf=5`) as a guaranteed
16th candidate, and scores each by mean MAE across the *same*
walk-forward folds used everywhere else in this file -- not sklearn's own
`RandomizedSearchCV`, whose default k-fold would shuffle a county's later
months into the same fold as its earlier ones, the exact future-leak
walk-forward CV exists to avoid (see train_model.py's module docstring).
The winning config feeds every RF fit downstream: the comparison table
below, the coverage check, and the production model itself.

This turned out to matter well beyond the Texas 6-month weakness that
motivated it (see the previous version of this section in git history --
that model beat naive by only 36.1% there, worst of any horizon/state,
with the least consistent fold-to-fold error of any combination this
project has measured). Every one of the six horizon/state combinations
below improved, most by a small amount, one by a lot:

| State | Horizon | Winning config (vs. the old default) | MAE improvement |
|---|---|---|---|
| TX | 1 month | `max_depth=None, min_samples_split=10, max_features='sqrt'` | 6.8% |
| TX | 3 months | `max_depth=None, min_samples_leaf=1, min_samples_split=5, max_features=1.0` | 3.7% |
| TX | 6 months | `n_estimators=200, max_depth=None, min_samples_split=2, max_features='log2'` | **17.1%** |
| PA | 1 month | `max_depth=None, min_samples_split=10, max_features='sqrt'` | 1.1% |
| PA | 3 months | `n_estimators=200, max_depth=20, min_samples_leaf=1, min_samples_split=2, max_features=1.0` | 1.5% |
| PA | 6 months | `n_estimators=200, max_depth=20, min_samples_leaf=1, min_samples_split=2, max_features=1.0` | 2.8% |

A pattern worth naming: every winning config keeps `max_depth` at 10 or
deeper (mostly unbounded) and widens `min_samples_split`/`max_features`
instead -- the old default's `max_depth=10` was apparently the wrong
lever for controlling overfitting on this feature set, constraining tree
*depth* uniformly rather than how much data or how many features get
considered at a given split. Also unexpected: this search targeted MAE
only, not calibration, but tuning fixed most of the uncertainty-band
problem as a side effect too -- see "Uncertainty calibration" in each
state's section below.

`random_search_gb()` does the same thing for the gradient boosting
comparison point (see module docstring), over its own search space
(`max_iter`/`learning_rate`/`max_depth`/`max_leaf_nodes`/
`min_samples_leaf`/`l2_regularization`):

| State | Horizon | Winning config (vs. the old default) | MAE improvement |
|---|---|---|---|
| TX | 1 month | `max_iter=500, max_depth=3, min_samples_leaf=30, l2_regularization=0.5` | 1.8% |
| TX | 3 months | `max_iter=400, max_depth=None, min_samples_leaf=20` | 1.0% |
| TX | 6 months | `max_iter=500, max_depth=3, min_samples_leaf=30, l2_regularization=0.5` | 2.8% |
| PA | 1 month | `max_iter=400, max_depth=None, min_samples_leaf=20` | 0.9% |
| PA | 3 months | `max_iter=400, max_depth=None, min_samples_leaf=20` | 1.3% |
| PA | 6 months | `max_iter=500, learning_rate=0.03, max_depth=10, min_samples_leaf=10, l2_regularization=1.0` | 0.7% |

Smaller gains than RF's across the board -- unsurprising, since the old
hand-picked GB config was never as clearly wrong as RF's fixed
`max_depth=10` turned out to be (see the pattern noted above). But small
and real still moves the comparison: see each state's Results section
below for what tuning *both* sides changed about which model actually
wins at which horizon.

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
| 1 month | 0.0162 ± 0.0013 | 0.0128 ± 0.0011 | 0.0127 ± 0.0004 | **0.0127 ± 0.0005** | 21.2% | 20.6% | **21.8%** |
| 3 months | 0.0378 ± 0.0067 | 0.0249 ± 0.0011 | 0.0222 ± 0.0017 | **0.0217 ± 0.0025** | 41.3% | 34.0% | **42.5%** |
| 6 months | 0.0505 ± 0.0059 | 0.0258 ± 0.0019 | 0.0267 ± 0.0072 | **0.0250 ± 0.0036** | 47.1% | 49.0% | **50.5%** |

(± is one standard deviation across the 4 folds -- how consistent each
model's error was across different stretches of time, not the accuracy
of a single number.)

**Once both models are actually tuned, gradient boosting wins outright
at every horizon in Texas**, including 1 month, where the random forest
had just taken the lead from it (21.2%) by a hair -- tuning GB too pushed
it back ahead (21.8%). That's a real, if modest, answer to the question
"is the comparison table honest, or does GB just look better because
nobody tuned RF as hard" (see the Scope section's original framing): with
both sides tuned, GB comes out ahead everywhere, not by an accident of
which model happened to get attention first. **6 months also improved
further past the hyperparameter-search finding that started this
section** -- the random forest went from the worst result in this whole
table (36.1%, before any tuning existed) to 47.1%, and GB's own tuning
pushed the horizon's best result to 50.5%. GB remains a comparison point,
not a production candidate, for the reason given in Scope above (its
sequential trees can't produce the same tree-spread uncertainty
estimate) -- but a comparison point this consistently ahead, on a fairly
tuned comparison, is exactly the kind of finding "Next steps" flags as
worth someday revisiting that production-model decision over.

Feature importances (production models, fit on all available data):

- **`month_sin`/`month_cos` are still present at every horizon but no
  longer dominate as heavily as before tuning**, especially at 1 month:
  together they're now ~22% (was closer to 50%+ pre-tuning), with
  `inventory_mom_pct`, `price_cut_pct_lag1`, and `price_cut_pct_roll3`
  each now carrying real, comparable weight (7-8% apiece). A shallower,
  more heavily-split-regularized forest (see the hyperparameter pattern
  above) apparently spreads its splits across more of the 15 features
  instead of leaning on the two strongest ones as hard.
- **`mortgage_rate`/`mortgage_rate_mom_change` remain a real presence at
  every horizon** -- around 12% combined at 1 month, 8% at 3 months, and
  15% combined at 6 months (`mortgage_rate_mom_change` alone is the
  *third*-most important feature there). Unlike before tuning, the
  random forest at 6 months is no longer struggling to generalize this
  signal (see the MAE improvement above) -- it's simply using it.
- `price_cut_pct` itself (current level) still shows a mean-reversion
  pattern across horizons -- see the Clay County case study below for a
  concrete example.
- Unemployment features stay modest across horizons, similar to before.

**Uncertainty calibration**: before applying any scale factor, 64-70% of
actual outcomes now fall within the random forest's raw predicted ±1 std
band across the three horizons (target for a well-calibrated
Gaussian-shaped spread: ~68%) -- **up from 36-42% before tuning**, close
enough to the target that the forest's own tree-spread is now nearly
honest on its own, not something that needed a ~2x correction. 91-95%
land within ±2 std (target: ~95%), also much closer than the 69-71%
before.

`train_model.py`'s `calibrate_uncertainty()` still fits a scale factor
the same way as before (68th/95th percentile of `|residual| / std` across
every walk-forward fold's held-out predictions -- see its own docstring),
but Texas's fitted factors are now much closer to 1 -- `c68` of 1.10
(1-month), 0.97 (3-month, actually *below* 1, meaning the raw band was
briefly slightly *too wide* there rather than too narrow), and 1.03
(6-month) -- compared to ~1.9 at every horizon before tuning.
`generate_predictions.py` still applies `c68` before writing the ± band
`/market-trends` shows, but the correction it's making today is a small
honest tweak, not the roughly-doubling fix it used to be.

## Results: Pennsylvania (walk-forward cross-validation, 2026-08-12)

66 PA counties, ~5,300-5,400 county-month training examples (fewer than
Texas simply because Pennsylvania has far fewer counties -- 67 total vs.
254), same 2019-03 through 2026-06 span and same 4-fold walk-forward
setup. RF and GB here each use their own horizon's winning config from
"Hyperparameter search" above, not the old fixed defaults.

| Horizon | Naive MAE | Linear MAE | RF MAE | GB MAE | RF vs. naive | Linear vs. naive | GB vs. naive |
|---|---|---|---|---|---|---|---|
| 1 month | 0.0184 ± 0.0028 | 0.0118 ± 0.0004 | 0.0126 ± 0.0005 | 0.0122 ± 0.0002 | 31.4% | **35.7%** | 33.4% |
| 3 months | 0.0379 ± 0.0032 | 0.0232 ± 0.0020 | 0.0216 ± 0.0010 | 0.0210 ± 0.0015 | 43.0% | 38.9% | **44.6%** |
| 6 months | 0.0518 ± 0.0151 | 0.0224 ± 0.0010 | 0.0234 ± 0.0019 | 0.0222 ± 0.0023 | 54.9% | 56.7% | **57.2%** |

**Pennsylvania's gains from tuning were real but modest on both sides**
-- 1-3 percentage points of MAE improvement at every horizon for RF, well
under 2 points for GB, versus Texas's much larger 6-month RF jump. Linear
regression still wins at 1 month and gradient boosting at 3 and 6 months,
same pattern as before GB was tuned too -- unlike Texas, tuning both
models here didn't flip which one wins anywhere, just widened gradient
boosting's existing edge at 3 and 6 months a little further (43.9% ->
44.6%, 56.9% -> 57.2%). The random forest -- the one actually deployed --
still narrowed the gap at every horizon versus its own pre-tuning
self. With a third as many counties and a smaller, more geographically compact
state, PA's 6-month fold error is also still the least consistent of any
horizon/state combination here (± up to 0.0151) -- a smaller, noisier
dataset is a plausible reason the more flexible models have a harder time
earning their extra complexity back. Feature importances follow the same
seasonality-dominant shape as Texas (`month_sin`/`month_cos` together
28-57% across horizons, still the largest single block but less
dominant at 1 and 3 months than before tuning -- `inventory_mom_pct`
alone is now the second-most important feature at 1 month, 12%), with
`mortgage_rate`/`mortgage_rate_mom_change` a modest but real presence
throughout (8-10% combined at every horizon, similar magnitude to Texas).

**Uncertainty calibration**: before applying any scale factor, 56-70%
coverage within ±1 std (target ~68%) -- **up from 45-55% before
tuning**, and 87-94% within ±2 std (target ~95%), also closer than
79-85% before. Same pattern as Texas: tuning for MAE alone fixed most of
the calibration gap as a side effect.

Pennsylvania's fitted `c68` factors are correspondingly smaller than
before tuning too -- 1.28 (1-month), 0.98 (3-month, like Texas's 3-month
figure, actually *below* 1), 0.95 (6-month) -- down from 1.58/1.36/1.29.
`c68` shrinking as the horizon lengthens is the same direction Texas
moved in this time (unlike before tuning, when the two states' factors
moved in opposite directions with horizon) -- one more sign that
tuning brought the two states' calibration behavior closer together, not
just their raw accuracy. `generate_predictions.py` still ships `std *
c68` rather than the raw tree-spread, but -- as in Texas -- the
correction it's making today is a small honest tweak, not the ~1.3-1.6x
fix it used to be.

## Case study: Clay County

Clay County's 6-month horizon is still one of the largest predicted
moves in the current output (see `public/tx_county_predictions.json`),
though no longer the single largest -- Andrews County's 6-month call
(-8.75 points) is now marginally bigger. Clay's own call barely moved
through hyperparameter tuning: a projected **-8.7 point** drop in
price-cut share, from a current 29.7% down toward roughly 21% (was -7.9
points before tuning).

Its actual history explains why: Clay sat in a 22-25% band through most
of 2025, dropped to a recent low of 10.6% in January 2026 -- then spiked
hard: 13.0% (Feb) -> 14.6% (Mar) -> 21.3% (Apr) -> 25.7% (May) -> 29.7%
(Jun 2026), nearly tripling in five months. That's a sharp, unusual move
for a county that had otherwise been comparatively range-bound.

The model isn't predicting Clay keeps climbing -- it's betting on
reversion, gradually: essentially flat at 1 month (-1.2 points), a
modest pullback at 3 months (-4.7 points), and the largest call, -8.7
points, only shows up at 6 months. The uncertainty band on that 6-month
number is now ±3.5 points -- *narrower* than the ±5.9 points before
tuning, even though the point estimate itself moved further from zero,
because the tuned forest's raw tree-spread is now close to honestly
calibrated (see "Uncertainty calibration" above) rather than needing a
~2x correction. Whether the call is *right* isn't knowable yet (2026-12
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
search" above) is now done, not an open item -- but the search itself
suggests two concrete follow-ups rather than closing the topic
entirely: `RANDOM_SEARCH_ITER` is currently 15 candidates per
horizon/state; a wider or more targeted search (e.g. sampling more
densely around the winning configs found here, which consistently
avoided a shallow `max_depth`) might find further gains, at the cost of
more CV fitting time.

Tuning gradient boosting's own hyperparameters (`random_search_gb()`,
same section above) is also now done, not an open item -- and it changed
the answer to "is GB actually better, or just untuned RF losing to a
better-tuned comparison": in Texas, GB now wins at **every** horizon,
including 1 month, where RF had just taken the lead from it by tuning
alone. Pennsylvania's picture barely moved (GB was already winning at 3
and 6 months, still does, by a slightly wider margin). That's a real,
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
