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
  feature -- see Results below for the before/after. The mortgage rate
  is national, not per-county, like Zillow's ZHVI/price-cut/inventory
  data itself -- see Results below for a real, mixed finding about
  whether it actually helped.
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
3. `train_model.py <state>` -- walk-forward cross-validation per horizon
   (random forest vs. linear regression vs. gradient boosting vs. a naive
   baseline, plus a check on whether the random forest's own uncertainty
   estimate is actually calibrated), then fits one final production
   random forest per horizon on all available data. Saves each as
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

## Results: Texas (walk-forward cross-validation, 2026-08-11)

207 TX counties, ~15,500 county-month training examples spanning 2019-03
through 2026-06. Evaluated on 4 rolling walk-forward folds per horizon
(3-month test windows each, all in 2024-11 through 2026-05) rather than
one fixed split -- see train_model.py's module docstring for why a
single split understates how much performance actually varies month to
month.

| Horizon | Naive MAE | Linear MAE | RF MAE | GB MAE | RF vs. naive | Linear vs. naive | GB vs. naive |
|---|---|---|---|---|---|---|---|
| 1 month | 0.0162 ± 0.0013 | 0.0128 ± 0.0011 | 0.0137 ± 0.0006 | 0.0129 ± 0.0004 | 15.5% | 20.6% | **20.3%** |
| 3 months | 0.0378 ± 0.0067 | 0.0249 ± 0.0011 | 0.0230 ± 0.0023 | 0.0219 ± 0.0026 | 39.1% | 34.0% | **41.9%** |
| 6 months | 0.0505 ± 0.0059 | 0.0258 ± 0.0019 | 0.0323 ± 0.0116 | 0.0257 ± 0.0064 | 36.1% | **49.0%** | 49.1% |

(± is one standard deviation across the 4 folds -- how consistent each
model's error was across different stretches of time, not the accuracy
of a single number. These numbers are after adding the national mortgage
rate as a feature -- see the honest, mixed finding on that below; the
gradient-boosting-wins-everywhere headline from before this addition no
longer holds cleanly at 1 month.)

**Adding mortgage rate genuinely helped some of these models and
genuinely hurt one, and the one it hurt is the one actually deployed.**
At 6 months specifically, the random forest went from beating naive by
46.8% (before mortgage rate) to just 36.1% -- and its fold-to-fold
consistency got much worse too (± 0.0116, versus ± 0.0069 before; one
fold's error nearly doubled). Linear regression and gradient boosting
both held up fine or improved at 6 months (49.0% and 49.1%, both up from
before). This isn't a case for reverting the feature -- `mortgage_rate`
and `mortgage_rate_mom_change` clearly carry real signal (see feature
importances below), and the *other* two models used it well -- but it's
a real, documented reason the production random forest's 6-month Texas
call should be read with extra caution right now, on top of the
calibration caveat below. Whether that's fixable by retuning the random
forest's own hyperparameters for the larger feature set, or is a sign
the production model choice deserves revisiting per-horizon rather than
fixed at "always random forest," is real follow-up work this table makes
concrete rather than a vague TODO.

Feature importances (production models, fit on all available data):

- **`month_sin`/`month_cos` together are still the dominant features at
  every horizon.**
- **`mortgage_rate`/`mortgage_rate_mom_change` are a real, if secondary,
  presence at every horizon** -- 7-9% combined at 1 and 3 months, and at
  6 months `mortgage_rate_mom_change` alone is the *second*-most
  important feature in the whole model (14%), ahead of every ZHVI and
  unemployment feature. A recent shift in national borrowing costs
  apparently carries real signal about a county's price-cut trajectory
  6 months out -- consistent with the 6-month MAE finding above, even
  though that finding is about the random forest specifically
  struggling to generalize it, not about the feature lacking real signal.
- `price_cut_pct` itself (current level) still shows a mean-reversion
  pattern across horizons -- see the Clay County case study below for a
  concrete example.
- Unemployment features stay modest across horizons, similar to before
  this change.

**Uncertainty calibration**: before calibration, 36-42% of actual
outcomes fell within the random forest's raw predicted ±1 std band
across the three horizons (target for a well-calibrated Gaussian-shaped
spread: ~68%), and 69-71% within ±2 std (target: ~95%) -- similar to
before mortgage rate was added, if slightly worse at 6 months
specifically (36% vs. 43% before), consistent with that horizon's random
forest also having gotten less accurate and less consistent above. **The
raw band was meaningfully overconfident** -- narrower than the model's
real uncertainty, not just imprecise.

Rather than leave that as a caveat, `train_model.py`'s
`calibrate_uncertainty()` now fixes it directly: it pools every walk-
forward fold's (residual, predicted std) pairs -- held-out predictions
the model never trained on, so this is a fair calibration set, not a
circular one -- and takes the 68th/95th percentile of `|residual| / std`
as a per-horizon scale factor (`c68`, `c95`). Multiplying future std
predictions by `c68` makes 68% coverage hold *by construction* on that
same held-out data, the same logic as split-conformal prediction.
Texas's fitted factors: `c68` of 1.94 (1-month), 1.86 (3-month), and 1.94
(6-month) -- the raw band was undershooting the true spread by roughly
double at every horizon, not just at the specific horizons flagged
above. `generate_predictions.py` applies `c68` before writing the ± band
`/market-trends` actually shows, so the reduced-opacity/± range shipped
today reflects this calibrated, wider band -- no longer "less sure than
it looks."

## Results: Pennsylvania (walk-forward cross-validation, 2026-08-11)

66 PA counties, ~5,300-5,400 county-month training examples (fewer than
Texas simply because Pennsylvania has far fewer counties -- 67 total vs.
254), same 2019-03 through 2026-06 span and same 4-fold walk-forward
setup.

| Horizon | Naive MAE | Linear MAE | RF MAE | GB MAE | RF vs. naive | Linear vs. naive | GB vs. naive |
|---|---|---|---|---|---|---|---|
| 1 month | 0.0184 ± 0.0028 | 0.0118 ± 0.0004 | 0.0128 ± 0.0004 | 0.0124 ± 0.0002 | 30.6% | **35.7%** | 32.9% |
| 3 months | 0.0379 ± 0.0032 | 0.0232 ± 0.0020 | 0.0219 ± 0.0012 | 0.0213 ± 0.0013 | 42.2% | 38.9% | **43.9%** |
| 6 months | 0.0518 ± 0.0151 | 0.0224 ± 0.0010 | 0.0240 ± 0.0019 | 0.0223 ± 0.0026 | 53.6% | 56.7% | **56.9%** |

**Unlike Texas, mortgage rate was a clean win across the board in
Pennsylvania.** Every model improved at every horizon compared to before
this feature was added -- the random forest's 3-month score alone jumped
from 37.0% to 42.2% vs. naive, and 6-month from 47.4% to 53.6%.
Calibration improved too (see below). Linear regression still wins at 1
and 6 months and gradient boosting at 3, same pattern as before this
change, but the random forest -- the one actually deployed -- is
genuinely closer to competitive here than it was, and closer than it is
in Texas right now. With a third as many counties and a smaller, more
geographically compact state, PA's 6-month fold error is also still the
least consistent of any horizon/state combination here (± up to 0.0151)
-- a smaller, noisier dataset is a plausible reason the more flexible
models have a harder time earning their extra complexity back, though
less so now than before mortgage rate was added. Feature importances
follow the same seasonality-dominant shape as Texas (`month_sin`/
`month_cos` together 46-83% across horizons), with `mortgage_rate`/
`mortgage_rate_mom_change` a modest but real presence throughout (7-8%
combined at every horizon, similar magnitude to Texas).

**Uncertainty calibration**: before calibration, 45-55% coverage within
±1 std (target ~68%), 79-85% within ±2 std (target ~95%) -- still
overconfident, but improved from before mortgage rate was added at 3 and
6 months specifically (51%->55% and 51%->55%), the opposite direction
from Texas's slight 6-month regression above. Same overall conclusion as
Texas, just less severe: real uncertainty existed beyond what the raw
band showed.

Applying the same held-out-residual calibration described in the Texas
section above (see `calibrate_uncertainty()`), Pennsylvania's fitted
factors come out smaller than Texas's -- `c68` of 1.58 (1-month), 1.36
(3-month), 1.29 (6-month) -- consistent with PA's raw band already being
less overconfident than Texas's before any correction. `c68` shrinking
as the horizon lengthens (1.58 -> 1.29) is also the opposite direction
from Texas, whose factors stay essentially flat (1.94 -> 1.86 -> 1.94);
one more data point for the "the two states don't behave the same way"
theme running through this section, alongside the mortgage-rate finding
below. As in Texas, `generate_predictions.py` now ships `std * c68`
rather than the raw tree-spread, so the ± band on `/market-trends-pa`
reflects this calibrated 68% coverage rather than the raw, overconfident
one.

**Why the same feature helped one state and hurt the other's deployed
model isn't fully explained here** -- a real, open question rather than
a loose end papered over. Plausible contributors: Pennsylvania's smaller
dataset may simply have had more room to gain from an informative
national-level feature that adds the same value to every county-month
row, while Texas's random forest, already fit against many more county-
month combinations, may have found spurious mortgage-rate-correlated
splits in some of its 254 fairly different county subpopulations that
didn't generalize the same way forward. That's a hypothesis, not a
verified explanation -- worth real investigation before concluding much
more than "the effect isn't uniform and shouldn't be assumed to be."

## Case study: Clay County

The single largest predicted move in the current output (see
`public/tx_county_predictions.json`) is still Clay County's 6-month
horizon, same county as before mortgage rate was added, though a smaller
call now: a projected **-7.9 point** drop in price-cut share, from a
current 29.7% down toward roughly 22% (previously a -11.9 point call
toward roughly 18%, before this feature was added).

Its actual history explains why: Clay sat in a 22-25% band through most
of 2025, dropped to a recent low of 10.6% in January 2026 -- then spiked
hard: 13.0% (Feb) -> 14.6% (Mar) -> 21.3% (Apr) -> 25.7% (May) -> 29.7%
(Jun 2026), nearly tripling in five months. That's a sharp, unusual move
for a county that had otherwise been comparatively range-bound.

The model isn't predicting Clay keeps climbing -- it's betting on
reversion, gradually: essentially flat at 1 month (-1.0 points), a
modest pullback at 3 months (-4.6 points), and the largest call, -7.9
points, only shows up at 6 months. The uncertainty band on that 6-month
number is now ±5.9 points, once the calibration factor from "Uncertainty
calibration" above is applied (up from the ±3.0 points the raw,
uncalibrated tree-spread would have shown -- itself already wider than
the ±2.5 points before mortgage rate was added). That's not the model
getting less sure about Clay specifically; it's the same 6-month
`c68`≈1.94 factor Texas gets everywhere, now actually applied instead of
left as a documented caveat. Whether the call is *right* isn't knowable
yet (2026-12 data doesn't exist yet); what's checkable today is that the
prediction is legible, tied to a real, visible pattern in the underlying
data, and now paired with an honestly-sized uncertainty range rather
than an overconfident one -- which is the whole point of shipping the
drill-down chart on `/market-trends` alongside the map.

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
Retuning the random forest's hyperparameters for the now-larger feature
set (see the Texas 6-month finding above) and investigating the
state-specific mortgage-rate effect are both now concrete, evidence-
backed follow-ups rather than open-ended ideas.

The uncertainty-band fix above (see "Uncertainty calibration" in each
state's Results section) is a single scale factor per horizon, fit once
on the whole state -- it corrects the *average* overconfidence but
doesn't know that some counties' predictions are better-calibrated than
others'. A per-county or feature-dependent calibration (e.g. conformal
prediction with a learned conformity score, or quantile regression
forests predicting the band directly) would be more precise, at the cost
of needing more held-out data than a single scalar does -- worth
revisiting once there's more history to calibrate against, not before.
