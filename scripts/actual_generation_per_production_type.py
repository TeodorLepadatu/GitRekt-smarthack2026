"""Scrape ENTSO-E "Actual Generation per Production Type" for Romania into one CSV.

Default range: today back 4 years. Output: data/actual_generation_per_production_type.csv

    $env:ENTSOE_TOKEN = "eyJ..."     # Authorization header from a logged-in browser session
    python scripts/actual_generation_per_production_type.py [--start YYYY-MM-DD] [--end YYYY-MM-DD]
"""

import sys

from entsoe_export import run

if __name__ == "__main__":
    sys.exit(run(
        name="actual_generation_per_production_type",
        use_case="generation/actual/perType/generation/export",
        tz="UTC",
    ))
