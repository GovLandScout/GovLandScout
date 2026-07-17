"""
GovLandScout - City of Houston Real Property Scraper

houstontx.gov/generalservices/realbids.html lists City-owned real estate
being sold off. As of this scraper being written, the page has zero
active offerings ("Active Offerings: None at this time") -- there's no
real listing to verify a structured parser against, unlike every other
scraper in this project. So this deliberately stays conservative: it
detects the "Active Offerings" bullet list, skips the "none at this
time" placeholder, and captures whatever text/link each real entry
contains (raw, unparsed) rather than guessing at a field layout (address,
price, etc.) that might be wrong the first time real data shows up.
Revisit this once an actual listing appears.
"""

import hashlib
import sqlite3
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

import combined_db

URL = "https://www.houstontx.gov/generalservices/realbids.html"
DB_PATH = "houston_properties.db"
PLACEHOLDER_TEXT = "none at this time"

HEADERS = {
    "User-Agent": "GovLandScout-SchoolProject/1.0 (contact: your-email@example.com)"
}


def fetch_page_html() -> str:
    resp = requests.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_listings(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")

    heading = soup.find("p", class_="contBold", string=lambda s: s and "Active Offerings" in s)
    if not heading:
        return []

    listing_ul = heading.find_next("ul", class_="bullets")
    if not listing_ul:
        return []

    listings = []
    for li in listing_ul.find_all("li"):
        text = li.get_text(strip=True)
        if not text or text.lower() == PLACEHOLDER_TEXT:
            continue

        link = li.find("a")
        href = link.get("href") if link else None
        if href and href.startswith("/"):
            href = "https://www.houstontx.gov" + href

        listings.append({
            "text": text,
            "source_url": href,
            "raw_hash": hashlib.sha256(text.encode()).hexdigest(),
        })

    return listings


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS houston_properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT,
            source_url TEXT,
            raw_hash TEXT UNIQUE,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    conn.commit()


def upsert_listing(conn: sqlite3.Connection, listing: dict):
    """
    Keyed on a hash of the entry's text -- there's no stable per-listing id
    available in an unstructured bullet list like there is everywhere else
    in this project.
    """
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT id FROM houston_properties WHERE raw_hash = ?",
        (listing["raw_hash"],),
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE houston_properties SET last_seen = ? WHERE raw_hash = ?",
            (now, listing["raw_hash"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO houston_properties (text, source_url, raw_hash, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?)
            """,
            (listing["text"], listing["source_url"], listing["raw_hash"], now, now),
        )
    conn.commit()


def main():
    print(f"Fetching {URL} ...")
    html = fetch_page_html()
    listings = parse_listings(html)
    print(f"Found {len(listings)} active offering(s).")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    combined_conn = combined_db.get_connection()

    for listing in listings:
        upsert_listing(conn, listing)

        combined_db.upsert_listing(
            combined_conn,
            county="Municipal",
            account_number=listing["raw_hash"][:16],
            precinct="Houston",
            minimum_bid=None,
            estimated_value=None,
            address=None,
            description=listing["text"],
            status="Available",
            source="houstontx.gov",
            source_url=listing["source_url"],
        )

    combined_conn.close()

    print(f"Stored {len(listings)} listings into {DB_PATH}")
    conn.close()


if __name__ == "__main__":
    main()
