"""
GovLandScout - PA county assessment value backfill (manual/occasional, NOT in the daily pipeline)

Fills in estimated_value for Pennsylvania listings using each county's own
public ArcGIS "Parcels" open data layer -- the same trusted, no-auth
pattern this project already uses for HUD's open data (see
hud_reo_scraper.py). Modeled on hcad_value_backfill.py: a manual,
occasional script, not something run.py wires into the daily schedule.

Critical wrinkle this was built around: Pennsylvania counties assess
property at a fraction of market value, not market value itself (a "base
year" assessment system) -- using a raw assessed value as estimated_value
directly would silently understate a property's real value, by a lot.
Confirmed on Chester County specifically during development: a real
listing's TOT_ASSESS was $116,640, but Chester's own published conversion
factor puts its actual market value around $381,000 -- a 3.27x
difference. Every county's factor (the reciprocal of its "Common Level
Ratio", CLR) is published once a year in one shared statewide PDF by PA's
Department of Revenue; STATE_CLR_FACTORS below is that table, current as
of the 2025 CLR (in effect 7/1/2026-6/30/2027). Source:
https://www.pa.gov/content/dam/copapwp-pagov/en/revenue/documents/taxtypes/rtt/documents/clr_factor_current.pdf
-- re-check and update this table when that PDF's effective dates roll
over, roughly annually.

Each county publishes its parcels layer under its own GIS org, with its
own field names and its own account-number format -- there's no
standardized statewide schema, so this is a per-county config
(COUNTY_CONFIGS below), not one general parser. Seven counties confirmed
against real listings already in the database before writing this:

  - Chester: id field (UPI) matches account_number exactly.
  - Montgomery: id field (PARCEL) matches only after stripping the dashes
    our own account_number has ("01-00-01606-02-2" -> "010001606022").
  - Cumberland: id field (PIN) matches only after reducing account_number
    to its shared "NN-NN-NNNN-NNN" base id -- confirmed against a real
    parcel whose GIS-published SITUS address ("493 ARLINGTON ROAD")
    exactly matched what bid4assets_scraper.py had already scraped for
    the same parcel. See cumberland_base_id()'s own docstring for the
    per-lot suffix shapes this strips.
  - Monroe: id field (PARID) matches account_number exactly -- the same
    county-run iasWorld layer pa_parcel_geocode.py's MONROE_SOURCE
    already uses for geocoding, with a value split across two fields
    (BLDGVALUE/LANDVALUE, summed) instead of one combined assessment
    column -- see its own COUNTY_CONFIGS comment.
  - Berks: id field (PROPID) matches account_number's first 14
    characters -- Berks County's own GIS-published parcel layer, not PA
    DEP's statewide one pa_parcel_geocode.py uses for Berks geocoding
    (that one has no value field at all) -- see berks_first14()'s own
    docstring.
  - Potter: id field (Map_Number) matches after stripping this project's
    own sale-type suffix -- the county's own self-hosted ArcGIS Server,
    found via its public Web AppBuilder app's config rather than a
    direct search (that only turns up the app, not the data server it
    points at). See potter_strip_sale_type()'s own docstring.
  - Bedford: id field (taxidnum) matches account_number exactly -- same
    "found via the county's own web app's config" pattern as Potter,
    this time hosted on ArcGIS Online under the county planning office's
    own org rather than the county's own server.

Adding another county means finding its own ArcGIS FeatureServer (search
arcgis.com's public item search, e.g. "<county> County Pennsylvania
Parcels"), confirming it's actually publicly queryable without an auth
token (ruled out Beaver County's own official source this way -- also
just unavailable/"not started" regardless when this was written), and
matching a real account_number already in the database against the
layer's own id field to work out whatever format difference exists (see
each config's `normalize_id`) -- same investigation as the three above.
"""

import re
from datetime import datetime, timezone

import requests

import combined_db

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

# ArcGIS REST query endpoints accept a request body this large without
# trouble; chunking is mainly to keep any one response's payload small
# and to avoid an extremely long IN (...) clause.
BATCH_SIZE = 100

# Reciprocals of each county's Common Level Ratio -- multiply a raw
# assessed value by this to get an estimated market value. See module
# docstring for source and why this matters. Philadelphia is omitted
# (has two different factors depending on transaction date within the
# effective period, and this project doesn't scrape Philadelphia yet
# anyway -- see bid4assets_scraper.py's docstring).
STATE_CLR_FACTORS = {
    "Adams": 1.48, "Allegheny": 2.03, "Armstrong": 4.07, "Beaver": 1.34,
    "Bedford": 1.91, "Berks": 3.12, "Blair": 1.28, "Bradford": 5.99,
    "Bucks": 17.86, "Butler": 16.67, "Cambria": 8.93, "Cameron": 5.03,
    "Carbon": 5.81, "Centre": 6.10, "Chester": 3.27, "Clarion": 1.00,
    "Clearfield": 5.21, "Clinton": 1.93, "Columbia": 7.35, "Crawford": 6.33,
    "Cumberland": 1.56, "Dauphin": 2.61, "Delaware": 1.83, "Elk": 5.56,
    "Erie": 1.99, "Fayette": 2.46, "Forest": 8.77, "Franklin": 13.89,
    "Fulton": 4.95, "Greene": 2.75, "Huntingdon": 8.00, "Indiana": 1.42,
    "Jefferson": 5.24, "Juniata": 13.51, "Lackawanna": 1.00, "Lancaster": 2.00,
    "Lawrence": 2.20, "Lebanon": 1.92, "Lehigh": 2.13, "Luzerne": 1.14,
    "Lycoming": 2.29, "McKean": 2.08, "Mercer": 9.52, "Mifflin": 4.83,
    "Monroe": 2.27, "Montgomery": 3.36, "Montour": 2.23, "Northampton": 6.21,
    "Northumberland": 10.53, "Perry": 1.00, "Pike": 11.24, "Potter": 6.21,
    "Schuylkill": 1.00, "Snyder": 10.87, "Somerset": 5.52, "Sullivan": 2.47,
    "Susquehanna": 6.13, "Tioga": 1.25, "Union": 2.33, "Venango": 2.05,
    "Warren": 1.00, "Washington": 1.47, "Wayne": 1.49, "Westmoreland": 11.49,
    "Wyoming": 9.01, "York": 2.05,
}


def strip_trailing_period(account_number: str) -> str:
    return account_number.strip().rstrip(".")


CUMBERLAND_BASE_ID_PATTERN = re.compile(r"^(\d{2}-\d{2}-\d{4}-\d{3})")


def cumberland_base_id(account_number: str) -> str:
    """Same base-id extraction as pa_parcel_geocode.py's
    cumberland_bid4assets_transform() (duplicated here, not imported --
    this module reads a different ArcGIS layer for a different purpose,
    it doesn't need that module's geocoding-specific machinery, just the
    same regex). A plain strip_trailing_period() only handled the
    simplest suffix shape (a bare trailing "."); the remaining
    unmatched Cumberland listings all carry a longer per-lot suffix
    (".-TR012345", ".-U725" -- a specific site within a larger
    subdivided tract, e.g. a mobile home park lot) that this layer's own
    PIN field never has. Verified against all 156 real Cumberland
    listings still missing a value after the simple strip: all 156
    (100%) matched once reduced to the shared "NN-NN-NNNN-NNN" base id
    every variant starts with -- several listings share one base id and
    so resolve to the same value, a real limitation of parcel-level data
    for a subdivided tract, not a bug (same tradeoff already documented
    for geocoding this same shape)."""
    match = CUMBERLAND_BASE_ID_PATTERN.match(account_number.strip())
    return match.group(1) if match else account_number.strip().rstrip(".")


def berks_first14(account_number: str) -> str:
    """Bid4Assets' own Berks numbers are a 2-digit municipal code prepended
    to a 12-digit parcel id (14 digits total), same shape
    pa_parcel_geocode.py's berks_bid4assets_transform already documented
    for DEP's layer -- but unlike DEP's PARCEL_ID (which stores the
    12-digit id *without* that prefix), Berks County's own PROPID field
    stores the full 14-digit id, prefix included, so this doesn't strip
    it. A second, longer format (a 3-character sub-unit suffix appended,
    e.g. "...T58") is handled by just taking the first 14 characters --
    on a plain 14-digit id that's a no-op, so one function covers both
    shapes. Verified against all 1,467 real Berks listings with no value
    yet: 1,464 (99.8%) match this way."""
    first14 = account_number.strip()[:14]
    if first14.isdigit() and len(first14) == 14:
        return first14
    return account_number.strip()


def strip_dashes(account_number: str) -> str:
    return account_number.replace("-", "").strip()


def identity(account_number: str) -> str:
    return account_number.strip()


def potter_strip_sale_type(account_number: str) -> str:
    """bid4assets_scraper.py appends this project's own sale-type suffix
    to Potter's account numbers (e.g. "010-010 -062_UPSET" for an upset
    sale) -- Potter's own Map_Number field never carries it
    ("010-010 -062"). Verified against all 56 real Potter listings
    still missing a value: 54 (96.4%) matched once the suffix (and
    everything after the underscore, in case a "_JUDICIAL" or similar
    variant shows up too) is stripped."""
    return account_number.strip().split("_")[0].strip()


COUNTY_CONFIGS = {
    "Chester": {
        "query_url": "https://services.arcgis.com/G4S1dGvn7PIgYd6Y/arcgis/rest/services/Parcels_owners/FeatureServer/0/query",
        "id_field": "UPI",
        "value_fields": ("TOT_ASSESS",),
        "normalize_id": strip_trailing_period,
    },
    "Montgomery": {
        "query_url": "https://services1.arcgis.com/kOChldNuKsox8qZD/arcgis/rest/services/Montgomery_County_Parcels/FeatureServer/6/query",
        "id_field": "PARCEL",
        "value_fields": ("TOTAL_ASSE",),
        "normalize_id": strip_dashes,
    },
    "Cumberland": {
        "query_url": "https://services1.arcgis.com/1Cfo0re3un0w6a30/arcgis/rest/services/Tax_Parcels/FeatureServer/0/query",
        "id_field": "PIN",
        "value_fields": ("TOTAL_VAL",),
        "normalize_id": cumberland_base_id,
    },
    # Same county-run iasWorld layer pa_parcel_geocode.py's MONROE_SOURCE
    # already uses for geocoding (PARID matches bid4assets.com's scraped
    # account_number directly, no transform needed -- 99% hit rate there
    # against 1,439 real listings). That layer has no single combined
    # assessment field like the other three counties' TOT_ASSESS/
    # TOTAL_ASSE/TOTAL_VAL -- BLDGVALUE and LANDVALUE are separate columns
    # instead, summed here into one assessed value before the same CLR
    # conversion below. Verified directly against all 1,439 real
    # ungeocoded-value Monroe listings before adding: 1,430 (99.4%) match
    # with a positive summed value, same real-world range ($360-$60,350
    # assessed) as the vacant/rural lots this county's tax sales are
    # mostly made of. PREFVALUE (a "Clean & Green" preferential-use
    # assessment some enrolled farmland/forest parcels carry instead of
    # their fair-market one) came back 0 for every sampled row and isn't
    # included -- BLDGVALUE/LANDVALUE is what these listings actually
    # carry.
    "Monroe": {
        "query_url": "https://monroegis.org/webadaptor/rest/services/Tylers_IAS/Parcels_PublicView/MapServer/0/query",
        "id_field": "PARID",
        "value_fields": ("BLDGVALUE", "LANDVALUE"),
        "normalize_id": identity,
    },
    # Berks County's own GIS department publishes this directly (found by
    # searching ArcGIS Online for county-owned items, not PA DEP's
    # statewide PA_Parcels layer pa_parcel_geocode.py uses for Berks
    # geocoding -- that one has no value field at all, checked directly).
    # An older "_Public" version of this same service (also turned up by
    # that search) is retired/empty (returns 0 rows for any query) --
    # deliberately not used here. VALUTOTAL is a raw base-year assessed
    # total (same CLR treatment as every other county here, not already a
    # market value) -- confirmed by its magnitude: the median matched raw
    # VALUTOTAL among these listings is $42,850, which is an implausibly
    # low median home value on its own but a plausible one once
    # multiplied by Berks' 3.12 CLR factor (~$134k), and separately by
    # this layer's schema itself carrying VALULAND/VALUBLDG/VALUTOTAL
    # unmarked next to a distinctly-named VALULNDMKT ("land market")
    # field -- i.e. the schema's own naming draws the same
    # assessed-vs-market line CLR conversion exists to cross.
    "Berks": {
        "query_url": "https://services3.arcgis.com/dGYe1jDYrTw1wwpc/arcgis/rest/services/Berks_County_Parcels_V2/FeatureServer/2/query",
        "id_field": "PROPID",
        "value_fields": ("VALUTOTAL",),
        "normalize_id": berks_first14,
    },
    # Potter County Assessment & GIS Dept's own self-hosted ArcGIS Server
    # (maps.pottercountypa.net) -- found by pulling the operational-layer
    # URLs out of the county's public "Tax Parcel Viewer" Web AppBuilder
    # app's config (the app itself is hosted on ArcGIS Online, but the
    # data it points at lives on the county's own server, not
    # arcgis.com). No statewide DEP-layer equivalent was checked for
    # Potter since this county-run layer already had what was needed.
    "Potter": {
        "query_url": "https://maps.pottercountypa.net/arcgis/rest/services/TaxParcel/TaxParcels/MapServer/0/query",
        "id_field": "Map_Number",
        "value_fields": ("Current_Total_Value",),
        "normalize_id": potter_strip_sale_type,
    },
    # Same pattern as Potter -- Bedford County's own Web AppBuilder
    # "Online Parcel Viewer" app config points at a county-run parcel
    # layer hosted on ArcGIS Online under the county planning office's
    # own org (bedfordplanning.maps.arcgis.com), not a third party.
    "Bedford": {
        "query_url": "https://services2.arcgis.com/tXFMtuwRfEDEFdnG/arcgis/rest/services/April2023Parcels/FeatureServer/0/query",
        "id_field": "taxidnum",
        "value_fields": ("TOT_VAL",),
        "normalize_id": identity,
    },
}


def fetch_target_accounts(conn: combined_db.PgConnection, county: str) -> list[str]:
    """Account numbers for this PA county's listings that don't have a usable estimated_value yet."""
    rows = conn.execute("""
        SELECT account_number FROM listings
        WHERE state = 'PA' AND county = ?
          AND (estimated_value IS NULL OR estimated_value = '' OR CAST(estimated_value AS REAL) <= 0)
    """, (county,)).fetchall()
    return [r[0] for r in rows]


def query_batch(query_url: str, id_field: str, value_fields: tuple[str, ...], ids: list[str]) -> dict[str, float]:
    """value_fields is summed per feature -- most counties here have one
    combined assessment column (a 1-tuple), but Monroe's layer splits it
    across BLDGVALUE/LANDVALUE instead (see its own COUNTY_CONFIGS
    comment), so this always sums rather than special-casing the
    single-field counties."""
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
    clr_factor = STATE_CLR_FACTORS[county]

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
            market_value = round(assessed_value * clr_factor, 2)
            for account_number in id_to_originals.get(normalized_id, []):
                combined_db.update_estimated_value(conn, county, account_number, str(market_value), state="PA")
                updated += 1

    print(f"{county}: backfilled {updated} of {len(target_accounts)} listing(s).")
    return updated


# Philadelphia isn't in COUNTY_CONFIGS/STATE_CLR_FACTORS above -- it's a
# genuinely different case, not just another ArcGIS layer with its own
# field names. It publishes its own assessor data (Office of Property
# Assessment) through the city's Carto SQL API, not an ArcGIS
# FeatureServer, and unlike every other county here, its market_value
# field is *already* a real market value, not a fractional base-year
# assessment -- Philadelphia moved to full-market-value assessment
# (the "Actual Value Initiative") in 2013, which is also why
# STATE_CLR_FACTORS above explicitly omits it ("has two different
# factors depending on transaction date" -- that comment describes the
# pre-AVI system; this project didn't scrape Philadelphia listings at
# all when it was written, so it was never revisited). Applying a CLR
# multiplier here would inflate an already-correct market value, not
# correct an assessed one -- confirmed directly: the median matched
# value among these listings is $149,350, itself a plausible Philadelphia
# home value with no adjustment needed. id field (parcel_number) matches
# account_number exactly -- verified against all 126 real Philadelphia
# listings with no value yet: 124 (98.4%) matched.
PHILADELPHIA_CARTO_URL = "https://phl.carto.com/api/v2/sql"


def backfill_philadelphia(conn: combined_db.PgConnection) -> int:
    target_accounts = fetch_target_accounts(conn, "Philadelphia")
    print(f"Philadelphia: {len(target_accounts)} listing(s) currently have no estimated value.")
    if not target_accounts:
        return 0

    updated = 0
    for i in range(0, len(target_accounts), BATCH_SIZE):
        chunk = target_accounts[i:i + BATCH_SIZE]
        quoted_ids = ",".join("'" + a.replace("'", "''") + "'" for a in chunk)
        resp = requests.get(
            PHILADELPHIA_CARTO_URL,
            params={"q": f"SELECT parcel_number, market_value FROM opa_properties_public "
                          f"WHERE parcel_number IN ({quoted_ids})"},
            headers=HEADERS, timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        if "error" in payload:
            raise RuntimeError(f"Carto SQL API error: {payload['error']}")
        for row in payload.get("rows", []):
            value = row.get("market_value")
            account_number = row.get("parcel_number")
            if value and value > 0 and account_number:
                combined_db.update_estimated_value(conn, "Philadelphia", account_number, str(value), state="PA")
                updated += 1

    print(f"Philadelphia: backfilled {updated} of {len(target_accounts)} listing(s).")
    return updated


def main():
    conn = combined_db.get_connection()
    total_updated = 0
    for county in COUNTY_CONFIGS:
        total_updated += backfill_county(conn, county)
    total_updated += backfill_philadelphia(conn)
    conn.close()
    print(f"\n{total_updated} listing(s) total backfilled with an estimated value.")


if __name__ == "__main__":
    main()
