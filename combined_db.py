"""
GovLandScout - Combined listings storage

hctax_scraper.py and lgbs_scraper.py acquire data completely differently
(regex-over-scraped-HTML vs. a JSON API) and keep their own detailed,
source-specific tables. Their fetch/parse logic isn't worth unifying -- it's
too different to share usefully. What's worth unifying is the OUTPUT: this
module normalizes every county's listings into one shared table so
find_deals.py and web.py can rank/display them together.
"""

import sqlite3
from datetime import datetime, timezone

DB_PATH = "govlandscout.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    conn: sqlite3.Connection,
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
    existing = conn.execute(
        "SELECT id FROM listings WHERE county = ? AND account_number = ?",
        (county, account_number),
    ).fetchone()

    if existing:
        conn.execute(
            """
            UPDATE listings SET
                precinct = ?, minimum_bid = ?, estimated_value = ?, address = ?,
                description = ?, status = ?, source = ?, source_url = ?,
                latitude = ?, longitude = ?, last_seen = ?
            WHERE county = ? AND account_number = ?
            """,
            (
                precinct, minimum_bid, estimated_value, address, description, status,
                source, source_url, latitude, longitude, now, county, account_number,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO listings (
                county, account_number, precinct, minimum_bid, estimated_value,
                address, description, status, source, source_url, latitude,
                longitude, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                county, account_number, precinct, minimum_bid, estimated_value,
                address, description, status, source, source_url, latitude,
                longitude, now, now,
            ),
        )
    conn.commit()


def update_estimated_value(
    conn: sqlite3.Connection, county: str, account_number: str, estimated_value: str
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
