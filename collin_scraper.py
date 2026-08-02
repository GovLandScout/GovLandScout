"""
GovLandScout - Collin County Constable Sale Scraper

Collin County isn't a client of any trustee firm this project already
scrapes (LGBS/PBFCM/MVBA), isn't on RealAuction, and its own GovEase
listing ("TX - McLennan - MVBA" is the only Collin-area entry there, and
that's McLennan, not Collin) doesn't cover it either -- see
govease_scraper.py's docstring. Its sales are posted directly at
collincountytx.gov/courts/constables/constable-sales as an accordion, one
section per constable precinct, each row a plain HTML card (not a table
DataGrid, not a PDF-per-county listing like MVBA/PBFCM/Guadalupe) with:
Defendant, Legal Description, Sale Date, and a link to that property's own
"Notice of Constable Sale" PDF.

That page's "Current Sales" section (upcoming/active only) reads empty
far more often than not -- these are civil judgment-execution sales
(Tax Code Chapter 34, same legal process RealAuction's counties use, see
that scraper's docstring), not a rolling delinquent-tax resale list, so
they only get posted a few weeks out and there frequently isn't one
scheduled. The accordion below it is a full *archive* back to 2015, mixed
past/future/cancelled -- so this filters that archive down to what
"Current Sales" would show if populated: Sale Date >= today, and not
cancelled (the site marks these inconsistently -- sometimes in the
defendant name as "***CANCELLED***", sometimes in the legal description
text as "This Sale was cancelled").

Unlike every other source here, this page has no minimum bid or account
number of its own -- just a legal description (which usually, not always,
embeds a GEO/parcel ID: "TRACT 1: GEO: R276200002001 ...") and a link to
a per-property PDF. That PDF is a plain-English notice, not a table (see
a sample: "...against the said Anselmo J. Ordonez for the sum of
$3,380.36 principal representing delinquent taxes ... court costs in the
amount of $1,233.00..."), so minimum_bid here is computed as principal +
costs (the practical floor a bidder needs to clear), same concept as
MVBA/PBFCM's own MIN BID column even though this site doesn't label it
that way. Best-effort: a listing missing this (unparseable PDF, wording
that doesn't match) is still worth keeping, same reasoning as
web.py's geocode_address.
"""

import re
import sqlite3
import time
from datetime import date, datetime, timezone
from io import BytesIO
from urllib.parse import urljoin

import pdfplumber
import requests
from bs4 import BeautifulSoup

import combined_db

BASE_URL = "https://www.collincountytx.gov"
LISTING_PAGE_URL = f"{BASE_URL}/courts/constables/constable-sales"
DB_PATH = "collin_properties.db"
REQUEST_DELAY_SECONDS = 2  # no robots.txt restriction found; still pace requests as a courtesy

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

# Collin CAD geo/parcel IDs: "R" + digit + more alnum, e.g. "R276200002001"
# or (for common-area/condo tracts) "R224000B006R1". Usually but not always
# preceded by a "GEO:" label, so that label isn't part of the match itself.
GEO_PATTERN = re.compile(r"\bR\d[\dA-Z]{5,}\b")
JUDGMENT_PATTERN = re.compile(
    r"for the sum of \$([\d,]+\.\d{2})\s+principal.*?"
    r"court costs in the amount of \$([\d,]+\.\d{2})",
    re.IGNORECASE | re.DOTALL,
)
CASE_PATTERN = re.compile(r"[Cc]ase:\s*([\w-]+)")
# Both straight and curly/smart quotes show up in these notices.
QUOTE_CHARS = "'‘’\"“”"
COMMONLY_KNOWN_PATTERN = re.compile(rf"commonly known as [{QUOTE_CHARS}]?([^.{QUOTE_CHARS}]+)", re.IGNORECASE)
LOCATED_AT_PATTERN = re.compile(rf"located at [{QUOTE_CHARS}]([^{QUOTE_CHARS}]+)[{QUOTE_CHARS}]", re.IGNORECASE)


def is_cancelled(defendant: str, legal_description: str) -> bool:
    combined = f"{defendant} {legal_description}".lower()
    return "cancel" in combined


def extract_address(legal_description: str) -> str | None:
    for pattern in (COMMONLY_KNOWN_PATTERN, LOCATED_AT_PATTERN):
        match = pattern.search(legal_description)
        if match:
            return match.group(1).strip().rstrip(",")
    return None


def parse_listing_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    listings = []

    for section in soup.select(".advListTableWrap.accordion-section"):
        header = section.select_one(".accordion-header")
        precinct = header.get_text(strip=True) if header else None
        if not precinct:
            continue  # not a real precinct section (e.g. an unrelated accordion widget elsewhere on the page)

        for row in section.select(".advListDataRow"):
            cells = {
                cell.get("data-col"): cell.get_text(" ", strip=True)
                for cell in row.select(".advListDataCell")
            }
            defendant = cells.get("Title", "")
            legal_description = cells.get("Description", "")
            sale_date_text = cells.get("Date", "")
            link = row.select_one('.advListDataCell[data-col="Document"] a')

            if not defendant or not sale_date_text:
                continue
            try:
                sale_date = datetime.strptime(sale_date_text, "%m/%d/%Y").date()
            except ValueError:
                continue
            if sale_date < date.today():
                continue
            if is_cancelled(defendant, legal_description):
                continue

            geo_match = GEO_PATTERN.findall(legal_description)
            account_number = "_".join(geo_match) if geo_match else f"{defendant}-{sale_date_text}"

            listings.append({
                "precinct": precinct,
                "account_number": account_number,
                "sale_date": sale_date_text,
                "legal_description": legal_description,
                "address": extract_address(legal_description),
                "pdf_url": urljoin(BASE_URL, link["href"]) if link and link.get("href") else None,
            })

    return listings


def fetch_pdf_details(session: requests.Session, pdf_url: str) -> dict:
    """Best-effort: a listing is still worth keeping if the PDF is unreachable or its wording doesn't match."""
    try:
        resp = session.get(pdf_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        with pdfplumber.open(BytesIO(resp.content)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except (requests.RequestException, ValueError, OSError):
        return {}

    result = {}
    judgment_match = JUDGMENT_PATTERN.search(text)
    if judgment_match:
        principal = float(judgment_match.group(1).replace(",", ""))
        costs = float(judgment_match.group(2).replace(",", ""))
        result["minimum_bid"] = f"{principal + costs:.2f}"
    case_match = CASE_PATTERN.search(text)
    if case_match:
        result["cause_number"] = case_match.group(1)
    return result


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collin_properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            precinct TEXT,
            account_number TEXT,
            sale_date TEXT,
            minimum_bid TEXT,
            address TEXT,
            description TEXT,
            source_url TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_collin_account
        ON collin_properties(account_number)
    """)
    conn.commit()


def upsert_local(conn: sqlite3.Connection, listing: dict):
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT id FROM collin_properties WHERE account_number = ?",
        (listing["account_number"],),
    ).fetchone()

    fields = (
        listing["precinct"], listing["sale_date"], listing["minimum_bid"],
        listing["address"], listing["description"], listing["source_url"],
    )
    if existing:
        conn.execute(
            """UPDATE collin_properties SET
                precinct = ?, sale_date = ?, minimum_bid = ?, address = ?,
                description = ?, source_url = ?, last_seen = ?
               WHERE account_number = ?""",
            fields + (now, listing["account_number"]),
        )
    else:
        conn.execute(
            """INSERT INTO collin_properties (
                precinct, sale_date, minimum_bid, address, description, source_url,
                account_number, first_seen, last_seen
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            fields + (listing["account_number"], now, now),
        )
    conn.commit()


def main():
    session = requests.Session()
    resp = session.get(LISTING_PAGE_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    listings = parse_listing_page(resp.text)
    print(f"Found {len(listings)} upcoming, non-cancelled sale(s).")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    combined_conn = combined_db.get_connection()

    for i, listing in enumerate(listings):
        if i > 0:
            time.sleep(REQUEST_DELAY_SECONDS)

        details = fetch_pdf_details(session, listing["pdf_url"]) if listing["pdf_url"] else {}
        cause_number = details.get("cause_number")
        account_number = f"{listing['account_number']}_{cause_number}" if cause_number else listing["account_number"]

        description_parts = [listing["legal_description"], f"Sale scheduled {listing['sale_date']}"]
        listing_out = {
            "precinct": listing["precinct"],
            "account_number": account_number,
            "sale_date": listing["sale_date"],
            "minimum_bid": details.get("minimum_bid"),
            "address": listing["address"],
            "description": " -- ".join(p for p in description_parts if p),
            "source_url": listing["pdf_url"],
        }

        upsert_local(conn, listing_out)
        combined_db.upsert_listing(
            combined_conn,
            county="Collin",
            account_number=account_number,
            precinct=listing["precinct"],
            minimum_bid=listing_out["minimum_bid"],
            estimated_value=None,  # no independent value estimate -- see module docstring
            address=listing_out["address"],
            description=listing_out["description"],
            status="Active",
            source="collincountytx.gov",
            source_url=listing["pdf_url"],
        )

    combined_conn.close()
    conn.close()
    print(f"\n{len(listings)} listing(s) stored into {DB_PATH}")


if __name__ == "__main__":
    main()
