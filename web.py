"""
GovLandScout - Web viewer

FastAPI app that serves the ranked deals list scraped by hctax_scraper.py.
Run with: venv/bin/uvicorn web:app --reload
"""

import sqlite3
from html import escape

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from find_deals import fetch_priced_listings, fetch_unpriced_count

DB_PATH = "tax_sales.db"

app = FastAPI(title="GovLandScout")


def get_ranked_listings() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    listings = fetch_priced_listings(conn)
    listings.sort(key=lambda l: l["equity_pct"], reverse=True)
    conn.close()
    return listings


@app.get("/", response_class=HTMLResponse)
def deals_page():
    listings = get_ranked_listings()

    conn = sqlite3.connect(DB_PATH)
    unpriced_count = fetch_unpriced_count(conn)
    conn.close()

    rows = "".join(
        f"<tr><td>{escape(l['precinct'])}</td>"
        f"<td>{escape(l['account_number'])}</td>"
        f"<td>${l['minimum_bid']:,.2f}</td>"
        f"<td>${l['adjudged_value']:,.2f}</td>"
        f"<td>${l['equity']:,.2f}</td>"
        f"<td>{l['equity_pct']:.0%}</td>"
        f"<td>{escape(l['legal_description'][:120])}</td></tr>"
        for l in listings
    )

    return f"""
    <html>
    <head>
      <title>GovLandScout - Harris County Tax Sale Deals</title>
      <style>
        html {{ color-scheme: light; }}
        body {{ font-family: sans-serif; margin: 2rem; background: #fff; color: #111; }}
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; font-size: 0.9rem; color: #111; }}
        th {{ background: #f5f5f5; position: sticky; top: 0; }}
        tr:nth-child(even) td {{ background: #fafafa; }}
        tr:nth-child(odd) td {{ background: #fff; }}
      </style>
    </head>
    <body>
      <h1>Harris County Tax Sale Deals</h1>
      <p>{len(listings)} priced listings, ranked by equity (adjudged value minus minimum bid).
         {unpriced_count} additional listings have no pricing published yet and are not shown.</p>
      <table>
        <tr>
          <th>Precinct</th><th>Account #</th><th>Min Bid</th>
          <th>Adjudged Value</th><th>Equity</th><th>Equity %</th><th>Description</th>
        </tr>
        {rows}
      </table>
    </body>
    </html>
    """


@app.get("/api/deals")
def deals_api():
    return get_ranked_listings()
