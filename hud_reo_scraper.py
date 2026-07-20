"""
GovLandScout - HUD Foreclosed Home Scraper (Texas)

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
"""

import time
from datetime import datetime, timezone

import requests

import combined_db

FEATURE_SERVER_URL = "https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/SF_REO/FeatureServer/0/query"
COUNTY_LOOKUP_URL = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"

HEADERS = {
    "User-Agent": "GovLandScout-SchoolProject/1.0 (contact: your-email@example.com)"
}


def fetch_tx_properties() -> list[dict]:
    params = {
        "where": "STATE_CODE='TX'",
        "outFields": "*",
        "f": "json",
        "resultRecordCount": 2000,  # well above the ~60 TX records this feed currently has
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


def build_address(attrs: dict) -> str | None:
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
    return f"{street}, {city}, TX {zip_code}" if zip_code else f"{street}, {city}, TX"


def main():
    print("Fetching TX properties from HUD's Open Data feature service ...")
    properties = fetch_tx_properties()
    print(f"Found {len(properties)} TX propert(y/ies).")

    combined_conn = combined_db.get_connection()

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

        address = build_address(attrs)
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
        )
        stored += 1

    combined_conn.close()
    print(f"Stored {stored} listings ({skipped_no_county} skipped -- county lookup failed).")


if __name__ == "__main__":
    main()
