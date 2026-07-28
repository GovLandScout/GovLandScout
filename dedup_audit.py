"""
GovLandScout - Duplicate listing audit

Reports likely duplicate properties in the combined listings table. Run by
hand (not part of the daily scrape pipeline) -- this only reports
candidates for a human to review, it never deletes or merges anything,
since telling a coincidental match apart from a real duplicate sometimes
needs judgment a script can't safely automate.

Two different checks, because a naive "same address" match is too noisy
on its own -- rural vacant-land parcels routinely share a road name with
no house number (one run found 38 distinct Jasper County lots all
addressed "OFF READMAN RD, WOODVILLE, TX 75979"), which would drown out
every real duplicate in false positives.

1. Stale composite-key resurfacing: several scrapers (MVBA, PBFCM,
   realauction_scraper.py) key account_number as "<real_id>_<suit/cause
   number>" so a property that doesn't sell and gets re-noticed the next
   month keeps its old row (a different suit/cause number = a different
   key = combined_db.upsert_listing's ON CONFLICT never matches it) while
   also creating a new one. Flags any base id recurring under multiple
   suffixes within the same county+source.

2. Cross-source same-address: the actual risk this project has had to
   design around directly -- see realauction_scraper.py's docstring on
   why it only covers Travis/Caldwell and not the ~20 other counties that
   platform also hosts, to avoid exactly this. Restricted to addresses
   starting with a real house number (regex ^[0-9]) to sidestep the rural
   road-name collisions that make unrestricted address matching useless.
"""

import combined_db


def find_stale_composite_keys(conn: combined_db.PgConnection) -> list[tuple]:
    return conn.execute("""
        SELECT county, source, split_part(account_number, '_', 1) AS base,
               COUNT(*) AS n, array_agg(account_number ORDER BY account_number) AS accounts
        FROM listings
        WHERE position('_' in account_number) > 0
        GROUP BY county, source, base
        HAVING COUNT(*) > 1
        ORDER BY n DESC
    """).fetchall()


def find_cross_source_addresses(conn: combined_db.PgConnection) -> list[tuple]:
    return conn.execute("""
        SELECT county, address, COUNT(DISTINCT account_number) AS n,
               array_agg(DISTINCT source ORDER BY source) AS sources,
               array_agg(account_number ORDER BY account_number) AS accounts
        FROM listings
        WHERE address ~ '^[0-9]'
        GROUP BY county, address
        HAVING COUNT(DISTINCT source) > 1
        ORDER BY n DESC
    """).fetchall()


def main():
    conn = combined_db.get_connection()

    stale_keys = find_stale_composite_keys(conn)
    print(f"=== Stale composite-key resurfacing: {len(stale_keys)} candidate group(s) ===")
    for county, source, base, n, accounts in stale_keys:
        print(f"  {county} / {source} / base id {base!r}: {n} rows -- {accounts}")
    if not stale_keys:
        print("  none found")

    print()

    cross_source = find_cross_source_addresses(conn)
    print(f"=== Cross-source same-address matches: {len(cross_source)} candidate group(s) ===")
    for county, address, n, sources, accounts in cross_source:
        print(f"  {county} / {address!r}: {n} accounts across sources {sources} -- {accounts}")
    if not cross_source:
        print("  none found")

    conn.close()


if __name__ == "__main__":
    main()
