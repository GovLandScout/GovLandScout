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


def _get_live_connection(pool: psycopg2.pool.SimpleConnectionPool):
    """
    SimpleConnectionPool doesn't validate a connection before handing it
    back out on getconn() -- if Neon has dropped a connection that sat
    idle long enough server-side (confirmed 2026-08-06: bid4assets_scraper.py's
    ~54-minute idle stretch during its slow per-property HTTP phase, with
    no query against this connection the whole time), the pool still hands
    that now-dead connection right back out, and the caller's first real
    query fails outright with "SSL connection has been closed unexpectedly"
    instead of a clean reconnect. A cheap SELECT 1 catches that before any
    real query runs, and discards the dead connection (rather than
    recycling it back into the pool) so the retry actually gets a live one.
    """
    raw_conn = pool.getconn()
    try:
        with raw_conn.cursor() as cur:
            cur.execute("SELECT 1")
        return raw_conn
    except psycopg2.OperationalError:
        pool.putconn(raw_conn, close=True)
        return pool.getconn()


def get_connection() -> PgConnection:
    pool = _get_pool()
    conn = PgConnection(_get_live_connection(pool), pool)
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
    # Added once govease_scraper.py started covering Pennsylvania counties
    # alongside its original Texas ones (see that module's docstring) --
    # every row before that was implicitly Texas, and county names aren't
    # unique across states (both TX and PA have a Potter County), so
    # DEFAULT 'TX' backfills existing rows correctly rather than leaving
    # them ambiguous. Postgres fills existing rows from the DEFAULT in the
    # same statement, so this is safe to run against a table that already
    # has data.
    conn.execute("ALTER TABLE listings ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT 'TX'")
    # Replaces the old (county, account_number) key -- kept as a plain DROP
    # (not IF EXISTS-guarded on the old name only) so a fresh database
    # never creates the stale index in the first place.
    conn.execute("DROP INDEX IF EXISTS idx_county_account")
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_state_county_account
        ON listings(state, county, account_number)
    """)

    # One row per scraper per daily run (see run_daily_scrapers.py) --
    # raw pass/fail log. notify_bell.py reads the most recent run_started_at
    # batch of these to build the one-per-day summary the site's
    # notification bell actually displays (see bell_notifications below);
    # this table itself isn't read directly by web.py.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scrape_runs (
            id SERIAL PRIMARY KEY,
            run_started_at TEXT NOT NULL,
            scraper TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT
        )
    """)

    # One row per day, written ~1 hour after the scrape by notify_bell.py
    # (run as its own separate scheduled job) -- deliberately not just
    # "whatever's freshest in scrape_runs whenever the bell happens to be
    # viewed", so the bell shows a stable, once-a-day summary rather than
    # a state that could flicker mid-run if someone loads the site while
    # that day's scrape is still in progress.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bell_notifications (
            id SERIAL PRIMARY KEY,
            created_at TEXT NOT NULL,
            error_count INTEGER NOT NULL,
            summary TEXT NOT NULL
        )
    """)

    # A single-row cache of the home page's precomputed listings JSON (see
    # build_home_cache.py, run as the last step of run_daily_scrapers.py).
    # Rebuilding this -- fetching all ~4,500 listings, running the
    # per-listing search-text/image-url transform, and JSON-serializing
    # the result -- was measured taking the home page from ~5s to ~18s to
    # respond, on every single request, even though the underlying data
    # only changes once a day. Written once per scrape, read on every page
    # load instead of rebuilt on every page load. `id=1` enforced so
    # there's ever only one row -- writing always replaces it, there's no
    # history to keep.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS home_page_cache (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            listings_json TEXT NOT NULL,
            value_min INTEGER NOT NULL,
            value_max INTEGER NOT NULL,
            total_count INTEGER NOT NULL,
            priced_count INTEGER NOT NULL,
            generated_at TEXT NOT NULL
        )
    """)
    # A research-only archive of historical sale notices -- distinct from
    # `listings` above in both purpose and shape. `listings` represents
    # current, actionable state (one row per property, upserted in place
    # every run) and is what web.py serves publicly; this table instead
    # keeps every distinct sale event a source's own archive exposes,
    # including past and cancelled ones, for whatever county's site
    # actually publishes that kind of history (Collin County's constable
    # sale notices go back years, unlike every other current-listings-only
    # source here). Deliberately never queried by web.py or exposed on the
    # site -- purely for internal research. Keyed on (county,
    # account_number, sale_date) rather than just (county, account_number)
    # since the same property can legitimately recur at more than one sale
    # date over time, and each is a distinct historical event worth keeping,
    # not a duplicate to collapse.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS historical_listings (
            id SERIAL PRIMARY KEY,
            county TEXT NOT NULL,
            account_number TEXT NOT NULL,
            precinct TEXT,
            sale_date TEXT,
            is_cancelled BOOLEAN,
            minimum_bid TEXT,
            address TEXT,
            description TEXT,
            source TEXT,
            source_url TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_historical_county_account_date
        ON historical_listings(county, account_number, sale_date)
    """)

    conn.commit()


def record_scrape_result(
    conn: PgConnection, run_started_at: str, scraper: str, status: str, error: str | None = None
):
    conn.execute(
        "INSERT INTO scrape_runs (run_started_at, scraper, status, error) VALUES (?, ?, ?, ?)",
        (run_started_at, scraper, status, error),
    )
    conn.commit()


def fetch_latest_scrape_run(conn: PgConnection) -> list[tuple[str, str, str | None]]:
    """(scraper, status, error) for every scraper in the most recent run batch."""
    latest = conn.execute("SELECT MAX(run_started_at) FROM scrape_runs").fetchone()[0]
    if latest is None:
        return []
    return conn.execute(
        "SELECT scraper, status, error FROM scrape_runs WHERE run_started_at = ? ORDER BY scraper",
        (latest,),
    ).fetchall()


def write_bell_notification(conn: PgConnection, error_count: int, summary: str):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO bell_notifications (created_at, error_count, summary) VALUES (?, ?, ?)",
        (now, error_count, summary),
    )
    conn.commit()


def fetch_latest_bell_notification(conn: PgConnection) -> tuple[str, int, str] | None:
    """(created_at, error_count, summary) for the most recent day's notification, or None if notify_bell.py hasn't run yet."""
    row = conn.execute(
        "SELECT created_at, error_count, summary FROM bell_notifications ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return tuple(row) if row else None


def write_home_page_cache(
    conn: PgConnection, listings_json: str, value_min: int, value_max: int,
    total_count: int, priced_count: int,
):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO home_page_cache (id, listings_json, value_min, value_max, total_count, priced_count, generated_at)
        VALUES (1, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            listings_json = EXCLUDED.listings_json,
            value_min = EXCLUDED.value_min,
            value_max = EXCLUDED.value_max,
            total_count = EXCLUDED.total_count,
            priced_count = EXCLUDED.priced_count,
            generated_at = EXCLUDED.generated_at
        """,
        (listings_json, value_min, value_max, total_count, priced_count, now),
    )
    conn.commit()


def fetch_home_page_cache(conn: PgConnection) -> dict | None:
    """None if build_home_cache.py hasn't ever run yet -- caller falls back to computing live in that case."""
    row = conn.execute(
        "SELECT listings_json, value_min, value_max, total_count, priced_count, generated_at FROM home_page_cache WHERE id = 1"
    ).fetchone()
    if not row:
        return None
    listings_json, value_min, value_max, total_count, priced_count, generated_at = row
    return {
        "listings_json": listings_json,
        "value_min": value_min,
        "value_max": value_max,
        "total_count": total_count,
        "priced_count": priced_count,
        "generated_at": generated_at,
    }


def fetch_cached_enrichment_bulk(
    conn: PgConnection, state: str, county: str, account_numbers: list[str]
) -> dict[str, tuple[str | None, str | None]]:
    """
    {account_number: (address, description)} for every one of
    account_numbers already in the table (an entry simply isn't in the
    returned dict if it isn't in the table yet). One round trip for a
    whole county's worth of listings, not one per listing -- see
    bid4assets_scraper.py's module docstring on the 2026-08-06 incident
    this replaced fetch_cached_enrichment (a one-at-a-time version) to
    help fix: holding a database connection open across thousands of
    individually-interleaved DB calls and slow, rate-limited HTTP fetches
    is what caused it, so that scraper now does all its DB reads for a
    county in this one call, then closes the connection completely before
    doing any of the slow network work.
    """
    if not account_numbers:
        return {}
    placeholders = ",".join(["?"] * len(account_numbers))
    rows = conn.execute(
        f"""SELECT account_number, address, description FROM listings
            WHERE state = ? AND county = ? AND account_number IN ({placeholders})""",
        (state, county, *account_numbers),
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


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
    commit: bool = True,
    state: str = "TX",
):
    """
    Keyed on (state, county, account_number) -- account numbers aren't
    unique across counties, and county names aren't unique across states
    (both TX and PA have a Potter County). Defaults to "TX" since every
    caller except govease_scraper.py's Pennsylvania auctions is still
    Texas-only; that default keeps this a source-compatible change for
    every other scraper in the project.

    commit=False lets a caller writing many rows in one loop (lgbs_scraper.py
    inserting 3,000-6,000+ listings a run, say) batch them into one commit
    at the end instead of one network round-trip per row -- on 2026-07-29
    that per-row commit pattern was very likely what pushed an already-slow
    run (LGBS's API retrying through connection timeouts) over the daily
    job's 20-minute timeout, since the log showed LGBS's fetch finishing
    but the job dying before the *next* scraper ever started -- i.e. stuck
    writing, not fetching. Every other caller keeps the default (commit
    immediately, matching the behavior this always had).
    """
    if not account_number:
        return  # can't track/dedupe a listing without a stable identifier

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO listings (
            county, account_number, precinct, minimum_bid, estimated_value,
            address, description, status, source, source_url, latitude,
            longitude, first_seen, last_seen, state
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (state, county, account_number) DO UPDATE SET
            precinct = EXCLUDED.precinct,
            minimum_bid = EXCLUDED.minimum_bid,
            -- COALESCE, not a plain overwrite, for estimated_value/address/
            -- latitude/longitude: every caller of this function defaults
            -- latitude/longitude to None (no scraper geocodes itself --
            -- that's geocode_backfill.py/pa_parcel_geocode.py's job, run as
            -- separate steps afterward), and several scrapers can
            -- legitimately come back with address=None or estimated_value=
            -- None on a given run too (a tripped detail-fetch circuit
            -- breaker, a site that just doesn't always publish one --
            -- hctax_scraper.py's own adjudged_value is empty for some
            -- Harris County rows even though hcad_value_backfill.py fills
            -- in a real estimated_value for them separately, same pattern
            -- as address/lat-lon). A plain overwrite here meant every
            -- single re-scrape of an already-enriched listing silently
            -- wiped that enrichment back to NULL -- confirmed directly on
            -- 2026-08-13 for coordinates specifically: Cumberland County's
            -- listings, geocoded via pa_parcel_geocode.py earlier that
            -- session, dropped to 0/397 geocoded after bid4assets_scraper.py's
            -- next routine run. COALESCE keeps whatever's already stored
            -- whenever this particular run didn't produce a real value,
            -- while still accepting a genuine new value when one exists.
            estimated_value = COALESCE(EXCLUDED.estimated_value, listings.estimated_value),
            address = COALESCE(EXCLUDED.address, listings.address),
            description = EXCLUDED.description,
            status = EXCLUDED.status,
            source = EXCLUDED.source,
            source_url = EXCLUDED.source_url,
            latitude = COALESCE(EXCLUDED.latitude, listings.latitude),
            longitude = COALESCE(EXCLUDED.longitude, listings.longitude),
            last_seen = EXCLUDED.last_seen
        """,
        (
            county, account_number, precinct, minimum_bid, estimated_value,
            address, description, status, source, source_url, latitude,
            longitude, now, now, state,
        ),
    )
    if commit:
        conn.commit()


def upsert_historical_listing(
    conn: PgConnection,
    county: str,
    account_number: str | None,
    precinct: str | None,
    sale_date: str | None,
    is_cancelled: bool,
    minimum_bid: str | None,
    address: str | None,
    description: str | None,
    source: str,
    source_url: str | None = None,
    commit: bool = True,
):
    """Same upsert shape as upsert_listing, but into historical_listings --
    see that table's own comment in init_db for why this is a separate
    table and key rather than reusing `listings`."""
    if not account_number:
        return  # can't track/dedupe a record without a stable identifier

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO historical_listings (
            county, account_number, precinct, sale_date, is_cancelled,
            minimum_bid, address, description, source, source_url,
            first_seen, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (county, account_number, sale_date) DO UPDATE SET
            precinct = EXCLUDED.precinct,
            is_cancelled = EXCLUDED.is_cancelled,
            minimum_bid = EXCLUDED.minimum_bid,
            address = EXCLUDED.address,
            description = EXCLUDED.description,
            source = EXCLUDED.source,
            source_url = EXCLUDED.source_url,
            last_seen = EXCLUDED.last_seen
        """,
        (
            county, account_number, precinct, sale_date, is_cancelled,
            minimum_bid, address, description, source, source_url, now, now,
        ),
    )
    if commit:
        conn.commit()


def update_estimated_value(
    conn: PgConnection, county: str, account_number: str, estimated_value: str,
    state: str = "TX",
):
    """
    Narrow update for backfill scripts (e.g. hcad_value_backfill.py) that
    enrich an existing listing with a value from a source other than the
    one that originally scraped it -- unlike upsert_listing, this touches
    only estimated_value and doesn't require (or overwrite) every field.
    Takes `state` (default "TX" so hcad_value_backfill.py's existing
    Harris-County-only calls don't need updating -- Harris isn't a real
    county name collision risk, PA has no Harris County) because a PA
    assessment backfill absolutely is one: Texas has its own Montgomery
    County too, same reasoning as update_lat_lon's own state param.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE listings SET estimated_value = ?, last_seen = ? WHERE state = ? AND county = ? AND account_number = ?",
        (estimated_value, now, state, county, account_number),
    )
    conn.commit()


def update_lat_lon(
    conn: PgConnection, county: str, account_number: str,
    latitude: float | None, longitude: float | None,
    state: str = "TX",
):
    """
    Narrow update for geocoding backfill scripts (e.g. geocode_backfill.py)
    -- same shape as update_estimated_value. Takes `state` (unlike that
    function) because geocode_backfill.py runs across every source/state,
    not just one hardcoded county, so it needs the full key to avoid
    touching the wrong state's same-named county (see upsert_listing).
    latitude/longitude accept None -- geocode_backfill.py's
    clear_out_of_bounds_coordinates() uses this to blank out a coordinate
    it no longer trusts, not just to fill one in.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE listings SET latitude = ?, longitude = ?, last_seen = ? WHERE state = ? AND county = ? AND account_number = ?",
        (latitude, longitude, now, state, county, account_number),
    )
    conn.commit()


def update_address(conn: PgConnection, county: str, account_number: str, address: str):
    """Narrow update for backfill scripts (e.g. hcad_address_backfill.py) -- same shape as update_estimated_value."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE listings SET address = ?, last_seen = ? WHERE county = ? AND account_number = ?",
        (address, now, county, account_number),
    )
    conn.commit()
