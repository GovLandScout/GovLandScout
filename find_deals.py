"""
GovLandScout - Deal ranking

Reads tax_sales.db (populated by hctax_scraper.py) and ranks listings
by how far the minimum bid sits below the adjudged value. Listings
missing pricing data are reported separately rather than dropped.
"""

import sqlite3

DB_PATH = "tax_sales.db"


def fetch_priced_listings(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT precinct, account_number, minimum_bid, adjudged_value, legal_description
        FROM tax_sale_listings
        WHERE minimum_bid IS NOT NULL AND adjudged_value IS NOT NULL
    """).fetchall()

    listings = []
    for precinct, account_number, minimum_bid, adjudged_value, description in rows:
        min_bid = float(minimum_bid)
        adj_value = float(adjudged_value)
        if adj_value <= 0:
            continue  # avoid divide-by-zero on bad data
        equity = adj_value - min_bid
        listings.append({
            "precinct": precinct,
            "account_number": account_number,
            "minimum_bid": min_bid,
            "adjudged_value": adj_value,
            "equity": equity,
            "equity_pct": equity / adj_value,
            "legal_description": description or "",
        })
    return listings


def fetch_unpriced_count(conn: sqlite3.Connection) -> int:
    return conn.execute("""
        SELECT COUNT(*) FROM tax_sale_listings
        WHERE minimum_bid IS NULL OR adjudged_value IS NULL
    """).fetchone()[0]


def main(top_n: int = 20):
    conn = sqlite3.connect(DB_PATH)

    listings = fetch_priced_listings(conn)
    listings.sort(key=lambda l: l["equity_pct"], reverse=True)

    unpriced_count = fetch_unpriced_count(conn)

    print(f"{len(listings)} listings have both a minimum bid and adjudged value.")
    print(f"{unpriced_count} listings are missing pricing data and are excluded from ranking below.\n")

    print(f"Top {min(top_n, len(listings))} deals by equity (adjudged value - minimum bid):\n")
    for l in listings[:top_n]:
        print(
            f"  {l['precinct']:<12} Acct#{l['account_number']:<15} "
            f"Min Bid: ${l['minimum_bid']:>12,.2f}  "
            f"Adjudged: ${l['adjudged_value']:>12,.2f}  "
            f"Equity: ${l['equity']:>12,.2f} ({l['equity_pct']:.0%})"
        )
        print(f"      {l['legal_description'][:100]}")

    conn.close()


if __name__ == "__main__":
    main()
