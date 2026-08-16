"""
GovLandScout - TX county appraisal district (CAD) value backfill (manual/occasional, NOT in the daily pipeline)

Fills in estimated_value for Texas listings whose scraper source has no
value of its own -- mvbalaw.com in particular publishes only a minimum
bid, never an independent value estimate (see mvba_scraper.py's own
docstring), so every listing it produces starts out unpriced. Modeled
on hcad_value_backfill.py and pa_assessment_backfill.py: a manual,
occasional script, not something run.py wires into the daily schedule.

Unlike Harris County's own bulk CAMA download (hcad_value_backfill.py)
or Pennsylvania's per-county ArcGIS layers (pa_assessment_backfill.py),
this reads a live, per-property HTML page rather than a bulk export or
a spatial query -- Bell and Taylor counties' appraisal districts both
happen to run "eSearch", a Central-Appraisal-District product built by
True Automation (a Harris Govern brand, unrelated to Harris County, TX
-- the branding just collides), and a plain unauthenticated GET to
`/Property/View/{property_id}` returns a real property page with a
"Market Value:" row, no login, session, or CAPTCHA required (confirmed
directly: /search/shouldUseRecaptcha on Bell's own site returns
`{"shouldUseRecaptcha":false}`). No CLR-style conversion is needed the
way Pennsylvania's assessments require -- Texas appraises property at
100% of market value by law, so the page's own "Market Value" is
already the right number to ship.

Verified this is a real match, not a coincidental parcel-id collision:
Bell County listing "103125_25DCV350972" (1313 S 57th St, Temple, TX)
scraped from mvbalaw.com matches property id 103125 on
esearch.bellcad.org, whose page's own Situs Address ("1313 S 57TH ST,
TEMPLE, TX 76504") is an exact match.

Only two counties are wired in here, not the full ~10-county
mvbalaw.com/pbfcm.com/govease.com gap this project's own audits have
found (see model/README.md-adjacent commit history) -- each county's
CAD runs its own domain and, sometimes, its own quirks:

  - Bell (esearch.bellcad.org): account_number's first segment before
    the "_" (mvbalaw.com's own scraped case-number suffix, e.g.
    "103125_25DCV350972" -> "103125") matches this site's property id
    directly. Verified against all 38 real ungeocoded-value Bell
    listings: 36 (94.7%) matched.
  - Taylor (esearch.taylor-cad.org): same account_number shape, same
    transform. Verified against all 41 real listings: 41 (100%)
    matched.

Investigated and ruled out this round, for future reference rather than
silently dropped:
  - Leon: the "esearch.leoncad.org" domain search results kept pointing
    to doesn't actually exist -- confirmed directly, it has no DNS
    record at all (`dig` returns nothing), not a transient outage.
  - Williamson: "esearch.wcad.org" resolves but serves a TLS
    certificate for a different hostname (hostname mismatch, not this
    project's fault); the (differently-named) "search.wcad.org" does
    resolve and does present a valid cert, but returns a bare 503 on
    every request tried.
  - Brown: "esearch.browncad.com" (the domain consistently suggested)
    also has no DNS record.
  - Jasper: esearch.jaspercad.org is real and reachable (200 OK), but
    the account_number shape bid4assets.com uses for Jasper
    ("R000109_7911") doesn't match this site's own property-id
    convention the same simple way Bell/Taylor's do -- would need a
    real investigation of Jasper's own id format, not attempted here.

A real next step for whoever picks this up: repeat this same
domain-hunt-then-verify process for the rest of the mvbalaw.com/
pbfcm.com/govease.com counties this project has never priced at all
(Jasper, McLennan, Williamson, Brown, Eastland, Runnels, Comanche,
Hale, Bastrop, Rusk, Wichita, Hill, Grayson, Denton, Bosque, Johnson,
Hays, Lampasas, Medina, and others) -- each is its own small
investigation, the same shape as this file's own two entries, not a
generalizable crawl (McLennan alone, though also True-Automation-
powered, was TLS-unreachable from here the same way Williamson's main
domain was, and never got a working workaround).
"""

import re
import time

import requests

import combined_db

HEADERS = {
    "User-Agent": "GovLandScout-SchoolProject/1.0 (contact: your-email@example.com)"
}

# Courteous delay between requests -- this scrapes live HTML pages one
# property at a time from a small vendor's public portal, not a bulk
# export meant for scripted access the way HCAD's own zip download is.
REQUEST_DELAY_SECONDS = 0.3

MARKET_VALUE_PATTERN = re.compile(r'Market Value:</th><td class="table-number">\$([\d,]+)')


def strip_case_number(account_number: str) -> str:
    """mvbalaw.com's own scraped account numbers carry this project's
    appended court case number after the CAD's real property id (e.g.
    "103125_25DCV350972") -- see mvba_scraper.py. Both counties wired in
    here use the same shape, so one transform covers both rather than
    each COUNTY_CONFIGS entry duplicating it."""
    return account_number.strip().split("_")[0]


COUNTY_CONFIGS = {
    "Bell": {
        "domain": "esearch.bellcad.org",
        "normalize_id": strip_case_number,
    },
    "Taylor": {
        "domain": "esearch.taylor-cad.org",
        "normalize_id": strip_case_number,
    },
}


def fetch_target_accounts(conn: combined_db.PgConnection, county: str) -> list[str]:
    """Account numbers for this TX county's listings that don't have a usable estimated_value yet."""
    rows = conn.execute("""
        SELECT account_number FROM listings
        WHERE state = 'TX' AND county = ?
          AND (estimated_value IS NULL OR estimated_value = '' OR CAST(estimated_value AS REAL) <= 0)
    """, (county,)).fetchall()
    return [r[0] for r in rows]


def fetch_market_value(domain: str, property_id: str) -> float | None:
    url = f"https://{domain}/Property/View/{property_id}"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    match = MARKET_VALUE_PATTERN.search(resp.text)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def backfill_county(conn: combined_db.PgConnection, county: str) -> int:
    config = COUNTY_CONFIGS[county]

    target_accounts = fetch_target_accounts(conn, county)
    print(f"{county}: {len(target_accounts)} listing(s) currently have no estimated value.")
    if not target_accounts:
        return 0

    updated = 0
    for account_number in target_accounts:
        property_id = config["normalize_id"](account_number)
        value = fetch_market_value(config["domain"], property_id)
        if value and value > 0:
            combined_db.update_estimated_value(conn, county, account_number, str(value))
            updated += 1
        time.sleep(REQUEST_DELAY_SECONDS)

    print(f"{county}: backfilled {updated} of {len(target_accounts)} listing(s).")
    return updated


def main():
    conn = combined_db.get_connection()
    total_updated = 0
    for county in COUNTY_CONFIGS:
        total_updated += backfill_county(conn, county)

    print(f"\n{total_updated} listing(s) total backfilled with an estimated value.")
    conn.close()


if __name__ == "__main__":
    main()
