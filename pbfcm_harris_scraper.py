"""
GovLandScout - PBFCM Harris County Tax Sale Scraper

Perdue, Brandon, Fielder, Collins & Mott (PBFCM) publishes tax sale
listings as per-county PDF documents at pbfcm.com/taxsale.html, rather
than an HTML table or API. Different counties use different table
layouts (confirmed by sampling Dallas vs. Harris before building this --
they don't match), so this scraper is scoped to Harris County only for
now rather than guessing at every county's format.

PBFCM represents Harris County listings for OTHER taxing entities than
hctax_scraper.py's source (Pasadena ISD, City of South Houston, various
MUDs, etc. -- not just the county/city judgments hctax.net shows), so
this is a genuinely complementary source, not a duplicate. Because the
same property can be the subject of two different lawsuits from two
different taxing entities at once, listings here are keyed on
(cad_account, cause_no) together rather than cad_account alone -- unlike
a duplicate, two different judgments against the same property are two
real, distinct opportunities.

pbfcm.com's homepage was found to have spam links injected into the live
DOM via client-side JavaScript (not present in the raw HTML). This
scraper uses `requests` only -- it never executes the page's JS, so it
never triggers that injection.
"""

import re
import sqlite3
from datetime import datetime, timezone
from io import BytesIO

import pdfplumber
import requests
from bs4 import BeautifulSoup

import combined_db

BASE_URL = "https://www.pbfcm.com"
LISTING_PAGE_URL = f"{BASE_URL}/taxsale.html"
DB_PATH = "pbfcm_harris_properties.db"

HEADERS = {
    "User-Agent": "GovLandScout-SchoolProject/1.0 (contact: your-email@example.com)"
}

ADDRESS_PATTERN = re.compile(r"^\d[\w\s.,#\-]*,\s*[A-Z][A-Za-z ]*,\s*TX\s*\d{5}(?:-\d{4})?$")
DATE_PATTERN = re.compile(r"^\d{1,2}-[A-Za-z]{3}-\d{2}$")


def find_harris_pdf_links() -> list[str]:
    resp = requests.get(LISTING_PAGE_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "harris" in href.lower() and href.lower().endswith(".pdf"):
            links.append(href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}")
    return sorted(set(links))


def extract_precinct(first_page_text: str) -> str | None:
    match = re.search(r"HARRIS COUNTY (PCT\.?\s*\d+)", first_page_text, re.IGNORECASE)
    return match.group(1).replace(".", "").upper() if match else None


def parse_cause_cell(text: str) -> tuple[str, str | None, str | None]:
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    cause_no = lines[0]
    date_idx = next((i for i, l in enumerate(lines) if DATE_PATTERN.match(l)), None)
    judgment_date = lines[date_idx] if date_idx is not None else None
    district_court = " ".join(lines[1:date_idx]) if date_idx else " ".join(lines[1:])
    return cause_no, district_court or None, judgment_date


def parse_legal_address_cell(text: str) -> tuple[str, str | None]:
    """
    The address is always the last N lines of the cell, but N varies (the
    PDF just wraps naturally at the table's column width) -- grow the
    candidate from the end until it matches "number, ..., city, TX zip".
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for split_idx in range(len(lines) - 1, -1, -1):
        candidate = " ".join(lines[split_idx:])
        if ADDRESS_PATTERN.match(candidate):
            return " ".join(lines[:split_idx]) or None, candidate
    return " ".join(lines), None


def parse_money(text: str) -> str | None:
    cleaned = text.replace("$", "").replace(",", "").strip()
    return cleaned if cleaned else None


def parse_pdf(content: bytes, source_url: str) -> list[dict]:
    listings = []
    with pdfplumber.open(BytesIO(content)) as pdf:
        precinct = extract_precinct(pdf.pages[0].extract_text() or "")

        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    if len(row) < 7 or not row[0] or not row[0].strip().rstrip(".").isdigit():
                        continue  # header row or malformed row

                    cause_no, district_court, judgment_date = parse_cause_cell(row[1])
                    style_of_case = " ".join(l.strip() for l in row[2].split("\n") if l.strip())
                    legal_description, address = parse_legal_address_cell(row[3])
                    adjudged_value = parse_money(row[4])
                    estimated_minimum = parse_money(row[5])
                    cad_lines = [l.strip() for l in row[6].split("\n") if l.strip()]
                    cad_account = cad_lines[0] if cad_lines else None

                    if not cad_account or not cause_no:
                        continue  # can't identify/dedupe this row reliably

                    listings.append({
                        "cause_no": cause_no,
                        "district_court": district_court,
                        "judgment_date": judgment_date,
                        "style_of_case": style_of_case,
                        "legal_description": legal_description,
                        "address": address,
                        "adjudged_value": adjudged_value,
                        "estimated_minimum": estimated_minimum,
                        "cad_account": cad_account,
                        "precinct": precinct,
                        "source_url": source_url,
                    })

    return listings


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pbfcm_harris_properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cause_no TEXT,
            district_court TEXT,
            judgment_date TEXT,
            style_of_case TEXT,
            legal_description TEXT,
            address TEXT,
            adjudged_value TEXT,
            estimated_minimum TEXT,
            cad_account TEXT,
            precinct TEXT,
            source_url TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_pbfcm_harris_cause_account
        ON pbfcm_harris_properties(cad_account, cause_no)
    """)
    conn.commit()


def upsert_listing(conn: sqlite3.Connection, listing: dict):
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT id FROM pbfcm_harris_properties WHERE cad_account = ? AND cause_no = ?",
        (listing["cad_account"], listing["cause_no"]),
    ).fetchone()

    fields = (
        listing["district_court"], listing["judgment_date"], listing["style_of_case"],
        listing["legal_description"], listing["address"], listing["adjudged_value"],
        listing["estimated_minimum"], listing["precinct"], listing["source_url"],
    )

    if existing:
        conn.execute(
            """
            UPDATE pbfcm_harris_properties SET
                district_court = ?, judgment_date = ?, style_of_case = ?,
                legal_description = ?, address = ?, adjudged_value = ?,
                estimated_minimum = ?, precinct = ?, source_url = ?, last_seen = ?
            WHERE cad_account = ? AND cause_no = ?
            """,
            fields + (now, listing["cad_account"], listing["cause_no"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO pbfcm_harris_properties (
                district_court, judgment_date, style_of_case, legal_description,
                address, adjudged_value, estimated_minimum, precinct, source_url,
                cad_account, cause_no, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            fields + (listing["cad_account"], listing["cause_no"], now, now),
        )
    conn.commit()


def main():
    print(f"Finding Harris County PDF links on {LISTING_PAGE_URL} ...")
    pdf_links = find_harris_pdf_links()
    print(f"Found {len(pdf_links)} Harris County PDF(s).")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    combined_conn = combined_db.get_connection()

    total_listings = 0
    for url in pdf_links:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 404:
                print(f"  {url} -- 404, skipping (link on the page is stale)")
                continue
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  {url} -- failed to fetch ({e}), skipping")
            continue

        try:
            listings = parse_pdf(resp.content, url)
        except Exception as e:
            print(f"  {url} -- failed to parse ({e}), skipping")
            continue

        print(f"  {url} -- {len(listings)} listing(s)")
        total_listings += len(listings)

        for listing in listings:
            upsert_listing(conn, listing)

            combined_db.upsert_listing(
                combined_conn,
                county="Harris",
                account_number=f"{listing['cad_account']}_{listing['cause_no']}",
                precinct=listing["precinct"],
                minimum_bid=listing["estimated_minimum"],
                estimated_value=listing["adjudged_value"],
                address=listing["address"],
                description=f"{listing['style_of_case']} -- {listing['legal_description']}",
                status="Active",
                source="pbfcm.com",
                source_url=listing["source_url"],
            )

    combined_conn.close()
    print(f"\nStored {total_listings} listings into {DB_PATH}")
    conn.close()


if __name__ == "__main__":
    main()
