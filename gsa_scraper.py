"""
GovLandScout - Federal Surplus Real Estate Scraper (GSA)

realestatesales.gov (run by the General Services Administration) lists
surplus federal real estate up for competitive auction -- a genuinely
different kind of asset than the county tax-deed sales the rest of this
project covers. There's no independent appraised "value" the way tax
sales have an adjudged value to compare a minimum bid against; GSA's
`property_price` is just the auction's STARTING price, and bidding can
push the current price above or below it depending on demand. So there's
no honest "equity" calculation here -- minimum_bid is set to the live
price (current bid if any exist, else the starting price), but
estimated_value is intentionally left unset. These listings fall into
find_deals.py's "unpriced" bucket rather than being ranked alongside
tax sale deals, since ranking them the same way would be comparing two
different things.

The page itself is server-rendered, but pagination/filtering goes
through a JSON endpoint at the same URL (POST, not GET) -- found by
reading our_listing_js.js's $.ajax() call rather than guessing.
"""

import sqlite3
from datetime import datetime, timezone

import requests

import combined_db

URL = "https://realestatesales.gov/our-listing/"
DB_PATH = "gsa_real_estate_sales.db"
PAGE_SIZE = 48

HEADERS = {
    "User-Agent": "GovLandScout-SchoolProject/1.0 (contact: your-email@example.com)",
    "X-Requested-With": "XMLHttpRequest",
}


def fetch_all_listings() -> list[dict]:
    """
    listing_filter=all_listing matches what the site shows by default
    (active + coming-soon auctions) -- excludes closed/sold listings,
    which aren't opportunities anyway.
    """
    listings = []
    page = 1

    while True:
        resp = requests.post(
            URL,
            headers=HEADERS,
            data={
                "search": "",
                "perpage": PAGE_SIZE,
                "page": page,
                "listing_filter": "all_listing",
                "sort_column": "auction_start_date_asc",
                "agent_id": "",
                "filter_asset_type": "",
                "filter_auction_type": "",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        page_listings = data.get("property_list") or []
        listings.extend(page_listings)

        if not page_listings or page >= data.get("no_page", 1):
            break
        page += 1

    return listings


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gsa_listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            property_id INTEGER,
            name TEXT,
            case_number TEXT,
            property_asset TEXT,
            auction_type TEXT,
            status TEXT,
            address_one TEXT,
            city TEXT,
            state_name TEXT,
            iso_state_name TEXT,
            postal_code TEXT,
            property_price REAL,
            property_current_price REAL,
            sold_for REAL,
            bidding_start TEXT,
            bidding_end TEXT,
            latitude REAL,
            longitude REAL,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_gsa_property_id
        ON gsa_listings(property_id)
    """)
    conn.commit()


def upsert_listing(conn: sqlite3.Connection, listing: dict):
    """Keyed on property_id, GSA's own stable identifier."""
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT id FROM gsa_listings WHERE property_id = ?",
        (listing["id"],),
    ).fetchone()

    fields = (
        listing["name"], listing.get("case_number"), listing.get("property_asset"),
        listing.get("auction_type"), listing.get("status"), listing.get("address_one"),
        listing.get("city"), listing.get("state_name"), listing.get("iso_state_name"),
        listing.get("postal_code"), listing.get("property_price"),
        listing.get("property_current_price"), listing.get("sold_for"),
        listing.get("bidding_start"), listing.get("bidding_end"),
        listing.get("latitude"), listing.get("longitude"),
    )

    if existing:
        conn.execute(
            """
            UPDATE gsa_listings SET
                name = ?, case_number = ?, property_asset = ?, auction_type = ?,
                status = ?, address_one = ?, city = ?, state_name = ?,
                iso_state_name = ?, postal_code = ?, property_price = ?,
                property_current_price = ?, sold_for = ?, bidding_start = ?,
                bidding_end = ?, latitude = ?, longitude = ?, last_seen = ?
            WHERE property_id = ?
            """,
            fields + (now, listing["id"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO gsa_listings (
                property_id, name, case_number, property_asset, auction_type,
                status, address_one, city, state_name, iso_state_name,
                postal_code, property_price, property_current_price, sold_for,
                bidding_start, bidding_end, latitude, longitude,
                first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (listing["id"],) + fields + (now, now),
        )
    conn.commit()


def main():
    print(f"Fetching listings from {URL} ...")
    listings = fetch_all_listings()
    print(f"Found {len(listings)} listings.")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    combined_conn = combined_db.get_connection()

    for listing in listings:
        upsert_listing(conn, listing)

        address = ", ".join(
            part for part in (
                listing.get("address_one"),
                listing.get("city"),
                f"{listing.get('iso_state_name', '')} {listing.get('postal_code', '')}".strip(),
            ) if part
        )

        current_price = listing.get("property_current_price") or 0
        starting_price = listing.get("property_price")
        minimum_bid = current_price if current_price > 0 else starting_price

        combined_db.upsert_listing(
            combined_conn,
            county="Federal",
            account_number=str(listing["id"]),
            precinct=listing.get("state_name"),
            minimum_bid=str(minimum_bid) if minimum_bid is not None else None,
            estimated_value=None,  # no independent value estimate exists for these
            address=address,
            description=listing.get("name"),
            status=listing.get("status"),
            source="realestatesales.gov",
            source_url=f"https://realestatesales.gov/asset-details/?property_id={listing['id']}",
            latitude=float(listing["latitude"]) if listing.get("latitude") else None,
            longitude=float(listing["longitude"]) if listing.get("longitude") else None,
        )

    combined_conn.close()

    print(f"Stored {len(listings)} listings into {DB_PATH}")

    rows = conn.execute(
        "SELECT name, property_price, property_current_price, city, state_name "
        "FROM gsa_listings LIMIT 3"
    ).fetchall()
    print("\nSample of stored listings:")
    for row in rows:
        print(f"  {row[0]} | Starting: {row[1]} | Current: {row[2]} | {row[3]}, {row[4]}")

    conn.close()


if __name__ == "__main__":
    main()
