"""
GovLandScout - Notification bell finalizer

Run as its own scheduled job, 1 hour after run_daily_scrapers.py's cron
(see .github/workflows/notify.yml vs scrape.yml) -- comfortably after even
a slow day's scrape run (20 min timeout) finishes, so this always reads a
complete, settled batch rather than racing a run still in progress.

Reads the most recent run's per-scraper results out of scrape_runs and
writes one summary row to bell_notifications, which is all the site's
notification bell (see web.py) actually reads. Deliberately a separate
step from run_daily_scrapers.py itself -- the bell should update once a
day on a predictable schedule, not flicker mid-scrape for anyone loading
the site while that day's run is still underway.
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
