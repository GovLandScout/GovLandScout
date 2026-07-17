"""
GovLandScout - LGBS Tax Sale Scraper (all Texas counties)

Linebarger Goggan Blair & Sampson's public API at taxsales.lgbs.com covers
delinquent tax sale listings across every county they represent -- not just
one at a time. Calling it with no `county` filter returns everything, so
rather than one scraper per county (which is what dallas_scraper.py and
tarrant_scraper.py used to be, before being folded into this), this fetches
the whole dataset in one pass and lets each listing's own `county` field
route it.

Two exclusions:
  - Non-Texas counties. LGBS also operates in at least Pennsylvania; this
    project is Texas-only.
  - Harris County. LGBS represents ~381 Harris County listings (likely a
    different taxing entity than the county itself -- school district, city,
    or MUD collections can be handled by a different firm than the county's
    own delinquent tax collections). hctax_scraper.py already covers Harris
    via the county's own site; mixing in a second, differently-sourced set
    of Harris listings under the same county key risks silently colliding
    with or overwriting that data.
"""

import sqlite3
from datetime import datetime, timezone

import requests

import combined_db

API_URL = "https://taxsales.lgbs.com/api/property_sales/"
DB_PATH = "lgbs_tax_sales.db"
PAGE_SIZE = 200
EXCLUDED_COUNTIES = {"HARRIS COUNTY"}

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
    params = {"limit": PAGE_SIZE, "ordering": "uid"}

    while url:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        listings.extend(data["results"])
        url = data.get("next")
        params = None  # `next` already includes all query params

    return listings


def normalize_county_name(raw_county: str) -> str:
    """'DALLAS COUNTY' -> 'Dallas', 'LA SALLE COUNTY' -> 'La Salle'"""
    name = raw_county.upper()
    if name.endswith(" COUNTY"):
        name = name[: -len(" COUNTY")]
    return name.title()


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lgbs_tax_sale_listings (
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
        CREATE UNIQUE INDEX IF NOT EXISTS idx_lgbs_uid
        ON lgbs_tax_sale_listings(uid)
    """)
    conn.commit()


def upsert_listing(conn: sqlite3.Connection, listing: dict):
    """Keyed on uid, the API's own stable per-listing identifier."""
    now = datetime.now(timezone.utc).isoformat()

    geometry = listing.get("geometry") or {}
    coords = geometry.get("coordinates") or [None, None]
    longitude, latitude = coords[0], coords[1]

    existing = conn.execute(
        "SELECT id FROM lgbs_tax_sale_listings WHERE uid = ?",
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
            UPDATE lgbs_tax_sale_listings SET
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
            INSERT INTO lgbs_tax_sale_listings (
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
    print(f"Fetching all listings from {API_URL} ...")
    listings = fetch_all_listings()
    print(f"Found {len(listings)} listings before filtering.")

    listings = [
        l for l in listings
        if l["state"] == "TX" and l["county"] not in EXCLUDED_COUNTIES
    ]
    counties = sorted(set(l["county"] for l in listings))
    print(f"{len(listings)} listings remain after filtering to Texas, excluding Harris.")
    print(f"Covers {len(counties)} counties.")

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
            county=normalize_county_name(listing["county"]),
            account_number=listing["account_nbr"],
            precinct=listing["precinct"] or None,
            minimum_bid=listing["minimum_bid"],
            estimated_value=listing["value"],
            address=address,
            description=None,  # LGBS doesn't provide a separate legal description
            status=listing["status"],
            source="taxsales.lgbs.com",
        )

    combined_conn.close()

    print(f"Stored {len(listings)} listings into {DB_PATH}")

    rows = conn.execute(
        "SELECT county, account_nbr, minimum_bid, value, prop_address_one "
        "FROM lgbs_tax_sale_listings LIMIT 3"
    ).fetchall()
    print("\nSample of stored listings:")
    for row in rows:
        print(f"  {row[0]} | Acct#{row[1]} | Min Bid: {row[2]} | Value: {row[3]} | {row[4]}")

    conn.close()


if __name__ == "__main__":
    main()
