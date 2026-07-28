"""
GovLandScout - RealAuction/RealForeclose Scraper

Every Texas tax foreclosure sale, including the ones LGBS/PBFCM/MVBA
already get scraped from, is legally conducted by the county Sheriff or
Constable under Tax Code Chapter 34 -- "trustee" in that chapter refers to
something else entirely (property that didn't sell and got struck off to
the taxing unit itself), not a different selling officer or process. What
actually differs county to county is just which *platform* the listings
and online bidding happen on: most of this site's counties hired a
trustee law firm (LGBS etc.) that publishes its own listings; Travis
(Constable Precinct 5) and Caldwell (County Sheriff) instead contract
directly with Realauction.com LLC's platform, branded per county as either
"RealForeclose" or "SheriffSaleAuctions" (same underlying ColdFusion app).
Deed type, redemption periods, etc. are identical either way -- see the
Investment Info page's "County tax sales" section, which now covers this
platform too.

That platform also happens to host ~20 OTHER Texas counties (Dallas, El
Paso, Galveston, Kaufman, Montgomery, Smith, ...), all already covered on
this site via LGBS/PBFCM's own listings. Deliberately not scraping those
here -- a county can engage a trustee firm for marketing/paperwork *and*
list the same sale on this platform for the actual bidding, so scraping
both risks double-counting the same property under two different sources.
Travis and Caldwell aren't LGBS/PBFCM/MVBA clients at all, so there's
nothing to double up against.

How the site works (reverse-engineered, no public API docs):
1. GET .../index.cfm?zaction=AUCTION&Zmethod=PREVIEW&AUCTIONDATE=MM/DD/YYYY
   sets server-side session state for "which sale date am I viewing" (this
   is why a plain single request for the property list returns nothing --
   the page shell loads first, then step 2 below is fired by the page's own
   JS to populate it. A real browser visiting the PREVIEW url does see the
   listings, since that JS runs automatically).
2. GET .../index.cfm?zaction=AUCTION&Zmethod=UPDATE&FNC=LOAD&AREA=W&...
   returns the actual property data as JSON: {"retHTML": "<compressed
   HTML>", "rlist": "id,id,..."}. AREA=W is "Auctions Waiting" -- i.e. not
   yet sold -- as opposed to AREA=C ("Closed or Canceled") or AREA=R.

The "compressed HTML" isn't real compression, just single-token stand-ins
for the auction template's repeated class names/tags (reverse-engineered
by diffing this against the browser's own decompressed DOM -- see
TOKEN_SUBSTITUTIONS). If a future template change introduces a token this
dictionary doesn't cover, decompression is left with a literal "@X" in the
output; detected and treated as a parse failure for that date rather than
silently emitting corrupted text (see decompress()).

Sales run the first Tuesday of the month; this script doesn't scrape the
calendar widget for exact dates (also JS-driven) and instead just computes
the next few first-Tuesdays and asks each one for its "waiting" list --
a month with nothing scheduled comes back with an empty AREA=W and is
skipped, same as a month that hasn't been listed yet.
"""

import json
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

import combined_db

DB_PATH = "realauction_properties.db"
REQUEST_DELAY_SECONDS = 3  # no robots.txt on either domain; still pace requests as a courtesy
MONTHS_AHEAD = 3  # listings typically post ~2-3 weeks before the sale; querying further out just wastes requests on empty results

COUNTIES = [
    ("Travis", "travis.texas.realforeclose.com"),
    ("Caldwell", "caldwell.texas.sheriffsaleauctions.com"),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

# Reverse-engineered by diffing a captured retHTML blob against the same
# auction item's decompressed outerHTML in a real browser DOM. Order
# doesn't matter -- none of these substrings are substrings of each other.
TOKEN_SUBSTITUTIONS = {
    "@A": '<div class="',
    "@B": "</div>",
    "@C": ' class="',
    "@E": "AUCTION",
    "@G": "</td></tr>",
    "@I": "table",
}
LEFTOVER_TOKEN_PATTERN = re.compile(r"@[A-Z]")


def first_tuesdays(months_ahead: int) -> list[date]:
    today = date.today()
    dates = []
    year, month = today.year, today.month
    for _ in range(months_ahead + 1):
        d = date(year, month, 1)
        d += timedelta(days=(1 - d.weekday()) % 7)  # weekday() Monday=0 .. Tuesday=1
        if d >= today:
            dates.append(d)
        month += 1
        if month > 12:
            month = 1
            year += 1
    return dates


def decompress(compressed: str) -> str | None:
    text = compressed
    for token, replacement in TOKEN_SUBSTITUTIONS.items():
        text = text.replace(token, replacement)
    text = text.replace('\\"', '"').replace("\\/", "/")
    if LEFTOVER_TOKEN_PATTERN.search(text):
        return None  # unrecognized token -- template changed, don't trust the rest
    return text


def fetch_waiting_items(session: requests.Session, base_url: str, sale_date: date) -> str | None:
    """Returns the decompressed HTML fragment for AREA=W (not-yet-sold properties), or None if unavailable."""
    date_str = sale_date.strftime("%m/%d/%Y")

    preview_resp = session.get(
        f"https://{base_url}/index.cfm",
        params={"zaction": "AUCTION", "Zmethod": "PREVIEW", "AUCTIONDATE": date_str},
        headers=HEADERS, timeout=30,
    )
    if preview_resp.status_code != 200:
        return None

    ts = int(time.time() * 1000)
    update_resp = session.get(
        f"https://{base_url}/index.cfm",
        params={
            "zaction": "AUCTION", "Zmethod": "UPDATE", "FNC": "LOAD", "AREA": "W",
            "PageDir": "0", "doR": "1", "tx": ts, "bypassPage": "1", "test": "1", "_": ts,
        },
        headers=HEADERS, timeout=30,
    )
    if update_resp.status_code != 200:
        return None

    match = re.search(r'\{"retHTML".*\}', update_resp.text)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except ValueError:
        return None

    ret_html = payload.get("retHTML", "")
    if not ret_html:
        return None  # nothing scheduled for this date yet -- normal, not an error

    decompressed = decompress(ret_html)
    if decompressed is None:
        print(f"    WARNING: {base_url} {sale_date} -- unrecognized token in retHTML, site template may have changed")
    return decompressed


def parse_ad_table(table) -> dict[str, str]:
    """
    Each row is <th>Label:</th><td>Value</td> -- except the address, which
    can spill onto a second row with an empty <th> holding the city/state/
    zip continuation. That continuation is folded into "Property Address"
    with a comma, rather than kept as its own field, since it isn't a
    separate labeled attribute on the real site either.
    """
    fields: dict[str, str] = {}
    last_label = None
    for row in table.find_all("tr"):
        th, td = row.find("th"), row.find("td")
        if th is None or td is None:
            continue
        label = th.get_text(strip=True).rstrip(":")
        value = td.get_text(" ", strip=True)
        if label:
            fields[label] = value
            last_label = label
        elif last_label == "Property Address" and value:
            fields[last_label] = f"{fields[last_label]}, {value}"
    return fields


def parse_waiting_items(html: str, county: str, base_url: str, sale_date: date) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    listings = []

    for item in soup.select("div.AUCTION_ITEM"):
        table = item.find("table", class_="ad_tab")
        if table is None:
            continue
        fields = parse_ad_table(table)

        account_number = fields.get("Account Number", "").strip()
        cause_number = fields.get("Cause Number", "").strip()
        if not account_number or not cause_number:
            continue  # not a real property row

        min_bid = fields.get("Est. Min. Bid", "").replace("$", "").replace(",", "").strip() or None
        est_value = fields.get("Adjudged Value", "").replace("$", "").replace(",", "").strip() or None

        description_parts = [
            f"Sheriff/Constable Sale -- Cause {cause_number}",
            f"Sale #{fields['Precinct/Sale Number'].lstrip('/')}" if fields.get("Precinct/Sale Number") else None,
        ]
        description = " -- ".join(p for p in description_parts if p)

        listings.append({
            "county": county,
            # Composite key: the same account can recur across sale dates
            # (a property that didn't sell gets re-listed the next month),
            # and cause_number disambiguates the rare case of two separate
            # judgments against the same account in the same batch.
            "account_number": f"{account_number}_{cause_number}",
            "minimum_bid": min_bid,
            "estimated_value": est_value,
            "address": fields.get("Property Address") or None,
            "description": description,
            "source_url": (
                f"https://{base_url}/index.cfm?zaction=AUCTION&Zmethod=PREVIEW"
                f"&AUCTIONDATE={sale_date.strftime('%m/%d/%Y')}"
            ),
        })

    return listings


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS realauction_properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            county TEXT,
            account_number TEXT,
            minimum_bid TEXT,
            estimated_value TEXT,
            address TEXT,
            description TEXT,
            source_url TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_realauction_county_account
        ON realauction_properties(county, account_number)
    """)
    conn.commit()


def upsert_local(conn: sqlite3.Connection, listing: dict):
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT id FROM realauction_properties WHERE county = ? AND account_number = ?",
        (listing["county"], listing["account_number"]),
    ).fetchone()

    fields = (
        listing["minimum_bid"], listing["estimated_value"], listing["address"],
        listing["description"], listing["source_url"],
    )
    if existing:
        conn.execute(
            """UPDATE realauction_properties SET
                minimum_bid = ?, estimated_value = ?, address = ?, description = ?,
                source_url = ?, last_seen = ?
               WHERE county = ? AND account_number = ?""",
            fields + (now, listing["county"], listing["account_number"]),
        )
    else:
        conn.execute(
            """INSERT INTO realauction_properties (
                minimum_bid, estimated_value, address, description, source_url,
                county, account_number, first_seen, last_seen
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            fields + (listing["county"], listing["account_number"], now, now),
        )
    conn.commit()


def main():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    combined_conn = combined_db.get_connection()

    dates = first_tuesdays(MONTHS_AHEAD)
    total_listings = 0

    for i, (county, base_url) in enumerate(COUNTIES):
        if i > 0:
            time.sleep(REQUEST_DELAY_SECONDS)

        for j, sale_date in enumerate(dates):
            if j > 0:
                time.sleep(REQUEST_DELAY_SECONDS)

            # A fresh session per date, not per county: the server appears
            # to cache the "waiting" list against session state on first
            # PREVIEW load and not refresh it on a second PREVIEW hit in the
            # same session, so reusing one session across dates silently
            # re-returns the first date's results for every date after it.
            session = requests.Session()
            html = fetch_waiting_items(session, base_url, sale_date)
            if html is None:
                print(f"  {county} {sale_date}: no data (not yet posted, or template changed)")
                continue

            listings = parse_waiting_items(html, county, base_url, sale_date)
            if not listings:
                print(f"  {county} {sale_date}: 0 properties waiting")
                continue

            print(f"  {county} {sale_date}: {len(listings)} properties")
            for listing in listings:
                upsert_local(conn, listing)
                combined_db.upsert_listing(
                    combined_conn,
                    county=listing["county"],
                    account_number=listing["account_number"],
                    precinct=None,  # "Precinct/Sale Number" here is a batch item index, not a geographic constable precinct
                    minimum_bid=listing["minimum_bid"],
                    estimated_value=listing["estimated_value"],
                    address=listing["address"],
                    description=listing["description"],
                    status="Active",
                    source="realauction.com",
                    source_url=listing["source_url"],
                )
                total_listings += 1

    combined_conn.close()
    conn.close()
    print(f"\n{total_listings} listing(s) stored into {DB_PATH}")


if __name__ == "__main__":
    main()
