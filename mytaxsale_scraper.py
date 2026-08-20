"""
GovLandScout - MyTaxSale.com / DeedAuction Scraper (California counties)

A third California tax-sale platform, distinct from both bid4assets.com and
govease.com (see those scrapers' own docstrings): Grant Street Group's
"DeedAuction" product, branded per county as "<subdomain>.mytaxsale.com".
Found by searching for what platform LA/San Diego/Orange/etc. actually run
on after noticing bid4assets_scraper.py's live storefront discovery only
ever turns up smaller/rural CA counties (Imperial, Shasta, Amador, Modoc,
...) -- none of the state's biggest counties by population. Confirmed
directly against the live sites before writing this: seven CA county
subdomains are real, reachable DeedAuction sites, not stale or
decommissioned --

    San Diego       sdttc.mytaxsale.com
    Sacramento      sacramento.mytaxsale.com
    San Francisco   sanfrancisco.mytaxsale.com
    San Bernardino  sbcounty.mytaxsale.com
    San Mateo       sanmateo.mytaxsale.com
    Orange          octaxauction.mytaxsale.com
    Solano          solano.mytaxsale.com

kern.mytaxsale.com also resolves but serves an "Unknown Site" placeholder
(confirmed live) -- Kern's actual current platform is GovEase (see
govease_scraper.py), so this deliberately excludes it rather than risk a
double-count against a site GSG has already migrated away from.

Orange County is not a double-count against its own bid4assets.com
storefront either: Grant Street Group's own press material describes this
specific platform as handling Orange County's tax-defaulted *timeshare*
auctions, a distinct product line from the real property sale
bid4assets_scraper.py already covers there.

## Why every county is empty right now

Every one of the seven counties' /auctions/upcoming endpoint returned
recordsTotal=0 when this was written (confirmed live, not a parsing
failure) -- California county tax-defaulted sales run about once a year
and none of these seven happen to have one scheduled at the moment:
San Diego's own site says the next auction starts "May 8" (of next year),
Sacramento's says this year's sale is over and next year's dates aren't
set, San Francisco/San Mateo/Orange/Solano all say nothing is scheduled
yet. Kept anyway, same reasoning as publicsurplus_scraper.py and
houston_scraper.py: this project's daily/regular re-scrape will pick up
each county's listings automatically once they're posted, without anyone
needing to remember to add code later.

## How the site works (reverse-engineered, no public API docs)

Built on a shared "DataTables server-side" pattern (see gsg_datatables.js on
any of these sites) rather than a plain REST API. Every step below was
confirmed against San Diego's own auction #49 -- currently closed, but
still serving its full original data live (546 of its 715 advertised
parcels came back marked available, each with a real opening bid and full
property detail), which is what let this be verified end-to-end despite
none of the seven counties above having anything open right now:

1. GET /auctions (or /auction/{id}) on a fresh session sets a session
   cookie and embeds a CSRF token in <meta name="csrf_token">. This is
   NOT one token per session -- confirmed live every page render gets its
   own, and a token from one page gets a flat 403 on a different page's
   own POST endpoints. fetch_auction_page_state() re-fetches one from
   /auction/{id} itself before querying that auction's items, rather than
   reusing whatever fetch_csrf_token() got from /auctions; that same
   per-auction token does stay valid across all of that auction's own
   paginated POSTs, so it's fetched once per auction, not once per page.
2. POST /auctions/upcoming, form-encoded with csrf_token and a
   datatables_ajax_data={"draw":1,"start":0,"length":N} field, returns
   clean JSON: {"recordsTotal": N, "data": [...]}. Each row (when
   non-empty) is expected to carry an auction_status/item_count field plus
   a batch_closing_end field holding a server-rendered
   "<a href=/auction/{id}>...</a>" snippet, based on this same site's
   /table/filter response shape below -- unconfirmed against a real
   non-empty row since none exist right now, so fetch_upcoming_auctions()
   stays defensive (regex for the href, skip the row if it's not there)
   rather than assuming a fixed structure.
3. POST /table/filter, form-encoded (uri=/auction/{id}, csrf_token, sort,
   sort_direction, page, rows, filter={}), returns JSON with the item
   table's HTML embedded as a string under a per-page key (e.g.
   "_auction_49"). Two fields matter beyond the obvious: "sort" has to be
   a column the server actually recognizes (an invented one like
   "deed_number" gets a 403, not a sort-ignored 200) so this always sends
   the page's own default, "batch_parent.first_batch_close"; "last_page"
   is required in the POST body at all (omitting it gets a 500) but
   rejects an unrealistic placeholder like 999999 (gets a 403) -- so
   fetch_auction_page_state() seeds it from whatever real value the plain
   /auction/{id} page already carries (computed there for that page's own
   default row count) rather than guessing, and every page after the
   first uses the real value the previous response returned instead.
   Each item is a "{id}.summary" row -- columns mapped from <thead> the
   same defensive way govease_scraper.py's parse_county_grid does, since
   label/order isn't guaranteed stable -- plus a same-numbered
   "{id}.message" row holding a special_message/removal_message div.
   "REDEEMED" was the only removal reason seen live (169 of the 715);
   SOLD/CANCELED/WITHDRAWN are documented in the site's own UI copy but
   unconfirmed, so INACTIVE_KEYWORDS below covers all four rather than
   just the one actually observed.
4. GET /auction/{auction_id}/{item_id}/item_details, sent by the page's own
   toggle_item_details() JS (see global_*.js), returns the real per-parcel
   detail: APN, Address, City, Postal Code, Land Value, Improvements, Total
   Assessed Value, Property Description, Assessee -- confirmed directly
   against a live item, a simple <table><tr><td class=label>Label:</td>
   <td>value</td></tr></table> layout, parsed by label text rather than
   position since the row order/count isn't guaranteed either.

Every endpoint above also requires browser-like headers, but not the same
ones throughout: the plain page loads (steps 1 above) need an ordinary
Accept: text/html -- sending the AJAX Accept/X-Requested-With pair used
for steps 2-4 here makes the server treat even a GET /auctions as an XHR
and return a small JSON redirect stub with no CSRF token in it at all,
confirmed live. A bare requests default User-Agent, on any endpoint, gets
a 403 outright.
"""

import re
import sqlite3
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

import combined_db

DB_PATH = "mytaxsale_properties.db"

# (county, subdomain) -- see module docstring for why Kern is excluded and
# Orange isn't a double-count against its own bid4assets.com storefront.
COUNTIES = [
    ("San Diego", "sdttc"),
    ("Sacramento", "sacramento"),
    ("San Francisco", "sanfrancisco"),
    ("San Bernardino", "sbcounty"),
    ("San Mateo", "sanmateo"),
    ("Orange", "octaxauction"),
    ("Solano", "solano"),
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# The initial page load needs a plain browser Accept header -- sending the
# AJAX one here (confirmed live) makes the server treat it as an XHR and
# return a small JSON redirect payload instead of the real HTML page, with
# no csrf_token in it at all. The AJAX endpoints below want the opposite:
# without X-Requested-With/Accept: application/json, /table/filter and
# /auction/.../item_details 403 outright (also confirmed live) rather than
# returning their JSON.
PAGE_HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
AJAX_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}

CSRF_PATTERN = re.compile(r'<meta name="csrf_token" content="([^"]+)"')
AUCTION_HREF_PATTERN = re.compile(r'href="/auction/(\d+)"')

# "REDEEMED" is the only one confirmed live (see module docstring); the
# other three are the site's own documented removal reasons for an item
# no longer up for sale, included defensively even though unobserved.
INACTIVE_KEYWORDS = ("redeem", "sold", "cancel", "withdraw")

ROWS_PER_PAGE = 100


def base_url(subdomain: str) -> str:
    return f"https://{subdomain}.mytaxsale.com"


def fetch_csrf_token(session: requests.Session, subdomain: str) -> str | None:
    resp = session.get(f"{base_url(subdomain)}/auctions", headers=PAGE_HEADERS, timeout=30)
    if resp.status_code != 200:
        return None
    match = CSRF_PATTERN.search(resp.text)
    return match.group(1) if match else None


def fetch_upcoming_auctions(session: requests.Session, subdomain: str, csrf_token: str) -> list[dict]:
    resp = session.post(
        f"{base_url(subdomain)}/auctions/upcoming",
        headers=AJAX_HEADERS, timeout=30,
        data={
            "csrf_token": csrf_token,
            "datatables_ajax_data": '{"draw":1,"start":0,"length":50}',
        },
    )
    resp.raise_for_status()
    payload = resp.json()

    auctions = []
    for row in payload.get("data", []):
        match = AUCTION_HREF_PATTERN.search(row.get("batch_closing_end", ""))
        if not match:
            continue
        auctions.append({
            "auction_id": match.group(1),
            "item_count": row.get("item_count"),
            "status": row.get("auction_status"),
        })
    return auctions


def parse_auction_items(html: str, headers_order: list[str] | None = None) -> tuple[list[dict], list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="results")
    if table is None:
        return [], headers_order or []

    thead = table.find("thead")
    if thead is not None and thead.find_all("th"):
        headers_order = [th.get_text(strip=True).rstrip(":") or "_" for th in thead.find_all("tr")[-1].find_all("th")]

    if not headers_order:
        return [], []

    col_index = {label: i for i, label in enumerate(headers_order) if label != "_"}

    items = []
    for row in table.find_all("tr", id=re.compile(r"^\d+\.summary$")):
        item_id = row["id"].split(".")[0]

        message = soup.find(id=f"removal_message.{item_id}") or soup.find(id=f"special_message.{item_id}")
        status_note = message.get_text(strip=True) if message else None
        if status_note and any(keyword in status_note.lower() for keyword in INACTIVE_KEYWORDS):
            continue

        cells = row.find_all("td")

        def cell_text(label: str) -> str | None:
            idx = col_index.get(label)
            if idx is None or idx >= len(cells):
                return None
            text = cells[idx].get_text(" ", strip=True)
            return text or None

        items.append({
            "item_id": item_id,
            "deed_number": cell_text("ID#"),
            "opening_bid": cell_text("Opening Bid"),
        })

    return items, headers_order


def fetch_auction_item_page(
    session: requests.Session, subdomain: str, csrf_token: str, auction_id: str,
    page: int, known_last_page: int,
) -> tuple[str, int]:
    headers = {**AJAX_HEADERS, "Referer": f"{base_url(subdomain)}/auction/{auction_id}"}
    resp = session.post(
        f"{base_url(subdomain)}/table/filter",
        headers=headers, timeout=30,
        data={
            "csrf_token": csrf_token,
            "uri": f"/auction/{auction_id}",
            # The page's own default sort field -- confirmed live that an
            # arbitrary column name here (e.g. "deed_number") gets a bare
            # 403 back instead of a sorted result, so this doesn't guess.
            "sort": "batch_parent.first_batch_close",
            "sort_direction": "asc",
            "page": str(page),
            # Required in the POST body -- confirmed live that omitting it
            # (this form field is normally kept in sync client-side by the
            # page's own JS, which a plain POST here doesn't run) gets a
            # 500 back, and an unrealistic placeholder (e.g. 999999, to
            # avoid needing a real value up front) gets a 403 -- some
            # bound/sanity check on the field itself, not just presence.
            # fetch_auction_items() seeds this from the value the plain
            # /auction/{id} page itself already carries (computed for that
            # page's own default row count) rather than guessing; it's
            # fine that this doesn't match ROWS_PER_PAGE's own page count
            # exactly -- confirmed live the response just recalculates it
            # for the real rows value below, same as every later page's
            # real value feeding the next call.
            "last_page": str(known_last_page),
            "rows": str(ROWS_PER_PAGE),
            "filter": "{}",
        },
    )
    resp.raise_for_status()
    payload = resp.json()
    html = payload.get(f"_auction_{auction_id}", "")
    last_page_match = re.search(r'name="last_page"\s+value="(\d+)"', html)
    last_page = int(last_page_match.group(1)) if last_page_match else page
    return html, last_page


def fetch_auction_page_state(session: requests.Session, subdomain: str, auction_id: str) -> tuple[str, int]:
    """
    The CSRF token isn't a single per-session value -- confirmed live it's
    reissued on every page render, and a token from one page (e.g.
    /auctions) gets a flat 403 when used against a *different* page's own
    /table/filter (e.g. /auction/49's). So this always grabs a fresh token
    from the specific auction page about to be queried, rather than
    reusing the one main() already has from /auctions. That same token
    does stay valid across this auction's own multiple paginated POSTs
    (confirmed live), so it's only fetched once per auction, not once per
    page.
    """
    resp = session.get(f"{base_url(subdomain)}/auction/{auction_id}", headers=PAGE_HEADERS, timeout=30)
    resp.raise_for_status()
    csrf_match = CSRF_PATTERN.search(resp.text)
    last_page_match = re.search(r'name="last_page"\s+value="(\d+)"', resp.text)
    return (
        csrf_match.group(1) if csrf_match else "",
        int(last_page_match.group(1)) if last_page_match else 1,
    )


def fetch_auction_items(session: requests.Session, subdomain: str, auction_id: str) -> list[dict]:
    auction_csrf_token, last_page = fetch_auction_page_state(session, subdomain, auction_id)

    all_items = []
    headers_order = None
    page = 1
    while page <= last_page:
        html, last_page = fetch_auction_item_page(
            session, subdomain, auction_csrf_token, auction_id, page, last_page,
        )
        items, headers_order = parse_auction_items(html, headers_order)
        all_items.extend(items)
        page += 1
    return all_items


def fetch_item_details(session: requests.Session, subdomain: str, auction_id: str, item_id: str) -> dict:
    headers = {**AJAX_HEADERS, "Referer": f"{base_url(subdomain)}/auction/{auction_id}"}
    resp = session.get(
        f"{base_url(subdomain)}/auction/{auction_id}/{item_id}/item_details",
        headers=headers, timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    html = payload.get(f"item_details.{item_id}", "")
    soup = BeautifulSoup(html, "html.parser")

    fields = {}
    for label_cell in soup.find_all("td", class_="label"):
        label = label_cell.get_text(strip=True).rstrip(":")
        value_cell = label_cell.find_next_sibling("td")
        fields[label] = value_cell.get_text(" ", strip=True) if value_cell else None

    return fields


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mytaxsale_properties (
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
        CREATE UNIQUE INDEX IF NOT EXISTS idx_mytaxsale_county_account
        ON mytaxsale_properties(county, account_number)
    """)
    conn.commit()


def upsert_local(conn: sqlite3.Connection, listing: dict):
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT id FROM mytaxsale_properties WHERE county = ? AND account_number = ?",
        (listing["county"], listing["account_number"]),
    ).fetchone()

    fields = (
        listing["minimum_bid"], listing["estimated_value"], listing["address"],
        listing["description"], listing["source_url"],
    )
    if existing:
        conn.execute(
            """UPDATE mytaxsale_properties SET
                minimum_bid = ?, estimated_value = ?, address = ?, description = ?,
                source_url = ?, last_seen = ?
               WHERE county = ? AND account_number = ?""",
            fields + (now, listing["county"], listing["account_number"]),
        )
    else:
        conn.execute(
            """INSERT INTO mytaxsale_properties (
                minimum_bid, estimated_value, address, description, source_url,
                county, account_number, first_seen, last_seen
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            fields + (listing["county"], listing["account_number"], now, now),
        )
    conn.commit()


def clean_money(text: str | None) -> str | None:
    if not text:
        return None
    value = text.replace("$", "").replace(",", "").strip()
    return value or None


def main():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    combined_conn = combined_db.get_connection()
    total_listings = 0

    for county, subdomain in COUNTIES:
        session = requests.Session()
        csrf_token = fetch_csrf_token(session, subdomain)
        if not csrf_token:
            print(f"  {county}: could not fetch CSRF token, skipping")
            continue

        auctions = fetch_upcoming_auctions(session, subdomain, csrf_token)
        if not auctions:
            print(f"  {county}: no upcoming auctions")
            continue

        for auction in auctions:
            items = fetch_auction_items(session, subdomain, auction["auction_id"])
            print(f"  {county} (auction {auction['auction_id']}): {len(items)} available item(s)")

            for item in items:
                details = fetch_item_details(session, subdomain, auction["auction_id"], item["item_id"])
                account_number = details.get("APN") or item["deed_number"]
                if not account_number:
                    continue

                listing = {
                    "county": county,
                    "account_number": account_number,
                    "minimum_bid": clean_money(item["opening_bid"]),
                    "estimated_value": clean_money(details.get("Total Assessed Value")),
                    "address": details.get("Address"),
                    "description": details.get("Property Description"),
                    "source_url": f"{base_url(subdomain)}/auction/{auction['auction_id']}/{item['item_id']}",
                }

                upsert_local(conn, listing)
                combined_db.upsert_listing(
                    combined_conn,
                    county=listing["county"],
                    account_number=listing["account_number"],
                    precinct=None,
                    minimum_bid=listing["minimum_bid"],
                    estimated_value=listing["estimated_value"],
                    address=listing["address"],
                    description=listing["description"],
                    status="Active",
                    source="mytaxsale.com",
                    source_url=listing["source_url"],
                    state="CA",
                )
                total_listings += 1

    combined_conn.close()
    conn.close()
    print(f"\n{total_listings} listing(s) stored into {DB_PATH}")


if __name__ == "__main__":
    main()
