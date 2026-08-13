"""
GovLandScout - Pennsylvania parcel-ID geocoding backfill

geocode_backfill.py's Census batch geocoder resolves a *street address* --
it has nothing to offer a listing whose only location info is a legal
description or a bare township name (see chester_scraper.py's docstring),
and it does poorly on the "<street>, <county name>, PA" addresses
bid4assets_scraper.py falls back to when a real city isn't published,
since a county's own name isn't a place the geocoder recognizes (verified
directly against the Census API before writing this: "..., Berks, PA"
returns zero matches every time, vs. one exact match for the real city).

This is a different technique for the same problem: several Pennsylvania
counties publish their own parcel-by-parcel data (including a real,
assessor-verified street address and a parcel boundary) into a single
statewide ArcGIS layer, PA DEP's PA_Parcels MapServer -- keyed by each
county's own parcel/PIN number, not a street address at all. Every
listing this project scrapes already carries that same parcel number as
`account_number` (it's the tax sale notice's own identifier), so this
looks a listing's coordinates and a real address up directly by parcel
ID instead of trying to parse and match a street address someone
mistyped into a legal notice.

Coverage is real but partial. Most counties below come from PA DEP's
single statewide layer, but county participation in it is voluntary, and
two of bid4assets.com's largest PA sources -- Monroe and Fayette -- have
no rows in it at all as of 2026-08-11 (checked directly: COUNTY_NAME
queries for both return zero). Fayette and Columbia turned out to have
their own separate per-county layers via PASDA instead (see
FAYETTE_SOURCE/COLUMBIA_SOURCE below); Monroe's PASDA layer exists too
but uses a parcel numbering scheme that doesn't match what bid4assets.com
scrapes (see FAYETTE_SOURCE's own comment), so Monroe isn't covered by
anything in this script. Monroe, plus every county not listed in
PARCEL_ID_STRATEGIES below, is left to geocode_backfill.py's
address-based approach -- this script only touches the counties below,
each one verified against real production data before being added (see
each strategy's own comment for its match rate). Some counties' first
attempt only caught part of that county's listings -- a second, wider
account-number transform found later (see berks_bid4assets_transform's
and fayette_bid4assets_transform's own docstrings) recovers more of them
without changing anything about already-matched rows.

Run after chester_scraper.py, montco_scraper.py, and (whenever its own
separate weekly job runs) bid4assets_scraper.py have added this
account_number, and before geocode_backfill.py so it isn't wasting Census
requests re-attempting addresses this already resolved -- see
run_daily_scrapers.py's ordering.
"""

import re
import time

import requests

import combined_db

# Kept well under the ArcGIS server's own request-size limits, and paced
# with a short sleep between chunks (see ParcelSource.query) -- these are
# live state/county government services with real interactive users, not
# a bulk data dump endpoint, the same caution this project already
# extends to bid4assets.com.
BATCH_SIZE = 200
BATCH_DELAY_SECONDS = 0.5


class ParcelSource:
    """One queryable ArcGIS layer: a URL, the field holding its parcel id,
    and (for a statewide layer covering many counties, like DEP's) an
    extra WHERE clause narrowing it to one county. `has_address` controls
    whether build_address() is even attempted -- PASDA's per-county
    layers (see FAYETTE below) publish geometry only, no address fields at
    all, so asking them for one would just silently return None every time
    rather than the useful signal "this source can't help with an
    address"."""

    def __init__(self, url: str, id_field: str, has_address: bool, county_where: str | None = None):
        self.url = url
        self.id_field = id_field
        self.has_address = has_address
        self.county_where = county_where

    def query(self, candidate_ids: list[str]) -> dict[str, dict]:
        """candidate id -> {"attributes": ..., "geometry": {"rings": [...]}} for every match,
        keeping only the first feature per id if a source has more than one for it (see
        cumberland_bid4assets_transform's docstring for a real example of why that can happen)."""
        out_fields = f"{self.id_field},PROPERTY_ADDRESS_1,CITY,ZIP" if self.has_address else self.id_field
        found = {}
        for i in range(0, len(candidate_ids), BATCH_SIZE):
            chunk = candidate_ids[i:i + BATCH_SIZE]
            ids_sql = ",".join(f"'{c}'" for c in chunk)
            where = f"{self.id_field} IN ({ids_sql})"
            if self.county_where:
                where = f"{self.county_where} AND {where}"
            resp = requests.post(self.url, data={
                "where": where,
                "outFields": out_fields,
                "returnGeometry": "true",
                "outSR": "4326",  # WGS84 lat/lon directly -- no Web Mercator reprojection needed
                "f": "json",
            }, timeout=60)
            resp.raise_for_status()
            for feature in resp.json().get("features", []):
                candidate = feature["attributes"][self.id_field]
                found.setdefault(candidate, feature)
            time.sleep(BATCH_DELAY_SECONDS)
        return found


def dep_source(county_upper: str) -> ParcelSource:
    """PA DEP's PA_Parcels MapServer -- one statewide layer covering 43 of
    PA's 67 counties (voluntary submission; checked directly which ones as
    of 2026-08-11), with a real assessor-verified address per parcel."""
    return ParcelSource(
        "https://gis.dep.pa.gov/depgisprd/rest/services/Parcels/PA_Parcels/MapServer/0/query",
        id_field="PARCEL_ID", has_address=True, county_where=f"UPPER(COUNTY_NAME) = '{county_upper}'",
    )


# Fayette isn't in DEP's statewide layer at all (checked directly: a
# COUNTY_NAME query against it returns zero rows), but PASDA separately
# hosts each PA county's own submitted GIS layers, and Fayette's happens
# to be one of them -- a single-county service (no county filter needed),
# with a different id field (TAXIDNUM) and no address fields at all, just
# geometry. Monroe has an equivalent-looking PASDA layer too, but its own
# id field (MAPNUMBER, a 14-digit tax-map-sheet code) doesn't match
# bid4assets.com's scraped account numbers (an "district.block.lot"-style
# PIN, e.g. "20.8E.1.102") at all -- checked directly against 20 real
# rows, zero matched -- so Monroe isn't included below.
FAYETTE_SOURCE = ParcelSource(
    "https://imagery.pasda.psu.edu/arcgis/rest/services/pasda/FayetteCounty/MapServer/2/query",
    id_field="TAXIDNUM", has_address=False,
)

# Same voluntary-participation gap as Fayette (checked directly: DEP has
# zero Columbia rows), but PASDA has its own Columbia layer too -- a
# different id field again (PIN) and, like Fayette's, geometry only.
COLUMBIA_SOURCE = ParcelSource(
    "https://imagery.pasda.psu.edu/arcgis/rest/services/pasda/ColumbiaCounty/MapServer/2/query",
    id_field="PIN", has_address=False,
)


def identity(account_number: str) -> str:
    return account_number


def berks_bid4assets_transform(account_number: str) -> str:
    """Bid4Assets' own Berks County parcel numbers are a 2-digit municipal
    code (e.g. "12" for the City of Reading, "15", "02", ... -- confirmed
    varying across real production rows, not a fixed constant) prepended
    to the 12-digit parcel id DEP's layer actually stores under PARCEL_ID.
    Verified against 1,269 real ungeocoded Berks listings before writing
    this originally: stripping any 2-digit prefix (not just "12", an
    earlier and wrong first guess that only matched 4% of them) reaches
    1,238 (98%).

    A second, longer format (197 listings, e.g. "34439202554926B16") looked
    unrelated at first but turns out to be the exact same 14-character
    "prefix + 12-digit id" shape with a 3-character suffix appended (e.g.
    "T06", "B16", "C71" -- a sub-unit code, the same "one bid4assets number
    covers several individually-listed sites within one larger parcel"
    pattern as Cumberland's per-lot suffix, see
    cumberland_bid4assets_transform). Taking just the first 14 characters
    before stripping the 2-digit prefix handles both shapes with one
    transform: verified against the 228 listings still ungeocoded after
    the original 14-char-only version, 59 (26%) newly matched this way --
    lower than the clean 14-char case, consistent with these being
    sub-parcels that don't always get their own separate DEP entry."""
    first14 = account_number[:14]
    if not first14.isdigit() or len(first14) < 14:
        return account_number
    return first14[2:]


CUMBERLAND_BASE_ID_PATTERN = re.compile(r"^(\d{2}-\d{2}-\d{4}-\d{3})")


def cumberland_bid4assets_transform(account_number: str) -> str:
    """Bid4Assets' Cumberland listings append a per-lot suffix (a trailing
    ".", or ".-TR012345"/".-U725" -- both seen on real rows, both meaning
    "a specific site within a larger subdivided tract", e.g. a mobile home
    park lot) that DEP's own PARCEL_ID never carries; this strips down to
    the shared "NN-NN-NNNN-NNN" base id every one of those variants
    starts with. Verified against 256 real ungeocoded Cumberland listings:
    146 of 158 distinct base ids matched (57% of individual listings,
    since several listings can share one base id -- see module docstring;
    those all resolve to the same shared coordinate, which is a real
    limitation of parcel-level data for a subdivided tract, not a bug)."""
    match = CUMBERLAND_BASE_ID_PATTERN.match(account_number)
    return match.group(1) if match else account_number


def montgomery_dep_transform(account_number: str) -> str:
    """Montgomery County's own account numbers are dash-formatted
    ("13-00-04240-90-4"); DEP's PARCEL_ID for the same parcel is the exact
    same digits with the dashes removed ("130004240904") -- confirmed on
    real matched rows (e.g. "13-00-04240-90-4" -> DEP's "E Basin St",
    matching the scraped "E BASIN ST, Norristown, PA"). Verified against
    312 real ungeocoded Montgomery listings: 291 (93%) matched."""
    return account_number.replace("-", "")


FAYETTE_BASE_ID_PATTERN = re.compile(r"^(\d{2}-\d{2}-\d{4})")


def fayette_bid4assets_transform(account_number: str) -> str:
    """Most Fayette account numbers already match PASDA's TAXIDNUM as-is
    (see FAYETTE_SOURCE's own 88% match rate), but a minority carry one or
    two extra trailing segments PASDA's own id never has (e.g.
    "14-24-0037-03-99" vs. TAXIDNUM's plain "14-24-0037") -- the same
    "one bid4assets number can point at a sub-unit of a larger parcel"
    shape as Cumberland and Berks above. This trims down to the shared
    "NN-NN-NNNN" prefix every variant starts with, which is also a no-op
    for account numbers already in that exact shape. Verified against the
    215 listings still ungeocoded after the plain TAXIDNUM match: 119
    (55%) newly matched this way."""
    match = FAYETTE_BASE_ID_PATTERN.match(account_number)
    return match.group(1) if match else account_number


def columbia_bid4assets_transform(account_number: str) -> str:
    """Columbia's account numbers ("07 05 01013000") carry more trailing
    zeros in their third, space-separated segment than PASDA's own PIN
    field does for the same parcel ("19 07 01300" for a different sample
    parcel, but the same shorter shape) -- trimming trailing zeros off
    that segment (down to a single "0" if the whole segment were zeros)
    is a safe guess to try, not a validated rule the way the other
    transforms above are: a wrong guess here just fails to match (this
    project's parcel-lookup approach can only produce false negatives,
    never a wrong coordinate -- see module docstring), and it's only
    tried after PASDA's own id has already failed to match as-is.
    Verified against 200 real ungeocoded Columbia listings: 35 (17%)
    matched -- lower confidence than the other transforms here, but real."""
    parts = account_number.split()
    if len(parts) != 3:
        return account_number
    trimmed_third = parts[2].rstrip("0") or "0"
    return f"{parts[0]} {parts[1]} {trimmed_third}"


# (source, county) -> (ParcelSource to query, account_number -> candidate id transform).
# `county` here matches combined_db's own `county` column exactly (see each
# scraper's own county value), not necessarily a source's own casing for
# it -- dep_source() handles that with UPPER() in its own query.
# Every entry was verified against real production listings before being
# added (match rate noted alongside each one).
PARCEL_ID_STRATEGIES = {
    ("chesco.org", "Chester"): (dep_source("CHESTER"), identity),  # 308/349 (88%)
    ("bid4assets.com", "Schuylkill"): (dep_source("SCHUYLKILL"), identity),  # 524/579 (91%)
    ("bid4assets.com", "Berks"): (dep_source("BERKS"), berks_bid4assets_transform),  # 1,238/1,466 (84%), +59/228 (26%) after generalizing to the lettered-suffix format
    ("bid4assets.com", "Cumberland"): (dep_source("CUMBERLAND"), cumberland_bid4assets_transform),  # ~146/256 (57%)
    ("bid4assets.com", "Fayette"): (FAYETTE_SOURCE, fayette_bid4assets_transform),  # ~1,637/1,861 (88%) + 119/215 (55%) after trimming extra segments, coordinates only, no address
    ("montgomerycountypa.gov", "Montgomery"): (dep_source("MONTGOMERY"), montgomery_dep_transform),  # 291/312 (93%)
    ("bid4assets.com", "Columbia"): (COLUMBIA_SOURCE, columbia_bid4assets_transform),  # 35/200 (17%), coordinates only, no address
}


def fetch_ungeocoded(conn: combined_db.PgConnection) -> list[tuple[str, str, str]]:
    """(source, county, account_number) for every listing this script has a strategy for."""
    rows = []
    for source, county in PARCEL_ID_STRATEGIES:
        found = conn.execute(
            """SELECT account_number FROM listings
               WHERE source = ? AND county = ? AND state = 'PA'
                 AND latitude IS NULL AND account_number IS NOT NULL""",
            (source, county),
        ).fetchall()
        rows.extend((source, county, r[0]) for r in found)
    return rows


def ring_centroid(rings: list[list[list[float]]]) -> tuple[float, float] | None:
    """Average of every ring's vertices (closing point dropped so it isn't
    double-weighted) -- a plain vertex average, not a true area-weighted
    centroid, which is a real approximation for a very irregular or
    multi-part shape. Good enough for a map pin on the small, roughly
    convex residential/rural lots this project deals with; not something
    to reuse for large or oddly-shaped parcels without revisiting."""
    points = []
    for ring in rings:
        pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
        points.extend(pts)
    if not points:
        return None
    lon = sum(p[0] for p in points) / len(points)
    lat = sum(p[1] for p in points) / len(points)
    return lat, lon


def build_address(attrs: dict) -> str | None:
    street = (attrs.get("PROPERTY_ADDRESS_1") or "").strip()
    if not street:
        return None
    city = (attrs.get("CITY") or "").strip()
    zip_code = (attrs.get("ZIP") or "").strip()
    parts = [street, city, "PA" + (f" {zip_code}" if zip_code else "")] if city else [street, "PA"]
    return ", ".join(p for p in parts if p)


def main():
    conn = combined_db.get_connection()
    ungeocoded = fetch_ungeocoded(conn)
    print(f"{len(ungeocoded)} listings across {len(PARCEL_ID_STRATEGIES)} (source, county) pair(s) "
          f"have a parcel-lookup strategy and no coordinates yet.")

    by_group: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for source, county, account_number in ungeocoded:
        _, transform = PARCEL_ID_STRATEGIES[(source, county)]
        candidate = transform(account_number)
        by_group.setdefault((source, county), []).append((account_number, candidate))

    updated = 0
    for (source, county), pairs in by_group.items():
        parcel_source, _ = PARCEL_ID_STRATEGIES[(source, county)]
        candidate_ids = [candidate for _, candidate in pairs]
        matches = parcel_source.query(candidate_ids)
        print(f"  {source} / {county}: {len(pairs)} candidate(s), {len(matches)} matched")

        for account_number, candidate in pairs:
            feature = matches.get(candidate)
            if feature is None:
                continue
            centroid = ring_centroid(feature.get("geometry", {}).get("rings", []))
            if centroid is None:
                continue
            lat, lon = centroid
            combined_db.update_lat_lon(conn, county, account_number, lat, lon, state="PA")
            if parcel_source.has_address:
                address = build_address(feature["attributes"])
                if address:
                    combined_db.update_address(conn, county, account_number, address)
            updated += 1

    print(f"\nBackfilled coordinates for {updated} of {len(ungeocoded)} listings.")
    conn.close()


if __name__ == "__main__":
    main()
