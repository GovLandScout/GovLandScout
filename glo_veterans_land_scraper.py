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
"""

import re
import sqlite3
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

import combined_db

LIST_URL = "https://www.glo.texas.gov/veterans/land-sale/public"
DETAIL_URL = "https://www.glo.texas.gov/veterans/land-sale/public/tract/{tract}"
DB_PATH = "glo_veterans_land.db"

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
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


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
    tracts = parse_tract_list(fetch(LIST_URL))
    print(f"Found {len(tracts)} tract(s) for sale.")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    combined_conn = combined_db.get_connection()

    addressed_count = 0
    geocoded_count = 0
    for tract in tracts:
        detail_url = DETAIL_URL.format(tract=tract["tract_number"])
        detail = parse_tract_detail(fetch(detail_url))
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
    print(
        f"Stored {len(tracts)} listings into {DB_PATH} "
        f"({addressed_count} with a street address, {geocoded_count} with GPS coordinates)."
    )
    conn.close()


if __name__ == "__main__":
    main()
