"""
GovLandScout - PublicSurplus Real Estate Scraper (Texas)

publicsurplus.com/sms/state,tx/browse/cataucs?catid=15 lists real estate
being auctioned by Texas government sellers -- a distinct channel from
the tax-trustee sites (properties end up here via surplus disposal, not
tax delinquency). As of this scraper being written, the category has
zero active listings for Texas ("No auctions found"), so like
houston_scraper.py there's no real listing to verify a structured parser
against yet. This deliberately stays conservative: it detects the
"No auctions found" placeholder and otherwise captures each row's link
and raw text without guessing at a specific column layout (price, time
left, etc.) that might be wrong the first time real data shows up.
Revisit this once an actual listing appears.
"""

import hashlib

import requests
from bs4 import BeautifulSoup

import combined_db

URL = "https://www.publicsurplus.com/sms/state,tx/browse/cataucs?catid=15"

HEADERS = {
    "User-Agent": "GovLandScout-SchoolProject/1.0 (contact: your-email@example.com)"
}


def fetch_page_html() -> str:
    resp = requests.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_listings(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")

    if soup.find(id="noAuctionsFound"):
        return []

    listings = []
    for row in soup.select("table#auctionTableView tbody tr"):
        text = row.get_text(" ", strip=True)
        if not text:
            continue

        link = row.find("a", href=True)
        href = link["href"] if link else None
        if href and href.startswith("/"):
            href = "https://www.publicsurplus.com" + href

        listings.append({
            "text": text,
            "source_url": href,
            "raw_hash": hashlib.sha256(text.encode()).hexdigest(),
        })

    return listings


def main():
    print(f"Fetching {URL} ...")
    listings = parse_listings(fetch_page_html())
    print(f"Found {len(listings)} active Texas real estate listing(s).")

    if not listings:
        return

    combined_conn = combined_db.get_connection()
    for listing in listings:
        combined_db.upsert_listing(
            combined_conn,
            county="State",
            account_number=listing["raw_hash"][:16],
            precinct=None,
            minimum_bid=None,
            estimated_value=None,
            address=None,
            description=listing["text"],
            status="Available",
            source="publicsurplus.com",
            source_url=listing["source_url"],
        )
    combined_conn.close()
    print(f"Stored {len(listings)} listings.")


if __name__ == "__main__":
    main()
