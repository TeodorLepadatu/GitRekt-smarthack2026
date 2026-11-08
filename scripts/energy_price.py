"""Scrape ENTSO-E day-ahead energy prices for Romania into one CSV.

Default range: 1 January of the current year until today.
Output: data/energy_price.csv with two columns: `timestamp` (Unix epoch seconds,
UTC, start of the MTU interval) and the day-ahead price in EUR/MWh.

    $env:ENTSOE_TOKEN = "eyJ..."     # Authorization header from a logged-in browser session
    python scripts/energy_price.py [--start YYYY-MM-DD] [--end YYYY-MM-DD]
"""

import csv
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from entsoe_export import run


PRICE_COLUMN = "Day-ahead Price (EUR/MWh)"


def mtu_to_unix(path: Path) -> None:
    """Keep only `timestamp` (Unix start of the MTU interval) and the day-ahead price."""
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    header, body = rows[0], rows[1:]
    if header[0] == "timestamp":  # already processed
        i = None
    else:
        i = next(k for k, c in enumerate(header) if c.startswith("MTU"))
    p = header.index(PRICE_COLUMN)

    def start(mtu: str) -> int:
        text = mtu.split(" - ")[0].strip()
        fmt = "%d/%m/%Y %H:%M:%S" if text.count(":") == 2 else "%d/%m/%Y %H:%M"
        return int(datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp())

    out = [[int(r[0]) if i is None else start(r[i]), r[p]] for r in body if r]
    out.sort(key=lambda r: r[0])
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", PRICE_COLUMN])
        w.writerows(out)


if __name__ == "__main__":
    sys.exit(run(
        name="energy_price",
        use_case="market/energyPrices/export",
        tz="UTC",
        default_start=date(date.today().year, 1, 1),
        postprocess=mtu_to_unix,
    ))
