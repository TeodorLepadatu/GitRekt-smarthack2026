"""Combine the ENTSO-E load and generation CSVs into data/dataset.csv.

Inputs:
  * total load  - MTU in CET/CEST, e.g. "07/11/2022 00:00 - 07/11/2022 00:15"
                  (clock-change rows carry "(CET)"/"(CEST)" suffixes)
  * generation  - MTU in UTC, e.g. "07/11/2022 00:00:00 - 07/11/2022 00:15:00",
                  one row per production type (long format)

Both are aligned on the UTC start of the MTU interval. `timestamp` is that start
as Unix epoch seconds (e.g. 1667779200 = 2022-11-07 00:00 UTC). Only intervals present in both
files are kept. Missing values ("-", "n/e") become empty cells.

Usage:
    python scripts/build_dataset.py [--load PATH] [--generation PATH] [--out data/dataset.csv]
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DATA = Path("data")
DEFAULT_LOAD = DATA / "total_load_day_ahead_actual" / "total_load_day_ahead_actual_2022-11-07_2026-11-07.csv"
DEFAULT_GENERATION = DATA / "actual_generation_per_production_type.csv"

# output column -> "Production Type" value in the generation CSV
GENERATION_COLUMNS = {
    "biomass": "Biomass",
    "energy_storage": "Energy storage",
    "fossil_brown_coal/Lignite": "Fossil Brown coal/Lignite",
    "fossil_gas": "Fossil Gas",
    "hydro_run_of_river_and_pondage": "Hydro Run-of-river and pondage",
    "hydro_water_reservoir": "Hydro Water Reservoir",
    "nuclear": "Nuclear",
    "solar": "Solar",
    "wind_onshore": "Wind Onshore",
}
HEADER = ["timestamp", "actual_total_load", *GENERATION_COLUMNS]
MISSING = {"", "-", "n/e", "N/A"}

_START = re.compile(r"^\s*(\d{2}/\d{2}/\d{4} \d{2}:\d{2}(?::\d{2})?)\s*(?:\((CET|CEST)\))?\s*-")


def _last_sunday(year: int, month: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - 6) % 7)


def _is_summer_utc(utc: datetime) -> bool:
    """EU DST: 01:00 UTC last Sunday of March .. 01:00 UTC last Sunday of October."""
    start = datetime.combine(_last_sunday(utc.year, 3), datetime.min.time()) + timedelta(hours=1)
    end = datetime.combine(_last_sunday(utc.year, 10), datetime.min.time()) + timedelta(hours=1)
    return start <= utc < end


def _parse_start(mtu: str) -> tuple[datetime, str | None]:
    m = _START.match(mtu)
    if not m:
        raise ValueError(f"Unrecognised MTU: {mtu!r}")
    text, zone = m.groups()
    fmt = "%d/%m/%Y %H:%M:%S" if text.count(":") == 2 else "%d/%m/%Y %H:%M"
    return datetime.strptime(text, fmt), zone


def utc_start_from_cet(mtu: str) -> datetime:
    local, zone = _parse_start(mtu)
    if zone:  # explicit suffix around clock changes (resolves the repeated hour)
        return local - timedelta(hours=2 if zone == "CEST" else 1)
    as_summer = local - timedelta(hours=2)
    return as_summer if _is_summer_utc(as_summer) else local - timedelta(hours=1)


def utc_start_from_utc(mtu: str) -> datetime:
    return _parse_start(mtu)[0]


def clean(value: str) -> str:
    return "" if value.strip() in MISSING else value.strip()


def read_load(path: Path) -> dict[datetime, str]:
    out: dict[datetime, str] = {}
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        mtu_col = next(c for c in reader.fieldnames if c.startswith("MTU"))
        for row in reader:
            ts = utc_start_from_cet(row[mtu_col])
            if ts in out:
                raise ValueError(f"Duplicate load timestamp {ts} from {row[mtu_col]!r}")
            out[ts] = clean(row["Actual Total Load (MW)"])
    return out


def read_generation(path: Path) -> dict[datetime, dict[str, str]]:
    wanted = {v: k for k, v in GENERATION_COLUMNS.items()}
    out: dict[datetime, dict[str, str]] = {}
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        mtu_col = next(c for c in reader.fieldnames if c.startswith("MTU"))
        for row in reader:
            col = wanted.get(row["Production Type"])
            if col is None:
                continue
            ts = utc_start_from_utc(row[mtu_col])
            out.setdefault(ts, {})[col] = clean(row["Generation (MW)"])
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--load", type=Path, default=DEFAULT_LOAD)
    p.add_argument("--generation", type=Path, default=DEFAULT_GENERATION)
    p.add_argument("--out", type=Path, default=DATA / "dataset.csv")
    args = p.parse_args()

    load = read_load(args.load)
    gen = read_generation(args.generation)
    common = sorted(load.keys() & gen.keys())
    if not common:
        print("No overlapping timestamps between the two files", file=sys.stderr)
        return 1

    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        for ts in common:
            g = gen[ts]
            writer.writerow([int(ts.replace(tzinfo=timezone.utc).timestamp()), load[ts],
                             *(g.get(c, "") for c in GENERATION_COLUMNS)])

    print(f"load: {len(load)} intervals, generation: {len(gen)} intervals")
    print(f"only in load: {len(load.keys() - gen.keys())}, only in generation: {len(gen.keys() - load.keys())}")
    print(f"{len(common)} rows ({common[0]:%Y-%m-%d %H:%M} .. {common[-1]:%Y-%m-%d %H:%M} UTC) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
