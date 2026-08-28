"""
GovLandScout - CA parcel coordinate backfill (manual/occasional, NOT in the daily pipeline)

Fills in latitude/longitude for California listings using each county's own
public ArcGIS parcel layer -- the same three layers (and the same per-
county normalize_id functions) ca_assessor_value_backfill.py already uses
for estimated_value, plus Butte (which has no public value field -- see
that module's docstring -- but does have a public parcel boundary layer).
Modeled on geocode_backfill.py in purpose, but a genuinely different
mechanism: that script geocodes a street address text string through the
Census Bureau, which can't do anything for a listing whose address is
"NONE DESIGNATED", a bare "CA 96101" with no street at all, or a real
street with no house number (a vacant lot, confirmed directly: Butte
APN 013-280-055-000's own SITUS field reads "No Address Available", yet
its GIS parcel layer still has a real, valid location -- a parcel exists
and has a shape whether or not it was ever assigned a mailing address).
This backfill is keyed on APN instead, so it recovers exactly the
listings geocode_backfill.py structurally cannot.

Rather than reading each layer's own native coordinate system (Alameda's
is Web Mercator meters, Monterey's is a state-plane feet-based CRS, Butte
even has ready-made Latitude/Longitude attribute fields already in
degrees), every query here asks the ArcGIS REST endpoint itself to
reproject the returned geometry to WGS84 (outSR=4326) -- confirmed
directly against real parcels in both Alameda and Monterey that this
returns real, correct lat/lon ring coordinates, not something this script
would otherwise have to get right itself with a projection library. The
centroid used for the map pin is a plain average of a parcel's outer
ring vertices -- not a true area-weighted polygon centroid, but plenty
accurate for placing a pin somewhere inside a single tax parcel (typically
well under an acre for the listings this project tracks), and the same
kind of "coarser than ideal, still useful" tradeoff pa_assessment_backfill.py
already accepts for its own subdivided-tract base ids.

Same is_within_state_bounds() safety net geocode_backfill.py uses --
a wrong coordinate is worse than no coordinate, and there's no reason
county GIS data couldn't have its own bad row.
"""

import re

import requests

import combined_db
from geocode_backfill import STATE_BOUNDING_BOXES, is_within_state_bounds

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

BATCH_SIZE = 100


def alameda_strip_item_suffix(account_number: str) -> str:
    return re.sub(r"\s*-\s*Item\s*#\s*\d+\s*$", "", account_number.strip(), flags=re.IGNORECASE)


def monterey_normalize_id(account_number: str) -> str:
    without_suffix = re.sub(r"-Item No:\s*\d+\s*$", "", account_number.strip(), flags=re.IGNORECASE)
    return without_suffix.replace("-", "")


def butte_first_apn(account_number: str) -> str:
    """A handful of Butte listings cover multiple adjoining parcels sold
    together as one lot ("041-700-061-000, 041-700-066-000") -- confirmed
    directly, only 2 of 92 real Butte listings missing a coordinate are
    this shape. Using just the first APN's centroid is coarser than the
    true combined lot, but still lands the pin on the right property, not
    a wrong one."""
    return account_number.split(",")[0].strip()


COUNTY_CONFIGS = {
    "Butte": {
        "query_url": "http://gis.buttecounty.net/arcgis/rest/services/Incident/FeatureServer/6/query",
        "id_field": "APN",
        "normalize_id": butte_first_apn,
    },
    "Alameda": {
        "query_url": "https://services5.arcgis.com/ROBnTHSNjoZ2Wm1P/arcgis/rest/services/Parcels/FeatureServer/0/query",
        "id_field": "APN",
        "normalize_id": alameda_strip_item_suffix,
    },
    "Monterey": {
        "query_url": "https://services2.arcgis.com/nOGTdfb4kF4dZljH/arcgis/rest/services/Parcels_Data/FeatureServer/0/query",
        "id_field": "APN",
        "normalize_id": monterey_normalize_id,
    },
}


def fetch_target_accounts(conn: combined_db.PgConnection, county: str) -> list[str]:
    rows = conn.execute(
        "SELECT account_number FROM listings WHERE state = 'CA' AND county = ? AND latitude IS NULL",
        (county,),
    ).fetchall()
    return [r[0] for r in rows]


def polygon_centroid(rings: list) -> tuple[float, float] | None:
    """Plain average of the outer ring's own vertices -- see module
    docstring for why this, not a true area-weighted centroid, is good
    enough here. Esri repeats the first vertex as the last one to close
    the ring; that duplicate just nudges the average negligibly toward
    the starting point on a small parcel, not worth stripping out."""
    if not rings or not rings[0]:
        return None
    outer_ring = rings[0]
    lon_sum = sum(pt[0] for pt in outer_ring)
    lat_sum = sum(pt[1] for pt in outer_ring)
    count = len(outer_ring)
    return lat_sum / count, lon_sum / count


def query_batch(query_url: str, id_field: str, ids: list[str]) -> dict[str, tuple[float, float]]:
    quoted_ids = ",".join("'" + i.replace("'", "''") + "'" for i in ids)
    resp = requests.post(
        query_url,
        data={
            "where": f"{id_field} IN ({quoted_ids})",
            "outFields": id_field,
            "outSR": "4326",
            "returnGeometry": "true",
            "f": "json",
        },
        headers=HEADERS, timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if "error" in payload:
        raise RuntimeError(f"ArcGIS query error: {payload['error']}")

    found = {}
    for feature in payload.get("features", []):
        parcel_id = feature["attributes"].get(id_field)
        centroid = polygon_centroid(feature.get("geometry", {}).get("rings"))
        if parcel_id and centroid:
            found[parcel_id] = centroid
    return found


def backfill_county(conn: combined_db.PgConnection, county: str) -> int:
    config = COUNTY_CONFIGS[county]

    target_accounts = fetch_target_accounts(conn, county)
    print(f"{county}: {len(target_accounts)} listing(s) currently have no coordinates.")
    if not target_accounts:
        return 0

    id_to_originals: dict[str, list[str]] = {}
    for account_number in target_accounts:
        normalized = config["normalize_id"](account_number)
        id_to_originals.setdefault(normalized, []).append(account_number)

    normalized_ids = list(id_to_originals.keys())
    updated = 0
    rejected = 0
    for i in range(0, len(normalized_ids), BATCH_SIZE):
        chunk = normalized_ids[i:i + BATCH_SIZE]
        centroids = query_batch(config["query_url"], config["id_field"], chunk)
        for normalized_id, (lat, lon) in centroids.items():
            if not is_within_state_bounds("CA", lat, lon):
                rejected += 1
                continue
            for account_number in id_to_originals.get(normalized_id, []):
                combined_db.update_lat_lon(conn, county, account_number, lat, lon, state="CA")
                updated += 1

    print(f"{county}: backfilled {updated} of {len(target_accounts)} listing(s)."
          + (f" ({rejected} rejected as outside CA's bounds)" if rejected else ""))
    return updated


def main():
    assert "CA" in STATE_BOUNDING_BOXES, "geocode_backfill.py's STATE_BOUNDING_BOXES needs a CA entry for the safety net above"
    conn = combined_db.get_connection()
    total_updated = 0
    for county in COUNTY_CONFIGS:
        total_updated += backfill_county(conn, county)
    conn.close()
    print(f"\n{total_updated} listing(s) total backfilled with coordinates.")


if __name__ == "__main__":
    main()
