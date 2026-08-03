"""
GovLandScout - Geocoding backfill

Fills in latitude/longitude for listings that have a usable address but no
coordinates -- most notably all of Harris County (hctax_scraper.py never
geocodes), plus smaller gaps scattered across other counties/sources.
Listings with no address at all can't be geocoded from this script; they
need a different data source (e.g. HCAD's own parcel data, like
hcad_value_backfill.py already pulls for estimated_value).

Uses the Census Bureau's free, keyless batch geocoder -- a single POST
with a CSV of up to 10,000 addresses, rather than one request per address.
Run as the last step of run_daily_scrapers.py, once, after every scraper
that could have added a new address has already run -- geocoding a fixed
street address doesn't change day to day, so there's nothing to gain from
running it more than once per day. Can still be run by hand any time too
(e.g. right after a manual scraper run outside the daily schedule).
"""

import csv
import io
import re

import requests

import combined_db

BATCH_GEOCODE_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"

# A dangling state token where a city should be -- "E Oak St, Texas 76853"
# or "Lampasas, Texas" -- means this address never actually named a city at
# all, just street + state(+zip). Treated as no city rather than guessed at.
DANGLING_STATE_PATTERN = re.compile(r"^(texas|tx)\b", re.IGNORECASE)


def fetch_ungeocoded(conn: combined_db.PgConnection) -> list[tuple[str, str, str]]:
    """(county, account_number, address) for listings with an address but no coordinates."""
    rows = conn.execute("""
        SELECT county, account_number, address FROM listings
        WHERE latitude IS NULL AND address IS NOT NULL AND address != ''
    """).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def parse_address(address: str, county_fallback: str | None = None) -> tuple[str, str, str, str] | None:
    """
    Addresses are usually formatted "<street>, <city>, TX <zip>" -- split
    into the street/city/state/zip fields the batch geocoder expects.
    Increasingly permissive as information gets scarcer, since the batch
    geocoder accepts a blank zip and a real street can often still be
    resolved without one:

      - 3+ comma segments: zip is whatever digits are in the 3rd segment,
        blank if there aren't any (e.g. MVBA addresses ending "..., Bastrop,
        Texas" with no zip at all -- previously rejected outright for that).
      - Exactly 2 segments ("<street>, <city>"): treated as street/city with
        a blank zip. Most of these are GovEase listings, whose own site
        never gives more than "<street>, <city>" to begin with.
      - No comma at all: there's no city in the text either, so the
        listing's own county name is used as a stand-in city -- coarser
        than a real one, but the Census geocoder can often still resolve a
        street within a named county, and a wrong guess just comes back
        No_Match rather than a bad coordinate (see geocode_batch). This is
        what recovers GovEase's Denton listings, whose site gives only a
        bare street with no city at all.

    Returns None for the minority still too irregular to use even this way
    -- no street, or a dangling state token where a city should be.
    """
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if not parts or not parts[0]:
        return None
    street = parts[0]

    if len(parts) >= 2:
        city = parts[1]
        if DANGLING_STATE_PATTERN.match(city):
            return None  # "<street>, Texas[ zip]" -- no real city was ever given
        zip_code = "".join(c for c in parts[2].split()[-1] if c.isdigit())[:5] if len(parts) >= 3 and parts[2].split() else ""
    elif county_fallback:
        city = county_fallback
        zip_code = ""
    else:
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
        parsed = parse_address(address, county_fallback=county)
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
