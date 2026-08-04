"""
GovLandScout model (Phase 1) - Data fetch

Downloads and caches the raw historical data this model trains on:

- Three Zillow Research county-level CSVs (home values, price cuts,
  for-sale inventory). URLs come from zillow.com/research/data/ --
  Zillow occasionally reshuffles these paths, so if a download starts
  failing, that's the first thing to check.
- Per-county unemployment/employment level series from FRED, used to
  compute a monthly unemployment rate. FRED doesn't uniformly publish a
  ready-made monthly county *rate* series under a predictable ID (its
  "LAUCNxxxxx...003A" rate series is annual-only for most counties), but
  the level series behind it -- unemployment count (measure code 004)
  and employment count (005) -- are published monthly under a fully
  predictable ID built from the county's own FIPS code:
  "LAUCN" + <5-digit FIPS> + "0000000" + <004 or 005>. Rate is then just
  unemployment / (unemployment + employment) * 100, computed locally
  once both are fetched. No API key needed for either source -- FRED's
  plain fredgraph.csv endpoint and Zillow's static CSVs are both public.

Everything lands in data/ (gitignored -- this is raw multi-hundred-county
data, not something to commit) so re-runs after the first don't
re-download what's already cached.
"""

import csv
import io
import time
from pathlib import Path

import requests

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

ZILLOW_BASE = "https://files.zillowstatic.com/research/public_csvs"
ZILLOW_DATASETS = {
    "zhvi_county.csv": f"{ZILLOW_BASE}/zhvi/County_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv",
    "price_cut_county.csv": f"{ZILLOW_BASE}/perc_listings_price_cut/County_perc_listings_price_cut_uc_sfrcondo_sm_month.csv",
    "inventory_county.csv": f"{ZILLOW_BASE}/invt_fs/County_invt_fs_uc_sfrcondo_sm_month.csv",
}

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_REQUEST_DELAY_SECONDS = 0.3  # courtesy pacing across ~400+ requests, not a stated FRED requirement

HEADERS = {
    "User-Agent": "GovLandScout-SchoolProject/1.0 (contact: your-email@example.com)"
}


def fetch_zillow_datasets() -> None:
    for filename, url in ZILLOW_DATASETS.items():
        dest = DATA_DIR / filename
        if dest.exists():
            print(f"  {filename}: already cached, skipping")
            continue
        print(f"  Fetching {filename} ...")
        resp = requests.get(url, headers=HEADERS, timeout=60)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        print(f"    {len(resp.content):,} bytes")


def texas_counties_with_fips() -> list[tuple[str, str]]:
    """(RegionName, 5-digit FIPS) for every TX county present in the price-cut
    dataset -- that's the narrowest of the three Zillow datasets, so it's
    the real limiting set for which counties end up in the final model."""
    price_cut_names = set()
    with open(DATA_DIR / "price_cut_county.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["State"] == "TX":
                price_cut_names.add(row["RegionName"])

    counties = []
    with open(DATA_DIR / "zhvi_county.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["State"] == "TX" and row["RegionName"] in price_cut_names:
                fips = row["StateCodeFIPS"] + row["MunicipalCodeFIPS"].zfill(3)
                counties.append((row["RegionName"], fips))
    return sorted(counties)


def fetch_fred_series(series_id: str) -> str | None:
    resp = requests.get(FRED_CSV_URL, params={"id": series_id}, headers=HEADERS, timeout=30)
    if resp.status_code != 200 or not resp.text.startswith("observation_date"):
        return None  # series doesn't exist under this ID -- not every county publishes both measures
    return resp.text


def fetch_county_unemployment(counties: list[tuple[str, str]]) -> None:
    dest = DATA_DIR / "unemployment_county.csv"
    if dest.exists():
        print(f"  {dest.name}: already cached, skipping")
        return

    rows = []
    for i, (name, fips) in enumerate(counties):
        if i > 0:
            time.sleep(FRED_REQUEST_DELAY_SECONDS)

        unemployed_csv = fetch_fred_series(f"LAUCN{fips}0000000004")
        employed_csv = fetch_fred_series(f"LAUCN{fips}0000000005")
        if not unemployed_csv or not employed_csv:
            print(f"  SKIP {name} ({fips}) -- FRED series not available")
            continue

        unemployed = {r["observation_date"]: r[f"LAUCN{fips}0000000004"] for r in csv.DictReader(io.StringIO(unemployed_csv))}
        employed = {r["observation_date"]: r[f"LAUCN{fips}0000000005"] for r in csv.DictReader(io.StringIO(employed_csv))}

        for date in sorted(set(unemployed) & set(employed)):
            try:
                u, e = float(unemployed[date]), float(employed[date])
            except ValueError:
                continue  # FRED uses "." for missing observations
            labor_force = u + e
            if labor_force <= 0:
                continue
            rows.append((name, fips, date, round(u / labor_force * 100, 2)))

        if (i + 1) % 25 == 0:
            print(f"  ... {i + 1}/{len(counties)} counties fetched")

    with open(dest, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["RegionName", "FIPS", "observation_date", "unemployment_rate"])
        writer.writerows(rows)
    print(f"  Wrote {len(rows):,} rows for {dest.name}")


def main():
    print("Fetching Zillow datasets ...")
    fetch_zillow_datasets()

    print("\nDetermining Texas county universe ...")
    counties = texas_counties_with_fips()
    print(f"  {len(counties)} TX counties with both ZHVI and price-cut data")

    print("\nFetching FRED unemployment data (per county) ...")
    fetch_county_unemployment(counties)

    print("\nDone.")


if __name__ == "__main__":
    main()
