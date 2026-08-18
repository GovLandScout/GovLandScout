"""
GovLandScout - GovEase Scraper (Texas, Pennsylvania, and California counties)

GovEase (liveauctions.govease.com) is another online tax sale platform,
the same relationship to this project as RealAuction (see
realauction_scraper.py's docstring): the trustee firm/county markets a
sale, but the actual bidding happens on GovEase's shared multi-state
platform. Its own site-wide "Choose State/County" dropdown lists exactly
four Texas auctions:

    TX - Denton
    TX - Grayson
    TX - McLennan - MVBA
    TX - Wichita

McLennan is explicitly labeled "- MVBA" and already shows up in this
project's data via mvba_scraper.py's own PDF listings for that county --
scraping it here too would double-count the same properties under two
sources, so it's deliberately excluded (same reasoning as RealAuction's
exclusion list). Denton, Grayson, and Wichita aren't LGBS/MVBA/PBFCM
clients, so there's nothing to double up against for those three.

As of adding Pennsylvania support ("Scraping pennsylvania tax sales"),
the same dropdown also lists seven PA county auctions, conducted under
Pennsylvania's Real Estate Tax Sale Law rather than Texas Property Tax
Code Chapter 34 (different legal process, same platform -- see the
Investment Info page):

    PA - Beaver - Upset Sale
    PA - Bucks - Upset Sale
    PA - Erie - Judicial Sale
    PA - Erie - Upset Sale
    PA - Lawrence - Judicial Sale
    PA - Potter - Upset Sale
    PA - York - Upset Sale

None of these are known to double-list on another PA trustee site this
project scrapes (there isn't one yet), so all seven are included, unlike
the TX McLennan exclusion above. Every listing carries a `state` tag
("TX"/"PA") through to combined_db.upsert_listing -- required because
county names collide across states (Texas has its own Potter County,
around Amarillo, entirely unrelated to Pennsylvania's).

As of adding California support, the dropdown lists three CA auctions:

    CA - Kern
    CA - Los Angeles
    CA - Los Angeles Follow-Up

Neither Kern nor Los Angeles overlaps any bid4assets.com CA storefront
this project already scrapes (confirmed directly against that scraper's
live discovery results before adding these -- see bid4assets_scraper.py)
-- Los Angeles and Kern simply aren't among the counties currently
running a Bid4Assets auction. "Los Angeles Follow-Up" is the same kind
of re-listing GovEase's own auction naming already documents happening
for PA (a Judicial Sale re-offering whatever an Upset Sale didn't sell)
and for TX's McLennan (a second "- Linebarger"-labeled listing of the
same county's own auction under another trustee) -- the same parcel can
plausibly show up in both the main and follow-up California auctions,
so both get an explicit `sale_type` tag ("Main"/"FollowUp") the same
uniform way every PA county already does, not left to collide silently
if that ever actually happens. Los Angeles's main auction had an empty
property grid when this was added (its list wasn't published yet --
confirmed by fetching the live page directly, not a parsing failure);
it's included anyway since GovEase auctions publish their list closer
to the sale date and this project's regular scraper runs will pick it
up once that happens, the same as any other county whose listings
haven't gone live yet.

Unlike RealAuction, this doesn't need any session/JS reverse-engineering:
each auction's /browsestandard page 302-redirects to /browse, which
server-renders the full property grid directly in the initial HTML
(confirmed via the page's own inline script: the DataTable is initialized
with "paging": false, i.e. everything is on one page, no pagination to
walk).

Column layout varies slightly between auctions/states (e.g. Denton's bid
column is labeled "Minimum Bid", Grayson's and every PA county's "Face
Value" -- and other states' auctions add columns like "Property
Description" that TX's don't have), so this reads the actual <thead> to
map label -> column index per auction rather than assuming a fixed
position, the same defensiveness realauction_scraper.py's parse_ad_table
uses for its own label/value pairs.

"Unique #" is just that auction's row sequence number (resets and isn't
stable across runs), so it's not usable as a dedupe key -- "Parcel #"
holds the real case/cause number (e.g. "19-7161-16") and is what gets
used as account_number instead.
"""

import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

import combined_db

BASE_URL = "https://liveauctions.govease.com"
DB_PATH = "govease_properties.db"

# (county, state, slug, auction_id, sale_type) -- McLennan deliberately
# omitted, see module docstring. `state` doubles as both the URL path
# segment GovEase expects (lowercase) and, uppercased, the `state` value
# written to combined_db -- values and auction IDs reverse-engineered from
# the <select> on liveauctions.govease.com's own auction-picker dropdown.
#
# `sale_type` is None for every TX county (one auction each, so a parcel
# number is already a unique key -- kept exactly as before, unsuffixed, so
# this doesn't orphan any of the ~3 years of TX rows already in
# production). PA counties get a real sale_type because Erie runs BOTH an
# Upset and a Judicial sale, and Pennsylvania's tax sale process is
# specifically designed to re-list a parcel that didn't sell at Upset into
# a later Judicial sale (see Investment Info) -- the same parcel number can
# legitimately appear in both, as two different, currently-active listings
# with different terms and bid amounts, not a duplicate to collapse. Every
# PA county gets the suffix uniformly (not just Erie) so the key format
# doesn't depend on which counties happen to collide today.
COUNTIES = [
    ("Denton", "tx", "txdenton", 1355, None),
    ("Grayson", "tx", "txgrayson", 1280, None),
    ("Wichita", "tx", "txwichita", 1429, None),
    ("Beaver", "pa", "pabeaverupset", 1533, "Upset"),
    ("Bucks", "pa", "pabucksupset", 1535, "Upset"),
    ("Erie", "pa", "paeriejudicial", 1528, "Judicial"),
    ("Erie", "pa", "paerieupset", 1527, "Upset"),
    ("Lawrence", "pa", "palawrencejudicial", 1492, "Judicial"),
    ("Potter", "pa", "papotterupset", 1453, "Upset"),
    ("York", "pa", "payorkupset", 1350, "Upset"),
    ("Kern", "ca", "cakern", 1348, "Main"),
    ("Los Angeles", "ca", "calosangeles", 1391, "Main"),
    ("Los Angeles", "ca", "calosangelesfollowup", 1396, "FollowUp"),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

BID_COLUMN_LABELS = {"Minimum Bid", "Starting Bid", "Face Value"}


def clean_money(text: str) -> str | None:
    value = text.replace("$", "").replace(",", "").strip()
    return value or None


def parse_county_grid(html: str, county: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="dt-auctions")
    if table is None or table.find("tbody") is None:
        return []

    headers = [th.get_text(strip=True).rstrip(":") for th in table.find("thead").find_all("th")]
    bid_label = next((label for label in headers if label in BID_COLUMN_LABELS), None)
    col_index = {label: i for i, label in enumerate(headers) if label}

    listings = []
    for row in table.find("tbody").find_all("tr"):
        cells = row.find_all("td")
        if len(cells) != len(headers):
            continue  # not a real data row (e.g. DataTables' own "no data" placeholder)

        def cell_text(label: str) -> str:
            idx = col_index.get(label)
            return cells[idx].get_text(" ", strip=True) if idx is not None else ""

        account_number = cell_text("Parcel #").strip()
        if not account_number:
            continue

        link = cells[col_index["Unique #"]].find("a") if "Unique #" in col_index else None
        source_url = urljoin(BASE_URL, link["href"]) if link and link.get("href") else None

        address = cell_text("Parcel Address").strip()
        if address.upper() in ("", "N/A"):
            address = None

        min_bid = clean_money(cell_text(bid_label)) if bid_label else None

        description = " -- ".join(
            p for p in (cell_text("Auction Name").strip(), cell_text("Auction Type").strip()) if p
        ) or None

        listings.append({
            "county": county,
            "account_number": account_number,
            "minimum_bid": min_bid,
            "address": address,
            "description": description,
            "source_url": source_url,
        })

    return listings


def fetch_county(session: requests.Session, state: str, slug: str, auction_id: int) -> str | None:
    resp = session.get(
        f"{BASE_URL}/{state}/{slug}/{auction_id}/browsestandard",
        headers=HEADERS, timeout=30,
    )
    if resp.status_code != 200:
        return None
    return resp.text


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS govease_properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            county TEXT,
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
        CREATE UNIQUE INDEX IF NOT EXISTS idx_govease_county_account
        ON govease_properties(county, account_number)
    """)
    conn.commit()


def upsert_local(conn: sqlite3.Connection, listing: dict):
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT id FROM govease_properties WHERE county = ? AND account_number = ?",
        (listing["county"], listing["account_number"]),
    ).fetchone()

    fields = (listing["minimum_bid"], listing["address"], listing["description"], listing["source_url"])
    if existing:
        conn.execute(
            """UPDATE govease_properties SET
                minimum_bid = ?, address = ?, description = ?, source_url = ?, last_seen = ?
               WHERE county = ? AND account_number = ?""",
            fields + (now, listing["county"], listing["account_number"]),
        )
    else:
        conn.execute(
            """INSERT INTO govease_properties (
                minimum_bid, address, description, source_url,
                county, account_number, first_seen, last_seen
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            fields + (listing["county"], listing["account_number"], now, now),
        )
    conn.commit()


def main():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    combined_conn = combined_db.get_connection()

    session = requests.Session()
    total_listings = 0

    for county, state, slug, auction_id, sale_type in COUNTIES:
        html = fetch_county(session, state, slug, auction_id)
        if html is None:
            print(f"  {county}: fetch failed")
            continue

        listings = parse_county_grid(html, county)
        print(f"  {county}: {len(listings)} propert{'y' if len(listings) == 1 else 'ies'}")

        for listing in listings:
            if sale_type:
                listing["account_number"] = f"{listing['account_number']}_{sale_type.upper()}"
            upsert_local(conn, listing)
            combined_db.upsert_listing(
                combined_conn,
                county=listing["county"],
                account_number=listing["account_number"],
                precinct=None,
                minimum_bid=listing["minimum_bid"],
                estimated_value=None,  # no independent value estimate on this platform, only a minimum bid
                address=listing["address"],
                description=listing["description"],
                status="Active",
                source="govease.com",
                source_url=listing["source_url"],
                state=state.upper(),
            )
            total_listings += 1

    combined_conn.close()
    conn.close()
    print(f"\n{total_listings} listing(s) stored into {DB_PATH}")


if __name__ == "__main__":
    main()
