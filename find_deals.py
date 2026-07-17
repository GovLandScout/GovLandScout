"""
GovLandScout - Deal ranking

Reads govlandscout.db's combined listings table (populated by both
hctax_scraper.py and lgbs_scraper.py via combined_db.py) and ranks
listings across all counties by how far the minimum bid sits below the
estimated value. Listings missing pricing data are reported separately
rather than dropped.
"""

import sqlite3
from urllib.parse import quote

import combined_db

DB_PATH = combined_db.DB_PATH


def build_maps_url(address: str | None, latitude: float | None, longitude: float | None) -> str | None:
    """Prefer exact coordinates when available; fall back to an address search."""
    if latitude is not None and longitude is not None:
        return f"https://www.google.com/maps?q={latitude},{longitude}"
    if address:
        return f"https://www.google.com/maps/search/?api=1&query={quote(address)}"
    return None


def fetch_priced_listings(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT county, precinct, account_number, minimum_bid, estimated_value, address,
               description, source_url, latitude, longitude
        FROM listings
        WHERE minimum_bid IS NOT NULL AND estimated_value IS NOT NULL
    """).fetchall()

    listings = []
    for (county, precinct, account_number, minimum_bid, estimated_value, address,
         description, source_url, latitude, longitude) in rows:
        min_bid = float(minimum_bid)
        est_value = float(estimated_value)
        if est_value <= 0:
            continue  # avoid divide-by-zero on bad data
        if min_bid <= 0:
            continue  # a $0 minimum bid means "not yet set" (seen on future
            # sale listings), not a real bid floor -- treat as unpriced
        equity = est_value - min_bid
        listings.append({
            "county": county,
            "precinct": precinct or "",
            "account_number": account_number,
            "minimum_bid": min_bid,
            "estimated_value": est_value,
            "equity": equity,
            "equity_pct": equity / est_value,
            "address": address or "",
            "description": description or "",
            "source_url": source_url,
            "maps_url": build_maps_url(address, latitude, longitude),
        })
    return listings


def fetch_unpriced_count(conn: sqlite3.Connection) -> int:
    return conn.execute("""
        SELECT COUNT(*) FROM listings
        WHERE minimum_bid IS NULL OR estimated_value IS NULL
           OR CAST(minimum_bid AS REAL) <= 0 OR CAST(estimated_value AS REAL) <= 0
    """).fetchone()[0]


def main(top_n: int = 20):
    conn = combined_db.get_connection()

    listings = fetch_priced_listings(conn)
    listings.sort(key=lambda l: l["equity_pct"], reverse=True)

    unpriced_count = fetch_unpriced_count(conn)

    print(f"{len(listings)} listings have both a minimum bid and estimated value.")
    print(f"{unpriced_count} listings are missing pricing data and are excluded from ranking below.\n")

    print(f"Top {min(top_n, len(listings))} deals by equity (estimated value - minimum bid):\n")
    for l in listings[:top_n]:
        print(
            f"  {l['county']:<8} {l['precinct']:<12} Acct#{l['account_number']:<20} "
            f"Min Bid: ${l['minimum_bid']:>12,.2f}  "
            f"Est. Value: ${l['estimated_value']:>12,.2f}  "
            f"Equity: ${l['equity']:>12,.2f} ({l['equity_pct']:.0%})"
        )
        location = l["address"] or l["description"] or "(no address or description on file)"
        print(f"      {location[:100]}")
        if l["source_url"]:
            print(f"      Listing: {l['source_url']}")
        if l["maps_url"]:
            print(f"      Map: {l['maps_url']}")

    conn.close()


if __name__ == "__main__":
    main()
