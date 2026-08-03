"""
GovLandScout - PBFCM Tax Sale Scraper (multi-format)

Perdue, Brandon, Fielder, Collins & Mott (PBFCM) publishes tax sale
listings as per-county PDF documents at pbfcm.com/taxsale.html, rather
than an API or one consistent HTML/PDF table. Sampling ~15 counties
turned up at least 6 genuinely different table layouts -- not just
different column order, but different fields present at all (some
counties never publish an adjudged value; Smith County embeds the
account number as text inside the legal description instead of giving
it its own column; Waller embeds the adjudged value as text the same
way). This scraper detects which known layout a given PDF uses from its
header row and parses accordingly. A PDF whose layout doesn't match any
known format is skipped and logged rather than guessed at -- more
counties will surface more format variants over time, and a silent
wrong-column parse is worse than a clean skip.

PBFCM's data is genuinely complementary to hctax_scraper.py/lgbs_scraper.py
rather than a duplicate: it represents different taxing entities (school
districts, MUDs, cities) that can have their own simultaneous, separate
judgment against the same property. So listings here are keyed on
(account_number, cause_no) together, not account_number alone -- two
different lawsuits against the same property are two distinct real
opportunities, not duplicates to merge.

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
DB_PATH = "pbfcm_properties.db"

HEADERS = {
    "User-Agent": "GovLandScout-SchoolProject/1.0 (contact: your-email@example.com)"
}

ADDRESS_PATTERN = re.compile(r"^\d[\w\s.,#\-]*,\s*[A-Z][A-Za-z ]*,\s*TX\s*\d{5}(?:-\d{4})?$")
DATE_PATTERN = re.compile(r"^\d{1,2}-[A-Za-z]{3}-\d{2}$")
COUNTY_PATTERN = re.compile(
    # allows "COUNTY SALES FOR" as well as variants with 1-2 extra words in
    # between, e.g. "COUNTY SHERIFF SALE FOR" or "COUNTY TAX SALE FOR"
    r"([A-Z][A-Z ]+?)\s+COUNTY(?:\s+PCT\.?\s*(\d+))?(?:\s+[A-Z]+){0,2}\s+SALES?\s+FOR",
    re.IGNORECASE,
)
# Fallback for documents whose page-1 text doesn't follow COUNTY_PATTERN at
# all -- e.g. a banner page ("SALES FOR AUGUST 4, 2026") with the county
# named only in the filename, or wording COUNTY_PATTERN doesn't anticipate
# ("Hays County Government Center" has no "SALES FOR" nearby). PBFCM's own
# filenames are consistent enough to lean on: "{date}{county}taxsale.pdf" or
# "{county}pct{n}taxsale.pdf".
FILENAME_COUNTY_PATTERN = re.compile(r"([a-z]+?)(?:pct(\d+))?taxsale", re.IGNORECASE)
FILENAME_COUNTY_OVERRIDES = {"ftbend": "Fort Bend"}
# Filenames ending in this aren't a county's own sale at all (e.g.
# "whitneyisdtaxsale.pdf" is Whitney ISD's), so leave those unresolved --
# same as COUNTY_PATTERN finding nothing for them in the page text.
FILENAME_NON_COUNTY_SUFFIXES = ("isd",)
EMBEDDED_ACCOUNT_PATTERN = re.compile(r"ACCOUNT\s*#?\s*(?:NUMBER)?\s*:?\s*(\d{6,})", re.IGNORECASE)
# Tolerates extra wording between "Account No" and the value, e.g. "Tax
# Account No. R53102" (Hays' embedded-account phrasing, alphanumeric, unlike
# EMBEDDED_ACCOUNT_PATTERN's plain-digit "ACCOUNT #" phrasing above).
EMBEDDED_TAX_ACCOUNT_PATTERN = re.compile(r"Tax Account No\.?\s*([A-Za-z0-9]+)", re.IGNORECASE)
# Tolerates extra wording between "Adjudged Value" and the colon, e.g. "Adjudged
# Value (at time of judgment): $74,980.00" (Hays' phrasing) as well as the
# plain "Adjudged Value: $8,900.00" (Waller's) the original pattern covered.
EMBEDDED_VALUE_PATTERN = re.compile(r"Adjudged Value[^:$]*:\s*\$?([\d,]+\.\d{2})", re.IGNORECASE)


def extract_county_from_filename(source_url: str) -> tuple[str | None, str | None]:
    basename = source_url.rsplit("/", 1)[-1].lower()
    match = FILENAME_COUNTY_PATTERN.search(basename)
    if not match:
        return None, None
    slug = match.group(1)
    if slug.endswith(FILENAME_NON_COUNTY_SUFFIXES):
        return None, None
    if slug.endswith("county"):
        slug = slug[: -len("county")]
    county = FILENAME_COUNTY_OVERRIDES.get(slug, slug.title())
    precinct = f"PCT {match.group(2)}" if match.group(2) else None
    return county, precinct


# ---------------------------------------------------------------------------
# Shared low-level cell parsers
# ---------------------------------------------------------------------------

def parse_cause_cell(text: str) -> tuple[str, str | None, str | None]:
    """'202316962\n61ST\nDISTRICT\nCOURT\n2-Dec-24' -> (cause_no, court, date)"""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    cause_no = lines[0]
    date_idx = next((i for i, l in enumerate(lines) if DATE_PATTERN.match(l)), None)
    judgment_date = lines[date_idx] if date_idx is not None else None
    district_court = " ".join(lines[1:date_idx]) if date_idx else " ".join(lines[1:])
    return cause_no, district_court or None, judgment_date


def parse_legal_address_cell(text: str) -> tuple[str, str | None]:
    """
    The address, when present, is the last N lines of the cell -- N varies
    since the PDF just wraps naturally at the column width. Grow the
    candidate from the end until it matches "number, ..., city, TX zip".
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for split_idx in range(len(lines) - 1, -1, -1):
        candidate = " ".join(lines[split_idx:])
        if ADDRESS_PATTERN.match(candidate):
            return " ".join(lines[:split_idx]) or None, candidate
    return " ".join(lines), None


MONEY_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)")


def parse_money(text: str | None) -> str | None:
    """
    Only the *leading* number has to match, not the whole cleaned string --
    Brazoria and Hays both follow the dollar figure with more text on the
    same cell ("$17,684.51\n2025 Taxes\nDue", "$34,588.55\nSubject to\n2026
    taxes"), which a full-string match rejected outright even though the
    number itself parses fine. Still correctly rejects a pure placeholder
    like "TBD" -- there's no leading digit there at all.
    """
    if not text:
        return None
    cleaned = text.replace("$", "").replace(",", "").strip()
    match = MONEY_PATTERN.match(cleaned)
    return match.group(1) if match else None


def join_lines(text: str) -> str:
    return " ".join(l.strip() for l in text.split("\n") if l.strip())


def first_line(text: str) -> str:
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    return lines[0] if lines else ""


# ---------------------------------------------------------------------------
# Per-format row parsers. Each takes a raw table row (list of cell strings)
# and returns a normalized dict, or None if the row isn't real data (e.g. a
# repeated header row on a later page).
# ---------------------------------------------------------------------------

def parse_row_7col(row: list[str]) -> dict | None:
    """Harris / Galveston style: idx | cause+court+date | style | legal+address | adjudged | minimum | account+other"""
    if len(row) < 7 or not row[0] or not row[0].strip().rstrip(".").isdigit():
        return None
    cause_no, court, jdate = parse_cause_cell(row[1])
    legal, addr = parse_legal_address_cell(row[3])
    account_lines = [l.strip() for l in row[6].split("\n") if l.strip()]
    return {
        "cause_no": cause_no, "district_court": court, "judgment_date": jdate,
        "style_of_case": join_lines(row[2]), "legal_description": legal, "address": addr,
        "adjudged_value": parse_money(row[4]), "minimum_bid": parse_money(row[5]),
        "account_number": account_lines[0] if account_lines else None,
    }


def parse_row_6col(row: list[str]) -> dict | None:
    """Cameron / Hidalgo style: cause+court+date | style | legal | adjudged | minimum | account"""
    if len(row) < 6 or not row[0] or not re.search(r"\d", row[0]):
        return None
    cause_no, court, jdate = parse_cause_cell(row[0])
    if not cause_no or not re.search(r"\d", cause_no):
        return None
    legal, addr = parse_legal_address_cell(row[2])
    account_lines = [l.strip() for l in row[5].split("\n") if l.strip()]
    return {
        "cause_no": cause_no, "district_court": court, "judgment_date": jdate,
        "style_of_case": join_lines(row[1]), "legal_description": legal, "address": addr,
        "adjudged_value": parse_money(row[3]), "minimum_bid": parse_money(row[4]),
        "account_number": account_lines[0] if account_lines else None,
    }


def parse_row_5col_taxpayer(row: list[str]) -> dict | None:
    """Dallas / Kaufman / Johnson style: cause_no | legal+address | minimum_bid | account | taxpayer"""
    if len(row) < 5 or not row[0] or not re.search(r"\d", row[0]):
        return None
    cause_no = join_lines(row[0])
    legal, addr = parse_legal_address_cell(row[1])
    account = first_line(row[3])
    if not account or not re.search(r"\d", account):
        return None
    return {
        "cause_no": cause_no, "district_court": None, "judgment_date": None,
        "style_of_case": join_lines(row[4]), "legal_description": legal, "address": addr,
        "adjudged_value": None, "minimum_bid": parse_money(row[2]),
        "account_number": account,
    }


def parse_row_4col_embedded_account(row: list[str]) -> dict | None:
    """Smith style: case_no(+style) | legal description with embedded account# | adjudged | minimum"""
    if len(row) < 4 or not row[0] or not re.search(r"\d", row[0]):
        return None
    lines = [l.strip() for l in row[0].split("\n") if l.strip()]
    case_no = lines[0] if lines else None
    if not case_no:
        return None
    style = " ".join(lines[1:]) if len(lines) > 1 else None
    legal = join_lines(row[1])
    account_match = EMBEDDED_ACCOUNT_PATTERN.search(legal)
    if not account_match:
        return None
    return {
        "cause_no": case_no, "district_court": None, "judgment_date": None,
        "style_of_case": style, "legal_description": legal, "address": None,
        "adjudged_value": parse_money(row[2]), "minimum_bid": parse_money(row[3]),
        "account_number": account_match.group(1),
    }


def parse_row_5col_embedded_value(row: list[str]) -> dict | None:
    """Waller style: sale_no | cause_no(+style) | legal description with embedded adjudged value | account | minimum"""
    if len(row) < 5 or not row[1] or not re.search(r"\d", row[1]):
        return None
    lines = [l.strip() for l in row[1].split("\n") if l.strip()]
    cause_no = lines[0] if lines else None
    if not cause_no or not re.search(r"\d", cause_no):
        return None
    style = " ".join(lines[1:]) if len(lines) > 1 else None
    legal = join_lines(row[2])
    value_match = EMBEDDED_VALUE_PATTERN.search(legal)
    account_lines = [l.strip() for l in row[3].split("\n") if l.strip()]
    # The longest digit-only (dashes allowed, e.g. "7025-0592-000") line is
    # the CAD/geographic ID; short alphanumeric lines like "R17703" seen
    # alongside it are a different roll reference, not the account number.
    account = max(
        (l for l in account_lines if l.replace(" ", "").replace("-", "").isdigit()),
        key=len, default=None,
    )
    return {
        "cause_no": cause_no, "district_court": None, "judgment_date": None,
        "style_of_case": style, "legal_description": legal, "address": None,
        "adjudged_value": parse_money(value_match.group(1)) if value_match else None,
        "minimum_bid": parse_money(row[4]),
        "account_number": account,
    }


def parse_row_case_legal_bid(row: list[str], value_kind: str | None) -> dict | None:
    """
    Hall/Hockley/Lamb/Nolan/Scurry/Stonewall/Wheeler style: case_no | legal
    description + address | minimum bid | optional 4th column. That 4th
    column's meaning varies by county -- a GEO/CAD code (Hall), an appraised
    value (Wheeler), free-form notes (Hockley, Stonewall), or nothing at all
    (Lamb, Nolan, Scurry) -- so parse_pdf() inspects the real header text and
    tells this which one it's looking at via value_kind ("geo", "value", or
    None). No county here publishes an account number of its own when this
    format lacks a GEO column, so cause_no doubles as account_number in that
    case -- it's already the real per-row unique identifier this site's data
    is keyed on (see module docstring).
    """
    if len(row) < 3 or not row[0] or not re.search(r"\d", row[0]):
        return None
    cause_no = join_lines(row[0])
    legal, addr = parse_legal_address_cell(row[1])
    adjudged_value = None
    account_number = None
    if len(row) > 3 and row[3]:
        fourth = join_lines(row[3])
        if value_kind == "value":
            adjudged_value = parse_money(fourth)
        elif value_kind == "geo":
            account_number = fourth or None
    return {
        "cause_no": cause_no, "district_court": None, "judgment_date": None,
        "style_of_case": None, "legal_description": legal, "address": addr,
        "adjudged_value": adjudged_value, "minimum_bid": parse_money(row[2]),
        "account_number": account_number or cause_no,
    }


def parse_row_item_suit_legal_bid(row: list[str]) -> dict | None:
    """Hays style: item# | tax suit no(+style) | legal description with embedded
    adjudged value and tax account # | minimum bid. Falls back to cause_no as
    account_number when the account isn't embedded in the legal text, same
    reasoning as parse_row_case_legal_bid above."""
    if len(row) < 4 or not row[1] or not re.search(r"\d", row[1]):
        return None
    lines = [l.strip() for l in row[1].split("\n") if l.strip()]
    cause_no = lines[0] if lines else None
    if not cause_no or not re.search(r"\d", cause_no):
        return None
    style = " ".join(lines[1:]) if len(lines) > 1 else None
    legal = join_lines(row[2])
    value_match = EMBEDDED_VALUE_PATTERN.search(legal)
    account_match = EMBEDDED_TAX_ACCOUNT_PATTERN.search(legal)
    return {
        "cause_no": cause_no, "district_court": None, "judgment_date": None,
        "style_of_case": style, "legal_description": legal, "address": None,
        "adjudged_value": parse_money(value_match.group(1)) if value_match else None,
        "minimum_bid": parse_money(row[3]),
        "account_number": account_match.group(1) if account_match else cause_no,
    }


def parse_row_brazoria(row: list[str]) -> dict | None:
    """Brazoria style: item# | cause_no(+style) | [blank, blank] | legal
    description with embedded adjudged value | [blank, blank] | Geographic
    ID (the real account number here, unlike the embedded-and-mixed-in
    account/roll numbers parse_row_5col_embedded_value has to disentangle
    for Waller) | minimum bid."""
    if len(row) < 9 or not row[1] or not re.search(r"\d", row[1]):
        return None
    lines = [l.strip() for l in row[1].split("\n") if l.strip()]
    cause_no = lines[0] if lines else None
    if not cause_no or not re.search(r"\d", cause_no):
        return None
    style = " ".join(lines[1:]) if len(lines) > 1 else None
    legal = join_lines(row[4])
    value_match = EMBEDDED_VALUE_PATTERN.search(legal)
    return {
        "cause_no": cause_no, "district_court": None, "judgment_date": None,
        "style_of_case": style, "legal_description": legal, "address": None,
        "adjudged_value": parse_money(value_match.group(1)) if value_match else None,
        "minimum_bid": parse_money(row[8]),
        "account_number": join_lines(row[7]) or cause_no,
    }


# Header signature -> (column count, required keyword per column index, parser).
# Matched by column count first, then by keyword presence, so different
# header wording for the same layout (e.g. "Taxpayer" vs "Taxpayer Name")
# still matches.
FORMATS = [
    {
        "name": "7col-harris",
        "columns": 7,
        "keywords": {2: "style", 4: "adjudged", 6: "account"},
        "parser": parse_row_7col,
    },
    {
        "name": "6col-cameron",
        "columns": 6,
        "keywords": {1: "style", 3: "adjudged", 5: "account"},
        "parser": parse_row_6col,
    },
    {
        "name": "5col-taxpayer",
        "columns": 5,
        "keywords": {1: "legal", 2: "minimum", 4: "taxpayer"},
        "parser": parse_row_5col_taxpayer,
    },
    {
        "name": "4col-embedded-account",
        "columns": 4,
        "keywords": {1: "legal", 2: "adjudged", 3: "minimum"},
        "parser": parse_row_4col_embedded_account,
    },
    {
        "name": "5col-embedded-value",
        "columns": 5,
        "keywords": {2: "legal", 4: "minimum"},
        "parser": parse_row_5col_embedded_value,
    },
    {
        "name": "case-legal-bid",
        "columns": 3,
        "keywords": {0: "case", 1: "legal", 2: "minimum"},
        "parser": parse_row_case_legal_bid,  # bound to a real value_kind in parse_pdf() -- see there
    },
    {
        "name": "item-suit-legal-bid",
        "columns": 4,
        "keywords": {1: "suit", 2: "legal", 3: "minimum"},
        "parser": parse_row_item_suit_legal_bid,
    },
    {
        "name": "brazoria",
        "columns": 9,
        "keywords": {1: "cause", 4: "legal", 7: "geographic", 8: "minimum"},
        "parser": parse_row_brazoria,
    },
]


def detect_format(header_row: list[str]) -> dict | None:
    header_lower = [(c or "").lower() for c in header_row]
    for fmt in FORMATS:
        if len(header_row) < fmt["columns"]:
            continue
        if all(kw in header_lower[i] for i, kw in fmt["keywords"].items()):
            return fmt
    return None


def extract_county(first_page_text: str) -> tuple[str | None, str | None]:
    match = COUNTY_PATTERN.search(first_page_text)
    if not match:
        return None, None
    county = match.group(1).strip().title()
    precinct = f"PCT {match.group(2)}" if match.group(2) else None
    return county, precinct


def find_pdf_links() -> list[str]:
    resp = requests.get(LISTING_PAGE_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".pdf"):
            links.append(href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}")
    return sorted(set(links))


def bind_parser(fmt: dict, header_row: list[str]):
    """Most formats' parser is ready to use as-is; case-legal-bid's 4th column
    means something different per county (see parse_row_case_legal_bid), so
    that one gets bound to whichever meaning this document's actual header
    text implies before it's used on any row."""
    if fmt["name"] != "case-legal-bid":
        return fmt["parser"]
    fourth_label = (header_row[3] or "").lower() if len(header_row) > 3 else ""
    if "value" in fourth_label:
        value_kind = "value"
    elif "geo" in fourth_label or "code" in fourth_label:
        value_kind = "geo"
    else:
        value_kind = None
    return lambda row: parse_row_case_legal_bid(row, value_kind)


def parse_pdf(content: bytes, source_url: str) -> tuple[list[dict], str | None]:
    """Returns (listings, skip_reason). skip_reason is None on success."""
    with pdfplumber.open(BytesIO(content)) as pdf:
        first_page_text = pdf.pages[0].extract_text() or ""
        county, precinct = extract_county(first_page_text)
        if not county:
            # Some documents never name their county in the running text at
            # all (a bare "SALES FOR AUGUST 4, 2026" banner) or phrase it in
            # a way COUNTY_PATTERN doesn't anticipate -- the filename is the
            # fallback of last resort, see extract_county_from_filename.
            county, precinct = extract_county_from_filename(source_url)
        if not county:
            return [], "no county pattern found (likely a city/ISD-specific or non-listing document)"

        fmt = None
        parser = None
        listings = []

        for page in pdf.pages:
            for table in page.extract_tables():
                if not table:
                    continue
                rows = table
                if fmt is None:
                    # The real header isn't always row 0 -- a section-title
                    # banner row (e.g. "Fort Bend Constable Precinct 1") can
                    # come first, pushing the actual header to row 1. Skip
                    # everything up through wherever the header actually was
                    # rather than relying on each parser's own guard clause to
                    # reject it -- a banner row can slip past that guard (Fort
                    # Bend's contains a stray digit, from "Precinct 1").
                    for idx, candidate in enumerate(table[:3]):
                        fmt = detect_format(candidate)
                        if fmt:
                            parser = bind_parser(fmt, candidate)
                            rows = table[idx + 1:]
                            break
                    if fmt is None:
                        continue  # keep looking -- this "table" might be a stray box, not the real one
                for row in rows:
                    parsed = parser(row) if parser else None
                    if parsed:
                        parsed["county"] = county
                        parsed["precinct"] = precinct
                        parsed["source_url"] = source_url
                        listings.append(parsed)

        if fmt is None:
            return [], "no recognized table format"
        return listings, None


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pbfcm_properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            county TEXT,
            precinct TEXT,
            cause_no TEXT,
            district_court TEXT,
            judgment_date TEXT,
            style_of_case TEXT,
            legal_description TEXT,
            address TEXT,
            adjudged_value TEXT,
            minimum_bid TEXT,
            account_number TEXT,
            source_url TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_pbfcm_account_cause
        ON pbfcm_properties(county, account_number, cause_no)
    """)
    conn.commit()


def upsert_listing(conn: sqlite3.Connection, listing: dict):
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT id FROM pbfcm_properties WHERE county = ? AND account_number = ? AND cause_no = ?",
        (listing["county"], listing["account_number"], listing["cause_no"]),
    ).fetchone()

    fields = (
        listing["precinct"], listing["district_court"], listing["judgment_date"],
        listing["style_of_case"], listing["legal_description"], listing["address"],
        listing["adjudged_value"], listing["minimum_bid"], listing["source_url"],
    )

    if existing:
        conn.execute(
            """
            UPDATE pbfcm_properties SET
                precinct = ?, district_court = ?, judgment_date = ?, style_of_case = ?,
                legal_description = ?, address = ?, adjudged_value = ?, minimum_bid = ?,
                source_url = ?, last_seen = ?
            WHERE county = ? AND account_number = ? AND cause_no = ?
            """,
            fields + (now, listing["county"], listing["account_number"], listing["cause_no"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO pbfcm_properties (
                precinct, district_court, judgment_date, style_of_case,
                legal_description, address, adjudged_value, minimum_bid, source_url,
                county, account_number, cause_no, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            fields + (listing["county"], listing["account_number"], listing["cause_no"], now, now),
        )
    conn.commit()


def main():
    print(f"Finding PDF links on {LISTING_PAGE_URL} ...")
    pdf_links = find_pdf_links()
    print(f"Found {len(pdf_links)} PDF(s).")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    combined_conn = combined_db.get_connection()

    total_listings = 0
    parsed_docs = 0
    skipped_docs = 0
    failed_docs = 0

    for url in pdf_links:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 404:
                failed_docs += 1
                continue
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  SKIP {url} -- fetch failed ({e})")
            failed_docs += 1
            continue

        try:
            listings, skip_reason = parse_pdf(resp.content, url)
        except Exception as e:
            print(f"  SKIP {url} -- parse error ({e})")
            failed_docs += 1
            continue

        if skip_reason:
            print(f"  SKIP {url} -- {skip_reason}")
            skipped_docs += 1
            continue

        parsed_docs += 1
        total_listings += len(listings)

        for listing in listings:
            if not listing["account_number"] or not listing["cause_no"]:
                continue
            upsert_listing(conn, listing)

            description_parts = [p for p in (listing["style_of_case"], listing["legal_description"]) if p]
            combined_db.upsert_listing(
                combined_conn,
                county=listing["county"],
                account_number=f"{listing['account_number']}_{listing['cause_no']}",
                precinct=listing["precinct"],
                minimum_bid=listing["minimum_bid"],
                estimated_value=listing["adjudged_value"],
                address=listing["address"],
                description=" -- ".join(description_parts) or None,
                status="Active",
                source="pbfcm.com",
                source_url=listing["source_url"],
            )

    combined_conn.close()
    conn.close()

    print(
        f"\n{parsed_docs} document(s) parsed ({total_listings} listings), "
        f"{skipped_docs} skipped (unrecognized format/document), "
        f"{failed_docs} failed (404/fetch/parse error)."
    )
    print(f"Stored into {DB_PATH}")


if __name__ == "__main__":
    main()
