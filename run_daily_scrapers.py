"""
Runs all county scrapers back to back so the combined listings table in
Postgres (see combined_db.py) gets a full refresh in one daily pass.

Uses subprocess rather than importing and calling main() directly so each
scraper still runs as its own clean process (matching how they behave when
run manually), and one crashing doesn't take the others down with it.

Each scraper's outcome is also recorded to scrape_runs (see combined_db.py)
under one shared run_started_at timestamp -- this workflow reporting
"success" on GitHub Actions only means the *runner* didn't crash, not that
every scraper actually pulled fresh data (exactly how lgbs_scraper.py's
daily failures went unnoticed for days). notify_bell.py reads this table
~1 hour later to build the site's notification bell.
"""

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import combined_db

SCRAPERS = [
    "hctax_scraper.py", "lgbs_scraper.py", "gsa_scraper.py",
    "tdhca_scraper.py", "houston_scraper.py", "pbfcm_scraper.py",
    "mvba_scraper.py",  # respects a mandatory 10s per-request crawl-delay,
    # so this alone adds ~90s to every run (9 documents)
    "glo_veterans_land_scraper.py", "hud_reo_scraper.py", "publicsurplus_scraper.py",
    "houston_landbank_scraper.py", "irs_auction_scraper.py",
    "realauction_scraper.py",
]

# Kept short -- this ends up in a DB row and eventually the bell's
# tooltip/summary text, not meant to hold a full traceback.
ERROR_SNIPPET_CHARS = 500


def run_step(conn: combined_db.PgConnection, project_dir: Path, run_started_at: str, script: str):
    print(f"--- Running {script} ---")
    result = subprocess.run(
        [sys.executable, script], cwd=project_dir,
        capture_output=True, text=True,
    )
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)

    if result.returncode == 0:
        combined_db.record_scrape_result(conn, run_started_at, script, "success")
    else:
        print(f"--- {script} failed with exit code {result.returncode} ---")
        error = result.stderr.strip()[-ERROR_SNIPPET_CHARS:] or f"exit code {result.returncode}"
        combined_db.record_scrape_result(conn, run_started_at, script, "failed", error)


def main():
    project_dir = Path(__file__).resolve().parent
    run_started_at = datetime.now(timezone.utc).isoformat()

    conn = combined_db.get_connection()

    for scraper in SCRAPERS:
        run_step(conn, project_dir, run_started_at, scraper)

    # Runs last, once, against whatever addresses today's scrapers just
    # added -- not its own scraper, so it isn't in SCRAPERS above, but it's
    # tracked the same way so a broken geocoder shows up on the bell too.
    run_step(conn, project_dir, run_started_at, "geocode_backfill.py")

    conn.close()


if __name__ == "__main__":
    main()
