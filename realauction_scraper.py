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

## Pennsylvania sheriff sales (bank foreclosures)

This same platform also hosts several Pennsylvania counties' Sheriff Sales
-- confirmed live on butler/washington/lawrence.pa.realforeclose.com,
found via that platform's own "Jump To" county switcher (also lists Berks,
Fayette, Lancaster, Pike, York, not yet added here -- see "PA counties not
yet added" below for why each of those needed more than this file's normal
level of verification before being trusted). Unlike Texas, where this
platform carries *tax* foreclosure sales, Pennsylvania's Sheriff Sale is
predominantly the mortgage-foreclosure execution sale -- the county's
*Tax Claim Bureau* Upset Sale (already covered separately for Montgomery
and Chester counties, see montco_scraper.py/chester_scraper.py) is a
different legal process entirely. This isn't asserted as "100% mortgage
foreclosure" though: a Sheriff Sale is legally just an execution on any
money judgment, so an occasional HOA or municipal-claim judgment can show
up in the same feed -- confirmed directly against Allegheny County's own
published sale list (see the "Considered but not built" section of
model/README.md), which explicitly tags each sale's type and shows tax
liens and municipal claims mixed in alongside "Mortgage Foreclosure"
entries. Labeled here as "Sheriff Sale" rather than "Bank Foreclosure" for
that reason.

Each PA county's field template (Case Status/Case #/Final Judgment
Amount/Parcel ID/Property Address/Opening Bid) is identical across all
three counties actually fetched and confirmed live during development --
a real, checked fact, not assumed from one county alone -- but distinct
from Texas's own template (Account Number/Cause Number/Est. Min.
Bid/Adjudged Value/Precinct-Sale Number), so it gets its own parser
(parse_pa_waiting_items) rather than being forced through the TX one.
There's no separate appraisal figure here the way TX's "Adjudged Value"
is -- Final Judgment Amount is the debt being executed on, not an
independent value estimate -- so, same as montco_scraper.py's reasoning,
it's folded into the description rather than stored as estimated_value.

Pennsylvania has no statewide sale-day rule the way Texas's first-Tuesday
is, and RealForeclose's own calendar widget is JS-driven the same as the
existing TX code already couldn't reach (confirmed directly: probing
Zmethod=CALENDAR/GETDATES/LISTDATES/DATES against Butler's own domain all
just fall back to the plain homepage, not real endpoints). So instead of
one shared date-generator, each PA county's actual recurring sale-day rule
or explicit calendar is sourced by hand from that county's own published
schedule (see PA_EXPLICIT_SALE_DATES and washington_sale_dates() below) --
Washington's "first Friday except August" is a real standing rule so it's
computed programmatically for any year; Butler and Lawrence don't reduce
to a simple rule, so their 2026 dates are hardcoded and will need a manual
refresh once that year's calendar is exhausted, the same kind of tradeoff
this project already accepts elsewhere (e.g. the TIGERweb county boundary
files in model/, sourced once and not re-fetched automatically).

## PA counties not yet added

Investigated and deliberately left out of PA_SHERIFF_COUNTIES rather than
guessed into it:
- **Berks**: confirmed still running its Sheriff Sales through Bid4Assets
  through the October 6, 2026 sale, switching to this RealAuction platform
  only as of the November 6, 2026 sale (berkspa.gov/departments/sheriff/
  real-estate-executions) -- not live on this platform yet as of this
  writing.
- **York** and **Lancaster**: each has a live-looking
  {county}.pa.realforeclose.com homepage, but each county's own site
  points bidders and case notices at a *different* platform for the
  authoritative listing (York: attorneyportal.yorkcountypa.gov; Lancaster:
  portal.lancaster.pa.countysuite-azuregov.us) -- real risk the
  RealForeclose subdomain is a dormant/legacy registration rather than
  today's live feed for these two specifically, not verified either way
  by actually pulling a real sale date's AREA=W data the way Butler/
  Washington/Lawrence were.
- **Fayette** and **Lebanon**: RealForeclose subdomain confirmed live and
  branded correctly, but no reliable full-year sale-day rule or calendar
  could be pinned down (Fayette also publishes its own notices via
  fayettecountypa.org's DocumentCenter; Lebanon's own legal-journal
  postings only surfaced two 2026 dates, not an obviously-recurring
  pattern) -- left out rather than hardcoding a guessed schedule that's
  likely to silently miss real sales.
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

PA_SHERIFF_COUNTIES = [
    ("Butler", "butler.pa.realforeclose.com"),
    ("Washington", "washington.pa.realforeclose.com"),
    ("Lawrence", "lawrence.pa.realforeclose.com"),
]

# Sourced by hand from each county's own published schedule (see the module
# docstring's "Pennsylvania sheriff sales" section for why this can't just
# be computed the way Texas's first-Tuesday rule is). Needs a manual
# refresh once a county's listed year is exhausted -- Washington isn't
# here at all since its rule is computed directly, see
# washington_sale_dates() below.
PA_EXPLICIT_SALE_DATES = {
    # https://www.butlercountypa.gov/360/Sheriff-Sales -- 2026: Jan 16,
    # Mar 20, May 15, Jul 17, Sep 18, Nov 20 (bi-monthly, odd months)
    "Butler": [date(2026, 9, 18), date(2026, 11, 20)],
    # https://www.lawrencecountypa.gov -- 2026-Sheriff-Sale-Dates.pdf.
    # Moved from an in-person to this RealAuction platform starting with
    # the September 2026 sale -- confirmed directly that 09/09/2026
    # already returns real data on this platform, not still the old venue.
    "Lawrence": [date(2026, 9, 9), date(2026, 11, 10)],
}

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


def washington_sale_dates(months_ahead: int) -> list[date]:
    """First Friday of every month except August -- Washington County's own
    published rule (washingtoncopa.gov/sheriff/sale), computed directly
    rather than hardcoded since, unlike Butler/Lawrence, it actually is a
    simple recurring rule."""
    today = date.today()
    dates = []
    year, month = today.year, today.month
    for _ in range(months_ahead + 2):
        if month != 8:
            d = date(year, month, 1)
            d += timedelta(days=(4 - d.weekday()) % 7)  # weekday() Monday=0 .. Friday=4
            if d >= today:
                dates.append(d)
        month += 1
        if month > 12:
            month = 1
            year += 1
    return dates


def pa_sale_dates(county: str) -> list[date]:
    if county == "Washington":
        return washington_sale_dates(MONTHS_AHEAD)
    today = date.today()
    return [d for d in PA_EXPLICIT_SALE_DATES.get(county, []) if d >= today]


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


def parse_pa_waiting_items(html: str, county: str, base_url: str, sale_date: date) -> list[dict]:
    """Same AUCTION_ITEM/ad_tab structure as parse_waiting_items, but a
    different field template -- confirmed identical across Butler,
    Washington, and Lawrence's own live responses -- so it gets its own
    field mapping rather than being forced through the TX one (see module
    docstring)."""
    soup = BeautifulSoup(html, "html.parser")
    listings = []

    for item in soup.select("div.AUCTION_ITEM"):
        table = item.find("table", class_="ad_tab")
        if table is None:
            continue
        fields = parse_ad_table(table)

        parcel_id = fields.get("Parcel ID", "").strip()
        case_number = fields.get("Case #", "").strip()
        if not parcel_id or not case_number:
            continue  # not a real property row

        min_bid = fields.get("Opening Bid", "").replace("$", "").replace(",", "").strip() or None
        judgment = fields.get("Final Judgment Amount", "").replace("$", "").replace(",", "").strip() or None

        description_parts = [
            f"Sheriff Sale -- Case {case_number}",
            f"Judgment ${judgment}" if judgment else None,
        ]
        description = " -- ".join(p for p in description_parts if p)

        address = fields.get("Property Address") or None
        if address:
            address = f"{address}, PA"  # the raw field has city/zip, never state

        listings.append({
            "county": county,
            # Composite key, same reasoning as parse_waiting_items: a
            # parcel can recur across sale dates under a new case number.
            "account_number": f"{parcel_id}_{case_number}",
            "minimum_bid": min_bid,
            "estimated_value": None,  # Final Judgment Amount is debt owed, not an appraisal -- see docstring
            "address": address,
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
    # Added once PA_SHERIFF_COUNTIES introduced non-Texas counties -- county
    # names aren't guaranteed unique across states, same reasoning as
    # combined_db.py's own identical migration. DEFAULT 'TX' backfills
    # every existing row correctly since every county scraped here before
    # this was Texas-only.
    conn.execute("ALTER TABLE realauction_properties ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT 'TX'")
    conn.execute("DROP INDEX IF EXISTS idx_realauction_county_account")
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_realauction_state_county_account
        ON realauction_properties(state, county, account_number)
    """)
    conn.commit()


def upsert_local(conn: sqlite3.Connection, listing: dict, state: str = "TX"):
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT id FROM realauction_properties WHERE state = ? AND county = ? AND account_number = ?",
        (state, listing["county"], listing["account_number"]),
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
               WHERE state = ? AND county = ? AND account_number = ?""",
            fields + (now, state, listing["county"], listing["account_number"]),
        )
    else:
        conn.execute(
            """INSERT INTO realauction_properties (
                minimum_bid, estimated_value, address, description, source_url,
                state, county, account_number, first_seen, last_seen
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            fields + (state, listing["county"], listing["account_number"], now, now),
        )
    conn.commit()


def run_county_group(
    conn: sqlite3.Connection, combined_conn, counties: list[tuple[str, str]],
    dates_for_county, parse_fn, state: str, source: str, start_index: int,
) -> int:
    """Shared fetch/parse/store loop for one state's group of counties --
    only the date list, field-parsing function, and state/source labels
    differ between the TX and PA groups (see main())."""
    total_listings = 0
    for i, (county, base_url) in enumerate(counties):
        if start_index + i > 0:
            time.sleep(REQUEST_DELAY_SECONDS)

        dates = dates_for_county(county)
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

            listings = parse_fn(html, county, base_url, sale_date)
            if not listings:
                print(f"  {county} {sale_date}: 0 properties waiting")
                continue

            print(f"  {county} {sale_date}: {len(listings)} properties")
            for listing in listings:
                upsert_local(conn, listing, state=state)
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
                    source=source,
                    source_url=listing["source_url"],
                    state=state,
                )
                total_listings += 1
    return total_listings


def main():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    combined_conn = combined_db.get_connection()

    tx_dates = first_tuesdays(MONTHS_AHEAD)
    total_listings = 0

    total_listings += run_county_group(
        conn, combined_conn, COUNTIES, lambda _county: tx_dates,
        parse_waiting_items, state="TX", source="realauction.com", start_index=0,
    )
    total_listings += run_county_group(
        conn, combined_conn, PA_SHERIFF_COUNTIES, pa_sale_dates,
        parse_pa_waiting_items, state="PA", source="realauction.com",
        start_index=len(COUNTIES),
    )

    combined_conn.close()
    conn.close()
    print(f"\n{total_listings} listing(s) stored into {DB_PATH}")


if __name__ == "__main__":
    main()
