"""
GovLandScout - CA county assessor value backfill (manual/occasional, NOT in the daily pipeline)

Fills in estimated_value for California listings using each county's own
public ArcGIS parcel layer, the same pattern pa_assessment_backfill.py
already established for Pennsylvania (see that module's own docstring).
Modeled on it directly: a manual, occasional script, not something
run_daily_scrapers.py wires into the daily schedule.

Unlike Pennsylvania, this doesn't apply any market-value conversion factor
to the raw assessed value. PA's "base year" system has one statewide
published Common Level Ratio per county, a single number that converts a
stale base-year assessment to today's market value uniformly across every
parcel in that county. California's Prop 13 assessed value has no
equivalent: each parcel's own assessed value is capped at (and only rises
from) whatever it was at that specific parcel's last change of ownership
or new construction, compounding at a small fixed rate since -- there's no
single ratio that converts a two-year-old purchase and a forty-year-old
one back to today's market value the same way. So this stores the raw
assessed value as-is, same choice already made in mytaxsale_scraper.py for
the assessed values that platform hands over directly -- a legitimate
floor on a property's value (California counties tax at 1% of assessed
value, not below-market by policy the way a base-year system can drift),
just not a market-value estimate the way PA's converted figure is.

## Why only two counties

This project's CA listings are dominated by small, rural counties (Modoc,
Butte, Imperial, Siskiyou, Lassen, Shasta, Madera, ... -- see
bid4assets_scraper.py's own live storefront discovery), and most of them
don't publish assessed value through a public ArcGIS layer the way every
PA county checked did. Confirmed directly before concluding that, not
assumed: Butte County's own public "Live.GIS.Base_Parcel" layer has APN
and ownership fields but no value field at all; Kern County's own GEODAT
"Assessor Parcels Land" layer is the same (boundaries and APN only). What
these smaller counties actually publish for assessed value is gated
behind ParcelQuest Lite, a third-party statewide vendor with a one-parcel-
at-a-time public lookup page, not a bulk-queryable API -- confirmed live
for both Imperial and Siskiyou specifically (their own Assessor pages
route bidders there for value lookups). A regional SCAG ArcGIS layer
covering six Southern California counties (maps.scag.ca.gov) was also
checked and ruled out: current as of 2016 assessed values from 2015, and
its own description explicitly excludes Imperial County anyway -- a
decade-stale figure being silently shown as current would be worse than
showing nothing, not a shortcut worth taking.

Alameda and Monterey are the exceptions: both are large enough counties to
run their own real county GIS department, and both publish current
assessed value directly on their own official ArcGIS Online org (not a
third party) -- confirmed against real listings already in the database
before adding either:

  - Alameda: id field (APN) matches account_number once bid4assets_scraper.py's
    own appended "- Item #N" suffix is stripped ("1-115-12 - Item #3" ->
    "1-115-12"). TotalNetValue is used directly rather than summing
    Land/Imps separately -- it's the layer's own net-of-exemption combined
    figure (Land + Imps - HOEX - OTEX), the same "one combined column"
    shape most PA counties' layers have.
  - Monterey: id field (APN) matches account_number once both
    bid4assets_scraper.py's own appended "-Item No: N" suffix and every
    dash are stripped ("032-121-018-000-Item No: 8" -> "032121018000").
    Land_Value and Imp_Value are separate columns here (no combined field
    published), summed the same way PA's Monroe county config already
    sums BLDGVALUE/LANDVALUE.

Adding another county means finding its own county-owned (not third-party)
ArcGIS FeatureServer with a real value field -- search arcgis.com's public
item search for "<county> County California Parcels assessed" or similar,
same discovery method used for Alameda/Monterey/Kern/Butte here -- and
matching a real account_number already in the database against the
layer's own id field the same way. A layer with only APN/boundary fields
and no value column isn't a partial win worth adding; ParcelQuest Lite
being the only place a smaller county actually publishes assessed value
is a real, confirmed dead end for this approach, not a research gap to
push through by force.
"""

import re

import requests

import combined_db

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

# ArcGIS REST query endpoints accept a request body this large without
# trouble; chunking is mainly to keep any one response's payload small and
# to avoid an extremely long IN (...) clause.
BATCH_SIZE = 100

ALAMEDA_ITEM_SUFFIX_PATTERN = re.compile(r"\s*-\s*Item\s*#\s*\d+\s*$", re.IGNORECASE)


def alameda_strip_item_suffix(account_number: str) -> str:
    return ALAMEDA_ITEM_SUFFIX_PATTERN.sub("", account_number.strip())


MONTEREY_ITEM_SUFFIX_PATTERN = re.compile(r"-Item No:\s*\d+\s*$", re.IGNORECASE)


def monterey_normalize_id(account_number: str) -> str:
    without_suffix = MONTEREY_ITEM_SUFFIX_PATTERN.sub("", account_number.strip())
    return without_suffix.replace("-", "")


COUNTY_CONFIGS = {
    "Alameda": {
        "query_url": "https://services5.arcgis.com/ROBnTHSNjoZ2Wm1P/arcgis/rest/services/Parcels/FeatureServer/0/query",
        "id_field": "APN",
        "value_fields": ("TotalNetValue",),
        "normalize_id": alameda_strip_item_suffix,
    },
    "Monterey": {
        "query_url": "https://services2.arcgis.com/nOGTdfb4kF4dZljH/arcgis/rest/services/Parcels_Data/FeatureServer/0/query",
        "id_field": "APN",
        "value_fields": ("Land_Value", "Imp_Value"),
        "normalize_id": monterey_normalize_id,
    },
}


def fetch_target_accounts(conn: combined_db.PgConnection, county: str) -> list[str]:
    """Account numbers for this CA county's listings that don't have a usable estimated_value yet."""
    rows = conn.execute("""
        SELECT account_number FROM listings
        WHERE state = 'CA' AND county = ?
          AND (estimated_value IS NULL OR estimated_value = '' OR CAST(estimated_value AS REAL) <= 0)
    """, (county,)).fetchall()
    return [r[0] for r in rows]


def query_batch(query_url: str, id_field: str, value_fields: tuple[str, ...], ids: list[str]) -> dict[str, float]:
    """value_fields is summed per feature -- Alameda has one combined
    column (a 1-tuple), Monterey splits it across Land_Value/Imp_Value
    instead (see COUNTY_CONFIGS), so this always sums rather than
    special-casing the single-field county."""
    quoted_ids = ",".join("'" + i.replace("'", "''") + "'" for i in ids)
    resp = requests.post(
        query_url,
        data={
            "where": f"{id_field} IN ({quoted_ids})",
            "outFields": f"{id_field}," + ",".join(value_fields),
            "f": "json",
        },
        headers=HEADERS, timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if "error" in payload:
        raise RuntimeError(f"ArcGIS query error: {payload['error']}")

    found = {}
    for feature in payload.get("features", []):
        attrs = feature["attributes"]
        value = sum(attrs.get(f) or 0 for f in value_fields)
        parcel_id = attrs.get(id_field)
        if value and value > 0 and parcel_id:
            # A parcel split across multiple map polygons appears as
            # several features with the same id and value -- a dict
            # naturally collapses those back into one entry.
            found[parcel_id] = value
    return found


def backfill_county(conn: combined_db.PgConnection, county: str) -> int:
    config = COUNTY_CONFIGS[county]

    target_accounts = fetch_target_accounts(conn, county)
    print(f"{county}: {len(target_accounts)} listing(s) currently have no estimated value.")
    if not target_accounts:
        return 0

    # normalized id -> every original account_number that normalizes to it
    # (almost always one, but a shared key isn't impossible and shouldn't
    # silently drop one of the affected listings).
    id_to_originals: dict[str, list[str]] = {}
    for account_number in target_accounts:
        normalized = config["normalize_id"](account_number)
        id_to_originals.setdefault(normalized, []).append(account_number)

    normalized_ids = list(id_to_originals.keys())
    updated = 0
    for i in range(0, len(normalized_ids), BATCH_SIZE):
        chunk = normalized_ids[i:i + BATCH_SIZE]
        values = query_batch(config["query_url"], config["id_field"], config["value_fields"], chunk)
        for normalized_id, assessed_value in values.items():
            for account_number in id_to_originals.get(normalized_id, []):
                combined_db.update_estimated_value(conn, county, account_number, str(assessed_value), state="CA")
                updated += 1

    print(f"{county}: backfilled {updated} of {len(target_accounts)} listing(s).")
    return updated


def main():
    conn = combined_db.get_connection()
    total_updated = 0
    for county in COUNTY_CONFIGS:
        total_updated += backfill_county(conn, county)
    conn.close()
    print(f"\n{total_updated} listing(s) total backfilled with an estimated value.")


if __name__ == "__main__":
    main()
