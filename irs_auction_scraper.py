"""
GovLandScout - IRS Seized Real Estate Auction Scraper (Texas)

irsauctions.gov (run by the IRS/Department of the Treasury) lists real
property seized under Internal Revenue Code 6331 for nonpayment of
FEDERAL income taxes -- a distinct legal process from the county
property-tax sales the rest of this project covers, and from GSA's
federal surplus real estate. Nationwide volume is low (regularly under
10 real-estate listings at a time across the whole country), so this
runs the same as houston_scraper.py/publicsurplus_scraper.py: cheap to
keep running daily even when it finds nothing, ready the moment a new
Texas listing appears.

The list view only gives city/state/zip, not a street address or county
-- both come from the "Notice of Sale" prose on each listing's detail
page, extracted with a regex and then geocoded (which also resolves the
county, since IRS doesn't provide one) via the Census Bureau's address
API, the same one geocode_backfill.py already uses elsewhere.
"""

import re

import requests
from bs4 import BeautifulSoup

import combined_db

LIST_URL = "https://www.irsauctions.gov/auction/items"
REAL_ESTATE_ASSET_TYPE = "8"
GEOCODE_URL = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"

HEADERS = {
    "User-Agent": "GovLandScout-SchoolProject/1.0 (contact: your-email@example.com)"
}

# Detail pages phrase the address a couple of different ways within the
# prose "Notice of Public Auction Sale" text -- try the most explicit
# form first ("Place of Sale: <address>"), then the looser one used on
# some listings ("more commonly known as <address>,").
ADDRESS_PATTERNS = [
    re.compile(r"Place of Sale:\s*([^\n]+?,\s*TX\s*\d{5})", re.IGNORECASE),
    re.compile(r"known as\s*([^,]+,\s*[^,]+,\s*TX\s*\d{5})", re.IGNORECASE),
]


def fetch_real_estate_cards() -> list[dict]:
    resp = requests.get(LIST_URL, headers=HEADERS, params={"field_asset_type_target_id": REAL_ESTATE_ASSET_TYPE}, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    cards = []
    for article in soup.select("article.irs-ad"):
        link = article.select_one("h3.usa-card__heading a")
        min_bid_el = article.select_one(".field--name-field-minimum-bid .field__item")
        if not link:
            continue

        # The dedicated location field is sometimes blank even when the
        # listing is clearly in Texas (title says so) -- check both
        # rather than relying on the location field alone.
        location_el = article.select_one(".field--name-field-property-address address")
        location_text = location_el.get_text(strip=True) if location_el else ""
        title_text = link.get_text(strip=True)
        combined_text = f"{location_text} {title_text}"
        if not re.search(r"\bTX\b", combined_text):
            continue  # not a Texas listing

        cards.append({
            "title": title_text,
            "detail_url": "https://www.irsauctions.gov" + link["href"],
            "minimum_bid": min_bid_el.get("content") if min_bid_el else None,
        })
    return cards


def extract_address(detail_html: str) -> str | None:
    soup = BeautifulSoup(detail_html, "html.parser")
    text = soup.get_text(" ", strip=True)
    for pattern in ADDRESS_PATTERNS:
        match = pattern.search(text)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
    return None


def geocode(address: str) -> tuple[float, float, str] | None:
    resp = requests.get(
        GEOCODE_URL,
        params={"address": address, "benchmark": "Public_AR_Current", "vintage": "Current_Current", "format": "json"},
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    matches = resp.json()["result"]["addressMatches"]
    if not matches:
        return None
    m = matches[0]
    counties = m["geographies"].get("Counties", [])
    if not counties:
        return None
    return m["coordinates"]["y"], m["coordinates"]["x"], counties[0]["BASENAME"]


def main():
    print(f"Fetching {LIST_URL} (Real-Estate, Texas) ...")
    cards = fetch_real_estate_cards()
    print(f"Found {len(cards)} Texas real estate listing(s).")

    if not cards:
        return

    combined_conn = combined_db.get_connection()
    stored = 0
    for card in cards:
        detail_resp = requests.get(card["detail_url"], headers=HEADERS, timeout=30)
        detail_resp.raise_for_status()
        address = extract_address(detail_resp.text)
        if not address:
            print(f"  skipping (no address found): {card['title']}")
            continue

        geocoded = geocode(address)
        if not geocoded:
            print(f"  skipping (geocode failed): {address}")
            continue
        latitude, longitude, county = geocoded

        # No stable per-listing case/parcel number is exposed on the
        # page -- the URL slug is the closest thing to a stable id.
        account_number = card["detail_url"].rsplit("/", 1)[-1][:32]

        combined_db.upsert_listing(
            combined_conn,
            county=county,
            account_number=account_number,
            precinct=None,
            minimum_bid=card["minimum_bid"],
            estimated_value=None,
            address=address,
            description=card["title"],
            status="Available",
            source="irsauctions.gov",
            source_url=card["detail_url"],
            latitude=latitude,
            longitude=longitude,
        )
        stored += 1

    combined_conn.close()
    print(f"Stored {stored} listings.")


if __name__ == "__main__":
    main()
