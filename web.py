"""
GovLandScout - Web viewer

FastAPI app that serves every scraped listing across all three sources
(hctax_scraper.py, lgbs_scraper.py, gsa_scraper.py) via combined_db.py.
Listings with a real equity calculation are ranked first; listings
missing pricing data (or with no independent value estimate at all, like
the GSA federal auctions) are shown afterward with "No data available"
in place of the fields that don't apply, rather than being hidden.

The table renders fully server-side (all rows, all data attached), and a
small vanilla-JS layer filters/sorts the already-rendered rows in the
browser -- no extra requests, no client-side framework.

Run with: venv/bin/uvicorn web:app --reload
"""

import math
from html import escape

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

import combined_db
from find_deals import fetch_all_listings

app = FastAPI(title="GovLandScout")

NO_DATA = "No data available"
NO_DATA_HTML = f'<span class="nodata">{NO_DATA}</span>'

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


def money_cell(value: float | None) -> str:
    return f"${value:,.2f}" if value is not None else NO_DATA_HTML


def pct_cell(value: float | None) -> str:
    return f"{value:.0%}" if value is not None else NO_DATA_HTML


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

    def links_cell(l: dict) -> str:
        parts = []
        if l["source_url"]:
            parts.append(f'<a href="{escape(l["source_url"])}" target="_blank" rel="noopener noreferrer">Listing</a>')
        if l["maps_url"]:
            parts.append(f'<a href="{escape(l["maps_url"])}" target="_blank" rel="noopener noreferrer">Map</a>')
        return " · ".join(parts) if parts else NO_DATA_HTML

    def image_cell(l: dict) -> str:
        if l["latitude"] is None or l["longitude"] is None:
            return NO_DATA_HTML
        url = escape(satellite_thumbnail_url(l["latitude"], l["longitude"]))
        return f'<img src="{url}" width="80" height="80" loading="lazy" alt="Satellite view" class="thumb">'

    def row_html(l: dict) -> str:
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
        search_text = escape(f"{search_county} {l['precinct']} {search_place} {metro_terms}".lower())
        value_attr = l["estimated_value"] if l["estimated_value"] is not None else ""
        equity_pct_attr = l["equity_pct"] if l["equity_pct"] is not None else ""
        lat_attr = l["latitude"] if l["latitude"] is not None else ""
        lon_attr = l["longitude"] if l["longitude"] is not None else ""
        return (
            f'<tr data-search="{search_text}" data-value="{value_attr}" data-equity-pct="{equity_pct_attr}"'
            f' data-lat="{lat_attr}" data-lon="{lon_attr}">'
            f"<td>{escape(l['county'])}</td>"
            f"<td>{escape(l['precinct']) if l['precinct'] else NO_DATA_HTML}</td>"
            f"<td>{escape(l['account_number'])}</td>"
            f"<td>{money_cell(l['minimum_bid'])}</td>"
            f"<td>{money_cell(l['estimated_value'])}</td>"
            f"<td>{money_cell(l['equity'])}</td>"
            f"<td>{pct_cell(l['equity_pct'])}</td>"
            f"<td>{escape(l['address']) if l['address'] else NO_DATA_HTML}</td>"
            f"<td>{escape(l['description'][:120]) if l['description'] else NO_DATA_HTML}</td>"
            f"<td>{links_cell(l)}</td>"
            f"<td>{image_cell(l)}</td></tr>"
        )

    rows = "".join(row_html(l) for l in listings)

    return f"""
    <html>
    <head>
      <title>GovLandScout</title>
      <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
            integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="" />
      <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css"
            integrity="sha256-YU3qCpj/P06tdPBJGPax0bm6Q1wltfwjsho5TR4+TYc=" crossorigin="" />
      <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css"
            integrity="sha256-YSWCMtmNZNwqex4CEw1nQhvFub2lmU7vcCKP+XVwwXA=" crossorigin="" />
      <style>
        html {{ color-scheme: light; }}
        * {{ box-sizing: border-box; }}
        body {{
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
          margin: 0; padding: 2rem clamp(1rem, 4vw, 3rem);
          background: #f1f5f9; color: #0f172a;
        }}

        h1 {{ font-size: 2rem; font-weight: 800; letter-spacing: -0.02em; margin: 0 0 0.4rem; }}
        .subtitle {{ margin: 0 0 1.5rem; color: #475569; font-size: 0.95rem; line-height: 1.55; max-width: 75ch; }}

        table {{
          border-collapse: collapse; width: 100%; background: #fff;
          border: 1px solid #e2e8f0; box-shadow: 0 1px 3px rgba(15, 23, 42, 0.06);
        }}
        th, td {{ border-bottom: 1px solid #e2e8f0; padding: 10px 14px; text-align: left; font-size: 0.875rem; color: #1e293b; }}
        th {{
          background: #f8fafc; position: sticky; top: 0; font-weight: 700; color: #334155;
          text-transform: uppercase; font-size: 0.72rem; letter-spacing: 0.04em; border-bottom: 2px solid #e2e8f0;
        }}
        tr:nth-child(even) td {{ background: #f8fafc; }}
        tr:nth-child(odd) td {{ background: #fff; }}
        tr:hover td {{ background: #eff6ff; }}
        .nodata {{ color: #94a3b8; font-style: italic; }}
        .thumb {{ display: block; object-fit: cover; border-radius: 6px; }}

        .card {{
          background: #fff; border: 1px solid #e2e8f0; border-radius: 12px;
          box-shadow: 0 1px 3px rgba(15, 23, 42, 0.06);
        }}

        .controls {{
          display: flex; flex-wrap: wrap; gap: 1.5rem; align-items: flex-end;
          padding: 1.25rem 1.5rem; margin-bottom: 1.25rem;
        }}
        .control {{ display: flex; flex-direction: column; gap: 0.35rem; }}
        .control label {{
          font-size: 0.75rem; font-weight: 700; color: #475569;
          text-transform: uppercase; letter-spacing: 0.04em;
        }}
        .control input[type="text"], .control select {{
          padding: 0.5rem 0.7rem; font-size: 0.9rem; border: 1px solid #cbd5e1; border-radius: 8px;
          background: #fff; color: #0f172a;
        }}
        .control input[type="text"]:focus, .control select:focus {{
          outline: none; border-color: #2563eb; box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.15);
        }}
        .range-control {{ min-width: 260px; }}
        .range-control .range-row {{ display: flex; align-items: center; gap: 0.5rem; }}
        .range-control input[type="range"] {{ flex: 1; accent-color: #2563eb; }}
        .range-value {{ font-size: 0.8rem; color: #475569; white-space: nowrap; min-width: 5.5rem; }}
        #resetFilters, #toggleMap {{
          padding: 0.55rem 1.1rem; font-size: 0.85rem; font-weight: 600; border-radius: 8px;
          cursor: pointer; margin-right: 0.5rem; border: 1px solid transparent;
          transition: background 0.15s ease, border-color 0.15s ease;
        }}
        #resetFilters {{ background: #fff; color: #334155; border-color: #cbd5e1; }}
        #resetFilters:hover {{ background: #f1f5f9; }}
        #toggleMap {{ background: #2563eb; color: #fff; }}
        #toggleMap:hover {{ background: #1d4ed8; }}
        #resultSummary {{ font-size: 0.85rem; color: #475569; }}
        #mapContainer {{ height: 500px; margin-bottom: 1.25rem; overflow: hidden; }}
      </style>
    </head>
    <body>
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
        <tbody id="dealsBody">
        {rows}
        </tbody>
      </table>

      <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
              integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
      <script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"
              integrity="sha256-Hk4dIpcqOSb0hZjgyvFOP+cEmDXUKKNE/tT542ZbNQg=" crossorigin=""></script>
      <script>
        const VALUE_MIN = {value_min};
        const VALUE_MAX = {value_max};

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

        function buildPopupContent(row) {{
          const div = document.createElement('div');

          const imgCell = row.cells[10];
          const img = imgCell.querySelector('img');
          if (img) {{
            const imgClone = img.cloneNode(true);
            imgClone.removeAttribute('loading');  // it's about to be visible -- fetch it now
            imgClone.width = 150;
            imgClone.height = 150;
            div.appendChild(imgClone);
            div.appendChild(document.createElement('br'));
          }}

          const county = document.createElement('strong');
          county.textContent = row.cells[0].textContent;
          div.appendChild(county);
          div.appendChild(document.createElement('br'));

          const address = row.cells[7].textContent;
          if (address) {{
            div.appendChild(document.createTextNode(address));
            div.appendChild(document.createElement('br'));
          }}

          div.appendChild(document.createTextNode(
            `Min bid: ${{row.cells[3].textContent}} · Est. value: ${{row.cells[4].textContent}} · Equity: ${{row.cells[6].textContent}}`
          ));

          const linksCell = row.cells[9];
          if (linksCell.querySelector('a')) {{
            div.appendChild(document.createElement('br'));
            const linksClone = linksCell.cloneNode(true);
            while (linksClone.firstChild) div.appendChild(linksClone.firstChild);
          }}

          return div;
        }}

        function updateMapMarkers() {{
          if (!markerLayer) return;
          markerLayer.clearLayers();
          const bounds = [];
          document.querySelectorAll('#dealsBody tr').forEach(row => {{
            if (row.style.display === 'none') return;
            const lat = parseFloat(row.dataset.lat);
            const lon = parseFloat(row.dataset.lon);
            if (isNaN(lat) || isNaN(lon)) return;
            const marker = L.marker([lat, lon]).bindPopup(buildPopupContent(row));
            markerLayer.addLayer(marker);
            bounds.push([lat, lon]);
          }});
          if (bounds.length) map.fitBounds(bounds, {{ padding: [20, 20], maxZoom: 12 }});
        }}

        function formatMoney(v) {{
          return '$' + Math.round(v).toLocaleString();
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

          const rows = document.querySelectorAll('#dealsBody tr');
          let visibleCount = 0;

          rows.forEach(row => {{
            let visible = true;

            if (locationQuery && !row.dataset.search.includes(locationQuery)) {{
              visible = false;
            }}

            if (visible && valueFilterActive) {{
              if (row.dataset.value === '') {{
                visible = false;  // no estimated value -- can't evaluate against an active range
              }} else {{
                const v = parseFloat(row.dataset.value);
                if (v < minValue || v > maxValue) visible = false;
              }}
            }}

            row.style.display = visible ? '' : 'none';
            if (visible) visibleCount++;
          }});

          document.getElementById('resultSummary').textContent = visibleCount + ' of ' + rows.length + ' listings shown';
          updateMapMarkers();
        }}

        function applySort() {{
          const dir = document.getElementById('equitySort').value;
          const tbody = document.getElementById('dealsBody');
          const rows = Array.from(tbody.querySelectorAll('tr'));

          rows.sort((a, b) => {{
            const aHas = a.dataset.equityPct !== '';
            const bHas = b.dataset.equityPct !== '';
            if (aHas && !bHas) return -1;
            if (!aHas && bHas) return 1;
            if (!aHas && !bHas) return 0;
            const av = parseFloat(a.dataset.equityPct);
            const bv = parseFloat(b.dataset.equityPct);
            return dir === 'asc' ? av - bv : bv - av;
          }});

          rows.forEach(row => tbody.appendChild(row));
        }}

        function resetFilters() {{
          document.getElementById('locationFilter').value = '';
          document.getElementById('minValue').value = VALUE_MIN;
          document.getElementById('maxValue').value = VALUE_MAX;
          document.getElementById('equitySort').value = 'desc';
          applyFilters();
          applySort();
        }}

        initMap();
        applyFilters();
      </script>
    </body>
    </html>
    """


@app.get("/api/deals")
def deals_api():
    return get_all_listings()
