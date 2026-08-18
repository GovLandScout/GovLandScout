"""
GovLandScout - HUD Foreclosed Home Scraper (TX, PA, CA)

HUD's own interactive listing site (hudhomestore.gov) blanket-disallows
bots in robots.txt, so this doesn't touch it. Instead it pulls the same
underlying inventory from HUD's official Open Data portal -- an
ArcGIS-hosted feature service HUD explicitly publishes for public/
programmatic use ("FHA Single Family REO Properties For Sale"), a
legitimate open-data API rather than a scrape of a site that's asked
not to be crawled.

Two tradeoffs from using this feed instead of the (blocked) interactive
site: no list price is included here (shows up as "No data available",
same as GSA's federal listings), and there's no county field -- only
city/zip -- so each property's lat/lon (which the feed does include
directly) gets reverse-geocoded against the Census Bureau's geographies
API to find its county.

Originally Texas-only, with a `WHERE STATE_CODE='TX'` query and ", TX"
baked directly into every built address -- the feed itself is genuinely
nationwide (HUD's own STATE_CODE field takes any state), so this was a
real, deliberate narrowing to fix, not a bug like the same-shaped issue
found in gsa_scraper.py/irs_auction_scraper.py (which silently
mislabeled other states' listings as Texas -- this scraper never did
that, it just never asked for anything else). Generalized to
TARGET_STATES (this project's own three), confirmed directly against
the live feed first: 34 Pennsylvania and 19 California properties are
listed there right now, on top of TX's own ~57.
"""

import time
from datetime import datetime, timezone

import requests

import combined_db

FEATURE_SERVER_URL = "https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/SF_REO/FeatureServer/0/query"
COUNTY_LOOKUP_URL = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
TARGET_STATES = ("TX", "PA", "CA")

HEADERS = {
    "User-Agent": "GovLandScout-SchoolProject/1.0 (contact: your-email@example.com)"
}


def fetch_properties(state: str) -> list[dict]:
    params = {
        "where": f"STATE_CODE='{state}'",
        "outFields": "*",
        "f": "json",
        "resultRecordCount": 2000,  # well above the ~60 records any one of these states' feed currently has
    }
    resp = requests.get(FEATURE_SERVER_URL, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return [f["attributes"] for f in resp.json().get("features", [])]


def lookup_county(latitude: float, longitude: float) -> str | None:
    params = {
        "x": longitude,
        "y": latitude,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "format": "json",
    }
    resp = requests.get(COUNTY_LOOKUP_URL, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    counties = resp.json()["result"]["geographies"].get("Counties", [])
    return counties[0]["BASENAME"] if counties else None


def build_address(attrs: dict, state: str) -> str | None:
    # Source fields come padded with trailing whitespace (e.g. "18896 ",
    # "ROLLING HILLS                 ") -- strip everything before joining.
    street_num = (attrs.get("STREET_NUM") or "").strip()
    direction = (attrs.get("DIRECTION_PREFIX") or "").strip()
    street_name = (attrs.get("STREET_NAME") or "").strip()
    city = (attrs.get("CITY") or "").strip()
    zip_code = attrs.get("DISPLAY_ZIP_CODE")

    street = " ".join(part for part in [street_num, direction, street_name] if part)
    if not street or not city:
        return None
    return f"{street}, {city}, {state} {zip_code}" if zip_code else f"{street}, {city}, {state}"


def main():
    combined_conn = combined_db.get_connection()
    total_stored = 0

    for state in TARGET_STATES:
        print(f"Fetching {state} properties from HUD's Open Data feature service ...")
        properties = fetch_properties(state)
        print(f"Found {len(properties)} {state} propert(y/ies).")

        stored = 0
        skipped_no_county = 0
        for attrs in properties:
            case_num = attrs.get("CASE_NUM")
            latitude = attrs.get("MAP_LATITUDE")
            longitude = attrs.get("MAP_LONGITUDE")
            if not case_num or latitude is None or longitude is None:
                continue

            county = lookup_county(latitude, longitude)
            time.sleep(0.5)  # be a reasonably light touch on the free Census API
            if not county:
                skipped_no_county += 1
                continue  # county is required to store a listing at all

            address = build_address(attrs, state)
            date_acquired_ms = attrs.get("DATE_ACQUIRED")
            description = "HUD-owned foreclosed home"
            if date_acquired_ms:
                acquired = datetime.fromtimestamp(date_acquired_ms / 1000, tz=timezone.utc)
                description += f", acquired {acquired.strftime('%Y-%m-%d')}"

            combined_db.upsert_listing(
                combined_conn,
                county=county,
                account_number=case_num,
                precinct=None,
                minimum_bid=None,
                estimated_value=None,
                address=address,
                description=description,
                status="Available",
                source="hudgis-hud.opendata.arcgis.com",
                source_url=f"https://www.hudhomestore.gov/propertydetails?caseNumber={case_num}",
                latitude=latitude,
                longitude=longitude,
                state=state,
            )
            stored += 1
        total_stored += stored
        print(f"  stored {stored} of {len(properties)} ({skipped_no_county} skipped -- county lookup failed).")

    combined_conn.close()
    print(f"\n{total_stored} listing(s) stored across {TARGET_STATES}.")


if __name__ == "__main__":
    main()
