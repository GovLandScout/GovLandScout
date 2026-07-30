"""
GovLandScout - Notification bell finalizer

Run as its own job (see .github/workflows/notify.yml), triggered by
scrape.yml's completion via workflow_run rather than a fixed clock offset.
An earlier version used "1 hour after scrape.yml's cron", reasoning that
was comfortably past even a slow run's 20 min timeout -- but GitHub's
schedule triggers aren't guaranteed to fire at their nominal time (each
workflow's trigger can drift independently under load), and on 2026-07-29
the two drifted close enough that notify ran 3 minutes *before* that
day's scrape actually finished, reading a 6-of-14-scrapers-run batch and
reporting it as final. workflow_run ties this to the scrape's actual
completion instead of guessing an offset, removing that race entirely.

Reads the most recent run's per-scraper results out of scrape_runs and
writes one summary row to bell_notifications, which is all the site's
notification bell (see web.py) actually reads. Deliberately a separate
step from run_daily_scrapers.py itself -- the bell should update once a
day when that day's run is actually done, not flicker mid-scrape for
anyone loading the site while it's still underway.
"""

import combined_db


def build_summary(results: list[tuple[str, str, str | None]]) -> tuple[int, str]:
    failed = [(scraper, error) for scraper, status, error in results if status != "success"]

    if not results:
        return 0, "No scrape run recorded yet."
    if not failed:
        return 0, f"All {len(results)} scrapers ran successfully."

    names = ", ".join(scraper for scraper, _ in failed)
    return len(failed), f"{len(failed)} of {len(results)} scraper(s) failed: {names}"


def main():
    conn = combined_db.get_connection()

    results = combined_db.fetch_latest_scrape_run(conn)
    error_count, summary = build_summary(results)
    combined_db.write_bell_notification(conn, error_count, summary)

    conn.close()
    print(summary)


if __name__ == "__main__":
    main()
