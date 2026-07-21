"""
GovLandScout - Houston Land Bank Scraper

The Houston Land Bank (a nonprofit created by the City of Houston) resells
tax-foreclosed vacant lots to the public, mostly in historically
underserved neighborhoods -- a distinct channel from the county tax
sale itself: these are properties that already went through tax
foreclosure, didn't sell (or were transferred to the land bank), and are
now being resold directly. Publishes its live inventory through
public-hlb.epropertyplus.com, a third-party government-property
platform (ePropertyPlus) with a public, keyless JSON API -- found by
reading the network calls the platform's own public listing page makes,
not by scraping rendered HTML.

The API also returns a "minimumBid" per property, but it's not a price
floor the way it is for tax sales -- askingPrice is consistently LOWER
than minimumBid across the board (one lot has a $1 asking price against
a $15,000 "minimum bid"), which only makes sense if minimumBid is
actually a required minimum development/construction investment, a
standard land-bank program condition to make sure lots get built on
rather than sitting vacant. Treating it as a real bid floor would make
every listing here look like steeply negative equity, which isn't what
it means -- so only askingPrice (the actual land price) is stored as
minimum_bid, and estimated_value is left unset (there's no independent
market-value estimate here, same as GSA/HUD).
"""

import requests

import combined_db

API_URL = "https://public-hlb.epropertyplus.com/landmgmtpub/remote/public/property/getPublishedProperties"
DETAIL_URL = "https://public-hlb.epropertyplus.com/property/{id}"

HEADERS = {
    "User-Agent": "GovLandScout-SchoolProject/1.0 (contact: your-email@example.com)",
    "Accept": "application/json",
}

PARAMS = {
    "page": 1,
    "limit": 500,  # well above current inventory (~17); one page covers it all
    "json": '{"criterias":[]}',
    "sort": '[{"property":"cleanupAssessment","direction":"asc"}]',
}


def fetch_properties() -> list[dict]:
    resp = requests.get(API_URL, headers=HEADERS, params=PARAMS, timeout=30)
    resp.raise_for_status()
    return resp.json().get("rows", [])


def build_address(row: dict) -> str | None:
    street = (row.get("propertyAddress1") or "").strip()
    city = (row.get("city") or "").strip()
    state = (row.get("state") or "TX").strip()
    zip_code = row.get("postalCode")
    if not street or not city:
        return None
    return f"{street}, {city}, {state} {zip_code}" if zip_code else f"{street}, {city}, {state}"


def build_description(row: dict) -> str:
    min_investment = row.get("minimumBid")
    parts = [
        row.get("propertyClass"),
        row.get("neighborhood") and f"Neighborhood: {row['neighborhood']}",
        min_investment is not None and f"Minimum required development investment: ${min_investment:,.0f}",
        row.get("legalDescription"),
    ]
    return " -- ".join(p for p in parts if p)


def main():
    print(f"Fetching {API_URL} ...")
    rows = fetch_properties()
    print(f"Found {len(rows)} published propert(y/ies).")

    combined_conn = combined_db.get_connection()
    for row in rows:
        parcel_number = row.get("parcelNumber")
        if not parcel_number:
            continue

        combined_db.upsert_listing(
            combined_conn,
            county=row.get("county") or "Harris",
            account_number=parcel_number,
            precinct=row.get("votingPrecinct"),
            minimum_bid=str(row["askingPrice"]) if row.get("askingPrice") is not None else None,
            estimated_value=None,
            address=build_address(row),
            description=build_description(row),
            status="Available",
            source="houstonlandbank.org",
            source_url=DETAIL_URL.format(id=row["id"]),
            latitude=row.get("latitude"),
            longitude=row.get("longitude"),
        )

    combined_conn.close()
    print(f"Stored {len(rows)} listings.")


if __name__ == "__main__":
    main()
