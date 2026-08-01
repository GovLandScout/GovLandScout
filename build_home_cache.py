"""
GovLandScout - Home page cache builder

Run as the last step of run_daily_scrapers.py, after geocode_backfill.py
(see run_daily_scrapers.py) so the cache reflects that day's freshest
data, geocoding included. Calls web.py's own compute_home_page_payload()
-- the exact same fetch/transform/serialize logic the home page would
otherwise run on every single request -- once, and stores the result via
combined_db.write_home_page_cache() for deals_page() to read back
instead of recomputing.

Rebuilding this from scratch (fetching ~4,500 listings, running the
per-listing search-text/image-url transform, JSON-serializing the result)
was measured taking the home page from ~5s to ~18s to respond. The
underlying data only changes once a day when the scrape pipeline runs, so
there was never a reason to pay that cost on every request instead of
once here.
"""

import combined_db
import web


def main():
    print("Computing home page payload ...")
    payload = web.compute_home_page_payload()
    print(
        f"{payload['total_count']} listings ({payload['priced_count']} priced), "
        f"{len(payload['listings_json']):,} bytes of JSON"
    )

    conn = combined_db.get_connection()
    combined_db.write_home_page_cache(
        conn,
        listings_json=payload["listings_json"],
        value_min=payload["value_min"],
        value_max=payload["value_max"],
        total_count=payload["total_count"],
        priced_count=payload["priced_count"],
    )
    conn.close()

    print("Cached.")


if __name__ == "__main__":
    main()
