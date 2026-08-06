"""
GovLandScout - Bid4Assets Pennsylvania Tax Sale Scraper

Bid4Assets (bid4assets.com) is a commercial multi-state auction platform
that several Pennsylvania county Tax Claim Bureaus use for repository and
judicial tax sales -- a different relationship to this project than
GovEase (see govease_scraper.py): GovEase server-renders its whole grid in
one page load, but Bid4Assets is a JS-driven Kendo UI app, and the
listings that actually matter (Address, Legal Description) live on a
per-property detail page, not the list view.

Unlike every other source in this project, this one is fronted by an
Akamai WAF (its own /robots.txt returns an Akamai "Access Denied" page
rather than a robots policy -- there's no stated crawl-delay to honor
because there's no accessible robots.txt at all). Its Terms of Service
didn't turn up any explicit no-automation clause on inspection, but given
the WAF and the sheer listing volume here (single counties running into
the thousands), this deliberately behaves more cautiously than any other
scraper in this project:

  - Runs as its own separate weekly job (see
    .github/workflows/scrape_bid4assets.yml), not part of the daily
    run_daily_scrapers.py batch -- fetching real addresses for thousands
    of properties across several counties does not fit in that job's
    shared ~35-minute budget.
  - Paces per-property detail fetches at DETAIL_FETCH_DELAY_SECONDS
    (see below) with no stated Crawl-delay to calibrate against.
  - Never re-fetches a property whose address it's already stored --
    checked against combined_db's own `listings` table (see
    combined_db.fetch_cached_enrichment_bulk), not the local
    bid4assets_properties.db, because that local file is gitignored and
    GitHub Actions runs from a fresh checkout every time, so it would be
    empty on every single run regardless of what a previous run found.
    Only the first run against a given county does the expensive part;
    later weekly runs mostly just refresh bid amounts and pick up
    genuinely new parcels.
  - Trips a circuit breaker (see MAX_CONSECUTIVE_DETAIL_FAILURES) if
    detail fetches start failing in a row, on the assumption that means
    something is actively blocking this run -- list-level data (parcel
    number, minimum bid, auction dates) still gets stored for every
    listing either way, so a mid-run block loses address enrichment for
    whatever's left, not the whole run.
  - Never holds a database connection open while doing the slow,
    rate-limited part of a run. This took two separate incidents on
    2026-08-06 to actually nail down:
      1. First attempt held ONE connection open, with commit=False, for
         an entire county's processing -- up to ~an hour on a large
         county. The *live site's* own database connections started
         hanging entirely (its "/" route timed out completely while
         static pages kept responding fine); cancelling this scraper's
         run fixed the site within seconds.
      2. Committing more often (every COMMIT_BATCH_SIZE rows) looked like
         the fix, but the SAME site hang recurred on the very next run --
         the connection was still open the whole time between commits,
         just committing more often didn't change that.
    The real fix: main()'s per-county loop is now three strictly separate
    phases -- (1) one bulk DB read for the whole county, connection closed
    immediately; (2) every slow, rate-limited HTTP request, with no
    database connection open at all; (3) one DB connection for writing the
    county's results, closed as soon as writing finishes. The database is
    now never touched at all during the part of this scraper that's
    actually slow.

Discovery is dynamic, not a hardcoded county list like govease_scraper.py's
COUNTIES: bid4assets.com/county-tax-sales lists whichever county auctions
happen to be open right now, and the storefront URL slug embeds the sale's
own month/year (e.g. "MonroePATaxAug26"), so a hardcoded slug would go
stale as soon as that auction closes. Every storefront whose listed name
mentions ", PA" is scraped, each one contributing whatever properties its
own auction currently has -- including several different storefronts for
the same county (Monroe runs a new one most months), which is normal, not
a bug: upsert_listing naturally collapses a re-listed parcel back onto the
same row.

Philadelphia is NOT covered here -- its sales run through a dedicated
/philataxsales page with a different structure than the rest of this
platform's county storefronts, not yet reverse-engineered. Worth
revisiting; Philadelphia is the platform's largest single PA source.

Two more structural things worth knowing before touching this file:

  - The auction list API's own `remaining` field is the most reliable
    signal for "is this property still actually available" (e.g. "Sold",
    seen on a closed batch during development) -- there's no single
    status code confirmed stable enough to switch on, so this excludes by
    keyword instead (see is_still_available).
  - Each auction's title format is NOT consistent across counties --
    "Berks County PA Tax: PIN: 123", "Fayette County PA Tax: Parcel:123",
    "Cumberland County, PA Tax: 123" (no label at all) were all observed
    during development. extract_account_number handles all of them by
    stripping whatever label (if any) follows "Tax:" rather than assuming
    one specific format -- the county itself is never parsed from the
    title at all, since it's already known from which storefront the
    listing came from.
"""

import json
import re
import sqlite3
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

import combined_db

BASE_URL = "https://www.bid4assets.com"
COUNTY_SALES_URL = f"{BASE_URL}/county-tax-sales"
DB_PATH = "bid4assets_properties.db"

# No accessible robots.txt to calibrate against (see module docstring) --
# picked conservatively rather than matched to a stated policy.
DETAIL_FETCH_DELAY_SECONDS = 1.5
LIST_API_DELAY_SECONDS = 1.0

# If this many detail fetches in a row fail, assume something (rate
# limiting, a WAF block) is actively stopping this run rather than one-off
# bad luck, and stop spending more requests on it for the rest of this run.
MAX_CONSECUTIVE_DETAIL_FAILURES = 8

# commit=False batches writes into one open transaction per
# combined_db.upsert_listing call (see its docstring -- avoids a
# round-trip per row across potentially thousands of listings). Now that
# the write phase has no sleep() in it at all (see module docstring), this
# is purely a throughput optimization, not a safety mechanism -- the
# entire phase this bounds typically finishes in well under a minute even
# for a large county.
COMMIT_BATCH_SIZE = 50

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}

STOREFRONT_LINK_PATTERN = re.compile(
    r'<a[^>]+href="(/storefront/[^"]+)"[^>]*>([^<]*County,?\s+PA[^<]*)</a>', re.IGNORECASE,
)
COUNTY_NAME_PATTERN = re.compile(r"^([A-Za-z]+)\s+County,?\s+PA\b", re.IGNORECASE)

STOREFRONT_ID_PATTERN = re.compile(r"getauctiondisplay/(\d+)\?storefrontCollectionId=")
COLLECTIONS_BLOB_PATTERN = re.compile(r'"data":\{"Data":(\[.*?\]),"Total":\d+\}')

# Matches every label variant seen across counties during development --
# "PIN:", "APN:", "Parcel:", "Parcel: " (with a space before the colon),
# or no label at all (Cumberland). Whatever's left after stripping the
# label (if present) is the parcel/account identifier.
ACCOUNT_NUMBER_PATTERN = re.compile(
    r"Tax:\s*(?:(?:PIN|APN|Parcel)\s*:\s*)?(.+)$", re.IGNORECASE,
)

INACTIVE_KEYWORDS = ("sold", "closed", "cancel")


def discover_pa_storefronts(html: str) -> list[tuple[str, str]]:
    """(county, storefront_slug) for every current Bid4Assets auction whose
    listed name mentions Pennsylvania. Philadelphia is deliberately excluded
    here -- see module docstring -- since its listing text says
    "Philadelphia Tax Sales", not "<County> County, PA", so it never
    matches COUNTY_NAME_PATTERN in the first place."""
    storefronts = []
    for href, label in STOREFRONT_LINK_PATTERN.findall(html):
        match = COUNTY_NAME_PATTERN.match(label.strip())
        if not match:
            continue
        slug = href.rsplit("/", 1)[-1]
        storefronts.append((match.group(1), slug))
    return storefronts


def parse_storefront_collections(html: str) -> tuple[int, list[int]] | None:
    """(storefrontId, [storefrontCollectionId, ...]) reverse-engineered from
    the Kendo ListView's inline data-bound JSON -- see module docstring."""
    id_match = STOREFRONT_ID_PATTERN.search(html)
    blob_match = COLLECTIONS_BLOB_PATTERN.search(html)
    if not id_match or not blob_match:
        return None
    try:
        collections = json.loads(blob_match.group(1))
    except ValueError:
        return None
    collection_ids = [c["StorefrontCollectionId"] for c in collections if "StorefrontCollectionId" in c]
    if not collection_ids:
        return None
    return int(id_match.group(1)), collection_ids


def fetch_collection_auctions(session: requests.Session, storefront_id: int, collection_id: int) -> list[dict]:
    resp = session.post(
        f"{BASE_URL}/api/storefront/auctions/index",
        params={"take": 9999, "skip": 0, "page": 1, "pageSize": 9999},
        json={"storefrontId": storefront_id, "storefrontCollectionId": collection_id},
        headers=HEADERS, timeout=30,
    )
    if resp.status_code != 200:
        return []
    try:
        return resp.json().get("data", [])
    except ValueError:
        return []


def is_still_available(row: dict) -> bool:
    remaining = (row.get("remaining") or "").lower()
    return not any(keyword in remaining for keyword in INACTIVE_KEYWORDS)


def extract_account_number(asset_title: str) -> str:
    """Falls back to the raw title (rather than dropping the row) if a
    future title format doesn't match any known shape -- every listing
    needs to survive even a format this hasn't seen before."""
    match = ACCOUNT_NUMBER_PATTERN.search(asset_title)
    return match.group(1).strip() if match else asset_title.strip()


def fetch_auction_detail(session: requests.Session, auction_id: int) -> dict | None:
    """{"address": ..., "legal_description": ...} (either may be None) --
    None only if the page itself couldn't be fetched at all."""
    try:
        resp = session.get(f"{BASE_URL}/auction/{auction_id}", headers=HEADERS, timeout=30)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    def field_text(label: str) -> str | None:
        label_tag = soup.find("strong", string=lambda s: s and s.strip() == label)
        if label_tag is None:
            return None
        row = label_tag.find_parent("tr")
        if row is None:
            return None
        cells = row.find_all("td")
        if len(cells) < 2:
            return None
        # ", " join turns a <br/>-separated "street<br/>city, state" cell
        # into "street, city, state" -- exactly the comma-delimited shape
        # geocode_backfill.py's parse_address already expects.
        text = cells[1].get_text(", ", strip=True)
        return text or None

    return {
        "address": field_text("Address"),
        "legal_description": field_text("Legal Description"),
    }


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bid4assets_properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            county TEXT,
            account_number TEXT,
            auction_id INTEGER,
            minimum_bid TEXT,
            address TEXT,
            description TEXT,
            source_url TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_bid4assets_county_account
        ON bid4assets_properties(county, account_number)
    """)
    conn.commit()


def upsert_local(conn: sqlite3.Connection, listing: dict):
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT id FROM bid4assets_properties WHERE county = ? AND account_number = ?",
        (listing["county"], listing["account_number"]),
    ).fetchone()

    fields = (
        listing["auction_id"], listing["minimum_bid"], listing["address"],
        listing["description"], listing["source_url"],
    )
    if existing:
        conn.execute(
            """UPDATE bid4assets_properties SET
                auction_id = ?, minimum_bid = ?, address = ?, description = ?,
                source_url = ?, last_seen = ?
               WHERE county = ? AND account_number = ?""",
            fields + (now, listing["county"], listing["account_number"]),
        )
    else:
        conn.execute(
            """INSERT INTO bid4assets_properties (
                auction_id, minimum_bid, address, description, source_url,
                county, account_number, first_seen, last_seen
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            fields + (listing["county"], listing["account_number"], now, now),
        )
    conn.commit()


def main():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    session = requests.Session()

    print(f"Fetching {COUNTY_SALES_URL} ...")
    resp = session.get(COUNTY_SALES_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    storefronts = discover_pa_storefronts(resp.text)
    print(f"Found {len(storefronts)} PA storefront(s): {storefronts}")

    total_stored = 0
    consecutive_detail_failures = 0
    circuit_tripped = False

    for i, (county, slug) in enumerate(storefronts):
        if i > 0:
            time.sleep(LIST_API_DELAY_SECONDS)

        storefront_resp = session.get(f"{BASE_URL}/storefront/{slug}", headers=HEADERS, timeout=30)
        if storefront_resp.status_code != 200:
            print(f"  {county} ({slug}): storefront page fetch failed")
            continue

        parsed = parse_storefront_collections(storefront_resp.text)
        if parsed is None:
            print(f"  {county} ({slug}): couldn't find storefront/collection IDs -- site template may have changed")
            continue
        storefront_id, collection_ids = parsed

        rows = []
        for collection_id in collection_ids:
            rows.extend(fetch_collection_auctions(session, storefront_id, collection_id))
            time.sleep(LIST_API_DELAY_SECONDS)

        active_rows = [r for r in rows if is_still_available(r)]
        print(f"  {county} ({slug}): {len(rows)} listing(s), {len(active_rows)} still available")
        if not active_rows:
            continue

        account_numbers = [extract_account_number(r.get("asset_title", "")) for r in active_rows]

        # Phase 1 (DB, fast): one bulk read for the whole county, connection
        # closed immediately after -- see module docstring and
        # fetch_cached_enrichment_bulk's own docstring for why this isn't
        # one query per listing anymore.
        read_conn = combined_db.get_connection()
        cached = combined_db.fetch_cached_enrichment_bulk(read_conn, "PA", county, account_numbers)
        read_conn.close()

        # Phase 2 (network, slow): every rate-limited HTTP request happens
        # here, with NO database connection open at all for the whole
        # phase -- this is the part that can run for tens of minutes on a
        # large county, and it now touches the database exactly zero times
        # while doing so.
        listings = []
        for row, account_number in zip(active_rows, account_numbers):
            auction_id = row.get("auctionID")
            minimum_bid = row.get("minimumBid")
            address, description = cached.get(account_number, (None, None))

            if address is None and not circuit_tripped:
                detail = fetch_auction_detail(session, auction_id)
                time.sleep(DETAIL_FETCH_DELAY_SECONDS)
                if detail is None:
                    consecutive_detail_failures += 1
                    if consecutive_detail_failures >= MAX_CONSECUTIVE_DETAIL_FAILURES:
                        circuit_tripped = True
                        print(f"    {MAX_CONSECUTIVE_DETAIL_FAILURES} detail fetches failed in a row -- "
                              f"stopping address lookups for the rest of this run")
                else:
                    consecutive_detail_failures = 0
                    address = detail["address"]
                    description = detail["legal_description"]

            listings.append({
                "county": county,
                "account_number": account_number,
                "auction_id": auction_id,
                "minimum_bid": str(minimum_bid) if minimum_bid is not None else None,
                "address": address,
                "description": description,
                "source_url": f"{BASE_URL}/auction/{auction_id}" if auction_id else None,
            })

        # Phase 3 (DB, fast): one connection for the whole county's writes,
        # batched commits within it (see COMMIT_BATCH_SIZE) purely to keep
        # any single commit's own work small, then closed -- there is no
        # sleep() anywhere in this phase, so it runs in seconds, not
        # minutes, regardless of how large the county's batch is.
        write_conn = combined_db.get_connection()
        for row_index, listing in enumerate(listings):
            upsert_local(conn, listing)
            combined_db.upsert_listing(
                write_conn,
                county=listing["county"],
                account_number=listing["account_number"],
                precinct=None,
                minimum_bid=listing["minimum_bid"],
                estimated_value=None,  # Bid4Assets doesn't publish an independent value estimate
                address=listing["address"],
                description=listing["description"],
                status="Active",
                source="bid4assets.com",
                source_url=listing["source_url"],
                state="PA",
                commit=False,  # see combined_db.upsert_listing's docstring -- committing per-row across
                                # potentially thousands of listings is what already blew a timeout for LGBS
            )
            total_stored += 1
            if (row_index + 1) % COMMIT_BATCH_SIZE == 0:
                write_conn.commit()
        write_conn.commit()
        write_conn.close()

    conn.close()
    print(f"\n{total_stored} listing(s) stored into {DB_PATH}")


if __name__ == "__main__":
    main()
