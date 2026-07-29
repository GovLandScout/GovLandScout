"""
GovLandScout - Guadalupe County Tax Sale Scraper

Guadalupe County (Seguin, TX -- San Antonio metro, ~201K people) isn't a
client of any trustee firm this project already scrapes (LGBS/PBFCM/MVBA).
It publishes its own tax sale bid list directly at
guadalupetx.gov/page/tax.sale, linked each month as "<Month> Tax Sale" --
pointing at a PDF whose *filename* changes every month (e.g.
March_Tax_Sale.pdf, then July_Tax_Sale.pdf), not just its content. So
there's no fixed URL to fetch; this discovers whichever one is currently
linked from that page, the same way mvba_scraper.py discovers its own
current PDFs rather than hardcoding them.

Table format is nearly identical to MVBA's (same combined "PROPERTY
DESCRIPTION, APPROXIMATE ADDRESS, ACCT #" cell, same "Account #" marker,
same legal-description-ends-in-"Texas)," convention right before the
address starts) plus two columns MVBA's doesn't have: a leading TRACT
number (folded into the description, not a real precinct) and a trailing
AMOUNT OF BID column (the actual winning bid, only filled in after the
sale happens -- always blank here, since this reads the upcoming sale).

One thing MVBA's format doesn't have: some rows carry "Withdrawn" injected
mid-sentence into the description cell -- a layout artifact from a status
stamp overlaid on the original page that pdfplumber's text extraction
pulls out of visual position and merges into the flowing paragraph. Pulled
out into its own status field rather than left in place, both because
leaving it in would put a stray word in the middle of a legal description
or address, and because it's genuinely useful (that property is no longer
actually being auctioned).

Webb County (Laredo, ~281K people) does the same self-published-PDF thing
and was considered too, but skipped: its PDFs are scanned images with no
text layer at all, which would need OCR to get anything out of -- a
different and much less reliable approach than anything else in this
project, not worth it for one county.

Known cosmetic quirk, not fixed: on some rows the "Withdrawn" stamp
overlaps the underlying paragraph text closely enough that pdfplumber's
extraction interleaves their characters instead of producing two clean
runs (e.g. "Guadalupe County, Texas" comes out "WGuadaliupte hCoudnty,
rTeaxasw"), which the simple `"Withdrawn" in text` check below can't
reliably catch or clean up. Only garbles the free-text `description`
field shown for a listing -- account number, minimum bid, and address all
parse correctly regardless, and `status` isn't rendered anywhere on the
site today, so this doesn't affect anything actionable.
"""

import re
import sqlite3
from datetime import datetime, timezone
from io import BytesIO

import pdfplumber
import requests
from bs4 import BeautifulSoup

import combined_db

BASE_URL = "https://www.guadalupetx.gov"
LISTING_PAGE_URL = f"{BASE_URL}/page/tax.sale"
DB_PATH = "guadalupe_properties.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

MONTH_LINK_PATTERN = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|October|November|December) Tax Sale$"
)
ACCOUNT_PATTERN = re.compile(r"Account\s*#\s*([A-Za-z0-9]+)", re.IGNORECASE)
CITATION_END_PATTERN = re.compile(r"\bTexas\)\s*,")


def find_current_pdf_url() -> str | None:
    resp = requests.get(LISTING_PAGE_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    for a in soup.find_all("a", href=True):
        if MONTH_LINK_PATTERN.match(a.get_text(strip=True)) and a["href"].lower().endswith(".pdf"):
            href = a["href"]
            return href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"
    return None


def parse_description_cell(text: str) -> tuple[str, str | None, str | None, str]:
    """Same combined-cell shape as MVBA's, plus "Withdrawn" pulled out into its own status -- see module docstring."""
    joined = " ".join(l.strip() for l in text.split("\n") if l.strip())

    status = "Withdrawn" if "Withdrawn" in joined else "Active"
    joined = re.sub(r"\s+", " ", joined.replace("Withdrawn", " ")).strip()

    account_match = ACCOUNT_PATTERN.search(joined)
    account = account_match.group(1) if account_match else None

    citation_matches = list(CITATION_END_PATTERN.finditer(joined))
    if citation_matches and account_match:
        citation_end = citation_matches[-1].end()
        address = joined[citation_end:account_match.start()].strip().rstrip(",") or None
        legal_description = joined[: citation_matches[-1].start() + len("Texas)")].strip()
    else:
        address = None
        legal_description = joined

    return legal_description, address, account, status


def parse_money(text: str | None) -> str | None:
    """
    Validates the result actually looks like a number -- the MIN BID cell
    sometimes carries a trailing note on its own line (e.g. "*Includes
    City of Seguin liens" under a tax sale that also covers city liens),
    which isn't part of the number itself.
    """
    if not text:
        return None
    cleaned = text.split("\n")[0].replace("$", "").replace(",", "").strip()
    if not cleaned or not re.match(r"^\d+(\.\d+)?$", cleaned):
        return None
    return cleaned


def parse_pdf(content: bytes) -> list[dict]:
    listings = []
    with pdfplumber.open(BytesIO(content)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                if not table or len(table[0]) < 5:
                    continue
                header_lower = [(c or "").lower().replace("\n", " ") for c in table[0]]
                if not ("suit" in header_lower[1] and "min bid" in header_lower[4]):
                    continue  # not the properties table (e.g. a stray box)

                for row in table[1:]:
                    if len(row) < 5 or not row[1] or not re.search(r"\d", row[1]):
                        continue
                    tract = (row[0] or "").strip()
                    suit_no = " ".join(l.strip() for l in row[1].split("\n") if l.strip())
                    style = " ".join(l.strip() for l in (row[2] or "").split("\n") if l.strip())
                    legal, address, account, status = parse_description_cell(row[3] or "")
                    min_bid = parse_money(row[4])

                    if not account or not suit_no:
                        continue

                    listings.append({
                        "tract": tract, "suit_no": suit_no, "style": style,
                        "legal_description": legal, "address": address,
                        "account_number": account, "minimum_bid": min_bid,
                        "status": status,
                    })
    return listings


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS guadalupe_properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tract TEXT,
            suit_no TEXT,
            style TEXT,
            legal_description TEXT,
            address TEXT,
            account_number TEXT,
            minimum_bid TEXT,
            status TEXT,
            source_url TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_guadalupe_account_suit
        ON guadalupe_properties(account_number, suit_no)
    """)
    conn.commit()


def upsert_local(conn: sqlite3.Connection, listing: dict, source_url: str):
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT id FROM guadalupe_properties WHERE account_number = ? AND suit_no = ?",
        (listing["account_number"], listing["suit_no"]),
    ).fetchone()

    fields = (
        listing["tract"], listing["style"], listing["legal_description"], listing["address"],
        listing["minimum_bid"], listing["status"], source_url,
    )
    if existing:
        conn.execute(
            """UPDATE guadalupe_properties SET
                tract = ?, style = ?, legal_description = ?, address = ?,
                minimum_bid = ?, status = ?, source_url = ?, last_seen = ?
               WHERE account_number = ? AND suit_no = ?""",
            fields + (now, listing["account_number"], listing["suit_no"]),
        )
    else:
        conn.execute(
            """INSERT INTO guadalupe_properties (
                tract, style, legal_description, address, minimum_bid, status, source_url,
                account_number, suit_no, first_seen, last_seen
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            fields + (listing["account_number"], listing["suit_no"], now, now),
        )
    conn.commit()


def main():
    print(f"Finding this month's tax sale PDF on {LISTING_PAGE_URL} ...")
    pdf_url = find_current_pdf_url()
    if not pdf_url:
        print("  No current tax sale PDF linked from the page -- nothing scheduled this month.")
        return

    print(f"Fetching {pdf_url} ...")
    resp = requests.get(pdf_url, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    listings = parse_pdf(resp.content)
    print(f"Found {len(listings)} listing(s).")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    combined_conn = combined_db.get_connection()

    for listing in listings:
        upsert_local(conn, listing, pdf_url)

        description_parts = [
            f"Tract {listing['tract']}" if listing["tract"] else None,
            listing["style"],
            listing["legal_description"],
        ]
        combined_db.upsert_listing(
            combined_conn,
            county="Guadalupe",
            account_number=f"{listing['account_number']}_{listing['suit_no']}",
            precinct=None,
            minimum_bid=listing["minimum_bid"],
            estimated_value=None,  # Guadalupe doesn't publish an independent value estimate
            address=listing["address"],
            description=" -- ".join(p for p in description_parts if p) or None,
            status=listing["status"],
            source="guadalupetx.gov",
            source_url=pdf_url,
        )

    combined_conn.close()
    conn.close()

    print(f"Stored {len(listings)} listings into {DB_PATH}")


if __name__ == "__main__":
    main()
