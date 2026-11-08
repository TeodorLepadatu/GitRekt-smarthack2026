"""Transelectrica (Romania's grid operator): national load and production per type, no login.

The public "Grafic SEN" chart on transelectrica.ro exports any date range as
samples about every 10 minutes (local time). ENTSO-E's Romanian data comes from
Transelectrica: against predictions/dataset.csv (30 Oct - 7 Nov 2026, 725
intervals) load, solar, wind, hydro and nuclear match with correlation
0.995-1.000; gas reads ~80 MW and coal ~120 MW higher (ENTSO-E's lignite has
reporting gaps, and "Carbune" includes hard coal).

    python transelectrica.py              # the last 2 days, as 15-min intervals
    python transelectrica.py --days 7
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import pandas as pd

URL = "https://www.transelectrica.ro/widget/web/tel/sen-grafic"
PORTLET = "SENGrafic_WAR_SENGraficportlet"
LOCAL_TZ = "Europe/Bucharest"
# Fields of one sample, after its timestamp
FIELDS = ["consumption", "planned_consumption", "production", "net_import",
          "coal", "gas", "hydro", "nuclear", "wind", "solar", "biomass"]
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; gitrekt-energy-agent/1.0)"}


class TranselectricaError(RuntimeError):
    pass


def _local_day(dt: datetime):
    return pd.Timestamp(dt).tz_convert(LOCAL_TZ)


def fetch_samples(start: datetime, end: datetime, retries: int = 3, timeout: int = 120) -> pd.DataFrame:
    """Raw samples between the local days of start and end: 'utc' (tz-aware) + FIELDS (MW)."""
    a, b = _local_day(start), _local_day(end)
    p = f"_{PORTLET}_"
    params = {
        "p_p_id": PORTLET, "p_p_lifecycle": "2", "p_p_state": "maximized", "p_p_mode": "view",
        "p_p_cacheability": "cacheLevelPage", f"{p}random": str(int(time.time())), f"{p}display": "CONS",
        f"{p}start_day": a.day, f"{p}start_month": a.month, f"{p}start_year": a.year,
        f"{p}start_Hour": 0, f"{p}start_Minute": 0,
        f"{p}end_day": b.day, f"{p}end_month": b.month, f"{p}end_year": b.year,
        f"{p}end_Hour": 23, f"{p}end_Minute": 59,
    }
    url = f"{URL}?{urllib.parse.urlencode(params)}"
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=timeout) as r:
                text = r.read().decode("utf-8", errors="replace")
            return parse(text)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries:
                raise TranselectricaError(f"transelectrica.ro unreachable: {e}") from e
            time.sleep(5 * attempt)
    raise AssertionError("unreachable")


def parse(text: str) -> pd.DataFrame:
    """'dd-mm-yyyy HH:MM:SS;v1;...;v11;|...' -> samples. Repeated DST hour samples are dropped."""
    rows = []
    for rec in text.split("|"):
        parts = rec.strip().split(";")
        if len(parts) < 1 + len(FIELDS) or not parts[0]:
            continue
        try:
            rows.append([parts[0], *(float(x) for x in parts[1:1 + len(FIELDS)])])
        except ValueError:
            continue
    if not rows and text.strip():
        raise TranselectricaError(f"unexpected reply from transelectrica.ro: {text[:200]!r}")
    df = pd.DataFrame(rows, columns=["local", *FIELDS])
    local = pd.to_datetime(df.pop("local"), format="%d-%m-%Y %H:%M:%S")
    df.insert(0, "utc", local.dt.tz_localize(LOCAL_TZ, ambiguous="NaT", nonexistent="NaT").dt.tz_convert("UTC"))
    return df.dropna(subset=["utc"]).reset_index(drop=True)


def to_intervals(samples: pd.DataFrame) -> pd.DataFrame:
    """Mean of the samples in each 15-min UTC interval: 'timestamp' (unix start) + FIELDS."""
    secs = (samples["utc"] - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1)
    out = samples[FIELDS].groupby((secs // 900) * 900).mean()
    out.index.name = "timestamp"
    return out.reset_index()


def fetch_intervals(start: datetime, end: datetime) -> pd.DataFrame:
    """15-min intervals starting in [start, end)."""
    df = to_intervals(fetch_samples(start, end))
    t0, t1 = int(start.timestamp()), int(end.timestamp())
    return df[(df.timestamp >= t0) & (df.timestamp < t1)].reset_index(drop=True)


def main() -> int:
    p = argparse.ArgumentParser(description="Fetch recent national load and production from Transelectrica.")
    p.add_argument("--days", type=int, default=2)
    args = p.parse_args()
    end = datetime.now(timezone.utc)
    df = fetch_intervals(end - timedelta(days=args.days), end)
    df.insert(1, "utc", pd.to_datetime(df.timestamp, unit="s"))
    print(df.tail(8).round(0).to_string(index=False))
    print(f"{len(df)} intervals")
    return 0


if __name__ == "__main__":
    sys.exit(main())
