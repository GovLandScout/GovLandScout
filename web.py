"""
GovLandScout - Web viewer

FastAPI app that serves every scraped listing across all three sources
(hctax_scraper.py, lgbs_scraper.py, gsa_scraper.py) via combined_db.py.
Listings with a real equity calculation are ranked first; listings
missing pricing data (or with no independent value estimate at all, like
the GSA federal auctions) are shown afterward with "No data available"
in place of the fields that don't apply, rather than being hidden.
Run with: venv/bin/uvicorn web:app --reload
"""

from html import escape

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

import combined_db
from find_deals import fetch_all_listings

app = FastAPI(title="GovLandScout")

NO_DATA = "No data available"
NO_DATA_HTML = f'<span class="nodata">{NO_DATA}</span>'


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


@app.get("/", response_class=HTMLResponse)
def deals_page():
    listings = get_all_listings()
    priced_count = sum(1 for l in listings if l["equity_pct"] is not None)

    def links_cell(l: dict) -> str:
        parts = []
        if l["source_url"]:
            parts.append(f'<a href="{escape(l["source_url"])}" target="_blank" rel="noopener noreferrer">Listing</a>')
        if l["maps_url"]:
            parts.append(f'<a href="{escape(l["maps_url"])}" target="_blank" rel="noopener noreferrer">Map</a>')
        return " · ".join(parts) if parts else NO_DATA_HTML

    rows = "".join(
        f"<tr><td>{escape(l['county'])}</td>"
        f"<td>{escape(l['precinct']) if l['precinct'] else NO_DATA_HTML}</td>"
        f"<td>{escape(l['account_number'])}</td>"
        f"<td>{money_cell(l['minimum_bid'])}</td>"
        f"<td>{money_cell(l['estimated_value'])}</td>"
        f"<td>{money_cell(l['equity'])}</td>"
        f"<td>{pct_cell(l['equity_pct'])}</td>"
        f"<td>{escape(l['address']) if l['address'] else NO_DATA_HTML}</td>"
        f"<td>{escape(l['description'][:120]) if l['description'] else NO_DATA_HTML}</td>"
        f"<td>{links_cell(l)}</td></tr>"
        for l in listings
    )

    return f"""
    <html>
    <head>
      <title>GovLandScout - Tax Sale Deals</title>
      <style>
        html {{ color-scheme: light; }}
        body {{ font-family: sans-serif; margin: 2rem; background: #fff; color: #111; }}
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; font-size: 0.9rem; color: #111; }}
        th {{ background: #f5f5f5; position: sticky; top: 0; }}
        tr:nth-child(even) td {{ background: #fafafa; }}
        tr:nth-child(odd) td {{ background: #fff; }}
        .nodata {{ color: #999; font-style: italic; }}
      </style>
    </head>
    <body>
      <h1>Tax Sale Deals</h1>
      <p>{len(listings)} total listings across all sources. {priced_count} have a full equity calculation and are
         ranked first below; the rest are shown afterward with "{NO_DATA}" where a field doesn't apply.</p>
      <table>
        <tr>
          <th>County</th><th>Precinct</th><th>Account #</th><th>Min Bid</th>
          <th>Est. Value</th><th>Equity</th><th>Equity %</th><th>Address</th><th>Description</th><th>Links</th>
        </tr>
        {rows}
      </table>
    </body>
    </html>
    """


@app.get("/api/deals")
def deals_api():
    return get_all_listings()
