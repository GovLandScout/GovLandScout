"""
GovLandScout - Geocoding backfill

Fills in latitude/longitude for listings that have a usable address but no
coordinates. Listings with no address at all can't be geocoded from this
script; they need a different data source (e.g. HCAD's own parcel data
for Harris County, TX -- see hcad_address_backfill.py, run separately and
occasionally since it's a 200MB+ bulk download, not part of this script
or the daily pipeline).

Uses the Census Bureau's free, keyless batch geocoder -- a single POST
with a CSV of up to 10,000 addresses, rather than one request per address.
A handful of addresses the batch endpoint can't confidently pick a single
match for ("Tie", not "No_Match" -- see resolve_tie()) get a second,
one-at-a-time pass against the same geocoder's single-address endpoint,
which resolves them; in every real case checked so far this project's own
Tie results turn out to be an address the batch endpoint could resolve
fine, just missing a zip code, not a genuinely ambiguous street.

Run as the last step of run_daily_scrapers.py, once, after every scraper
that could have added a new address has already run -- geocoding a fixed
street address doesn't change day to day, so there's nothing to gain from
running it more than once per day. Can still be run by hand any time too
(e.g. right after a manual scraper run outside the daily schedule).
"""

import csv
import io
import re
import time

import requests

import combined_db

BATCH_GEOCODE_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
SINGLE_GEOCODE_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"

# A short pause between resolve_tie() calls -- these run one at a time
# (the single-address endpoint has no batch form), and there are only
# ever a few dozen of them per run (see main()), so this costs seconds,
# not minutes, while still not hammering the endpoint request-by-request.
TIE_RESOLVE_DELAY_SECONDS = 0.3

# Rough (lat_min, lat_max, lon_min, lon_max) boxes, padded past each state's
# real border rather than drawn tight to it. On 2026-08-06 six real
# Cumberland County, PA listings ("...LOWER ALLEN TOWNSHIP, PA", "...UPPER
# FRANKFORD TOWNSHIP, PA" -- no zip code, an uncommon township name rather
# than an incorporated city) came back from the Census geocoder matched to
# Texas and upstate New York instead -- a wrong coordinate is worse than no
# coordinate, since it's silently shown as if it were correct. This isn't
# validating the geocoder's normal behavior, just catching the rare gross
# mismatch that lands a "PA" listing hundreds of miles outside Pennsylvania.
STATE_BOUNDING_BOXES = {
    "TX": (25.5, 36.6, -107.0, -93.3),
    "PA": (39.6, 42.3, -80.6, -74.6),
    "CA": (32.4, 42.1, -124.6, -114.0),
}


def is_within_state_bounds(state: str, latitude: float, longitude: float) -> bool:
    """True if no bounding box is known for this state -- can't validate what isn't defined, so this only ever
    rejects a match for a state it actually has a box for (see STATE_BOUNDING_BOXES)."""
    box = STATE_BOUNDING_BOXES.get(state)
    if box is None:
        return True
    lat_min, lat_max, lon_min, lon_max = box
    return lat_min <= latitude <= lat_max and lon_min <= longitude <= lon_max


def clear_out_of_bounds_coordinates(conn: combined_db.PgConnection) -> int:
    """
    Finds listings that already have a stored latitude/longitude landing
    outside their own state's bounding box -- a wrong geocoder match from
    before is_within_state_bounds existed, or before an entry was added to
    STATE_BOUNDING_BOXES -- and clears them back to NULL so fetch_ungeocoded
    picks them back up in the same run and gets a chance to either find a
    correct match or, worse case, leave them honestly ungeocoded instead of
    silently wrong. Runs every time (cheap at this table's current size, and
    a real safety net if the Census geocoder ever mismatches again), not
    just once as a one-off migration.
    """
    rows = conn.execute("""
        SELECT county, account_number, state, latitude, longitude FROM listings
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    """).fetchall()

    cleared = 0
    for county, account_number, state, latitude, longitude in rows:
        if not is_within_state_bounds(state, latitude, longitude):
            print(f"  Clearing out-of-bounds coordinate: {county}, {state} #{account_number} "
                  f"was ({latitude}, {longitude})")
            combined_db.update_lat_lon(conn, county, account_number, None, None, state=state)
            cleared += 1
    return cleared


def fetch_ungeocoded(conn: combined_db.PgConnection) -> list[tuple[str, str, str, str]]:
    """(county, account_number, address, state) for listings with an address but no coordinates."""
    rows = conn.execute("""
        SELECT county, account_number, address, state FROM listings
        WHERE latitude IS NULL AND address IS NOT NULL AND address != ''
    """).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


# Matches DANGLING_STATE_PATTERN's job but per-state: a dangling state name
# with no real city ("<street>, Pennsylvania <zip>"). Extend this alongside
# STATE_NAMES below if a source's addresses start spelling out a state this
# doesn't yet cover.
STATE_NAMES = {"TX": "texas", "PA": "pennsylvania"}


def parse_address(
    address: str, state: str, county_fallback: str | None = None
) -> tuple[str, str, str, str] | None:
    """
    Addresses are usually formatted "<street>, <city>, ST <zip>" -- split
    into the street/city/state/zip fields the batch geocoder expects.
    `state` is the listing's own state (see combined_db.py's `state` column)
    -- geocoding every listing against Texas regardless of where it actually
    is silently produced wrong or missing coordinates for the first
    non-Texas source (Pennsylvania's GovEase counties). Increasingly
    permissive as information gets scarcer, since the batch geocoder
    accepts a blank zip and a real street can often still be resolved
    without one:

      - 3+ comma segments: zip is whatever digits are in the 3rd segment,
        blank if there aren't any (e.g. MVBA addresses ending "..., Bastrop,
        Texas" with no zip at all -- previously rejected outright for that).
      - Exactly 2 segments ("<street>, <city>"): treated as street/city with
        a blank zip. Most of these are GovEase listings, whose own site
        never gives more than "<street>, <city>" to begin with.
      - No comma at all, OR exactly 2 segments where the second is a
        dangling state token ("<street>, PA" with no real city --
        Bid4Assets' Berks/Fayette listings are 100% this shape): the
        listing's own county name is used as a stand-in city -- coarser
        than a real one, but the Census geocoder can often still resolve a
        street within a named county, and a wrong guess just comes back
        No_Match rather than a bad coordinate (see geocode_batch). This is
        what recovers GovEase's Denton listings (bare street, no city at
        all) and, as of 2026-08-06, what stopped ~3,000 Bid4Assets PA
        listings (Berks, Fayette, and -- best-effort, since its "street"
        is really a township name -- Monroe) from having zero geocoding
        coverage: they weren't merely missing a zip, "<street>, PA" was
        being rejected outright as unusable.

    Returns None for the minority still too irregular to use even this way
    -- no street, or a dangling state token with no county to fall back on.
    """
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if not parts or not parts[0]:
        return None
    street = parts[0]

    dangling_pattern = re.compile(
        rf"^({re.escape(STATE_NAMES.get(state, state.lower()))}|{re.escape(state.lower())})\b",
        re.IGNORECASE,
    )

    if len(parts) >= 2:
        city = parts[1]
        if dangling_pattern.match(city):
            if not county_fallback:
                return None  # "<street>, <State>[ zip]" -- no real city, and nothing to fall back to
            city = county_fallback
            zip_code = ""
        else:
            zip_code = "".join(c for c in parts[2].split()[-1] if c.isdigit())[:5] if len(parts) >= 3 and parts[2].split() else ""
    elif county_fallback:
        city = county_fallback
        zip_code = ""
    else:
        return None

    return street, city, state, zip_code


def build_batch_csv(rows: list[tuple[str, str, str, str, str]]) -> str:
    """rows: (unique_id, street, city, state, zip)"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def geocode_batch(csv_text: str) -> tuple[dict[str, tuple[float, float]], list[str]]:
    """(matches, tie_ids) -- tie_ids are rows the batch endpoint refused to
    guess between (see resolve_tie()'s docstring for what to do with them),
    kept separate from a plain No_Match, which means the endpoint found
    nothing at all, not something it couldn't choose between."""
    resp = requests.post(
        BATCH_GEOCODE_URL,
        files={"addressFile": ("addresses.csv", csv_text, "text/csv")},
        data={"benchmark": "Public_AR_Current"},
        timeout=120,
    )
    resp.raise_for_status()

    found = {}
    tie_ids = []
    reader = csv.reader(io.StringIO(resp.text))
    for row in reader:
        # Match: id, input address, "Match", match type, matched address, "lon,lat", tiger line id, side
        # Tie/No_Match: just id, input address, status -- no coordinate columns at all
        unique_id, status = row[0], row[2]
        if status == "Match":
            lon, lat = row[5].split(",")
            found[unique_id] = (float(lat), float(lon))
        elif status == "Tie":
            tie_ids.append(unique_id)
    return found, tie_ids


def resolve_tie(street: str, city: str, state: str, zip_code: str) -> tuple[float, float] | None:
    """The batch endpoint (geocode_batch above) reports "Tie" rather than
    picking a match when it finds more than one equally-plausible
    candidate -- confirmed directly against real production rows before
    writing this: addresses like "1022 GREEN ST, Norristown, PA" (a real
    street + real city, just no zip -- Montgomery County's own scraper
    never captures one) come back "Tie" from the batch endpoint but a
    single, specific match from this same Census geocoder's other
    endpoint, /locations/onelineaddress, which doesn't refuse to pick one.
    Every Tie this project has actually inspected has been exactly this
    shape (missing zip, not a genuinely ambiguous street), so taking that
    single endpoint's first choice is a reasonable recovery, not blind
    guessing -- still checked against is_within_state_bounds by the
    caller, the same safety net every other geocoded coordinate here
    gets."""
    address = f"{street}, {city}, {state} {zip_code}".strip()
    resp = requests.get(SINGLE_GEOCODE_URL, params={
        "address": address, "benchmark": "Public_AR_Current", "format": "json",
    }, timeout=30)
    resp.raise_for_status()
    matches = resp.json().get("result", {}).get("addressMatches", [])
    if not matches:
        return None
    coords = matches[0]["coordinates"]
    return coords["y"], coords["x"]  # (lat, lon)


def main():
    conn = combined_db.get_connection()

    cleared = clear_out_of_bounds_coordinates(conn)
    if cleared:
        print(f"Cleared {cleared} listing(s) whose stored coordinates fell outside their own state.")

    ungeocoded = fetch_ungeocoded(conn)
    print(f"{len(ungeocoded)} listings have an address but no coordinates.")

    # unique_id must be a single token with no commas -- account numbers
    # aren't globally unique across counties (and county names aren't
    # unique across states), so the key combines all three.
    batch_rows = []
    id_to_key = {}
    skipped = 0
    for county, account_number, address, listing_state in ungeocoded:
        parsed = parse_address(address, listing_state, county_fallback=county)
        if parsed is None:
            skipped += 1
            continue
        street, city, geocode_state, zip_code = parsed
        unique_id = str(len(batch_rows))
        id_to_key[unique_id] = (county, account_number, listing_state)
        batch_rows.append((unique_id, street, city, geocode_state, zip_code))

    print(f"{len(batch_rows)} addresses well-formed enough to geocode ({skipped} skipped -- too irregular to split).")
    if not batch_rows:
        conn.close()
        return

    # id -> its own (street, city, state, zip) row, needed after the batch
    # pass to re-look-up Tie ids one at a time (see resolve_tie() below).
    id_to_row = {row[0]: row[1:] for row in batch_rows}

    # Census batch endpoint caps a single file at 10,000 records.
    updated = 0
    rejected = 0
    all_tie_ids = []
    for i in range(0, len(batch_rows), 10_000):
        chunk = batch_rows[i:i + 10_000]
        csv_text = build_batch_csv(chunk)
        print(f"Geocoding {len(chunk)} addresses ...")
        matches, tie_ids = geocode_batch(csv_text)
        print(f"Matched {len(matches)} of {len(chunk)} ({len(tie_ids)} more came back Tie).")
        all_tie_ids.extend(tie_ids)

        for unique_id, (lat, lon) in matches.items():
            county, account_number, listing_state = id_to_key[unique_id]
            if not is_within_state_bounds(listing_state, lat, lon):
                # See STATE_BOUNDING_BOXES's comment -- a gross mismatch
                # (real 2026-08-06 case: Cumberland County, PA matched to
                # Texas and upstate New York) is worse to store than to
                # leave ungeocoded, so this is treated the same as No_Match.
                print(f"  REJECTED {county}, {listing_state} #{account_number}: "
                      f"geocoded to ({lat}, {lon}), outside {listing_state}'s bounds")
                rejected += 1
                continue
            combined_db.update_lat_lon(conn, county, account_number, lat, lon, state=listing_state)
            updated += 1

    tie_recovered = 0
    if all_tie_ids:
        print(f"Resolving {len(all_tie_ids)} Tie result(s) one at a time (see resolve_tie())...")
        for i, unique_id in enumerate(all_tie_ids):
            if i > 0:
                time.sleep(TIE_RESOLVE_DELAY_SECONDS)
            street, city, geocode_state, zip_code = id_to_row[unique_id]
            resolved = resolve_tie(street, city, geocode_state, zip_code)
            if resolved is None:
                continue
            lat, lon = resolved
            county, account_number, listing_state = id_to_key[unique_id]
            if not is_within_state_bounds(listing_state, lat, lon):
                print(f"  REJECTED (Tie) {county}, {listing_state} #{account_number}: "
                      f"geocoded to ({lat}, {lon}), outside {listing_state}'s bounds")
                rejected += 1
                continue
            combined_db.update_lat_lon(conn, county, account_number, lat, lon, state=listing_state)
            tie_recovered += 1
        print(f"Recovered {tie_recovered} of {len(all_tie_ids)} Tie result(s).")

    print(f"Backfilled coordinates for {updated + tie_recovered} listings"
          + (f" ({rejected} rejected as outside their state's bounds)." if rejected else "."))
    conn.close()


if __name__ == "__main__":
    main()
