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

Seven counties are wired in here, not the full ~15-county
mvbalaw.com/pbfcm.com/govease.com gap this project's own audits have
found (see model/README.md-adjacent commit history) -- each county's
CAD runs its own domain and, sometimes, its own quirks. Every one below
uses the same `strip_case_number()` transform (first segment before the
scraper's own appended "_suffix") *except* where noted -- Wichita's
govease.com-sourced account numbers have no suffix to strip at all,
which the same transform handles as a no-op:

  - Bell (esearch.bellcad.org, mvbalaw.com): 36 of 38 (94.7%) matched.
  - Taylor (esearch.taylor-cad.org, mvbalaw.com): 41 of 41 (100%).
  - Hill (esearch.hillcad.org, mvbalaw.com): 15 of 15 (100%) -- verified
    via the listing's own `description` text (which embeds a real
    street address mvba_scraper.py never parses into the `address`
    column itself, see its own docstring) exactly matching this site's
    Situs Address for the same property id.
  - Bosque (esearch.bosquecad.com, mvbalaw.com): 13 of 13 (100%).
  - Comanche (esearch.comanchecad.org, mvbalaw.com): 19 of 20 (95%).
  - Wichita (esearch.wadtx.com, govease.com -- not mvbalaw.com; GovEase
    also publishes no independent value, same reason as MVBA/PBFCM):
    16 of 17 (94%).
  - Hays (esearch.hayscad.com, mvbalaw.com): 9 of 11 matched -- Hays
    also has separate hudgis-hud.opendata.arcgis.com-sourced listings
    (a HUD surplus feed, unrelated to the CAD entirely) that this
    doesn't and shouldn't try to match; the 2 unmatched here are
    genuine misses within the mvbalaw.com subset, not those.

Investigated and ruled out this round, for future reference rather than
silently dropped:
  - Leon, Brown: the domains search results consistently suggest
    ("esearch.leoncad.org", "esearch.browncad.com") don't actually
    exist -- confirmed directly, neither has a DNS record at all
    (`dig` returns nothing), not a transient outage.
  - Williamson: "esearch.wcad.org" resolves but serves a TLS
    certificate for a different hostname (hostname mismatch, not this
    project's fault); the (differently-named) "search.wcad.org" does
    resolve and does present a valid cert, but returns a bare 503 on
    every request tried.
  - McLennan, Rusk: both reachable in DNS but fail the TLS handshake
    itself (SSL_ERROR_SYSCALL / EOF, not a certificate problem) --
    plausibly a WAF or load balancer quirk specific to these two sites,
    not something a different User-Agent or retry fixed.
  - Jasper, Hale, Johnson: all three sites are real and reachable
    (200 OK), but none matched with the simple strip-after-"_" id --
    each county's own account-number shape (Jasper: "R000109_7911",
    Hale: an R-prefixed id followed by a full court case name after the
    "_", Johnson: a dashed "126-0244-03068" that looks like a
    Geographic ID rather than a Property ID) would need its own
    real investigation, not attempted here.
  - Grayson: esearch.graysonappraisal.org is reachable, but
    govease.com's own account numbers for Grayson ("T-20-3168") look
    like GovEase's internal sale identifiers, not a Grayson CAD
    property id at all -- a different kind of mismatch than the
    id-shape guesses above, likely unrecoverable without a separate
    GovEase-side lookup first.
  - Bastrop: esearch.bastropcad.org returns a persistent HTTP 500 on
    every request tried (confirmed non-transient across 3 retries) --
    a real server-side issue on their end, not a request-shape problem.

A real next step for whoever picks this up: repeat this same
domain-hunt-then-verify process for the rest of the mvbalaw.com/
pbfcm.com/govease.com counties this project has never priced at all
(Eastland, Runnels, Denton, Lampasas, Medina, and others) -- each is
its own small investigation, the same shape as this file's own entries,
not a generalizable crawl.
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
    "Hill": {
        "domain": "esearch.hillcad.org",
        "normalize_id": strip_case_number,
    },
    "Bosque": {
        "domain": "esearch.bosquecad.com",
        "normalize_id": strip_case_number,
    },
    "Comanche": {
        "domain": "esearch.comanchecad.org",
        "normalize_id": strip_case_number,
    },
    # govease.com-sourced, not mvbalaw.com -- account numbers here have
    # no case-number suffix at all (e.g. "116506"), so strip_case_number
    # is a safe no-op rather than the wrong transform.
    "Wichita": {
        "domain": "esearch.wadtx.com",
        "normalize_id": strip_case_number,
    },
    "Hays": {
        "domain": "esearch.hayscad.com",
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
