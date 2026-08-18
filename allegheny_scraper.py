"""
GovLandScout - Allegheny County (PA) Sheriff Sale Scraper (bank foreclosures)

Allegheny County publishes its monthly Sheriff Sale property list as one
PDF, discovered the same way montco_scraper.py finds Montgomery County's
Upset Sale PDF: follow a real link on the county's own page rather than
guessing at a filename, since the filename changes every month
("September-Sale-List.pdf" today, something else next month) and the
folder segment is dated to whichever month the file was uploaded, not the
sale month.

Unlike every other PA source this project scrapes (montco_scraper.py,
chester_scraper.py, both Tax Claim Bureau Upset Sales), this is the
Sheriff's own execution sale -- and, confirmed directly against a real
downloaded copy of this exact PDF, each property is explicitly tagged with
a Sale Type: "Real Estate Sale - Mortgage Foreclosure", "- Sci Fa Sur Tax
Lien", or "- Municipal Lien" all appear in the same feed. That explicit
tag is what model/README.md's "Considered but not built: private mortgage
foreclosure statistics" section flagged as the realistic free path to bank
foreclosure data -- this scraper is that path, and it's the only source in
this project that can label a listing "Mortgage Foreclosure" with the
source's own certainty rather than an inference (compare
realauction_scraper.py's PA_SHERIFF_COUNTIES, which cover the same kind of
sale on a different platform that doesn't publish a sale-type field at
all).

## A real reliability caveat, not glossed over

sheriffalleghenycounty.com sits behind an active WAF -- confirmed directly
during development: a handful of plain GET requests in quick succession
(different pages, spread over roughly a minute, completely unlike this
scraper's own one-request-per-day shape) got a 403 "Forbidden" nginx page
back, including for a PDF URL that had returned real content moments
earlier. That's a materially different risk profile than every other
county site this project scrapes (montco_scraper.py's own docstring
explicitly calls out its target as having no bot defense at all). A single
polite daily request is a very different traffic pattern than what
triggered that block and may well clear every time in production -- but
it isn't proven to, so this treats a fetch failure (403 or otherwise) as
"try again next run", the same as a sale that simply hasn't posted yet,
rather than raising and failing run_daily_scrapers.py's whole batch over
it.

## PDF layout (no ruled lines -- pdfplumber's own table detection finds
## zero tables on every page, confirmed directly)

Each property is two parts:
1. A value line matching VALUE_LINE_PATTERN: sale number, case number,
   "Real Estate Sale - <type>", status, tract count, and a dollar amount
   labeled "Cost & Tax" on the sale list itself -- the debt being executed
   on, not an independent value estimate, so (same reasoning as
   montco_scraper.py's "Approx. Sale Price") stored as minimum_bid with
   estimated_value left unset. Text-based parsing is reliable for this one
   line specifically -- confirmed directly, it never wraps across
   multiple lines in the real document.
2. A five-column Plaintiff(s)/Attorney/Defendant(s)/Property/Municipality/
   Parcel-Tax-ID block, immediately below a repeated mini-header row of
   those same five labels. This part isn't reliably text-parseable: a long
   Plaintiff or Defendant name can wrap onto its own second physical line,
   and pdfplumber's plain extract_text() has no way to tell that
   continuation apart from the start of the next field over (confirmed
   directly: a wrapped "Municipality of Penn Hills (COMMERCIAL)" plaintiff
   continuation, page 5 of the real September 2026 list). Only Property/
   Municipality/Parcel are actually needed here (Plaintiff/Attorney/
   Defendant aren't part of this project's schema), so this parses those
   three positionally instead: extract_words() gives each word's x0, the
   record's own mini-header row gives that record's three column
   boundaries directly (confirmed stable across every record checked, but
   read fresh per record rather than hardcoded, in case a future layout
   change shifts them), and every word at or past a boundary is bucketed
   into that column regardless of which physical line it's on -- which is
   what makes the Property column's own address-continuation line (e.g.
   "MCKEESPORT, PA 15133" under "605 SCENE RIDGE ROAD") fold in for free.
   **Known imprecision, left as-is rather than chased further**: this
   PDF's own text placement doesn't strictly clip to column width, so a
   long Plaintiff/Defendant continuation can occasionally drift far enough
   right to land past the Property column's boundary and get folded into
   the address (the Penn Hills case above ends up with an address of
   "1924 UNIVERSAL ROAD (COMMERCIAL) PITTSBURGH, PA 15235" -- the
   "(COMMERCIAL)" is a stray Plaintiff-continuation word, not part of the
   real address). Rare in the real document and not worth the
   layout-reconstruction complexity a full fix would need.
   Collection for a record stops at its own "Comments:" line (a free-text,
   often multi-line block that isn't part of this project's schema) or at
   the next record's value line, whichever comes first.
"""

import re
import sqlite3
from datetime import datetime, timezone
from io import BytesIO

import pdfplumber
import requests

import combined_db

BASE_URL = "https://sheriffalleghenycounty.com"
SALE_LIST_PAGE_URL = f"{BASE_URL}/sheriffs-sales/"
DB_PATH = "allegheny_properties.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Matches hrefs like ".../wp-content/uploads/2026/08/September-Sale-List.pdf"
# -- the folder is dated to the upload month, not the sale month, and the
# filename's own month name changes every run, so neither can be hardcoded.
SALE_LIST_HREF_PATTERN = re.compile(
    r'https://sheriffalleghenycounty\.com/wp-content/uploads/\d{4}/\d{2}/[A-Za-z]+-Sale-List\.pdf'
)

VALUE_LINE_PATTERN = re.compile(
    r'^(?P<sale_num>\S+)\s+(?P<case_number>\S+)\s+(?P<sale_type>Real Estate Sale.*?)\s+'
    r'(?P<status>[A-Za-z]+)\s+(?P<tracts>\d+)\s+\$(?P<amount>[\d,]+\.\d{2})'
)
ROW_TOLERANCE_POINTS = 2.0  # words within this many PDF points of vertical position are treated as one visual line


def find_sale_list_url(html: str) -> str | None:
    """The one link whose href matches the dated upload-folder/monthly-name
    shape above -- confirmed the only PDF on this page matching that
    pattern (the page's other PDFs, e.g. old court orders and the bidder
    packet, live under different, non-matching paths)."""
    match = SALE_LIST_HREF_PATTERN.search(html)
    return match.group(0) if match else None


def group_into_rows(words: list[dict]) -> list[list[dict]]:
    """pdfplumber's extract_words() returns words in no particular row
    order -- cluster by vertical position (within ROW_TOLERANCE_POINTS)
    into visual lines, since that's what both the value-line regex and the
    column-position logic below operate on."""
    rows = []
    current: list[dict] = []
    current_top = None
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if current_top is None or abs(word["top"] - current_top) <= ROW_TOLERANCE_POINTS:
            current.append(word)
            current_top = word["top"] if current_top is None else current_top
        else:
            rows.append(current)
            current = [word]
            current_top = word["top"]
    if current:
        rows.append(current)
    return rows


def row_text(row: list[dict]) -> str:
    return " ".join(w["text"] for w in sorted(row, key=lambda w: w["x0"]))


def find_column_boundaries(row: list[dict]) -> tuple[float, float, float] | None:
    """The Property/Municipality/Parcel-Tax-ID mini-header row that
    precedes each record's data -- returns each column's left x-boundary,
    or None if this row isn't that header."""
    labels = {w["text"].rstrip(":"): w["x0"] for w in row}
    if "Property" in labels and "Municipality" in labels and "Parcel/Tax" in labels:
        return labels["Property"], labels["Municipality"], labels["Parcel/Tax"]
    return None


def parse_page(page, source_url: str) -> list[dict]:
    rows = group_into_rows(page.extract_words())
    listings = []
    i = 0
    while i < len(rows):
        value_match = VALUE_LINE_PATTERN.match(row_text(rows[i]))
        if value_match is None:
            i += 1
            continue

        record = value_match.groupdict()
        j = i + 1
        boundaries = None
        while j < len(rows):
            boundaries = find_column_boundaries(rows[j])
            if boundaries is not None:
                j += 1
                break
            j += 1
        if boundaries is None:
            i += 1  # malformed record (page break mid-record, etc.) -- skip rather than guess
            continue
        property_x0, municipality_x0, parcel_x0 = boundaries

        address_words, municipality_words, parcel_words = [], [], []
        while j < len(rows):
            first_word = min(rows[j], key=lambda w: w["x0"])
            if first_word["text"] == "Comments:" or VALUE_LINE_PATTERN.match(row_text(rows[j])):
                break
            for word in sorted(rows[j], key=lambda w: w["x0"]):
                if word["x0"] >= parcel_x0:
                    parcel_words.append(word["text"])
                elif word["x0"] >= municipality_x0:
                    municipality_words.append(word["text"])
                elif word["x0"] >= property_x0:
                    address_words.append(word["text"])
            j += 1

        parcel_id = " ".join(parcel_words).strip()
        if not parcel_id:
            i = j
            continue  # not a real property row (or column layout didn't match this one)

        municipality = " ".join(municipality_words).strip()
        # The Property column's own continuation line already supplies a
        # full "CITY, PA ZIP" (confirmed directly: true for all 382 real
        # listings checked during development) -- that's the USPS mailing
        # city, which can legitimately differ from the legal Municipality
        # column (e.g. "Port Vue" the municipality vs. "MCKEESPORT" the
        # mailing city for the same parcel), so appending Municipality here
        # too would just produce a confusing two-city address rather than
        # a more complete one. Municipality is kept as its own field
        # (passed through as precinct) instead of folded in here.
        address = " ".join(address_words).strip() or None

        sale_type = record["sale_type"].removeprefix("Real Estate Sale - ").strip()
        case_number = record["case_number"].strip()
        description = f"Sheriff Sale - {sale_type} -- Case {case_number}"

        listings.append({
            "county": "Allegheny",
            # Composite key: the same parcel can recur across sale dates
            # (postponed or re-listed sales), same reasoning as
            # realauction_scraper.py's PA/TX composite keys.
            "account_number": f"{parcel_id}_{case_number}",
            "minimum_bid": record["amount"].replace(",", ""),
            "municipality": municipality or None,
            "address": address,
            "description": description,
            "source_url": source_url,
        })
        i = j
    return listings


def parse_pdf(content: bytes, source_url: str) -> list[dict]:
    listings = []
    with pdfplumber.open(BytesIO(content)) as pdf:
        for page in pdf.pages:
            listings.extend(parse_page(page, source_url))
    return listings


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS allegheny_properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            municipality TEXT,
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
        CREATE UNIQUE INDEX IF NOT EXISTS idx_allegheny_account
        ON allegheny_properties(account_number)
    """)
    conn.commit()


def upsert_local(conn: sqlite3.Connection, listing: dict):
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT id FROM allegheny_properties WHERE account_number = ?",
        (listing["account_number"],),
    ).fetchone()

    fields = (
        listing["municipality"], listing["minimum_bid"], listing["address"],
        listing["description"], listing["source_url"],
    )
    if existing:
        conn.execute(
            """UPDATE allegheny_properties SET
                municipality = ?, minimum_bid = ?, address = ?, description = ?,
                source_url = ?, last_seen = ?
               WHERE account_number = ?""",
            fields + (now, listing["account_number"]),
        )
    else:
        conn.execute(
            """INSERT INTO allegheny_properties (
                municipality, minimum_bid, address, description, source_url,
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
    try:
        page_resp = session.get(SALE_LIST_PAGE_URL, headers=HEADERS, timeout=30)
        page_resp.raise_for_status()
    except requests.RequestException as e:
        # Includes the WAF's own 403 -- see module docstring. Not a bug in
        # this scraper to fix, just a source that isn't guaranteed to
        # answer every single day; try again next run.
        print(f"Couldn't reach {SALE_LIST_PAGE_URL}: {e}")
        conn.close()
        return

    sale_list_url = find_sale_list_url(page_resp.text)
    if sale_list_url is None:
        print("Couldn't find this month's Sale Listings PDF link -- page structure may have changed.")
        conn.close()
        return

    print(f"Fetching {sale_list_url} ...")
    try:
        pdf_resp = session.get(sale_list_url, headers=HEADERS, timeout=60)
        pdf_resp.raise_for_status()
    except requests.RequestException as e:
        print(f"Couldn't fetch the sale list PDF: {e}")
        conn.close()
        return

    listings = parse_pdf(pdf_resp.content, sale_list_url)
    print(f"Found {len(listings)} listing(s).")

    combined_conn = combined_db.get_connection()
    for listing in listings:
        upsert_local(conn, listing)
        combined_db.upsert_listing(
            combined_conn,
            county=listing["county"],
            account_number=listing["account_number"],
            precinct=listing["municipality"],
            minimum_bid=listing["minimum_bid"],
            estimated_value=None,  # "Cost & Tax" is debt owed, not an appraisal -- see module docstring
            address=listing["address"],
            description=listing["description"],
            status="Active",
            source="sheriffalleghenycounty.com",
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
