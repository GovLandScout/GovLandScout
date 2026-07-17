"""
GovLandScout - TDHCA State Property Scraper

Texas Department of Housing & Community Affairs' public property
clearinghouse (an Oracle PL/SQL web toolkit app -- plain server-rendered
HTML, no API to speak of) lists state-owned/state-financed property
across four categories: TDHCA Mortgaged Properties, Housing Tax Credit
Qualified Contract, Housing Tax Credit Right of First Refusal, and TDHCA
Single Family Properties.

Like the GSA federal listings, there's no independent value estimate
here -- "Price" is just the asking price TDHCA is offering the property
for, not an appraisal to compare a bid against. So minimum_bid is set to
that price, but estimated_value is intentionally left unset.
"""

import re
import ssl
import sqlite3
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup

import combined_db

URL = "https://public.tdhca.state.tx.us/pub/T_HF_CLEARINGHOUSE.list_for_sale"
DETAIL_URL = "https://public.tdhca.state.tx.us/pub/T_HF_CLEARINGHOUSE.display_property"
DB_PATH = "tdhca_properties.db"

HEADERS = {
    "User-Agent": "GovLandScout-SchoolProject/1.0 (contact: your-email@example.com)"
}


class LegacySSLAdapter(HTTPAdapter):
    """
    This server (an old Oracle PL/SQL web toolkit app) only offers cipher
    suites that Python's default OpenSSL security level (2) rejects, even
    though it speaks TLS 1.2 fine -- curl accepts the same handshake with
    its own default settings. Lowering to SECLEVEL=1 permits those older
    ciphers without disabling certificate verification.
    """
    def init_poolmanager(self, *args, **kwargs):
        context = ssl.create_default_context()
        context.set_ciphers("DEFAULT@SECLEVEL=1")
        kwargs["ssl_context"] = context
        return super().init_poolmanager(*args, **kwargs)


def fetch_page_html() -> str:
    session = requests.Session()
    session.mount("https://", LegacySSLAdapter())
    resp = session.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_listings(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    listings = []

    for heading in soup.find_all(["h2"]):
        category = re.sub(r"\s*\(\d+\)\s*$", "", heading.get_text(strip=True))
        table = heading.find_next("table", class_="dataTable")
        if not table:
            continue

        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 6:
                continue  # header row or spacer

            link = cells[0].find("a")
            if not link:
                continue

            property_name = link.get_text(strip=True)
            href = link.get("href", "")
            id_match = re.search(r"v_id=(\d+)", href)
            property_id = id_match.group(1) if id_match else None

            listings.append({
                "property_id": property_id,
                "category": category,
                "property_name": property_name,
                "city": cells[1].get_text(strip=True),
                "units": cells[2].get_text(strip=True),
                "price": cells[3].get_text(strip=True).replace("$", "").replace(",", "").strip(),
                "contact_name": cells[4].get_text(strip=True),
                "date_posted": cells[5].get_text(strip=True),
            })

    return listings


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tdhca_properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            property_id TEXT,
            category TEXT,
            property_name TEXT,
            city TEXT,
            units TEXT,
            price TEXT,
            contact_name TEXT,
            date_posted TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tdhca_property
        ON tdhca_properties(property_id, category)
    """)
    conn.commit()


def upsert_listing(conn: sqlite3.Connection, listing: dict):
    """Keyed on (property_id, category) -- properties without a detail id
    (shouldn't happen given the table structure, but be defensive) fall
    back to keying on name+city so they don't collide with each other."""
    now = datetime.now(timezone.utc).isoformat()
    key_id = listing["property_id"] or f"{listing['property_name']}|{listing['city']}"

    existing = conn.execute(
        "SELECT id FROM tdhca_properties WHERE property_id = ? AND category = ?",
        (key_id, listing["category"]),
    ).fetchone()

    fields = (
        listing["category"], listing["property_name"], listing["city"], listing["units"],
        listing["price"], listing["contact_name"], listing["date_posted"],
    )

    if existing:
        conn.execute(
            """
            UPDATE tdhca_properties SET
                category = ?, property_name = ?, city = ?, units = ?,
                price = ?, contact_name = ?, date_posted = ?, last_seen = ?
            WHERE property_id = ? AND category = ?
            """,
            fields + (now, key_id, listing["category"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO tdhca_properties (
                property_id, category, property_name, city, units, price,
                contact_name, date_posted, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (key_id,) + fields + (now, now),
        )
    conn.commit()


def main():
    print(f"Fetching {URL} ...")
    html = fetch_page_html()
    listings = parse_listings(html)
    print(f"Found {len(listings)} listings.")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    combined_conn = combined_db.get_connection()

    for listing in listings:
        upsert_listing(conn, listing)

        price = listing["price"]
        try:
            minimum_bid = str(float(price)) if price else None
        except ValueError:
            minimum_bid = None  # e.g. "Call for price" or similar non-numeric text

        account_number = listing["property_id"] or f"{listing['property_name']}-{listing['city']}"
        source_url = (
            f"{DETAIL_URL}?v_id={listing['property_id']}&v_cmd=inq"
            if listing["property_id"] else None
        )

        combined_db.upsert_listing(
            combined_conn,
            county="State",
            account_number=account_number,
            precinct=listing["category"],
            minimum_bid=minimum_bid,
            estimated_value=None,  # no independent value estimate -- just an asking price
            address=f"{listing['property_name']}, {listing['city']}, TX",
            description=f"{listing['category']} -- {listing['units']} unit(s), contact {listing['contact_name']}",
            status="Available",
            source="public.tdhca.state.tx.us",
            source_url=source_url,
        )

    combined_conn.close()

    print(f"Stored {len(listings)} listings into {DB_PATH}")

    rows = conn.execute(
        "SELECT category, property_name, city, price FROM tdhca_properties LIMIT 5"
    ).fetchall()
    print("\nSample of stored listings:")
    for row in rows:
        print(f"  {row[0]} | {row[1]}, {row[2]} | ${row[3]}")

    conn.close()


if __name__ == "__main__":
    main()
