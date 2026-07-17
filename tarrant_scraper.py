"""
GovLandScout - Tarrant County Tax Sale Scraper

Same source and mechanism as dallas_scraper.py -- Tarrant County's tax sales
are also published through Linebarger Goggan Blair & Sampson's public API
at taxsales.lgbs.com, just filtered to a different county.
"""

import sqlite3
from datetime import datetime, timezone

import requests

import combined_db

API_URL = "https://taxsales.lgbs.com/api/property_sales/"
COUNTY = "TARRANT COUNTY"
DB_PATH = "tarrant_tax_sales.db"
PAGE_SIZE = 100

HEADERS = {
    "User-Agent": "GovLandScout-SchoolProject/1.0 (contact: your-email@example.com)"
}


def fetch_all_listings() -> list[dict]:
    """
    `ordering` is required, not optional -- without an explicit stable sort,
    limit/offset pagination over a dataset with ties in the default order
    can shift rows between pages mid-scrape, silently skipping or
    duplicating listings. `uid` alone is sufficient since it's unique.
    """
    listings = []
    url = API_URL
    params = {"county": COUNTY, "limit": PAGE_SIZE, "ordering": "uid"}

    while url:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        listings.extend(data["results"])
        url = data.get("next")
        params = None  # `next` already includes all query params

    return listings


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tarrant_tax_sale_listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid INTEGER,
            sale_id INTEGER,
            county TEXT,
            cause_nbr TEXT,
            precinct TEXT,
            sale_date TEXT,
            sale_type TEXT,
            status TEXT,
            account_nbr TEXT,
            prop_address_one TEXT,
            prop_city TEXT,
            prop_state TEXT,
            prop_zipcode TEXT,
            value TEXT,
            minimum_bid TEXT,
            latitude REAL,
            longitude REAL,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tarrant_uid
        ON tarrant_tax_sale_listings(uid)
    """)
    conn.commit()


def upsert_listing(conn: sqlite3.Connection, listing: dict):
    """Keyed on uid, the API's own stable per-listing identifier."""
    now = datetime.now(timezone.utc).isoformat()

    geometry = listing.get("geometry") or {}
    coords = geometry.get("coordinates") or [None, None]
    longitude, latitude = coords[0], coords[1]

    existing = conn.execute(
        "SELECT id FROM tarrant_tax_sale_listings WHERE uid = ?",
        (listing["uid"],),
    ).fetchone()

    fields = (
        listing["sale_id"], listing["county"], listing["cause_nbr"], listing["precinct"],
        listing["sale_date"], listing["sale_type"], listing["status"], listing["account_nbr"],
        listing["prop_address_one"], listing["prop_city"], listing["prop_state"],
        listing["prop_zipcode"], listing["value"], listing["minimum_bid"],
        latitude, longitude,
    )

    if existing:
        conn.execute(
            """
            UPDATE tarrant_tax_sale_listings SET
                sale_id = ?, county = ?, cause_nbr = ?, precinct = ?, sale_date = ?,
                sale_type = ?, status = ?, account_nbr = ?, prop_address_one = ?,
                prop_city = ?, prop_state = ?, prop_zipcode = ?, value = ?,
                minimum_bid = ?, latitude = ?, longitude = ?, last_seen = ?
            WHERE uid = ?
            """,
            fields + (now, listing["uid"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO tarrant_tax_sale_listings (
                uid, sale_id, county, cause_nbr, precinct, sale_date, sale_type,
                status, account_nbr, prop_address_one, prop_city, prop_state,
                prop_zipcode, value, minimum_bid, latitude, longitude,
                first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (listing["uid"],) + fields + (now, now),
        )
    conn.commit()


def main():
    print(f"Fetching {COUNTY} listings from {API_URL} ...")
    listings = fetch_all_listings()
    print(f"Found {len(listings)} listings.")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    combined_conn = combined_db.get_connection()

    for listing in listings:
        upsert_listing(conn, listing)

        address = ", ".join(
            part for part in (
                listing["prop_address_one"],
                listing["prop_city"],
                f"{listing['prop_state']} {listing['prop_zipcode']}".strip(),
            ) if part
        )
        combined_db.upsert_listing(
            combined_conn,
            county="Tarrant",
            account_number=listing["account_nbr"],
            precinct=listing["precinct"] or None,
            minimum_bid=listing["minimum_bid"],
            estimated_value=listing["value"],
            address=address,
            description=None,  # Tarrant doesn't provide a separate legal description
            status=listing["status"],
            source="taxsales.lgbs.com",
        )

    combined_conn.close()

    print(f"Stored {len(listings)} listings into {DB_PATH}")

    rows = conn.execute(
        "SELECT account_nbr, minimum_bid, value, prop_address_one "
        "FROM tarrant_tax_sale_listings LIMIT 3"
    ).fetchall()
    print("\nSample of stored listings:")
    for row in rows:
        print(f"  Acct#{row[0]} | Min Bid: {row[1]} | Value: {row[2]} | {row[3]}")

    conn.close()


if __name__ == "__main__":
    main()
