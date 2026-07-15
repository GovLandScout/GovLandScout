"""
GovLandScout 

Scraper number two, covering (hoepfully) 533 properties across dallas as of now.

This does use the site of the tax law firms, since county hasn't publishings.
"""

import sqlite3
from datetime import datetime, timezone

import requests

API_URL = "https://taxsales.lgbs.com/api/property_sales/"
COUNTY = "DALLAS COUNTY"
DB_PATH = "dallas_tax_sales.db"
PAGE_SIZE = 100

HEADERS = {
    "User-Agent": "GovLandScout-SchoolProject/1.0 (contact: your-email@example.com)"
}


def fetch_all_listings() -> list[dict]:
 
    listings = []
    url = API_URL
    params = {"county": COUNTY, "limit": PAGE_SIZE}

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
        CREATE TABLE IF NOT EXISTS dallas_tax_sale_listings (
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
        CREATE UNIQUE INDEX IF NOT EXISTS idx_dallas_uid
        ON dallas_tax_sale_listings(uid)
    """)
    conn.commit()


def upsert_listing(conn: sqlite3.Connection, listing: dict):
    """Keyed on uid, the API's own stable per-listing identifier."""
    now = datetime.now(timezone.utc).isoformat()

    geometry = listing.get("geometry") or {}
    coords = geometry.get("coordinates") or [None, None]
    longitude, latitude = coords[0], coords[1]

    existing = conn.execute(
        "SELECT id FROM dallas_tax_sale_listings WHERE uid = ?",
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
            UPDATE dallas_tax_sale_listings SET
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
            INSERT INTO dallas_tax_sale_listings (
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

    for listing in listings:
        upsert_listing(conn, listing)

    print(f"Stored {len(listings)} listings into {DB_PATH}")

    rows = conn.execute(
        "SELECT account_nbr, minimum_bid, value, prop_address_one "
        "FROM dallas_tax_sale_listings LIMIT 3"
    ).fetchall()
    print("\nSample of stored listings:")
    for row in rows:
        print(f"  Acct#{row[0]} | Min Bid: {row[1]} | Value: {row[2]} | {row[3]}")

    conn.close()


if __name__ == "__main__":
    main()