"""
GovLandScout - PublicSurplus Real Estate Scraper (Texas, California)

publicsurplus.com/sms/state,{state}/browse/cataucs?catid=15 lists real
estate being auctioned by that state's government sellers -- a distinct
channel from the tax-trustee sites (properties end up here via surplus
disposal, not tax delinquency). As of this scraper being written, the
category has zero active listings for either Texas or California ("No
auctions found" for both, confirmed directly against the live California
URL before adding it here -- same category, same site, so nothing about
the parser itself needed to change), so like houston_scraper.py there's
no real listing to verify a structured parser against yet. This
deliberately stays conservative: it detects the "No auctions found"
placeholder and otherwise captures each row's link and raw text without
guessing at a specific column layout (price, time left, etc.) that might
be wrong the first time real data shows up. Revisit this once an actual
listing appears, in either state.

California was added specifically because its own tax-defaulted property
auctions (bid4assets_scraper.py, govease_scraper.py) are seasonal -- most
counties hold exactly one a year -- so a second, non-tax, always-worth-
checking channel is a real (if currently empty) hedge against that, not
carried over from Texas just for consistency's sake.
"""

import hashlib

import requests
from bs4 import BeautifulSoup

import combined_db

TARGET_STATES = ("TX", "CA")

HEADERS = {
    "User-Agent": "GovLandScout-SchoolProject/1.0 (contact: your-email@example.com)"
}


def listing_url(state: str) -> str:
    return f"https://www.publicsurplus.com/sms/state,{state.lower()}/browse/cataucs?catid=15"


def fetch_page_html(state: str) -> str:
    resp = requests.get(listing_url(state), headers=HEADERS, timeout=30)
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
    combined_conn = combined_db.get_connection()
    total_stored = 0

    for state in TARGET_STATES:
        url = listing_url(state)
        print(f"Fetching {url} ...")
        listings = parse_listings(fetch_page_html(state))
        print(f"Found {len(listings)} active {state} real estate listing(s).")

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
                state=state,
            )
        total_stored += len(listings)

    combined_conn.close()
    print(f"Stored {total_stored} listing(s) across {TARGET_STATES}.")


if __name__ == "__main__":
    main()
