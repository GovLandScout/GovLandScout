"""
GovLandScout - Geocoding backfill (manual/occasional, NOT in the daily pipeline)

Fills in latitude/longitude for listings that have a usable address but no
coordinates -- most notably all of Harris County (hctax_scraper.py never
geocodes), plus smaller gaps scattered across other counties/sources.
Listings with no address at all can't be geocoded from this script; they
need a different data source (e.g. HCAD's own parcel data, like
hcad_value_backfill.py already pulls for estimated_value).

Uses the Census Bureau's free, keyless batch geocoder -- a single POST
with a CSV of up to 10,000 addresses, rather than one request per address.
Deliberately kept out of run_daily_scrapers.py: geocoding a fixed street
address doesn't change day to day, so there's nothing to re-run on a
schedule -- just run this by hand after a scrape adds new addresses.
"""

import csv
import io

import requests

import combined_db

BATCH_GEOCODE_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"


def fetch_ungeocoded(conn: combined_db.PgConnection) -> list[tuple[str, str, str]]:
    """(county, account_number, address) for listings with an address but no coordinates."""
    rows = conn.execute("""
        SELECT county, account_number, address FROM listings
        WHERE latitude IS NULL AND address IS NOT NULL AND address != ''
    """).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def parse_address(address: str) -> tuple[str, str, str, str] | None:
    """
    Addresses are formatted "<street>, <city>, TX <zip>" -- split into the
    separate street/city/state/zip fields the batch geocoder expects.
    Returns None for the minority too irregular to split reliably (no
    third comma segment, same rows web.py's extract_city() can't parse).
    """
    parts = [p.strip() for p in address.split(",")]
    if len(parts) < 3:
        return None
    street, city = parts[0], parts[1]
    zip_code = "".join(c for c in parts[2].split()[-1] if c.isdigit())[:5]
    if not street or not city or len(zip_code) != 5:
        return None
    return street, city, "TX", zip_code


def build_batch_csv(rows: list[tuple[str, str, str, str, str]]) -> str:
    """rows: (unique_id, street, city, state, zip)"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def geocode_batch(csv_text: str) -> dict[str, tuple[float, float]]:
    resp = requests.post(
        BATCH_GEOCODE_URL,
        files={"addressFile": ("addresses.csv", csv_text, "text/csv")},
        data={"benchmark": "Public_AR_Current"},
        timeout=120,
    )
    resp.raise_for_status()

    found = {}
    reader = csv.reader(io.StringIO(resp.text))
    for row in reader:
        # Match: id, input address, "Match", match type, matched address, "lon,lat", tiger line id, side
        # No_Match: just id, input address, "No_Match" -- no coordinate columns at all
        unique_id, status = row[0], row[2]
        if status == "Match":
            lon, lat = row[5].split(",")
            found[unique_id] = (float(lat), float(lon))
    return found


def main():
    conn = combined_db.get_connection()

    ungeocoded = fetch_ungeocoded(conn)
    print(f"{len(ungeocoded)} listings have an address but no coordinates.")

    # unique_id must be a single token with no commas -- account numbers
    # aren't globally unique across counties, so combine county+account.
    batch_rows = []
    id_to_key = {}
    skipped = 0
    for county, account_number, address in ungeocoded:
        parsed = parse_address(address)
        if parsed is None:
            skipped += 1
            continue
        street, city, state, zip_code = parsed
        unique_id = str(len(batch_rows))
        id_to_key[unique_id] = (county, account_number)
        batch_rows.append((unique_id, street, city, state, zip_code))

    print(f"{len(batch_rows)} addresses well-formed enough to geocode ({skipped} skipped -- too irregular to split).")
    if not batch_rows:
        conn.close()
        return

    # Census batch endpoint caps a single file at 10,000 records.
    updated = 0
    for i in range(0, len(batch_rows), 10_000):
        chunk = batch_rows[i:i + 10_000]
        csv_text = build_batch_csv(chunk)
        print(f"Geocoding {len(chunk)} addresses ...")
        matches = geocode_batch(csv_text)
        print(f"Matched {len(matches)} of {len(chunk)}.")

        for unique_id, (lat, lon) in matches.items():
            county, account_number = id_to_key[unique_id]
            combined_db.update_lat_lon(conn, county, account_number, lat, lon)
            updated += 1

    print(f"Backfilled coordinates for {updated} listings.")
    conn.close()


if __name__ == "__main__":
    main()
