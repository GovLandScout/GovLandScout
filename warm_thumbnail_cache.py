"""
GovLandScout - Thumbnail cache pre-warmer (manual/occasional, NOT in the daily pipeline)

/api/thumbnail in web.py caches each listing's satellite thumbnail on
first request, but that means the very first visitor to view any given
listing still pays Esri's ~500ms round-trip. This fetches every current
listing's thumbnail once up front (in parallel, since one at a time would
take ~30+ minutes for ~4,000 listings) so the cache is already warm
before real users show up.

Deliberately kept out of run_daily_scrapers.py: a listing's coordinates
essentially never change once geocoded, so there's nothing new to warm
on a daily schedule -- just re-run this by hand after a scrape adds a
meaningful number of newly-geocoded listings.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import combined_db
from web import satellite_thumbnail_url, thumbnail_cache_path

WORKERS = 10


def fetch_one(latitude: float, longitude: float) -> bool:
    """Returns True if this call actually hit Esri (cache miss), False if already cached."""
    cache_path = thumbnail_cache_path(latitude, longitude)
    if cache_path.exists():
        return False

    resp = requests.get(satellite_thumbnail_url(latitude, longitude), timeout=15)
    resp.raise_for_status()
    cache_path.write_bytes(resp.content)
    return True


def main():
    conn = combined_db.get_connection()
    rows = conn.execute(
        "SELECT DISTINCT latitude, longitude FROM listings WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
    ).fetchall()
    conn.close()

    print(f"{len(rows)} distinct coordinate(s) to warm.")

    fetched = 0
    already_cached = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(fetch_one, lat, lon): (lat, lon) for lat, lon in rows}
        for i, future in enumerate(as_completed(futures), 1):
            try:
                if future.result():
                    fetched += 1
                else:
                    already_cached += 1
            except Exception as e:
                failed += 1
                print(f"  failed for {futures[future]}: {e}")

            if i % 200 == 0:
                print(f"  {i}/{len(rows)} processed ...")

    print(f"Done. Fetched {fetched}, already cached {already_cached}, failed {failed}.")


if __name__ == "__main__":
    main()
