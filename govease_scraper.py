"""
GovLandScout - GovEase Scraper (Texas counties)

GovEase (liveauctions.govease.com) is another online tax sale platform,
the same relationship to this project as RealAuction (see
realauction_scraper.py's docstring): the trustee firm/county markets a
sale, but the actual bidding happens on GovEase's shared multi-state
platform. Its own site-wide "Choose State/County" dropdown lists exactly
four Texas auctions:

    TX - Denton
    TX - Grayson
    TX - McLennan - MVBA
    TX - Wichita

McLennan is explicitly labeled "- MVBA" and already shows up in this
project's data via mvba_scraper.py's own PDF listings for that county --
scraping it here too would double-count the same properties under two
sources, so it's deliberately excluded (same reasoning as RealAuction's
exclusion list). Denton, Grayson, and Wichita aren't LGBS/MVBA/PBFCM
clients, so there's nothing to double up against for those three.

Unlike RealAuction, this doesn't need any session/JS reverse-engineering:
each auction's /browsestandard page 302-redirects to /browse, which
server-renders the full property grid directly in the initial HTML
(confirmed via the page's own inline script: the DataTable is initialized
with "paging": false, i.e. everything is on one page, no pagination to
walk).

Column layout varies slightly between auctions/states (e.g. Denton's bid
column is labeled "Minimum Bid", Grayson's "Face Value" -- and other
states' auctions add columns like "Property Description" that TX's don't
have), so this reads the actual <thead> to map label -> column index per
auction rather than assuming a fixed position, the same defensiveness
realauction_scraper.py's parse_ad_table uses for its own label/value
pairs.

"Unique #" is just that auction's row sequence number (resets and isn't
stable across runs), so it's not usable as a dedupe key -- "Parcel #"
holds the real case/cause number (e.g. "19-7161-16") and is what gets
used as account_number instead.
"""

import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

import combined_db

BASE_URL = "https://liveauctions.govease.com"
DB_PATH = "govease_properties.db"

# (county, state, slug, auction_id) -- McLennan deliberately omitted, see
# module docstring.
COUNTIES = [
    ("Denton", "tx", "txdenton", 1355),
    ("Grayson", "tx", "txgrayson", 1280),
    ("Wichita", "tx", "txwichita", 1429),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

BID_COLUMN_LABELS = {"Minimum Bid", "Starting Bid", "Face Value"}


def clean_money(text: str) -> str | None:
    value = text.replace("$", "").replace(",", "").strip()
    return value or None


def parse_county_grid(html: str, county: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="dt-auctions")
    if table is None or table.find("tbody") is None:
        return []

    headers = [th.get_text(strip=True).rstrip(":") for th in table.find("thead").find_all("th")]
    bid_label = next((label for label in headers if label in BID_COLUMN_LABELS), None)
    col_index = {label: i for i, label in enumerate(headers) if label}

    listings = []
    for row in table.find("tbody").find_all("tr"):
        cells = row.find_all("td")
        if len(cells) != len(headers):
            continue  # not a real data row (e.g. DataTables' own "no data" placeholder)

        def cell_text(label: str) -> str:
            idx = col_index.get(label)
            return cells[idx].get_text(" ", strip=True) if idx is not None else ""

        account_number = cell_text("Parcel #").strip()
        if not account_number:
            continue

        link = cells[col_index["Unique #"]].find("a") if "Unique #" in col_index else None
        source_url = urljoin(BASE_URL, link["href"]) if link and link.get("href") else None

        address = cell_text("Parcel Address").strip()
        if address.upper() in ("", "N/A"):
            address = None

        min_bid = clean_money(cell_text(bid_label)) if bid_label else None

        description = " -- ".join(
            p for p in (cell_text("Auction Name").strip(), cell_text("Auction Type").strip()) if p
        ) or None

        listings.append({
            "county": county,
            "account_number": account_number,
            "minimum_bid": min_bid,
            "address": address,
            "description": description,
            "source_url": source_url,
        })

    return listings


def fetch_county(session: requests.Session, state: str, slug: str, auction_id: int) -> str | None:
    resp = session.get(
        f"{BASE_URL}/{state}/{slug}/{auction_id}/browsestandard",
        headers=HEADERS, timeout=30,
    )
    if resp.status_code != 200:
        return None
    return resp.text


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS govease_properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            county TEXT,
            account_number TEXT,
            minimum_bid TEXT,
            address TEXT,
            description TEXT,
            source_url TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_govease_county_account
        ON govease_properties(county, account_number)
    """)
    conn.commit()


def upsert_local(conn: sqlite3.Connection, listing: dict):
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT id FROM govease_properties WHERE county = ? AND account_number = ?",
        (listing["county"], listing["account_number"]),
    ).fetchone()

    fields = (listing["minimum_bid"], listing["address"], listing["description"], listing["source_url"])
    if existing:
        conn.execute(
            """UPDATE govease_properties SET
                minimum_bid = ?, address = ?, description = ?, source_url = ?, last_seen = ?
               WHERE county = ? AND account_number = ?""",
            fields + (now, listing["county"], listing["account_number"]),
        )
    else:
        conn.execute(
            """INSERT INTO govease_properties (
                minimum_bid, address, description, source_url,
                county, account_number, first_seen, last_seen
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            fields + (listing["county"], listing["account_number"], now, now),
        )
    conn.commit()


def main():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    combined_conn = combined_db.get_connection()

    session = requests.Session()
    total_listings = 0

    for county, state, slug, auction_id in COUNTIES:
        html = fetch_county(session, state, slug, auction_id)
        if html is None:
            print(f"  {county}: fetch failed")
            continue

        listings = parse_county_grid(html, county)
        print(f"  {county}: {len(listings)} propert{'y' if len(listings) == 1 else 'ies'}")

        for listing in listings:
            upsert_local(conn, listing)
            combined_db.upsert_listing(
                combined_conn,
                county=listing["county"],
                account_number=listing["account_number"],
                precinct=None,
                minimum_bid=listing["minimum_bid"],
                estimated_value=None,  # no independent value estimate on this platform, only a minimum bid
                address=listing["address"],
                description=listing["description"],
                status="Active",
                source="govease.com",
                source_url=listing["source_url"],
            )
            total_listings += 1

    combined_conn.close()
    conn.close()
    print(f"\n{total_listings} listing(s) stored into {DB_PATH}")


if __name__ == "__main__":
    main()
