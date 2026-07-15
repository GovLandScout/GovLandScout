"""
GovLandScout - Harris County Tax Sale Scraper

Scrapes the Harris County Tax Office's delinquent tax sale listing page
and parses it into structured records.

Approach: the page's HTML structure may be complex/JS-rendered, but every
listing on the page repeats the SAME set of labeled fields in plain text
(e.g. "Precinct: Precinct 1", "Minimum Bid: $41,205.46"). Rather than
relying on fragile CSS selectors, this scraper extracts the visible text
and pulls fields out using labeled regex patterns. This is more robust
to the page's markup changing, since the labels themselves rarely change.

NOTE: Once you have this running, open the page in Chrome DevTools and
compare against this approach -- if you find clean, stable HTML tags/classes
per listing, a BeautifulSoup selector-based approach can be added as a
second, more precise extraction path. For now, this gets you real data.
"""

import re
import sqlite3
import hashlib
from datetime import datetime, date, timezone
import requests

URL = "https://www.hctax.net/Property/listings/taxsalelisting"
DB_PATH = "tax_sales.db"

HEADERS = {
    "User-Agent": "GovLandScout-SchoolProject/1.0 (contact: your-email@example.com)"
}


def fetch_page_text() -> str:
    """Fetch the tax sale listing page and return its visible text."""
    resp = requests.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    # Strip HTML tags crudely to get visible text. If you later confirm
    # clean per-listing HTML tags in DevTools, swap this for BeautifulSoup
    # tag-based extraction -- it'll be more precise than text stripping.
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)


def split_into_listing_blocks(page_text: str) -> list[str]:
    """
    Each listing block ends with 'Close' after its description, and the
    next one begins after 'View Details'. Splitting on this boundary
    gives us one chunk of text per listing.
    """
    # Drop everything before the first real listing (nav menu, terms of use, etc.)
    start_marker = "Precinct 1 Precinct 2"
    idx = page_text.find(start_marker)
    if idx != -1:
        page_text = page_text[idx:]

    chunks = re.split(r"Close\s+View Details", page_text)
    return [c.strip() for c in chunks if c.strip()]


def extract_field(pattern: str, text: str, group: int = 1) -> str | None:
    match = re.search(pattern, text)
    return match.group(group).strip() if match else None


def parse_listing(chunk: str) -> dict | None:
    """Pull structured fields out of one listing's raw text chunk."""
    precinct = extract_field(r"Precinct:\s*(Precinct\s*\d+)", chunk)
    if not precinct:
        return None  # not a real listing block (e.g. leftover header text)

    sale_number = extract_field(r"Sale#:\s*(\d+)", chunk)
    listing_type = extract_field(r"Type:\s*([A-Z ]+?)\s*Cause#:", chunk)
    account_number = extract_field(r"Account#:\s*(\d+)", chunk)
    cause_number = extract_field(r"Cause#:\s*([\w\d]+)", chunk)
    judgment_date = extract_field(r"Judgment:\s*([\d/]+)", chunk)
    tax_years = extract_field(r"Tax Years in Judgement:\s*([\d\s\-]+)", chunk)
    minimum_bid = extract_field(r"Minimum Bid:\s*\$?([\d,]+\.\d{2})", chunk)
    adjudged_value = extract_field(r"Adjudged Value:\s*\$?([\d,]+\.\d{2})", chunk)

    description = extract_field(
        r"(?:For Sale Description|Cancelled Description)\s*(.*)", chunk
    )

    status = "cancelled" if "Cancelled" in chunk[:200] else "active"

    raw_hash = hashlib.sha256(chunk.encode()).hexdigest()

    return {
        "precinct": precinct,
        "sale_number": sale_number,
        "listing_type": listing_type,
        "account_number": account_number,
        "cause_number": cause_number,
        "judgment_date": judgment_date,
        "tax_years": tax_years,
        "minimum_bid": minimum_bid.replace(",", "") if minimum_bid else None,
        "adjudged_value": adjudged_value.replace(",", "") if adjudged_value else None,
        "legal_description": description,
        "status": status,
        "raw_hash": raw_hash,
        "raw_text": chunk,
    }


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tax_sale_listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            precinct TEXT,
            sale_number TEXT,
            listing_type TEXT,
            account_number TEXT,
            cause_number TEXT,
            judgment_date TEXT,
            tax_years TEXT,
            minimum_bid TEXT,
            adjudged_value TEXT,
            legal_description TEXT,
            status TEXT,
            raw_hash TEXT,
            raw_text TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_account_number
        ON tax_sale_listings(account_number)
    """)
    conn.commit()


def upsert_listing(conn: sqlite3.Connection, listing: dict):
    """
    Keyed on account_number, the stable per-property identifier -- raw_hash
    is derived from scraped text that shifts slightly between requests, so
    it can't be used to detect "same listing, scraped again".
    """
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT id FROM tax_sale_listings WHERE account_number = ?",
        (listing["account_number"],),
    ).fetchone()

    if existing:
        conn.execute(
            """
            UPDATE tax_sale_listings SET
                precinct = ?, sale_number = ?, listing_type = ?, cause_number = ?,
                judgment_date = ?, tax_years = ?, minimum_bid = ?, adjudged_value = ?,
                legal_description = ?, status = ?, raw_hash = ?, raw_text = ?, last_seen = ?
            WHERE account_number = ?
            """,
            (
                listing["precinct"], listing["sale_number"], listing["listing_type"],
                listing["cause_number"], listing["judgment_date"], listing["tax_years"],
                listing["minimum_bid"], listing["adjudged_value"], listing["legal_description"],
                listing["status"], listing["raw_hash"], listing["raw_text"], now,
                listing["account_number"],
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO tax_sale_listings (
                precinct, sale_number, listing_type, account_number,
                cause_number, judgment_date, tax_years, minimum_bid,
                adjudged_value, legal_description, status, raw_hash,
                raw_text, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing["precinct"], listing["sale_number"], listing["listing_type"],
                listing["account_number"], listing["cause_number"], listing["judgment_date"],
                listing["tax_years"], listing["minimum_bid"], listing["adjudged_value"],
                listing["legal_description"], listing["status"], listing["raw_hash"],
                listing["raw_text"], now, now,
            ),
        )
    conn.commit()


def main():
    print(f"Fetching {URL} ...")
    page_text = fetch_page_text()

    blocks = split_into_listing_blocks(page_text)
    print(f"Found {len(blocks)} candidate listing blocks.")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    parsed_count = 0
    for chunk in blocks:
        listing = parse_listing(chunk)
        if listing:
            upsert_listing(conn, listing)
            parsed_count += 1

    print(f"Parsed and stored {parsed_count} listings into {DB_PATH}")

    # Quick sanity check: print the first 3 parsed listings
    rows = conn.execute(
        "SELECT precinct, account_number, minimum_bid, legal_description "
        "FROM tax_sale_listings LIMIT 3"
    ).fetchall()
    print("\nSample of stored listings:")
    for row in rows:
        print(f"  {row[0]} | Acct#{row[1]} | Min Bid: {row[2]} | {row[3][:80]}...")

    conn.close()


if __name__ == "__main__":
    main()