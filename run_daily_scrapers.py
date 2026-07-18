"""
Runs all county scrapers back to back so govlandscout.db (the combined
table they all write into) gets a full refresh in one daily pass.

Uses subprocess rather than importing and calling main() directly so each
scraper still runs as its own clean process (matching how they behave when
run manually), and one crashing doesn't take the others down with it.
"""

import subprocess
import sys
from pathlib import Path

SCRAPERS = [
    "hctax_scraper.py", "lgbs_scraper.py", "gsa_scraper.py",
    "tdhca_scraper.py", "houston_scraper.py", "pbfcm_harris_scraper.py",
]


def main():
    project_dir = Path(__file__).resolve().parent

    for scraper in SCRAPERS:
        print(f"--- Running {scraper} ---")
        result = subprocess.run([sys.executable, scraper], cwd=project_dir)
        if result.returncode != 0:
            print(f"--- {scraper} failed with exit code {result.returncode} ---")


if __name__ == "__main__":
    main()
