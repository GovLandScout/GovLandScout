"""
GovLandScout - Placer County (CA) Tax Land Sale Scraper

Found while looking for California counties running a genuinely
independent tax-sale channel of their own, rather than through one of the
three shared platforms this project already covers (bid4assets.com,
govease.com, mytaxsale.com -- see those scrapers' own docstrings). Checked
roughly two dozen CA counties before finding this one: every single other
county checked (Riverside, Contra Costa, Fresno, Santa Clara, Calaveras,
Sonoma, Merced, Tulare, El Dorado, Humboldt, San Luis Obispo, San Joaquin,
Nevada, ...) runs on one of those three platforms, several of them
switching between bid4assets and govease from year to year -- California's
tax-sale vendor market turns out to be far more consolidated than
Pennsylvania's, where Allegheny/Chester/Montgomery genuinely self-host
(see those scrapers' own docstrings). Placer is the one real exception
found: by law (confirmed on the county's own Tax Land Sale page) its
annual sale is conducted in person only, no online bidding platform at
all -- "the County will not accept bids via mail, phone or fax" -- so
there's no vendor to have picked in the first place.

Same shape as this project's existing self-published county sources
(guadalupetx.gov, collincountytx.gov, montgomerycountypa.gov, ...): an
in-person sale isn't something a bidder can transact through this site,
but the property list itself -- APN, last assessee, location, minimum
bid -- is exactly the same kind of real, useful research data those
sources already provide, published directly on the county's own site as
a plain HTML table (not a PDF), confirmed live with 43 real current
parcels before writing this. status is set to "Available" uniformly
(unlike bid4assets/govease's own live inventory, this page doesn't
surface a separate sold/withdrawn state -- it's the county's own
currently-eligible list, refreshed whenever they update it).

Item numbers in the table's first column carry a leading run of
zero-width space characters (a copy-paste artifact from whatever the
county's CMS editor did, confirmed directly in the raw HTML -- not a
parsing bug) and a trailing "*" on roughly a third of rows marking a
"Sealed Bid Property" (a second, separate bidding process the page's own
footnote explains, still part of the same sale) -- both stripped from the
row-number field itself, with the asterisk instead folded into
`description` since it's a real distinction about how that specific
parcel is sold, not noise.
"""

import re
from datetime import datetime, timezone
import sqlite3

import requests
from bs4 import BeautifulSoup

import combined_db

LISTING_PAGE_URL = "https://www.placer.ca.gov/1431/Current-Tax-Land-Sale-Properties"
DB_PATH = "placer_properties.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

ITEM_NUMBER_PATTERN = re.compile(r"(\d+)(\*?)")
APN_PATTERN = re.compile(r"^\d{3}-\d{3}-\d{3}-\d{3}$")


# The county's own CMS pads several cells with these (confirmed directly
# in the raw HTML -- a copy-paste artifact, not a parsing bug), including
# inside the Minimum Bid cell itself where a plain .strip() wouldn't catch
# it, so every cell is cleaned of them at the same point text is pulled
# out of the table.
ZERO_WIDTH_SPACE = "​"


def cell_text(cells: list, col_index: dict[str, int], label: str) -> str:
    idx = col_index.get(label)
    if idx is None:
        return ""
    return cells[idx].get_text(" ", strip=True).replace(ZERO_WIDTH_SPACE, "").strip()


def clean_money(text: str) -> str | None:
    value = text.replace("$", "").replace(",", "").strip()
    return value or None


def parse_listings(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")

    listings = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
        col_index = {label: i for i, label in enumerate(headers)}
        if not {"APN", "Minimum Bid"} <= col_index.keys():
            continue  # not the properties table (e.g. the "* Sealed Bid Property" footnote table)

        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) != len(headers):
                continue

            account_number = cell_text(cells, col_index, "APN")
            if not APN_PATTERN.match(account_number):
                continue  # a stray/malformed row, not a real parcel

            item_match = ITEM_NUMBER_PATTERN.search(cell_text(cells, col_index, "Item #"))
            is_sealed_bid = bool(item_match and item_match.group(2) == "*")

            description_parts = [
                cell_text(cells, col_index, "Last Assessee") or None,
                "Sealed Bid Property" if is_sealed_bid else None,
            ]

            listings.append({
                "account_number": account_number,
                "address": cell_text(cells, col_index, "Location Description or Situs") or None,
                "minimum_bid": clean_money(cell_text(cells, col_index, "Minimum Bid")),
                "description": " -- ".join(p for p in description_parts if p) or None,
            })

    return listings


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS placer_properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_number TEXT,
            address TEXT,
            minimum_bid TEXT,
            description TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_placer_account
        ON placer_properties(account_number)
    """)
    conn.commit()


def upsert_local(conn: sqlite3.Connection, listing: dict):
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT id FROM placer_properties WHERE account_number = ?",
        (listing["account_number"],),
    ).fetchone()

    fields = (listing["address"], listing["minimum_bid"], listing["description"])
    if existing:
        conn.execute(
            """UPDATE placer_properties SET
                address = ?, minimum_bid = ?, description = ?, last_seen = ?
               WHERE account_number = ?""",
            fields + (now, listing["account_number"]),
        )
    else:
        conn.execute(
            """INSERT INTO placer_properties (
                address, minimum_bid, description, account_number, first_seen, last_seen
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            fields + (listing["account_number"], now, now),
        )
    conn.commit()


def main():
    print(f"Fetching {LISTING_PAGE_URL} ...")
    resp = requests.get(LISTING_PAGE_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    listings = parse_listings(resp.text)
    print(f"Found {len(listings)} propert{'y' if len(listings) == 1 else 'ies'}.")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    combined_conn = combined_db.get_connection()

    for listing in listings:
        upsert_local(conn, listing)
        combined_db.upsert_listing(
            combined_conn,
            county="Placer",
            account_number=listing["account_number"],
            precinct=None,
            minimum_bid=listing["minimum_bid"],
            estimated_value=None,  # Placer doesn't publish an independent value estimate
            address=listing["address"],
            description=listing["description"],
            status="Available",
            source="placer.ca.gov",
            source_url=LISTING_PAGE_URL,
            state="CA",
        )

    combined_conn.close()
    conn.close()
    print(f"Stored {len(listings)} listing(s) into {DB_PATH}")


if __name__ == "__main__":
    main()
