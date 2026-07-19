"""
GovLandScout - HCAD value backfill (manual/occasional, NOT in the daily pipeline)

Fills in estimated_value for Harris County listings that hctax_scraper.py
couldn't get a value for, using Harris Central Appraisal District's own
official market values.

Deliberately kept out of run_daily_scrapers.py and the scheduled GitHub
Actions scrape: HCAD only offers a full bulk export (no per-account API),
and that export is large -- around 210MB zipped, 870MB+ once the specific
file we need is unzipped, covering all 1.6 million Harris County accounts
just to look up a few hundred. Not worth that bandwidth/time on a
schedule for the ~40 listings it actually helps, and HCAD's yearly
assessment data doesn't change often enough to need it. Run this by hand
locally instead, whenever you want fresher HCAD data -- it writes
straight into the same Postgres database the scheduled scrapers use.

The download endpoint isn't documented -- found by reading the JS behind
HCAD's public data download page (HcadPdata.js), which calls a small
Craft CMS JSON API to list the current year's file links before falling
back to a hardcoded URL.
"""

import io
import zipfile

import requests

import combined_db

TAX_YEARS_URL = "https://hcad.org/actions/hcad-pdata/default/get-tax-years"
DOWNLOADS_URL = "https://hcad.org/actions/hcad-pdata/default/get-property-downloads"
DATA_FILENAME = "real_acct.txt"

HEADERS = {
    "User-Agent": "GovLandScout-SchoolProject/1.0 (contact: your-email@example.com)"
}


def get_current_tax_year() -> str:
    resp = requests.get(TAX_YEARS_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    years = resp.json()
    return years[0]["taxyears"]  # most recent year is listed first


def get_real_acct_zip_url(tax_year: str) -> str:
    resp = requests.get(
        DOWNLOADS_URL,
        params={"t": tax_year, "c": "CAMA", "s": "Real Property"},
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    for entry in resp.json():
        if entry["filename"] == "Real_acct_owner.zip":
            return entry["downloadLink"]
    raise RuntimeError("Real_acct_owner.zip not found in HCAD's current download list")


NUMERIC_PATTERN = r"^\d+(\.\d+)?$"


def fetch_target_accounts(conn: combined_db.PgConnection) -> set[str]:
    # The regex pattern is passed as a bound parameter, not embedded as a
    # raw literal in the SQL text -- PgConnection.execute() does a naive
    # '?' -> '%s' replace on the whole query string, which would otherwise
    # also corrupt the '?' inside "(\.\d+)?" if it were written inline.
    rows = conn.execute("""
        SELECT account_number FROM listings
        WHERE county = 'Harris'
          AND (estimated_value IS NULL OR NOT estimated_value ~ ?
               OR CAST(estimated_value AS REAL) <= 0)
    """, (NUMERIC_PATTERN,)).fetchall()
    return {r[0] for r in rows}


def find_values(zip_url: str, target_accounts: set[str]) -> dict[str, str]:
    """
    Streams the zip download directly into memory and reads real_acct.txt
    out of it without writing the (870MB+) extracted file to disk.
    """
    print(f"Downloading {zip_url} ...")
    resp = requests.get(zip_url, headers=HEADERS, timeout=300)
    resp.raise_for_status()
    print(f"Downloaded {len(resp.content) / 1_000_000:.0f} MB.")

    found = {}
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        with zf.open(DATA_FILENAME) as f:
            header = f.readline().decode("latin-1").rstrip("\n").split("\t")
            acct_idx = header.index("acct")
            mkt_idx = header.index("tot_mkt_val")

            for raw_line in f:
                line = raw_line.decode("latin-1").rstrip("\n")
                fields = line.split("\t")
                acct = fields[acct_idx].strip()
                if acct in target_accounts:
                    value = fields[mkt_idx].strip()
                    if value and value != "0":
                        found[acct] = value

    return found


def main():
    conn = combined_db.get_connection()

    target_accounts = fetch_target_accounts(conn)
    print(f"{len(target_accounts)} Harris listings currently have no estimated value.")
    if not target_accounts:
        conn.close()
        return

    tax_year = get_current_tax_year()
    print(f"Using HCAD tax year {tax_year}.")
    zip_url = get_real_acct_zip_url(tax_year)

    values = find_values(zip_url, target_accounts)
    print(f"Found a market value for {len(values)} of {len(target_accounts)} listings.")

    for account_number, value in values.items():
        combined_db.update_estimated_value(conn, "Harris", account_number, value)

    print(f"Backfilled {len(values)} listings' estimated_value.")
    conn.close()


if __name__ == "__main__":
    main()
