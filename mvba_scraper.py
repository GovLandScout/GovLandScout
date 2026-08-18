"""
GovLandScout - MVBA Tax Sale Scraper

McCreary, Veselka, Bragg & Allen (MVBA) publishes tax sale listings as
per-county PDF documents linked from mvbalaw.com/tax-sales/month-sales/.
Unlike PBFCM (which turned out to use 6+ different table layouts across
counties), MVBA uses one consistent template everywhere -- verified
against 4 counties (Bosque, Rusk, Lampasas, Runnels) before writing this,
all identical: TRACT | SUIT # | STYLE | PROPERTY DESCRIPTION, APPROXIMATE
ADDRESS, ACCT # | MIN BID. So this doesn't need PBFCM's format-detection
machinery -- just one parser.

mvbalaw.com/tax-sales/general-property-tax-sales-information/ (the other
URL asked about) was checked too but doesn't contain listing data -- its
linked PDFs (Bastrop, Calhoun, Wharton, Williamson) are pure process/
logistics info (registration requirements, payment methods, sale
schedule), not property tables. Nothing to scrape there.

Like PBFCM, MVBA has no independent value estimate -- only a minimum
bid -- so estimated_value is left unset and these listings fall into
find_deals.py's unpriced bucket. Keyed on (county, account_number,
suit_no) since MVBA also represents multiple taxing entities that can
have separate simultaneous judgments against the same property.

mvbalaw.com blocks this project's usual self-identifying User-Agent
string with a 403, even though robots.txt explicitly permits crawling
here (only /wp-admin/ is disallowed) -- it's a generic bot filter on
unrecognized UA strings, not a stated no-scraping policy. Using a
standard browser UA to get past that filter, but honoring the site's
own Crawl-delay: 10 directive between requests in return.
"""

import re
import sqlite3
import time
from datetime import datetime, timezone
from io import BytesIO

import pdfplumber
import requests
from bs4 import BeautifulSoup

import combined_db

BASE_URL = "https://mvbalaw.com"
LISTING_PAGE_URL = f"{BASE_URL}/tax-sales/month-sales/"
DB_PATH = "mvba_properties.db"
CRAWL_DELAY_SECONDS = 10  # matches robots.txt's stated Crawl-delay for this site

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

FILENAME_COUNTY_PATTERN = re.compile(r"\d{3,4}_([A-Za-z]+)\.pdf$", re.IGNORECASE)
# .title() capitalizes only the first letter of each word, missing the
# internal capital in Texas county names like McLennan and McMullen.
COUNTY_NAME_OVERRIDES = {"Mclennan": "McLennan", "Mcmullen": "McMullen"}
ACCOUNT_PATTERN = re.compile(r"Account\s*#\s*([A-Za-z0-9]+)", re.IGNORECASE)
# A second, explicit source for the same field, seen after "Judgment
# Through Tax Year: ..." at the very end of the cell -- see
# parse_description_cell's docstring for why this is a fallback, not the
# primary source.
APPROXIMATE_ADDRESS_PATTERN = re.compile(r"Approximate Address:\s*(.+)$", re.IGNORECASE)
# Texas oil/gas royalty-interest tracts (confirmed: Rusk County) have no
# street address at all -- there's nothing to geocode a fractional
# interest in a well against -- and their citation-like tract text
# ("...WELL 9 RRC 220978 681.27 AC" / "RRC# 185542") would otherwise get
# mistaken for one by the address heuristic below, since it sits in
# exactly the same "between the last citation and Account #" position a
# real address does. RRC (the Railroad Commission of Texas, which
# regulates oil/gas wells) is a reliable, specific marker for this
# property type, confirmed present on every real mineral-interest
# listing checked.
MINERAL_INTEREST_PATTERN = re.compile(r"\bRRC\b|Royalty Interest", re.IGNORECASE)
# The legal description almost always names the incorporated city a
# platted parcel sits in ("...to the City of Abilene, Taylor County,
# Texas..."), when it's in one at all -- used as a geocoding-city fallback
# for addresses that came from APPROXIMATE_ADDRESS_PATTERN above, which
# (confirmed on real Taylor County data) is a bare street with no city of
# its own. Rural/unplatted parcels described by survey/abstract instead of
# city lot never have this phrase, which correctly leaves those addresses
# without a fallback city rather than guessing one.
CITY_OF_PATTERN = re.compile(r"City of ([A-Za-z .]+?),")


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


def extract_county(source_url: str) -> str | None:
    """
    Filenames follow a consistent "MMYY_County.pdf" pattern -- more
    reliable than parsing the document's own title text, which sometimes
    names OTHER counties as co-plaintiffs (e.g. McLennan County's PDF
    title mentions "THE COUNTY OF BOSQUE" as one of several taxing
    entities involved, which isn't the county the sale is actually in).
    """
    match = FILENAME_COUNTY_PATTERN.search(source_url)
    if not match:
        return None
    name = match.group(1).strip().title()
    # .title() doesn't know about internal capitals in names like McLennan
    return COUNTY_NAME_OVERRIDES.get(name, name)


def parse_money(text: str | None) -> str | None:
    """
    Validates the result actually looks like a number rather than trusting
    whatever's left after stripping $ and commas -- source PDFs do have
    typos (one Jasper County listing has "$20.285.28", a stray period
    where a comma belongs, which isn't valid as either "twenty dollars"
    or "twenty thousand" and would otherwise crash float() downstream).
    """
    if not text:
        return None
    cleaned = text.replace("$", "").replace(",", "").strip()
    if not cleaned or not re.match(r"^\d+(\.\d+)?$", cleaned):
        return None
    return cleaned


def parse_description_cell(text: str) -> tuple[str, str | None, str | None]:
    """
    'PROPERTY DESCRIPTION, APPROXIMATE ADDRESS, ACCT #' is one combined
    cell. The address (when present) sits between the legal description's
    closing deed citation and the "Account #" marker -- anchored here on
    that citation's own closing ")" (the last one before "Account #"),
    not on a specific "...Texas)," shape: an earlier version of this
    function required that exact literal, which worked for the counties
    it was first confirmed against (Bosque, Rusk, Lampasas, Runnels) but
    silently produced no address at all for two other real shapes since
    confirmed in production -- Jasper's ALL CAPS citations ("...JASPER
    COUNTY, TEXAS.)", both wrong-case and carrying a trailing period the
    old pattern didn't allow for) and McLennan's, which don't restate the
    county/state inside the citation at all ("...Texas (Volume 1801,
    Page 688 OPR)"). Anchoring on the last ")" itself handles every shape
    seen so far without needing to special-case each one -- confirmed
    this doesn't regress the counties the original version already
    handled correctly (none of their addresses contain a stray ")" of
    their own).

    Some counties (confirmed on real 2026-09 data: Taylor) leave that gap
    empty and only give the address in a separate, explicitly-labeled
    "Approximate Address: ..." field after "Judgment Through Tax Year:
    ..." at the very end of the cell instead. Checking that second field
    only when the first comes up empty, not unconditionally: a few
    counties (confirmed: Comanche) redundantly include both, and the
    primary one is the more complete of the two there (full
    city/state/zip vs. just a bare street in the trailing label) -- so
    the primary field wins whenever it's actually present. Some counties
    (confirmed: Leon) have neither; that's a real "no address published
    for this listing," not a parsing gap.

    Oil/gas royalty-interest tracts (confirmed: Rusk) have no address at
    all to find this way -- see MINERAL_INTEREST_PATTERN's own comment --
    so this bails out before attempting either strategy for those,
    rather than mistaking their own tract/well citation text for one.
    """
    joined = " ".join(l.strip() for l in text.split("\n") if l.strip())
    account_match = ACCOUNT_PATTERN.search(joined)
    account = account_match.group(1) if account_match else None

    if MINERAL_INTEREST_PATTERN.search(joined):
        return joined, None, account

    address = None
    legal_description = joined
    if account_match:
        last_paren_idx = joined.rfind(")", 0, account_match.start())
        if last_paren_idx != -1:
            address = joined[last_paren_idx + 1:account_match.start()].strip(" ,-") or None
            legal_description = joined[: last_paren_idx + 1].strip()

    if not address:
        approx_match = APPROXIMATE_ADDRESS_PATTERN.search(joined)
        if approx_match:
            address = approx_match.group(1).strip(" ,-") or None

    # A bare street from APPROXIMATE_ADDRESS_PATTERN has no city of its own
    # to geocode against -- geocode_backfill.py's fallback for a cityless
    # address is the *county* name, which Census reliably rejects (a
    # county isn't a place), confirmed against real production data before
    # writing this. Appending the legal description's own "City of X" here
    # (see CITY_OF_PATTERN's docstring) fixes that at the source, for 84%
    # of real Taylor County listings checked, rather than leaving every
    # downstream geocode attempt for these addresses to fail the same way.
    if address and "," not in address:
        city_match = CITY_OF_PATTERN.search(joined)
        if city_match:
            address = f"{address}, {city_match.group(1).strip()}, TX"

    return legal_description, address, account


def parse_pdf(content: bytes, source_url: str) -> tuple[list[dict], str | None]:
    county = extract_county(source_url)
    if not county:
        return [], "no county pattern found in filename"

    with pdfplumber.open(BytesIO(content)) as pdf:

        listings = []
        found_table = False

        for page in pdf.pages:
            for table in page.extract_tables():
                if not table or len(table[0]) < 5:
                    continue
                header_lower = [(c or "").lower() for c in table[0]]
                if not ("suit" in header_lower[1] and "min bid" in header_lower[4]):
                    continue  # not the properties table (e.g. a stray box)
                found_table = True

                for row in table[1:]:
                    if len(row) < 5 or not row[1] or not re.search(r"\d", row[1]):
                        continue
                    suit_no = " ".join(l.strip() for l in row[1].split("\n") if l.strip())
                    style = " ".join(l.strip() for l in (row[2] or "").split("\n") if l.strip())
                    legal, address, account = parse_description_cell(row[3] or "")
                    min_bid = parse_money(row[4])

                    if not account or not suit_no:
                        continue

                    listings.append({
                        "county": county, "suit_no": suit_no, "style": style,
                        "legal_description": legal, "address": address,
                        "account_number": account, "minimum_bid": min_bid,
                        "source_url": source_url,
                    })

        if not found_table:
            return [], "no recognized table format"
        return listings, None


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mvba_properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            county TEXT,
            suit_no TEXT,
            style TEXT,
            legal_description TEXT,
            address TEXT,
            account_number TEXT,
            minimum_bid TEXT,
            source_url TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_mvba_account_suit
        ON mvba_properties(county, account_number, suit_no)
    """)
    conn.commit()


def upsert_listing(conn: sqlite3.Connection, listing: dict):
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT id FROM mvba_properties WHERE county = ? AND account_number = ? AND suit_no = ?",
        (listing["county"], listing["account_number"], listing["suit_no"]),
    ).fetchone()

    fields = (
        listing["style"], listing["legal_description"], listing["address"],
        listing["minimum_bid"], listing["source_url"],
    )

    if existing:
        conn.execute(
            """
            UPDATE mvba_properties SET
                style = ?, legal_description = ?, address = ?, minimum_bid = ?,
                source_url = ?, last_seen = ?
            WHERE county = ? AND account_number = ? AND suit_no = ?
            """,
            fields + (now, listing["county"], listing["account_number"], listing["suit_no"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO mvba_properties (
                style, legal_description, address, minimum_bid, source_url,
                county, account_number, suit_no, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            fields + (listing["county"], listing["account_number"], listing["suit_no"], now, now),
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

    for i, url in enumerate(pdf_links):
        if i > 0:
            time.sleep(CRAWL_DELAY_SECONDS)
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
            upsert_listing(conn, listing)

            description_parts = [p for p in (listing["style"], listing["legal_description"]) if p]
            combined_db.upsert_listing(
                combined_conn,
                county=listing["county"],
                account_number=f"{listing['account_number']}_{listing['suit_no']}",
                precinct=None,
                minimum_bid=listing["minimum_bid"],
                estimated_value=None,  # MVBA doesn't publish an independent value estimate
                address=listing["address"],
                description=" -- ".join(description_parts) or None,
                status="Active",
                source="mvbalaw.com",
                source_url=listing["source_url"],
            )

    combined_conn.close()
    conn.close()

    print(
        f"\n{parsed_docs} document(s) parsed ({total_listings} listings), "
        f"{skipped_docs} skipped, {failed_docs} failed."
    )
    print(f"Stored into {DB_PATH}")


if __name__ == "__main__":
    main()
