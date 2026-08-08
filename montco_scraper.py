"""
GovLandScout - Montgomery County (PA) Upset Tax Sale Scraper

Montgomery County publishes its own Upset Sale list directly on
montgomerycountypa.gov -- a plain government site with no bot defense,
unlike bid4assets.com (see bid4assets_scraper.py's docstring for that
whole saga). This scraper follows the same low-risk shape as
mvba_scraper.py: one PDF, one consistent table template, no rate-limiting
or connection-lifetime concerns since it's a single fetch-parse-store
pass, not a long-running loop.

Finding the actual PDF takes two hops, both done dynamically rather than
hardcoded, since both change every year:
  1. GET the Upset Sale page (SALE_LIST_PAGE_URL) and find whichever link
     has both an aria-label mentioning "Sale List" and an href pointing
     at /archival-document?id=<N> -- the id is a new number each year.
  2. That URL doesn't serve the PDF itself, only an "Archival Document"
     viewer page with the real asset URL (an assets.montgomerycountypa.gov
     link, filename dated to the day it was last updated) embedded in it.

The table itself is one consistent 6-column template across all 30 pages
of the 2026 list (Municipality, Sale Number, Parcel, BOA Owner Name,
BOA: Location, Approx. Sale Price) -- confirmed by checking every row's
length is exactly 6 with no exceptions, and every Parcel value is unique
(712 rows, 712 distinct parcels), so unlike pbfcm_scraper.py this doesn't
need format-detection machinery, just one parser. The header row only
appears once, as the first row of the first page that has a table --
every later page's table starts directly with data, so the parser
recognizes and skips a header by its first cell ("Municipality") rather
than assuming it only ever appears on page 1.

No independent value estimate is published here either (same as MVBA and
PBFCM) -- "Approx. Sale Price" is the amount owed, not an appraisal, so
it's stored as minimum_bid with estimated_value left unset.

Addresses have no city in them ("224 -228 FOREST AVE", nothing else) --
but the table's own Municipality column supplies one (the actual
municipality name, e.g. "Ambler", not just the county), giving
geocode_backfill.py a real city to work with rather than needing its
county-name fallback.
"""

import re
import sqlite3
from datetime import datetime, timezone
from io import BytesIO

import pdfplumber
import requests

import combined_db

BASE_URL = "https://www.montgomerycountypa.gov"
SALE_LIST_PAGE_URL = f"{BASE_URL}/2331/Upset-Sale"
DB_PATH = "montco_properties.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

LINK_TAG_PATTERN = re.compile(r"<a\s+[^>]*>", re.IGNORECASE)
HREF_PATTERN = re.compile(r'href="([^"]+)"')
ARIA_LABEL_PATTERN = re.compile(r'aria-label="([^"]+)"')
PDF_ASSET_PATTERN = re.compile(r'"(https://assets\.montgomerycountypa\.gov/files/[^"]+?\.pdf)"')

HEADER_FIRST_CELL = "Municipality"
EXPECTED_COLUMN_COUNT = 6


def find_archival_document_url(html: str) -> str | None:
    """The Upset Sale page's own link to this year's list -- matched by
    aria-label text and href shape rather than a hardcoded id, since a new
    id gets assigned every year (2026's was id=19406)."""
    for tag in LINK_TAG_PATTERN.findall(html):
        aria_match = ARIA_LABEL_PATTERN.search(tag)
        href_match = HREF_PATTERN.search(tag)
        if not aria_match or not href_match:
            continue
        if "sale list" in aria_match.group(1).lower() and "/archival-document" in href_match.group(1):
            return href_match.group(1)
    return None


def find_pdf_asset_url(html: str) -> str | None:
    """The archival-document link above resolves to a viewer page, not the
    PDF itself -- the real asset URL is embedded in that page's own markup."""
    match = PDF_ASSET_PATTERN.search(html)
    return match.group(1) if match else None


def parse_money(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = text.replace("$", "").replace(",", "").strip()
    if not cleaned or not re.match(r"^\d+(\.\d+)?$", cleaned):
        return None
    return cleaned


def collapse_whitespace(text: str | None) -> str | None:
    """PDF cells wrap long values onto multiple lines (e.g. a long owner
    name or municipality) -- join back into one line."""
    if not text:
        return None
    joined = " ".join(text.split())
    return joined or None


def parse_pdf(content: bytes, source_url: str) -> list[dict]:
    listings = []
    with pdfplumber.open(BytesIO(content)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    if len(row) != EXPECTED_COLUMN_COUNT:
                        continue
                    municipality, sale_number, parcel, owner_name, location, sale_price = row
                    if municipality == HEADER_FIRST_CELL:
                        continue  # the one header row, wherever it lands
                    parcel = collapse_whitespace(parcel)
                    if not parcel:
                        continue

                    municipality = collapse_whitespace(municipality)
                    location = collapse_whitespace(location)
                    address = f"{location}, {municipality}, PA" if location and municipality else None

                    sale_number = collapse_whitespace(sale_number)
                    description = f"Upset Sale -- Sale #{sale_number}" if sale_number else "Upset Sale"

                    listings.append({
                        "county": "Montgomery",
                        "precinct": municipality,
                        "account_number": parcel,
                        "minimum_bid": parse_money(sale_price),
                        "address": address,
                        "description": description,
                        "source_url": source_url,
                    })
    return listings


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS montco_properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            precinct TEXT,
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
        CREATE UNIQUE INDEX IF NOT EXISTS idx_montco_account
        ON montco_properties(account_number)
    """)
    conn.commit()


def upsert_local(conn: sqlite3.Connection, listing: dict):
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT id FROM montco_properties WHERE account_number = ?",
        (listing["account_number"],),
    ).fetchone()

    fields = (
        listing["precinct"], listing["minimum_bid"], listing["address"],
        listing["description"], listing["source_url"],
    )
    if existing:
        conn.execute(
            """UPDATE montco_properties SET
                precinct = ?, minimum_bid = ?, address = ?, description = ?,
                source_url = ?, last_seen = ?
               WHERE account_number = ?""",
            fields + (now, listing["account_number"]),
        )
    else:
        conn.execute(
            """INSERT INTO montco_properties (
                precinct, minimum_bid, address, description, source_url,
                account_number, first_seen, last_seen
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            fields + (listing["account_number"], now, now),
        )
    conn.commit()


def main():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    session = requests.Session()

    print(f"Fetching {SALE_LIST_PAGE_URL} ...")
    page_resp = session.get(SALE_LIST_PAGE_URL, headers=HEADERS, timeout=30)
    page_resp.raise_for_status()

    archival_url = find_archival_document_url(page_resp.text)
    if archival_url is None:
        print("Couldn't find this year's Sale List link -- page structure may have changed.")
        conn.close()
        return
    if not archival_url.startswith("http"):
        archival_url = f"{BASE_URL}{archival_url}"

    print(f"Fetching {archival_url} ...")
    viewer_resp = session.get(archival_url, headers=HEADERS, timeout=30)
    viewer_resp.raise_for_status()

    pdf_url = find_pdf_asset_url(viewer_resp.text)
    if pdf_url is None:
        print("Couldn't find the actual PDF asset URL on the archival document page.")
        conn.close()
        return

    print(f"Fetching {pdf_url} ...")
    pdf_resp = session.get(pdf_url, headers=HEADERS, timeout=60)
    pdf_resp.raise_for_status()

    listings = parse_pdf(pdf_resp.content, pdf_url)
    print(f"Found {len(listings)} listing(s).")

    combined_conn = combined_db.get_connection()
    for listing in listings:
        upsert_local(conn, listing)
        combined_db.upsert_listing(
            combined_conn,
            county=listing["county"],
            account_number=listing["account_number"],
            precinct=listing["precinct"],
            minimum_bid=listing["minimum_bid"],
            estimated_value=None,  # Montgomery doesn't publish an independent value estimate
            address=listing["address"],
            description=listing["description"],
            status="Active",
            source="montgomerycountypa.gov",
            source_url=listing["source_url"],
            state="PA",
            commit=False,
        )
    combined_conn.commit()
    combined_conn.close()

    conn.close()
    print(f"\n{len(listings)} listing(s) stored into {DB_PATH}")


if __name__ == "__main__":
    main()
