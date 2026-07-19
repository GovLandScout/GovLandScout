"""
GovLandScout - Combined listings storage

hctax_scraper.py and lgbs_scraper.py (and every other source scraper)
acquire data completely differently and keep their own detailed,
source-specific local SQLite tables. Their fetch/parse logic isn't worth
unifying -- it's too different to share usefully. What's worth unifying
is the OUTPUT: this module normalizes every county's listings into one
shared table so find_deals.py and web.py can rank/display them together.

This table lives in Postgres, not local SQLite, specifically so it
survives independently of any one process's lifetime. The site used to
re-run every scraper on every web server boot (no persistent disk on
Render's free tier) -- fine when there were 2-3 fast sources, but once
PBFCM and MVBA's slower, crawl-delay-bound scrapes were added, a full
run took 3+ minutes, long enough to blow past Render's startup timeout
and leave the live site serving whatever partial data happened to be
written when the boot got killed. Scraping now happens on its own
schedule (see .github/workflows/scrape.yml) and writes here; the web
service just connects and reads, so it starts in milliseconds regardless
of how long the last scrape took.

Individual scrapers' own per-source tables (tax_sales.db, etc.) are
untouched by this -- they're local-only, used just for that scraper's
own dedup bookkeeping, and never read by the web app.
"""

import os
from datetime import datetime, timezone

import psycopg2
import psycopg2.pool
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")

_pool: psycopg2.pool.SimpleConnectionPool | None = None


def _get_pool() -> psycopg2.pool.SimpleConnectionPool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set. Locally, put it in a .env file "
                "(RENTCAST_API_KEY-style, gitignored). On Render, set it as "
                "an environment variable in the service's dashboard."
            )
        _pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL)
    return _pool


class PgConnection:
    """
    Thin wrapper matching the sqlite3.Connection surface this codebase
    already uses (conn.execute(sql, params).fetchall(), conn.commit(),
    conn.close()) so find_deals.py, web.py, and every scraper's calls into
    this module don't need to know or care that the backing store changed.
    Translates sqlite-style '?' placeholders to psycopg2's '%s', and
    "close" returns the connection to the pool rather than tearing down
    the TCP connection -- get_connection()/conn.close() is called on
    every single web request, so reusing pooled connections instead of
    opening a fresh one each time matters for latency and for staying
    under a free-tier Postgres's concurrent connection cap.
    """

    def __init__(self, raw_conn, pool: psycopg2.pool.SimpleConnectionPool):
        self._conn = raw_conn
        self._pool = pool

    def execute(self, sql: str, params: tuple = ()):
        cur = self._conn.cursor()
        cur.execute(sql.replace("?", "%s"), params)
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        self._pool.putconn(self._conn)


def get_connection() -> PgConnection:
    pool = _get_pool()
    conn = PgConnection(pool.getconn(), pool)
    init_db(conn)
    return conn


def init_db(conn: PgConnection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            id SERIAL PRIMARY KEY,
            county TEXT NOT NULL,
            account_number TEXT NOT NULL,
            precinct TEXT,
            minimum_bid TEXT,
            estimated_value TEXT,
            address TEXT,
            description TEXT,
            status TEXT,
            source TEXT,
            source_url TEXT,
            latitude REAL,
            longitude REAL,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_county_account
        ON listings(county, account_number)
    """)
    conn.commit()


def upsert_listing(
    conn: PgConnection,
    county: str,
    account_number: str | None,
    precinct: str | None,
    minimum_bid: str | None,
    estimated_value: str | None,
    address: str | None,
    description: str | None,
    status: str | None,
    source: str,
    source_url: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
):
    """Keyed on (county, account_number) -- account numbers aren't unique across counties."""
    if not account_number:
        return  # can't track/dedupe a listing without a stable identifier

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO listings (
            county, account_number, precinct, minimum_bid, estimated_value,
            address, description, status, source, source_url, latitude,
            longitude, first_seen, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (county, account_number) DO UPDATE SET
            precinct = EXCLUDED.precinct,
            minimum_bid = EXCLUDED.minimum_bid,
            estimated_value = EXCLUDED.estimated_value,
            address = EXCLUDED.address,
            description = EXCLUDED.description,
            status = EXCLUDED.status,
            source = EXCLUDED.source,
            source_url = EXCLUDED.source_url,
            latitude = EXCLUDED.latitude,
            longitude = EXCLUDED.longitude,
            last_seen = EXCLUDED.last_seen
        """,
        (
            county, account_number, precinct, minimum_bid, estimated_value,
            address, description, status, source, source_url, latitude,
            longitude, now, now,
        ),
    )
    conn.commit()


def update_estimated_value(
    conn: PgConnection, county: str, account_number: str, estimated_value: str
):
    """
    Narrow update for backfill scripts (e.g. hcad_value_backfill.py) that
    enrich an existing listing with a value from a source other than the
    one that originally scraped it -- unlike upsert_listing, this touches
    only estimated_value and doesn't require (or overwrite) every field.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE listings SET estimated_value = ?, last_seen = ? WHERE county = ? AND account_number = ?",
        (estimated_value, now, county, account_number),
    )
    conn.commit()
