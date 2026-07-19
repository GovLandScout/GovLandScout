"""
GovLandScout - HCAD address backfill (manual/occasional, NOT in the daily pipeline)

Fills in address for Harris County listings hctax_scraper.py recorded with
no address at all, using Harris Central Appraisal District's own site
address fields (site_addr_1/2/3) from the same bulk export
hcad_value_backfill.py already pulls estimated_value from.

Deliberately kept separate from hcad_value_backfill.py rather than merged
into it: each script enriches one column, and re-running one doesn't force
a redundant 210MB download+parse for the other when only one is needed.
Also kept out of run_daily_scrapers.py for the same reason as the value
backfill -- HCAD only offers the full bulk export, not a per-account API.

Run geocode_backfill.py afterward to pick up coordinates for whatever
addresses this fills in -- it already finds any listing with an address
but no lat/lon, so newly-backfilled rows are picked up automatically.
"""

import io
import zipfile

import combined_db
from hcad_value_backfill import HEADERS, get_current_tax_year, get_real_acct_zip_url, requests

DATA_FILENAME = "real_acct.txt"


def fetch_target_accounts(conn: combined_db.PgConnection) -> set[str]:
    rows = conn.execute("""
        SELECT account_number FROM listings
        WHERE county = 'Harris' AND (address IS NULL OR address = '')
    """).fetchall()
    return {r[0] for r in rows}


def find_addresses(zip_url: str, target_accounts: set[str]) -> dict[str, str]:
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
            street_idx = header.index("site_addr_1")
            city_idx = header.index("site_addr_2")
            zip_idx = header.index("site_addr_3")

            for raw_line in f:
                line = raw_line.decode("latin-1").rstrip("\n")
                fields = line.split("\t")
                acct = fields[acct_idx].strip()
                if acct not in target_accounts:
                    continue
                street = fields[street_idx].strip()
                city = fields[city_idx].strip()
                zip_code = fields[zip_idx].strip()
                if not street or not city:
                    continue  # blank site address on file -- nothing usable
                found[acct] = f"{street}, {city}, TX {zip_code}".strip()

    return found


def main():
    conn = combined_db.get_connection()

    target_accounts = fetch_target_accounts(conn)
    print(f"{len(target_accounts)} Harris listings currently have no address.")
    if not target_accounts:
        conn.close()
        return

    tax_year = get_current_tax_year()
    print(f"Using HCAD tax year {tax_year}.")
    zip_url = get_real_acct_zip_url(tax_year)

    addresses = find_addresses(zip_url, target_accounts)
    print(f"Found a usable site address for {len(addresses)} of {len(target_accounts)} listings.")

    for account_number, address in addresses.items():
        combined_db.update_address(conn, "Harris", account_number, address)

    print(f"Backfilled {len(addresses)} listings' address.")
    conn.close()


if __name__ == "__main__":
    main()
