"""
GovLandScout - Collin County Historical Sale Archive (internal, hidden)

collin_scraper.py feeds the public site: it filters Collin County's
constable sale notices down to upcoming, non-cancelled sales, matching
what that site's own "Current Sales" section would show if populated
(see its docstring). This script is the opposite -- it walks the exact
same accordion but keeps every row that page's own "Archived Sales
Notice" section exposes: past sales, cancelled ones, all of it.

Deliberately NOT written to the public `listings` table. Writes instead
to historical_listings (see combined_db.py's init_db), a separate table
web.py never queries -- this is a research archive, not a live listing,
and most rows here describe a sale that already happened or was called
off, not something actually available to bid on today.

Reuses collin_scraper.py's own parsing (parse_listing_page,
fetch_pdf_details) rather than duplicating it -- the two scripts differ
only in which rows they keep and which table they write to, not in how
they read the page or the per-property PDFs.
"""

import time

import requests

import collin_scraper
import combined_db


def main():
    session = requests.Session()
    resp = session.get(collin_scraper.LISTING_PAGE_URL, headers=collin_scraper.HEADERS, timeout=30)
    resp.raise_for_status()

    listings = collin_scraper.parse_listing_page(resp.text)
    print(f"Found {len(listings)} historical record(s) (past, upcoming, and cancelled).")

    combined_conn = combined_db.get_connection()
    stored = 0

    for i, listing in enumerate(listings):
        if i > 0:
            time.sleep(collin_scraper.REQUEST_DELAY_SECONDS)

        details = (
            collin_scraper.fetch_pdf_details(session, listing["pdf_url"])
            if listing["pdf_url"] else {}
        )
        cause_number = details.get("cause_number")
        account_number = (
            f"{listing['account_number']}_{cause_number}" if cause_number else listing["account_number"]
        )

        status_note = "CANCELLED" if listing["is_cancelled"] else f"Sale scheduled {listing['sale_date']}"
        description_parts = [listing["legal_description"], status_note]

        combined_db.upsert_historical_listing(
            combined_conn,
            county="Collin",
            account_number=account_number,
            precinct=listing["precinct"],
            sale_date=listing["sale_date"],
            is_cancelled=listing["is_cancelled"],
            minimum_bid=details.get("minimum_bid"),
            address=listing["address"],
            description=" -- ".join(p for p in description_parts if p),
            source="collincountytx.gov",
            source_url=listing["pdf_url"],
        )
        stored += 1

    combined_conn.close()
    print(f"\n{stored} historical record(s) stored into historical_listings (hidden from the public site).")


if __name__ == "__main__":
    main()
