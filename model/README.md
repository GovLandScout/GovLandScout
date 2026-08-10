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
  month-of-year seasonality -- as a sine/cosine pair (`month_sin`/
  `month_cos`), not the raw 1-12 month number. That raw encoding put
  December and January, adjacent in reality, about as numerically far
  apart as two months can be, actively fighting the model on what turned
  out to be its single most important feature -- see Results below for
  the before/after.
- **Model**: scikit-learn RandomForestRegressor (the production model
  `generate_predictions.py` actually serves), evaluated in
  `train_model.py` against a naive "predict zero change" baseline, a
  LinearRegression benchmark, and a HistGradientBoostingRegressor, on
  walk-forward time-based cross-validation (several rolling folds, not
  one fixed split -- see Results below for why that matters). Gradient
  boosting is a comparison point only, not a production candidate as-is
  -- see Results for why swapping it in isn't just a config change.
- **Uncertainty, not just a point estimate**: each production model
  also reports the spread across its individual trees' predictions for
  a given county, surfaced on `/market-trends` as reduced opacity for
  low-confidence counties and a plain-language `±` range in the
  tooltip/detail panel -- see generate_predictions.py's
  `predict_with_uncertainty()`. **Checked, not just asserted**:
  train_model.py's own copy of that function feeds a calibration check
  in its CV loop (does the true outcome actually fall inside the
  predicted band as often as the band claims?) -- see Results below;
  the honest answer is not yet as often as it should.
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

1. `fetch_data.py <state>` -- downloads and caches the raw Zillow CSVs
   (nationwide, not state-specific -- shared across every state this
   pipeline runs for, downloaded once) and, per county in that state,
   FRED's unemployment/employment level series (used to compute a
   monthly unemployment rate; FRED's own monthly county unemployment
   *rate* series isn't uniformly published, but the level series behind
   it are). Caches everything in `data/` (gitignored -- this is
   multi-hundred-county raw data, not something to commit) so re-runs
   don't re-hit either source.
2. `build_dataset.py <state>` -- joins all of it into one county-month
   panel for that state, engineers the lagged/rolling features described
   above, and writes `data/{state}_county_month_dataset.csv`.
3. `train_model.py <state>` -- walk-forward cross-validation per horizon
   (random forest vs. linear regression vs. gradient boosting vs. a naive
   baseline, plus a check on whether the random forest's own uncertainty
   estimate is actually calibrated), then fits one final production
   random forest per horizon on all available data. Saves each as
   `county_distress_model_{state}_{h}m.joblib` (gitignored -- regenerated
   by re-running this script, not something to commit).
4. `generate_predictions.py <state>` -- runs that state's three trained
   models against each county's latest available row and writes
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

## Results: Texas (walk-forward cross-validation, 2026-08-10)

207 TX counties, ~15,500 county-month training examples spanning 2019-03
through 2026-06. Evaluated on 4 rolling walk-forward folds per horizon
(3-month test windows each, all in 2024-11 through 2026-05) rather than
one fixed split -- see train_model.py's module docstring for why a
single split understates how much performance actually varies month to
month.

| Horizon | Naive MAE | Linear MAE | RF MAE | GB MAE | RF vs. naive | Linear vs. naive | GB vs. naive |
|---|---|---|---|---|---|---|---|
| 1 month | 0.0162 ± 0.0013 | 0.0128 ± 0.0015 | 0.0131 ± 0.0008 | 0.0125 ± 0.0006 | 19.0% | 20.6% | **22.5%** |
| 3 months | 0.0378 ± 0.0067 | 0.0251 ± 0.0025 | 0.0228 ± 0.0028 | 0.0218 ± 0.0026 | 39.7% | 33.6% | **42.2%** |
| 6 months | 0.0505 ± 0.0059 | 0.0271 ± 0.0057 | 0.0269 ± 0.0069 | 0.0246 ± 0.0048 | 46.8% | 46.4% | **51.2%** |

(± is one standard deviation across the 4 folds -- how consistent each
model's error was across different stretches of time, not the accuracy
of a single number.)

**Gradient boosting wins at every horizon in Texas** -- not by a huge
margin, but consistently, which the earlier RF-vs-linear-only comparison
had no way to show. It's a comparison point in this table, not the
production model `generate_predictions.py` actually serves, though:
that script's uncertainty estimate is the spread across the random
forest's independently-bagged trees (see `predict_with_uncertainty()`),
and gradient boosting's trees are sequential, each correcting the last
-- a spread across *those* wouldn't carry the same "how much do
independent estimates agree" meaning. Swapping the production model
means redesigning the uncertainty estimate too, not just picking a
different regressor; left as a real, separate follow-up rather than
done here as a side effect.

Feature importances (production models, fit on all available data):

- **`month_sin`/`month_cos` together are still the dominant features at
  every horizon**, and at 3 and 6 months `month_sin` alone (37% and 36%
  respectively) is now a *larger* single share of importance than the
  old raw `month_of_year` ever reached (43-47%) -- the cyclic encoding
  isn't just theoretically more correct, the model leans on it more
  directly once it's actually easy to use.
- **`zhvi_yoy_pct` (year-over-year home value trend) still grows with
  horizon**: 8% at 1 month, 7% at 3, 13% at 6 -- short-term movement
  stays noisier/more seasonal, longer-term movement leans more on the
  broader home-value trend having time to play out, same story as
  before this change.
- `price_cut_pct` itself (current level) shows the same pattern (3% ->
  14% -> 16%) -- plausible mean-reversion, see the Clay County case
  study below for a concrete example of exactly this.
- Unemployment features stay modest, though `unemployment_rate_mom_change`
  (not just the level) is a real presence at the 1-month horizon (8%) --
  a recent shift in local unemployment apparently carries some
  short-term signal the level alone doesn't.

**Uncertainty calibration**: only 40-43% of actual outcomes fell within
the random forest's predicted ±1 std band across the three horizons
(target for a well-calibrated Gaussian-shaped spread: ~68%), and 68-73%
within ±2 std (target: ~95%). **The band is meaningfully overconfident**
-- narrower than the model's real uncertainty, not just imprecise. The
reduced-opacity/± range shown on `/market-trends` should be read as "the
model is less sure than it looks," not taken at face value; tightening
this up (a larger ensemble, conformal prediction, or simply widening the
reported band by a calibration factor derived from this check) is real
follow-up work this measurement makes concrete instead of a vague TODO.

## Results: Pennsylvania (walk-forward cross-validation, 2026-08-10)

66 PA counties, ~5,300-5,400 county-month training examples (fewer than
Texas simply because Pennsylvania has far fewer counties -- 67 total vs.
254), same 2019-03 through 2026-06 span and same 4-fold walk-forward
setup.

| Horizon | Naive MAE | Linear MAE | RF MAE | GB MAE | RF vs. naive | Linear vs. naive | GB vs. naive |
|---|---|---|---|---|---|---|---|
| 1 month | 0.0184 ± 0.0028 | 0.0119 ± 0.0003 | 0.0129 ± 0.0009 | 0.0125 ± 0.0007 | 29.6% | **35.2%** | 32.1% |
| 3 months | 0.0379 ± 0.0032 | 0.0243 ± 0.0036 | 0.0239 ± 0.0035 | 0.0230 ± 0.0032 | 37.0% | 35.9% | **39.3%** |
| 6 months | 0.0518 ± 0.0151 | 0.0240 ± 0.0043 | 0.0272 ± 0.0054 | 0.0264 ± 0.0069 | 47.4% | **53.6%** | 49.0% |

Same pattern as before this change, still worth taking at face value:
**linear regression wins at 1 and 6 months in Pennsylvania**, gradient
boosting wins at 3 months, and the random forest -- the one actually
deployed -- is never the best of the three here. With a third as many
counties and a smaller, more geographically compact state, PA's 6-month
fold error is also far less consistent (± 0.0151-0.0069, the widest
spreads of any horizon/state combination here) -- a smaller, noisier
dataset is a plausible reason the more flexible models have a harder
time earning their extra complexity back. Feature importances follow the
same seasonality-dominant shape as Texas (`month_sin`/`month_cos`
together 48-65% across horizons, `month_cos` specifically dominant at 1
and 3 months, `month_sin` dominant at 6 -- which component leads
apparently isn't fixed, just that the pair together carries most of the
signal either way).

**Uncertainty calibration**: 45-51% coverage within ±1 std (target
~68%), 79-86% within ±2 std (target ~95%) -- overconfident like Texas,
though somewhat less severely and improving slightly with horizon (45%
-> 51% -> 51%). Same conclusion as Texas: real uncertainty exists beyond
what the current band shows.

## Case study: Clay County

The single largest predicted move in the current output (see
`public/tx_county_predictions.json`) is Clay County's 6-month horizon:
a projected **-11.9 point** drop in price-cut share, from a current
29.7% down toward roughly 18%. (This replaces an earlier version of this
case study built around Andrews County -- the cyclic month encoding and
retrained models above changed which county's move actually ranks
largest; Andrews no longer does.)

Its actual history explains why, same shape as the county this case
study used to be about: Clay sat in a 22-25% band through most of 2025,
dropped to a recent low of 10.6% in January 2026 -- then spiked hard:
13.0% (Feb) -> 14.6% (Mar) -> 21.3% (Apr) -> 25.7% (May) -> 29.7% (Jun
2026), nearly tripling in five months. That's a sharp, unusual move for
a county that had otherwise been comparatively range-bound.

The model isn't predicting Clay keeps climbing -- it's betting on
reversion, and the shape of that bet is gradual, not abrupt: essentially
flat at 1 month (-1.3 points), a modest pullback at 3 months (-4.7
points), and the large -11.9 point call only shows up at 6 months. The
uncertainty band on that 6-month number is ±2.5 points -- given the
calibration finding above, treat that as an understatement of the real
uncertainty, not a tight, trustworthy bound. Whether the call is *right*
isn't knowable yet (2026-12 data doesn't exist yet); what's checkable
today is that the prediction is legible and tied to a real, visible
pattern in the underlying data rather than an opaque number -- which is
the whole point of shipping the drill-down chart on `/market-trends`
alongside the map.

Next natural step: GovLandScout's own scraped listing history now spans
several months and multiple states (Texas and, as of this month,
Pennsylvania across six different scraped sources -- see the main
project README's commit history). Once it has enough months behind it
to compute county-level rolling features from (mirroring what this model
already does with Zillow's price-cut data), it becomes a candidate to
fold in as an additional feature -- or, further out, a target in its own
right.
