"""
GovLandScout - Texas Veterans Land Board Scraper

The VLB resells land tracts that veterans/military members defaulted on
VLB land loans for -- a genuinely different kind of "government sold
property" than tax-delinquency sales, but public and for sale all the
same. Listed at glo.texas.gov/veterans/land-sale/public, currently ~23
tracts across a mix of Texas counties (several of which -- Comal,
Bandera, Medina -- have no other listings in this project at all).

Each tract has a summary row (county, acreage, price) on the list page
and a detail page with the legal description, driving directions, and
sometimes a street address. Address is inconsistent -- present for
maybe half the tracts, since this is often raw/rural land that was
never assigned a formal address -- so this deliberately leaves
latitude/longitude unset and lets geocode_backfill.py pick up whichever
addresses did come through, same as every other address-only source.

fetch() retries with backoff, and a single tract's detail page failing
even after that is skipped rather than allowed to crash the run -- before
this, one bad detail-page fetch (out of ~25, one request each) took the
whole script down with it, losing every tract from that point on, not
just the one that failed (2026-07-30).
"""

import re
import sqlite3
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

import combined_db

LIST_URL = "https://www.glo.texas.gov/veterans/land-sale/public"
DETAIL_URL = "https://www.glo.texas.gov/veterans/land-sale/public/tract/{tract}"
DB_PATH = "glo_veterans_land.db"
FETCH_RETRIES = 4
RETRY_BACKOFF_SECONDS = 5  # doubles each retry: 5s, 10s, 20s, 40s
# One tract's detail page timing out (glo.texas.gov, 2026-07-30) used to
# take the whole script down with it -- fetch() had no retry, and main()'s
# per-tract loop had no try/except, so an unhandled exception on tract N
# lost every tract from N onward too, not just that one.

HEADERS = {
    "User-Agent": "GovLandScout-SchoolProject/1.0 (contact: your-email@example.com)"
}

ADDRESS_PATTERN = re.compile(r"Address:\s*([^.]+(?:TX)?\s*\d{5})", re.IGNORECASE)
# Most tracts include this in plain decimal degrees (~65% of listings);
# the rest either give no coordinates at all or a degrees/minutes/seconds
# "Lat/Long" format instead, which isn't worth the parsing complexity for
# a source this small -- those fall back to whatever address is present,
# same as any other listing (see geocode_backfill.py).
GPS_PATTERN = re.compile(r"GPS Coordinates?:\s*(-?\d+\.\d+)°?,?\s*(-?\d+\.\d+)°?")


def fetch(url: str) -> str:
    last_error = None
    for attempt in range(FETCH_RETRIES):
        if attempt > 0:
            time.sleep(RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)))
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            last_error = e
            print(f"  fetch failed (attempt {attempt + 1}/{FETCH_RETRIES}): {e}")
    raise last_error


GLO_ERROR_MARKER = "Unable to retrieve search results"


def fetch_list_page() -> str:
    """
    The list page's tract table is populated by a third-party search
    widget (Cludo) that can fail server-side on GLO's own end -- when it
    does, the page still comes back 200 OK, just with "Unable to retrieve
    search results" where the table should be. fetch()'s retry only
    covers HTTP-level failures (timeouts, 5xx, ...) and wouldn't catch
    this at all, so it's checked for separately here. First seen
    2026-07-30, a few hours after this same page loaded 25 tracts fine.
    """
    last_html = None
    for attempt in range(FETCH_RETRIES):
        if attempt > 0:
            time.sleep(RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)))
        html = fetch(LIST_URL)
        if GLO_ERROR_MARKER not in html:
            return html
        last_html = html
        print(f'  GLO\'s own site returned "{GLO_ERROR_MARKER}" (attempt {attempt + 1}/{FETCH_RETRIES})')
    raise RuntimeError(
        f'GLO\'s site kept returning "{GLO_ERROR_MARKER}" after {FETCH_RETRIES} attempts -- '
        "their search backend looks down, not a bug on this end"
    )


def parse_tract_list(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="stack")
    if not table:
        return []

    tracts = []
    for row in table.find("tbody").find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 6:
            continue
        tract_number = cells[1].get_text(strip=True)
        county = cells[2].get_text(strip=True)
        acreage = cells[3].get_text(strip=True)
        price = cells[4].get_text(strip=True).lstrip("$").replace(",", "")
        detail_link = cells[5].find("a")
        if not tract_number or not detail_link:
            continue
        tracts.append({
            "tract_number": tract_number,
            "county": county,
            "acreage": acreage,
            "price": price,
        })
    return tracts


def parse_tract_detail(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    fields = {}
    for heading in soup.find_all("h3"):
        label = heading.get_text(strip=True)
        value_el = heading.find_next_sibling("p")
        if value_el:
            fields[label] = value_el.get_text(" ", strip=True)

    address_match = ADDRESS_PATTERN.search(text)
    address = address_match.group(1).strip().rstrip(".") if address_match else None

    gps_match = GPS_PATTERN.search(text)
    latitude = float(gps_match.group(1)) if gps_match else None
    longitude = float(gps_match.group(2)) if gps_match else None

    legal_description = fields.get("Legal Description", "")
    location = fields.get("Location", "")
    description = " ".join(part for part in [legal_description, location] if part)

    return {"address": address, "description": description, "latitude": latitude, "longitude": longitude}


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS glo_veterans_land (
            tract_number TEXT PRIMARY KEY,
            county TEXT,
            acreage TEXT,
            price TEXT,
            address TEXT,
            description TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    conn.commit()


def upsert_listing(conn: sqlite3.Connection, tract: dict):
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT tract_number FROM glo_veterans_land WHERE tract_number = ?",
        (tract["tract_number"],),
    ).fetchone()

    if existing:
        conn.execute(
            """
            UPDATE glo_veterans_land
            SET county = ?, acreage = ?, price = ?, address = ?, description = ?, last_seen = ?
            WHERE tract_number = ?
            """,
            (tract["county"], tract["acreage"], tract["price"], tract["address"],
             tract["description"], now, tract["tract_number"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO glo_veterans_land
            (tract_number, county, acreage, price, address, description, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (tract["tract_number"], tract["county"], tract["acreage"], tract["price"],
             tract["address"], tract["description"], now, now),
        )
    conn.commit()


def main():
    print(f"Fetching {LIST_URL} ...")
    tracts = parse_tract_list(fetch_list_page())
    print(f"Found {len(tracts)} tract(s) for sale.")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    combined_conn = combined_db.get_connection()

    addressed_count = 0
    geocoded_count = 0
    skipped_count = 0
    for tract in tracts:
        detail_url = DETAIL_URL.format(tract=tract["tract_number"])
        try:
            detail = parse_tract_detail(fetch(detail_url))
        except requests.RequestException as e:
            # One tract's detail page failing shouldn't cost the other 24
            # their update too -- this tract just keeps whatever it had
            # from the last successful run instead of getting refreshed.
            print(f"  giving up on tract {tract['tract_number']} after {FETCH_RETRIES} attempts ({e}) -- skipping it")
            skipped_count += 1
            continue
        tract["address"] = detail["address"]
        tract["description"] = f"{tract['acreage']} -- {detail['description']}"
        if tract["address"]:
            addressed_count += 1
        if detail["latitude"] is not None:
            geocoded_count += 1

        upsert_listing(conn, tract)

        combined_db.upsert_listing(
            combined_conn,
            county=tract["county"],
            account_number=tract["tract_number"],
            precinct=None,
            minimum_bid=tract["price"],
            estimated_value=None,
            address=tract["address"],
            description=tract["description"],
            status="Available",
            source="glo.texas.gov",
            source_url=detail_url,
            latitude=detail["latitude"],
            longitude=detail["longitude"],
        )

    combined_conn.close()
    stored_count = len(tracts) - skipped_count
    skip_note = f", {skipped_count} skipped (detail page never loaded)" if skipped_count else ""
    print(
        f"Stored {stored_count} listings into {DB_PATH} "
        f"({addressed_count} with a street address, {geocoded_count} with GPS coordinates{skip_note})."
    )
    conn.close()


if __name__ == "__main__":
    main()
