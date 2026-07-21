"""
GovLandScout - Web viewer

FastAPI app that serves every scraped listing across all three sources
(hctax_scraper.py, lgbs_scraper.py, gsa_scraper.py) via combined_db.py.
Listings with a real equity calculation are ranked first; listings
missing pricing data (or with no independent value estimate at all, like
the GSA federal auctions) are shown afterward with "No data available"
in place of the fields that don't apply, rather than being hidden.

Rows are NOT pre-rendered as HTML server-side -- with 4,000+ listings
that meant shipping a multi-megabyte page and building tens of thousands
of DOM nodes on every load, most of which were never looked at. Instead
the page embeds one compact JSON blob of every listing, and a small
vanilla-JS layer filters/sorts/paginates it client-side, building only
the current page's worth of <tr> elements -- no extra requests after the
initial load, no client-side framework.

Run with: venv/bin/uvicorn web:app --reload
"""

import json
import math
from html import escape
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response

import combined_db
from find_deals import fetch_all_listings

app = FastAPI(title="GovLandScout")

# Esri's satellite export explicitly disallows caching
# (Cache-Control: max-age=0, must-revalidate on their responses), so every
# page load was re-fetching every visible thumbnail from scratch -- the
# main thing making image loading feel slow. A given listing's coordinates
# never change, so the image doesn't either; cache it here once and serve
# it back with real caching headers from then on. Disk cache is ephemeral
# on Render (wiped on redeploy), but still eliminates re-fetching within a
# deploy's lifetime, which is the common case.
THUMBNAIL_CACHE_DIR = Path(__file__).resolve().parent / "thumbnail_cache"
THUMBNAIL_CACHE_DIR.mkdir(exist_ok=True)
THUMBNAIL_CACHE_CONTROL = "public, max-age=31536000, immutable"

NO_DATA = "No data available"

# Location search matches raw county/address text, which misses listings
# in a metro's collar counties/suburbs that don't happen to spell out the
# metro name anywhere (e.g. a Sugar Land or Katy listing has no "Houston"
# in its county or address). Rather than rely on incidental text matches,
# explicitly map well-known metro names to the counties people mean by
# them, so searching "houston" finds the whole metro, not just rows that
# happen to say "Houston" verbatim.
METRO_COUNTIES = {
    "houston": {"Harris", "Fort Bend", "Montgomery", "Galveston", "Brazoria", "Liberty", "Waller", "Chambers"},
    "dallas": {"Dallas", "Tarrant", "Collin", "Denton", "Rockwall", "Ellis", "Kaufman", "Johnson", "Parker", "Wise", "Hunt"},
    "fort worth": {"Dallas", "Tarrant", "Collin", "Denton", "Rockwall", "Ellis", "Kaufman", "Johnson", "Parker", "Wise", "Hunt"},
    "austin": {"Travis", "Williamson", "Hays", "Bastrop", "Caldwell"},
    "san antonio": {"Bexar", "Atascosa", "Bandera", "Comal", "Guadalupe", "Kendall", "Medina", "Wilson"},
}

# Reverse index: county -> metro alias words that should match it, e.g.
# a Tarrant County row should match searches for "dallas" or "fort worth".
COUNTY_METRO_ALIASES: dict[str, set[str]] = {}
for _alias, _counties in METRO_COUNTIES.items():
    for _county in _counties:
        COUNTY_METRO_ALIASES.setdefault(_county, set()).add(_alias)

# Esri's "World Imagery" service is a free, keyless satellite basemap --
# same usage tier as the OpenStreetMap tiles the map view already pulls
# from, just requested as a single flattened image for a small bounding
# box around a point instead of as map tiles. No account, no billing.
SATELLITE_EXPORT_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export"
THUMB_HALF_HEIGHT_DEG = 0.0006  # ~65m north/south -- tight enough to show just the parcel


def satellite_thumbnail_url(latitude: float, longitude: float) -> str:
    # Longitude degrees shrink as they approach the poles; correct by
    # latitude so the box covers roughly the same real-world distance
    # east/west as it does north/south, keeping the thumbnail square-ish.
    half_lon = THUMB_HALF_HEIGHT_DEG / math.cos(math.radians(latitude))
    bbox = (
        f"{longitude - half_lon},{latitude - THUMB_HALF_HEIGHT_DEG},"
        f"{longitude + half_lon},{latitude + THUMB_HALF_HEIGHT_DEG}"
    )
    return (
        f"{SATELLITE_EXPORT_URL}?bbox={bbox}&bboxSR=4326&imageSR=4326"
        "&size=160,160&format=jpg&f=image"
    )


def thumbnail_cache_path(latitude: float, longitude: float) -> Path:
    # Rounded to ~11cm precision -- far tighter than the ~65m thumbnail
    # itself, just enough to give identical coordinates a stable filename.
    return THUMBNAIL_CACHE_DIR / f"{latitude:.6f}_{longitude:.6f}.jpg"


@app.get("/api/thumbnail")
def get_thumbnail(lat: float, lon: float):
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(status_code=400, detail="lat/lon out of range")

    cache_path = thumbnail_cache_path(lat, lon)
    if not cache_path.exists():
        resp = requests.get(satellite_thumbnail_url(lat, lon), timeout=15)
        resp.raise_for_status()
        cache_path.write_bytes(resp.content)

    return Response(
        content=cache_path.read_bytes(),
        media_type="image/jpeg",
        headers={"Cache-Control": THUMBNAIL_CACHE_CONTROL},
    )


def get_all_listings() -> list[dict]:
    conn = combined_db.get_connection()
    listings = fetch_all_listings(conn)
    conn.close()
    # Priced listings first (best equity first); unpriced ones after,
    # in a stable order rather than however SQLite happened to return them.
    listings.sort(
        key=lambda l: (
            l["equity_pct"] is None,
            -(l["equity_pct"] or 0),
            l["county"],
            l["account_number"],
        )
    )
    return listings


PAGE_CSS = """
html { color-scheme: light; }
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  margin: 0; padding: 2rem clamp(1rem, 4vw, 3rem);
  background: #f1f5f9; color: #0f172a;
}

h1 { font-size: 2rem; font-weight: 800; letter-spacing: -0.02em; margin: 0 0 0.4rem; }
.subtitle { margin: 0 0 1.5rem; color: #475569; font-size: 0.95rem; line-height: 1.55; max-width: 75ch; }

.site-nav {
  display: flex; gap: 0.25rem; margin-bottom: 1.75rem;
  border-bottom: 1px solid #e2e8f0;
}
.site-nav a {
  padding: 0.75rem 1.1rem; text-decoration: none; color: #475569;
  font-weight: 600; font-size: 0.9rem; border-bottom: 2px solid transparent; margin-bottom: -1px;
}
.site-nav a:hover { color: #0f172a; }
.site-nav a.active { color: #2563eb; border-bottom-color: #2563eb; }

table {
  border-collapse: collapse; width: 100%; background: #fff;
  border: 1px solid #e2e8f0; box-shadow: 0 1px 3px rgba(15, 23, 42, 0.06);
}
th, td { border-bottom: 1px solid #e2e8f0; padding: 10px 14px; text-align: left; font-size: 0.875rem; color: #1e293b; }
th {
  background: #f8fafc; position: sticky; top: 0; font-weight: 700; color: #334155;
  text-transform: uppercase; font-size: 0.72rem; letter-spacing: 0.04em; border-bottom: 2px solid #e2e8f0;
}
tr:nth-child(even) td { background: #f8fafc; }
tr:nth-child(odd) td { background: #fff; }
tr:hover td { background: #eff6ff; }
.nodata { color: #94a3b8; font-style: italic; }
.thumb { display: block; object-fit: cover; border-radius: 6px; }
.equity-badge {
  display: inline-block; padding: 3px 10px; border-radius: 999px;
  color: #fff; font-weight: 700; font-size: 0.8rem; white-space: nowrap;
}

.card {
  background: #fff; border: 1px solid #e2e8f0; border-radius: 12px;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.06);
}

.controls {
  display: flex; flex-wrap: wrap; gap: 1.5rem; align-items: flex-end;
  padding: 1.25rem 1.5rem; margin-bottom: 1.25rem;
}
.control { display: flex; flex-direction: column; gap: 0.35rem; }
.control label {
  font-size: 0.75rem; font-weight: 700; color: #475569;
  text-transform: uppercase; letter-spacing: 0.04em;
}
.control input[type="text"], .control select {
  padding: 0.5rem 0.7rem; font-size: 0.9rem; border: 1px solid #cbd5e1; border-radius: 8px;
  background: #fff; color: #0f172a;
}
.control input[type="text"]:focus, .control select:focus {
  outline: none; border-color: #2563eb; box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.15);
}
.range-control { min-width: 260px; }
.range-control .range-row { display: flex; align-items: center; gap: 0.5rem; }
.range-control input[type="range"] { flex: 1; accent-color: #2563eb; }
.range-value { font-size: 0.8rem; color: #475569; white-space: nowrap; min-width: 5.5rem; }
#resetFilters, #toggleMap {
  padding: 0.55rem 1.1rem; font-size: 0.85rem; font-weight: 600; border-radius: 8px;
  cursor: pointer; margin-right: 0.5rem; border: 1px solid transparent;
  transition: background 0.15s ease, border-color 0.15s ease;
}
#resetFilters { background: #fff; color: #334155; border-color: #cbd5e1; }
#resetFilters:hover { background: #f1f5f9; }
#toggleMap { background: #2563eb; color: #fff; }
#toggleMap:hover { background: #1d4ed8; }
#resultSummary { font-size: 0.85rem; color: #475569; }
#mapContainer { height: 500px; margin-bottom: 1.25rem; overflow: hidden; }

.pagination {
  display: flex; align-items: center; justify-content: center; gap: 1rem;
  padding: 1rem; margin-top: -1px;
  background: #fff; border: 1px solid #e2e8f0; border-top: none;
}
.pagination button {
  padding: 0.45rem 0.9rem; font-size: 0.85rem; font-weight: 600; border-radius: 8px;
  cursor: pointer; border: 1px solid #cbd5e1; background: #fff; color: #334155;
}
.pagination button:hover:not(:disabled) { background: #f1f5f9; }
.pagination button:disabled { opacity: 0.4; cursor: default; }
#pageIndicator { font-size: 0.85rem; color: #475569; min-width: 8rem; text-align: center; }

.stats-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 1.25rem; margin-bottom: 2rem;
}
.stat-card { padding: 1.5rem; }
.stat-card .stat-value { font-size: 2.25rem; font-weight: 800; color: #2563eb; letter-spacing: -0.02em; }
.stat-card .stat-label { margin-top: 0.35rem; color: #475569; font-size: 0.9rem; }
.prose { max-width: 75ch; line-height: 1.65; color: #1e293b; font-size: 0.95rem; }
.prose h2 { font-size: 1.25rem; margin: 2rem 0 0.75rem; }
.prose h2:first-child { margin-top: 0; }
.prose a { color: #2563eb; }
.contact-card { padding: 1.5rem; max-width: 32rem; }
"""

LEAFLET_HEAD = """
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="" />
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css"
      integrity="sha256-YU3qCpj/P06tdPBJGPax0bm6Q1wltfwjsho5TR4+TYc=" crossorigin="" />
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css"
      integrity="sha256-YSWCMtmNZNwqex4CEw1nQhvFub2lmU7vcCKP+XVwwXA=" crossorigin="" />
"""


def nav_html(active: str) -> str:
    pages = [("/", "home", "Home"), ("/impact", "impact", "Impact"), ("/about", "about", "About & Contact")]
    links = "".join(
        f'<a href="{href}" class="{"active" if key == active else ""}">{label}</a>'
        for href, key, label in pages
    )
    return f'<nav class="site-nav">{links}</nav>'


def page_shell(title: str, active: str, body: str, extra_head: str = "") -> str:
    return f"""
    <html>
    <head>
      <title>{title}</title>
      {extra_head}
      <style>{PAGE_CSS}</style>
    </head>
    <body>
      {nav_html(active)}
      {body}
    </body>
    </html>
    """


def extract_city(address: str | None) -> str:
    # Addresses are formatted "<street>, <city>, TX <zip>" -- the city is
    # the second-to-last comma segment. Matching location search against
    # the raw street text (instead of just the city) is what let searches
    # like "houston" match "1204 S HOUSTON SCHOOL RD, LANCASTER, TX" --
    # Houston is an extremely common Texas street name, so this false
    # positive isn't unique to Houston, it'd happen for any city whose
    # name doubles as a street name elsewhere.
    if not address:
        return ""
    parts = [p.strip() for p in address.split(",")]
    return parts[-2] if len(parts) >= 3 else ""


@app.get("/", response_class=HTMLResponse)
def deals_page():
    listings = get_all_listings()
    priced_count = sum(1 for l in listings if l["equity_pct"] is not None)

    values = [l["estimated_value"] for l in listings if l["estimated_value"] is not None]
    value_min = math.floor(min(values) / 1000) * 1000 if values else 0
    value_max = math.ceil(max(values) / 1000) * 1000 if values else 0

    def listing_for_js(l: dict) -> dict:
        # Texas has an actual Houston County (Crockett/Kennard/Lovelady --
        # ~100mi from Houston, no relation to it) distinct from Harris
        # County, which is where the city of Houston actually is. Searching
        # "houston" should find Houston addresses, not Houston COUNTY's
        # unrelated listings, so leave the raw county name out of the
        # search blob for this one case -- those listings are still
        # findable by their own city/address text, just not by "houston".
        search_county = "" if l["county"] == "Houston" else l["county"]
        # Prefer matching on just the city; only fall back to the full
        # street address for the ~13% of records too irregularly formatted
        # to reliably split out a city (no third comma segment).
        city = extract_city(l["address"])
        search_place = city if city else (l["address"] or "")
        metro_terms = " ".join(sorted(COUNTY_METRO_ALIASES.get(l["county"], ())))
        search_text = f"{search_county} {l['precinct']} {search_place} {metro_terms}".lower()
        image_url = (
            f"/api/thumbnail?lat={l['latitude']}&lon={l['longitude']}"
            if l["latitude"] is not None and l["longitude"] is not None
            else None
        )
        return {
            "county": l["county"],
            "precinct": l["precinct"] or None,
            "account_number": l["account_number"],
            "minimum_bid": l["minimum_bid"],
            "estimated_value": l["estimated_value"],
            "equity": l["equity"],
            "equity_pct": l["equity_pct"],
            "address": l["address"] or None,
            "description": (l["description"][:120] if l["description"] else None),
            "source_url": l["source_url"],
            "maps_url": l["maps_url"],
            "latitude": l["latitude"],
            "longitude": l["longitude"],
            "image_url": image_url,
            "search_text": search_text,
        }

    # Escape "</" so a stray "</script>" inside any scraped field (address,
    # description, ...) can't break out of the script tag this gets embedded
    # in -- valid both as JSON and as the JS string it becomes once parsed.
    listings_json = json.dumps([listing_for_js(l) for l in listings]).replace("</", "<\\/")

    body = f"""
      <h1>GovLandScout - Texan's Distressed Property Finder</h1>
      <p class="subtitle">GovLandScout is a project attempting to show a state-wide listing of all property being sold by the government+, to try to help Texan's combat rising home prices and a lack of housing affordability. {len(listings)} total listings across all sources. {priced_count} have a full equity calculation and are
         ranked first below; the rest are shown afterward with "{NO_DATA}" where a field doesn't apply.</p>

      <div class="controls card">
        <div class="control">
          <label for="locationFilter">Location (county, precinct, or address)</label>
          <input type="text" id="locationFilter" placeholder="e.g. Dallas, Houston, Precinct 4..." oninput="applyFilters()">
        </div>

        <div class="control range-control">
          <label>Est. value: <span id="minValueLabel"></span> &ndash; <span id="maxValueLabel"></span></label>
          <div class="range-row">
            <span class="range-value">Min</span>
            <input type="range" id="minValue" min="{value_min}" max="{value_max}" step="1000" value="{value_min}" oninput="applyFilters()">
          </div>
          <div class="range-row">
            <span class="range-value">Max</span>
            <input type="range" id="maxValue" min="{value_min}" max="{value_max}" step="1000" value="{value_max}" oninput="applyFilters()">
          </div>
        </div>

        <div class="control">
          <label for="equitySort">Sort by equity</label>
          <select id="equitySort" onchange="applySort()">
            <option value="desc">Highest to lowest (default)</option>
            <option value="asc">Lowest to highest</option>
          </select>
        </div>

        <div class="control">
          <button id="resetFilters" onclick="resetFilters()">Reset filters</button>
          <button id="toggleMap" onclick="toggleMap()">Hide map</button>
        </div>

        <div class="control">
          <span id="resultSummary"></span>
        </div>
      </div>

      <div id="mapContainer" class="card"></div>

      <table id="dealsTable">
        <thead>
        <tr>
          <th>County</th><th>Precinct</th><th>Account #</th><th>Min Bid</th>
          <th>Est. Value</th><th>Equity</th><th>Equity %</th><th>Address</th><th>Description</th><th>Links</th><th>Image</th>
        </tr>
        </thead>
        <tbody id="dealsBody"></tbody>
      </table>

      <div class="pagination">
        <button id="prevPage" onclick="changePage(-1)">&larr; Prev</button>
        <span id="pageIndicator"></span>
        <button id="nextPage" onclick="changePage(1)">Next &rarr;</button>
      </div>

      <script type="application/json" id="listingsData">{listings_json}</script>

      <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
              integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
      <script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"
              integrity="sha256-Hk4dIpcqOSb0hZjgyvFOP+cEmDXUKKNE/tT542ZbNQg=" crossorigin=""></script>
      <script>
        const VALUE_MIN = {value_min};
        const VALUE_MAX = {value_max};
        const PAGE_SIZE = 100;
        const NO_DATA_HTML = '<span class="nodata">No data available</span>';

        const ALL_LISTINGS = JSON.parse(document.getElementById('listingsData').textContent);
        let filteredListings = [];
        let currentPage = 1;

        let map = null;
        let markerLayer = null;

        function initMap() {{
          const container = document.getElementById('mapContainer');
          container.style.display = 'block';
          map = L.map('mapContainer').setView([31.0, -99.0], 6);  // roughly centered on Texas
          L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
            maxZoom: 19,
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
          }}).addTo(map);
          // Clustering keeps the map from creating a DOM marker per listing
          // up front (~3,900 of them) -- it groups nearby pins into a
          // single icon until zoomed in close enough to separate them,
          // which is most of what made the map-open-by-default page load
          // slow to begin with.
          markerLayer = L.markerClusterGroup({{ maxClusterRadius: 60 }}).addTo(map);
        }}

        function toggleMap() {{
          const container = document.getElementById('mapContainer');
          const btn = document.getElementById('toggleMap');
          const showing = container.style.display === 'block';
          if (showing) {{
            container.style.display = 'none';
            btn.textContent = 'Show map';
            return;
          }}
          container.style.display = 'block';
          btn.textContent = 'Hide map';
          map.invalidateSize();
          updateMapMarkers();
        }}

        // Escapes text for safe insertion into an HTML string -- used for
        // every scraped field (address, description, ...) since none of it
        // can be trusted to be free of "<", "&", etc.
        function escapeHtml(s) {{
          const div = document.createElement('div');
          div.textContent = s;
          return div.innerHTML;
        }}

        function formatMoney(v) {{
          return '$' + Math.round(v).toLocaleString();
        }}

        function formatCurrency(v) {{
          return v != null ? '$' + v.toLocaleString(undefined, {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }}) : null;
        }}

        function formatPercent(v) {{
          return v != null ? Math.round(v * 100) + '%' : null;
        }}

        // Red -> amber -> green as equity_pct goes from -25% (or worse) up
        // to 100%. A handful of listings have wildly negative equity (bad
        // source data -- min bid far exceeding estimated value), so the
        // color scale clamps at -25% rather than stretching to fit those
        // outliers, which would otherwise make every normal listing look
        // uniformly green by comparison. The displayed number is never
        // clamped, only the color.
        const EQUITY_COLOR_FLOOR = -0.25;
        const EQUITY_COLOR_STOPS = [
          [220, 38, 38],   // red    (-25% and below)
          [245, 158, 11],  // amber  (0%)
          [22, 163, 74],   // green  (100%)
        ];

        function equityColor(pct) {{
          const clamped = Math.max(EQUITY_COLOR_FLOOR, Math.min(1, pct));
          const [from, to, t] = clamped <= 0
            ? [EQUITY_COLOR_STOPS[0], EQUITY_COLOR_STOPS[1], (clamped - EQUITY_COLOR_FLOOR) / -EQUITY_COLOR_FLOOR]
            : [EQUITY_COLOR_STOPS[1], EQUITY_COLOR_STOPS[2], clamped];
          const rgb = from.map((c, i) => Math.round(c + (to[i] - c) * t));
          return `rgb(${{rgb.join(',')}})`;
        }}

        function equityBadge(pct) {{
          if (pct == null) return NO_DATA_HTML;
          const label = formatPercent(pct);
          return `<span class="equity-badge" style="background:${{equityColor(pct)}}">${{label}}</span>`;
        }}

        function buildRowHtml(l) {{
          const linksParts = [];
          if (l.source_url) linksParts.push(`<a href="${{escapeHtml(l.source_url)}}" target="_blank" rel="noopener noreferrer">Listing</a>`);
          if (l.maps_url) linksParts.push(`<a href="${{escapeHtml(l.maps_url)}}" target="_blank" rel="noopener noreferrer">Map</a>`);
          const linksHtml = linksParts.length ? linksParts.join(' · ') : NO_DATA_HTML;
          const imageHtml = l.image_url
            ? `<img src="${{escapeHtml(l.image_url)}}" width="80" height="80" loading="lazy" alt="Satellite view" class="thumb">`
            : NO_DATA_HTML;

          return '<tr>'
            + `<td>${{escapeHtml(l.county)}}</td>`
            + `<td>${{l.precinct ? escapeHtml(l.precinct) : NO_DATA_HTML}}</td>`
            + `<td>${{escapeHtml(l.account_number)}}</td>`
            + `<td>${{formatCurrency(l.minimum_bid) || NO_DATA_HTML}}</td>`
            + `<td>${{formatCurrency(l.estimated_value) || NO_DATA_HTML}}</td>`
            + `<td>${{formatCurrency(l.equity) || NO_DATA_HTML}}</td>`
            + `<td>${{equityBadge(l.equity_pct)}}</td>`
            + `<td>${{l.address ? escapeHtml(l.address) : NO_DATA_HTML}}</td>`
            + `<td>${{l.description ? escapeHtml(l.description) : NO_DATA_HTML}}</td>`
            + `<td>${{linksHtml}}</td>`
            + `<td>${{imageHtml}}</td></tr>`;
        }}

        function buildPopupContent(l) {{
          const div = document.createElement('div');

          if (l.image_url) {{
            const img = document.createElement('img');
            img.src = l.image_url;
            img.width = 150;
            img.height = 150;
            img.alt = 'Satellite view';
            div.appendChild(img);
            div.appendChild(document.createElement('br'));
          }}

          const county = document.createElement('strong');
          county.textContent = l.county;
          div.appendChild(county);
          div.appendChild(document.createElement('br'));

          if (l.address) {{
            div.appendChild(document.createTextNode(l.address));
            div.appendChild(document.createElement('br'));
          }}

          div.appendChild(document.createTextNode(
            `Min bid: ${{formatCurrency(l.minimum_bid) || 'No data available'}} · `
            + `Est. value: ${{formatCurrency(l.estimated_value) || 'No data available'}} · `
            + `Equity: `
          ));
          if (l.equity_pct != null) {{
            const badge = document.createElement('span');
            badge.className = 'equity-badge';
            badge.style.background = equityColor(l.equity_pct);
            badge.textContent = formatPercent(l.equity_pct);
            div.appendChild(badge);
          }} else {{
            div.appendChild(document.createTextNode('No data available'));
          }}

          const links = [];
          if (l.source_url) links.push({{ text: 'Listing', href: l.source_url }});
          if (l.maps_url) links.push({{ text: 'Map', href: l.maps_url }});
          if (links.length) {{
            div.appendChild(document.createElement('br'));
            links.forEach((link, i) => {{
              if (i > 0) div.appendChild(document.createTextNode(' · '));
              const a = document.createElement('a');
              a.href = link.href;
              a.target = '_blank';
              a.rel = 'noopener noreferrer';
              a.textContent = link.text;
              div.appendChild(a);
            }});
          }}

          return div;
        }}

        function updateMapMarkers() {{
          if (!markerLayer) return;
          markerLayer.clearLayers();
          const bounds = [];
          filteredListings.forEach(l => {{
            if (l.latitude == null || l.longitude == null) return;
            const marker = L.marker([l.latitude, l.longitude]).bindPopup(buildPopupContent(l));
            markerLayer.addLayer(marker);
            bounds.push([l.latitude, l.longitude]);
          }});
          if (bounds.length) map.fitBounds(bounds, {{ padding: [20, 20], maxZoom: 12 }});
        }}

        function renderCurrentPage() {{
          const totalPages = Math.max(1, Math.ceil(filteredListings.length / PAGE_SIZE));
          currentPage = Math.min(Math.max(currentPage, 1), totalPages);

          const start = (currentPage - 1) * PAGE_SIZE;
          const pageItems = filteredListings.slice(start, start + PAGE_SIZE);

          document.getElementById('dealsBody').innerHTML = pageItems.map(buildRowHtml).join('');
          document.getElementById('pageIndicator').textContent =
            filteredListings.length ? `Page ${{currentPage}} of ${{totalPages}}` : 'No results';
          document.getElementById('prevPage').disabled = currentPage <= 1;
          document.getElementById('nextPage').disabled = currentPage >= totalPages;
        }}

        function changePage(delta) {{
          currentPage += delta;
          renderCurrentPage();
          document.getElementById('dealsTable').scrollIntoView({{ behavior: 'smooth', block: 'start' }});
        }}

        function sortListings(arr, dir) {{
          return arr.slice().sort((a, b) => {{
            const aHas = a.equity_pct != null;
            const bHas = b.equity_pct != null;
            if (aHas && !bHas) return -1;
            if (!aHas && bHas) return 1;
            if (!aHas && !bHas) return 0;
            return dir === 'asc' ? a.equity_pct - b.equity_pct : b.equity_pct - a.equity_pct;
          }});
        }}

        function applyFilters() {{
          const locationQuery = document.getElementById('locationFilter').value.toLowerCase().trim();
          let minValue = parseFloat(document.getElementById('minValue').value);
          let maxValue = parseFloat(document.getElementById('maxValue').value);

          // Keep the two handles from crossing each other.
          if (minValue > maxValue) {{
            [minValue, maxValue] = [maxValue, minValue];
          }}

          document.getElementById('minValueLabel').textContent = formatMoney(minValue);
          document.getElementById('maxValueLabel').textContent = formatMoney(maxValue);

          const valueFilterActive = minValue > VALUE_MIN || maxValue < VALUE_MAX;

          filteredListings = ALL_LISTINGS.filter(l => {{
            if (locationQuery && !l.search_text.includes(locationQuery)) return false;
            if (valueFilterActive) {{
              if (l.estimated_value == null) return false;  // can't evaluate against an active range
              if (l.estimated_value < minValue || l.estimated_value > maxValue) return false;
            }}
            return true;
          }});

          const dir = document.getElementById('equitySort').value;
          filteredListings = sortListings(filteredListings, dir);

          currentPage = 1;
          document.getElementById('resultSummary').textContent =
            filteredListings.length + ' of ' + ALL_LISTINGS.length + ' listings shown';
          renderCurrentPage();
          updateMapMarkers();
        }}

        function applySort() {{
          applyFilters();  // re-deriving is cheap for ~4,000 rows and keeps filter+sort in one place
        }}

        function resetFilters() {{
          document.getElementById('locationFilter').value = '';
          document.getElementById('minValue').value = VALUE_MIN;
          document.getElementById('maxValue').value = VALUE_MAX;
          document.getElementById('equitySort').value = 'desc';
          applyFilters();
        }}

        initMap();
        applyFilters();
      </script>
    """

    return page_shell("GovLandScout", "home", body, extra_head=LEAFLET_HEAD)


# Friendly labels for the raw `source` domains stored per listing --
# just for display on the Impact page, doesn't affect scraping/storage.
SOURCE_LABELS = {
    "hctax.net": "Harris County Tax Office",
    "taxsales.lgbs.com": "Linebarger Goggan Blair & Sampson (tax trustee)",
    "pbfcm.com": "Perdue Brandon Fielder Collins & Mott (tax trustee)",
    "mvbalaw.com": "McCreary Veselka Bragg & Allen (tax trustee)",
    "public.tdhca.state.tx.us": "Texas Dept. of Housing & Community Affairs",
    "realestatesales.gov": "GSA Federal Real Estate Sales",
    "glo.texas.gov": "Texas Veterans Land Board",
    "hudgis-hud.opendata.arcgis.com": "HUD Foreclosed Homes (Open Data)",
    "houstontx.gov": "City of Houston Real Property",
    "publicsurplus.com": "PublicSurplus (Texas government sellers)",
}


@app.get("/impact", response_class=HTMLResponse)
def impact_page():
    conn = combined_db.get_connection()
    listings = fetch_all_listings(conn)
    sources = [r[0] for r in conn.execute("SELECT DISTINCT source FROM listings ORDER BY source").fetchall()]
    conn.close()

    total = len(listings)
    counties = len({l["county"] for l in listings})
    with_coords = sum(1 for l in listings if l["latitude"] is not None)
    priced_with_equity = [l for l in listings if l["equity"] is not None and l["equity"] > 0]
    total_equity = sum(l["equity"] for l in priced_with_equity)

    stats = [
        (f"{total:,}", "Total listings tracked"),
        (f"{counties}", "Texas counties covered"),
        (f"{len(sources)}", "Independent data sources"),
        (f"{with_coords:,}", "Listings mapped with real coordinates"),
        (f"{len(priced_with_equity):,}", "Listings with a calculated equity opportunity"),
        (f"${total_equity:,.0f}", "Total estimated equity represented"),
    ]
    stat_cards = "".join(
        f'<div class="card stat-card"><div class="stat-value">{escape(value)}</div>'
        f'<div class="stat-label">{escape(label)}</div></div>'
        for value, label in stats
    )

    source_items = "".join(
        f"<li>{escape(SOURCE_LABELS.get(s, s))} <span class=\"nodata\">({escape(s)})</span></li>"
        for s in sources
    )

    body = f"""
      <h1>Impact &amp; Numbers</h1>
      <p class="subtitle">A live snapshot of what GovLandScout is currently tracking across Texas, recomputed from the database on every page load.</p>

      <div class="stats-grid">{stat_cards}</div>

      <div class="card prose" style="padding: 1.5rem 1.75rem;">
        <h2>What "equity" means here</h2>
        <p>For listings with both a minimum bid and an independent estimated value, equity is estimated value minus
           minimum bid -- roughly, how much value a winning bidder could be getting relative to what the property
           is actually worth. Not every source provides an independent value estimate (e.g. federal and state
           surplus listings), so equity can't be calculated for all {total:,} listings -- only the
           {len(priced_with_equity):,} shown above.</p>

        <h2>Where the data comes from</h2>
        <ul>{source_items}</ul>
      </div>
    """
    return page_shell("GovLandScout - Impact", "impact", body)


@app.get("/about", response_class=HTMLResponse)
def about_page():
    body = """
      <h1>About &amp; Contact</h1>

      <div class="card prose" style="padding: 1.5rem 1.75rem; margin-bottom: 1.5rem;">
        <h2>What this is</h2>
        <p>GovLandScout aggregates real estate being sold by government entities across Texas -- county tax
           foreclosure sales, federal and state surplus property, HUD-owned foreclosed homes, and Veterans Land
           Board tracts -- into one searchable, mappable place. Rising home prices and limited housing
           affordability make it harder to find a way in; these listings are already public, just scattered
           across dozens of separate county, state, and federal sites. This project pulls them together.</p>

        <h2>How it works</h2>
        <p>A set of scrapers run on a daily schedule, each pulling directly from an official or government-retained
           source (see the <a href="/impact">Impact page</a> for the full list), normalizing everything into one
           shared database. Nothing here is editorialized -- prices, descriptions, and account numbers are shown
           as published by the source, and every listing links back to where it came from so you can verify it
           yourself before bidding on anything.</p>

        <h2>A word of caution</h2>
        <p>This is an independent research tool, not legal or financial advice, and not affiliated with any county,
           state, or federal agency. Data can be outdated, incomplete, or contain source errors. Always verify
           details directly with the listing agency before bidding on or purchasing any property.</p>
      </div>

      <div class="card contact-card">
        <h2 style="margin-top:0;">Contact</h2>
        <p>Questions, corrections, or something looks wrong? Reach out at
           <a href="mailto:govlandscout@gmail.com">govlandscout@gmail.com</a>.</p>
        <p style="margin-bottom:0;">The project's source is publicly available on
           <a href="https://github.com/GovLandScout/GovLandScout" target="_blank" rel="noopener noreferrer">GitHub</a>.</p>
      </div>
    """
    return page_shell("GovLandScout - About", "about", body)


@app.get("/api/deals")
def deals_api():
    return get_all_listings()
