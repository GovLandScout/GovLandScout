# GovLandScout - County Distress Trend Model (Phase 1)

A random forest baseline predicting next-month change in Texas county
housing distress, using Zillow Research + FRED historical data rather
than GovLandScout's own scraped listings -- our own history is only a
few weeks old, nowhere near enough to train on yet. This is deliberately
kept separate from the scraper/web app: different dependencies (pandas,
scikit-learn), different deploy target (none -- this doesn't run on
Render or in the daily scrape workflow), different audience (research,
not the live site).

## Scope (Phase 1)

- **Target**: next month's `perc_listings_price_cut` (% of a county's
  active listings that took a price cut) -- the closest thing Zillow
  publishes to a direct distress signal, vs. ZHVI which just tracks
  price level.
- **Geography**: Texas counties Zillow has price-cut data for (208 of
  254).
- **Features**: lagged/rolling price-cut history, ZHVI trend
  (month-over-month and year-over-year % change), for-sale inventory
  and its trend, county unemployment rate (from FRED) and its trend,
  month-of-year seasonality.
- **Model**: scikit-learn RandomForestRegressor, evaluated against a
  naive persistence baseline (predict next month = this month) on a
  time-based holdout, not a random split -- a random split would leak
  future months into training and overstate accuracy.
- **Not in scope yet**: per-property predictions, anything served on
  the live site, non-Texas counties, GovLandScout's own scraped history
  as a feature (revisit once it has enough months behind it to matter).

## Pipeline

1. `fetch_data.py` -- downloads and caches the raw Zillow CSVs and, per
   county, FRED's unemployment/employment level series (used to compute
   a monthly unemployment rate; FRED's own monthly county unemployment
   *rate* series isn't uniformly published, but the level series behind
   it are). Caches everything in `data/` (gitignored -- this is
   multi-hundred-county raw data, not something to commit) so re-runs
   don't re-hit either source.
2. `build_dataset.py` -- joins all of it into one county-month panel,
   engineers the lagged/rolling features described above, and writes
   `data/county_month_dataset.csv`.
3. `train_model.py` -- time-based train/test split, trains the random
   forest, reports RMSE/MAE against the naive baseline, and prints
   feature importances.

## Running it

```bash
cd model
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3 fetch_data.py       # takes a few minutes -- ~400+ small requests to FRED
python3 build_dataset.py
python3 train_model.py
```

## Results (first run, 2026-08-04)

207 TX counties, 15,362 county-month training examples spanning 2019-03
through 2026-05. Time-based split: trained through 2024-10, evaluated on
everything from 2024-11 onward (3,369 rows, 202 counties) -- months the
model never saw during training.

| | RMSE | MAE |
|---|---|---|
| Naive baseline (predict no change) | 0.022 | 0.017 |
| Random forest | 0.019 | 0.014 |

**18% lower MAE than the naive baseline** on genuinely unseen months --
a real, if modest, improvement, not a wash.

Feature importances were lopsided: `price_cut_pct` (this month's own
value) alone accounted for 93% of the model's decisions, `month_of_year`
another 3%, and every other feature -- inventory trend, unemployment
trend, ZHVI trend, longer lags -- split the remaining ~4%. Read plainly:
at the monthly county level, *current momentum* is what actually carries
predictive power right now; the macro features add only a small marginal
edge on top of it in this framing. That's a legitimate, useful finding
on its own (it says where the signal is, not just whether the model
"worked"), and it's a natural place to push on next -- e.g. reframing
the target as month-over-month *change* rather than the level itself,
which would force the model to explain movement instead of being able
to mostly coast on autocorrelation.
