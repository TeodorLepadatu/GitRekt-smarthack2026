"""Add weather + calendar features to dataset.csv for ML training.

Only pulls weather that drives a column in the dataset:
  actual_total_load  <- temperature (population centres)
  solar              <- shortwave irradiance, cloud cover (PV regions)
  wind_onshore       <- wind speed at 80 m (Dobrogea / Galati)
  hydro_*            <- precipitation
Nuclear, gas, lignite and biomass are dispatch-driven, not weather-driven.

Source: Open-Meteo Historical Forecast API, 15-min resolution, UTC unix
timestamps (same grid as dataset.csv). Archived *forecasts* are used so the
training inputs look like what the live system receives.

Usage (from the repo root or predictions/):
    python predictions/build_dataset.py            # uses cached downloads
    python predictions/build_dataset.py --refresh  # re-download weather

Reads only the original energy columns (timestamp, load, production) from
dataset.csv, so it is safe to rerun, and writes the merged table back to it.
"""

from __future__ import annotations

import argparse
import time
from datetime import date, datetime, timezone
from pathlib import Path

import holidays
import numpy as np
import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
DATASET_CSV = HERE / "dataset.csv"
CACHE_DIR = HERE / "data" / "weather"

API_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
LOCAL_TZ = "Europe/Bucharest"

PRODUCTION_COLS = [
    "biomass", "energy_storage", "fossil_brown_coal/Lignite", "fossil_gas",
    "hydro_run_of_river_and_pondage", "hydro_water_reservoir", "nuclear",
    "solar", "wind_onshore",
]

# Variables fetched per site role.
ROLE_VARS = {
    "load": ["temperature_2m", "precipitation"],
    "solar": ["shortwave_radiation", "cloud_cover"],
    "wind": ["wind_speed_80m"],
}

# name: (lat, lon, role, weight). Load weights ~ population in millions.
SITES = {
    "bucharest": (44.43, 26.10, "load", 2.10),
    "cluj":      (46.77, 23.60, "load", 0.41),
    "iasi":      (47.16, 27.59, "load", 0.37),
    "timisoara": (45.75, 21.23, "load", 0.32),
    "constanta": (44.18, 28.63, "load", 0.28),
    "craiova":   (44.32, 23.80, "load", 0.27),
    "brasov":    (45.65, 25.61, "load", 0.25),
    "wind_tulcea":   (45.00, 28.80, "wind", 1.0),
    "wind_cogealac": (44.55, 28.55, "wind", 1.0),
    "wind_galati":   (45.40, 28.00, "wind", 0.5),
    "solar_olt":     (44.30, 24.00, "solar", 1.0),
    "solar_giurgiu": (43.90, 25.97, "solar", 1.0),
    "solar_banat":   (45.60, 21.50, "solar", 0.5),
}

# Final weather columns written to dataset.csv: (role, variable) -> column name.
WEATHER_COLS = {
    ("load", "temperature_2m"): "temperature_2m",
    ("load", "precipitation"): "precipitation",
    ("solar", "shortwave_radiation"): "shortwave_radiation",
    ("solar", "cloud_cover"): "cloud_cover",
    ("wind", "wind_speed_80m"): "wind_speed_80m",
}


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
def _get(params: dict, retries: int = 5, url: str = API_URL) -> dict:
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=180)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code}: {r.text[:200]}")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            wait = 5 * 2 ** attempt
            print(f"    request failed ({exc}); retry in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"Open-Meteo request failed after {retries} attempts")


def fetch_site(name: str, start: date, end: date, refresh: bool) -> pd.DataFrame:
    lat, lon, role, _ = SITES[name]
    variables = ROLE_VARS[role]
    chunks = []
    for year in range(start.year, end.year + 1):
        cache = CACHE_DIR / f"{name}_{year}.csv"
        if cache.exists() and not refresh:
            chunks.append(pd.read_csv(cache))
            continue
        s, e = max(start, date(year, 1, 1)), min(end, date(year, 12, 31))
        print(f"  downloading {name} {s} -> {e}")
        js = _get({
            "latitude": lat, "longitude": lon,
            "start_date": s.isoformat(), "end_date": e.isoformat(),
            "minutely_15": ",".join(variables),
            "timezone": "UTC", "timeformat": "unixtime",
        })
        df = pd.DataFrame(js["minutely_15"]).rename(columns={"time": "timestamp"})
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache, index=False)
        chunks.append(df)
        time.sleep(1)
    df = pd.concat(chunks, ignore_index=True).drop_duplicates("timestamp")
    return df.set_index("timestamp").sort_index()


def build_weather(timestamps: pd.Series, refresh: bool) -> pd.DataFrame:
    start = datetime.fromtimestamp(int(timestamps.min()), timezone.utc).date()
    end = datetime.fromtimestamp(int(timestamps.max()), timezone.utc).date()
    print(f"fetching weather {start} -> {end} for {len(SITES)} sites ...")

    per_site = {n: fetch_site(n, start, end, refresh).reindex(timestamps.values) for n in SITES}
    return aggregate_sites(per_site, timestamps.values)


def aggregate_sites(per_site: dict[str, pd.DataFrame], index) -> pd.DataFrame:
    """Combine per-site weather into the weighted national columns of WEATHER_COLS."""
    out = pd.DataFrame(index=index)
    for (role, var), col in WEATHER_COLS.items():
        names = [n for n, s in SITES.items() if s[2] == role]
        vals = np.column_stack([per_site[n][var].to_numpy(dtype=float) for n in names])
        w = np.array([SITES[n][3] for n in names])
        valid = ~np.isnan(vals)
        den = (valid * w).sum(axis=1)
        num = np.nansum(vals * w, axis=1)
        out[col] = np.where(den > 0, num / np.where(den > 0, den, 1), np.nan)

    # Fill short holes (<= 2 h) only; longer gaps stay NaN.
    out = out.interpolate(limit=8, limit_area="inside")
    out.index.name = "timestamp"
    return out.reset_index()


# --------------------------------------------------------------------------- #
# Calendar + targets
# --------------------------------------------------------------------------- #
def add_calendar(df: pd.DataFrame) -> pd.DataFrame:
    local = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(LOCAL_TZ)
    ro = holidays.Romania(years=range(local.dt.year.min(), local.dt.year.max() + 1))
    df["hour"] = local.dt.hour
    df["quarter_of_day"] = local.dt.hour * 4 + local.dt.minute // 15
    df["weekday"] = local.dt.weekday
    df["month"] = local.dt.month
    df["day_of_year"] = local.dt.dayofyear
    df["is_weekend"] = (df["weekday"] >= 5).astype(int)
    df["is_holiday"] = local.dt.date.map(lambda d: d in ro).astype(int)
    return df


def add_targets(df: pd.DataFrame) -> pd.DataFrame:
    df["total_production"] = df[PRODUCTION_COLS].sum(axis=1).round(2)
    # Zeros in load, or all always-on sources reading 0, are missing records.
    always_on = ["biomass", "fossil_gas", "hydro_run_of_river_and_pondage", "hydro_water_reservoir"]
    gap = (df["actual_total_load"] == 0) | (df[always_on] == 0).all(axis=1)
    df["valid"] = (~gap).astype(int)
    return df


# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--refresh", action="store_true", help="re-download weather even if cached")
    args = parser.parse_args()

    energy = pd.read_csv(DATASET_CSV, usecols=["timestamp", "actual_total_load", *PRODUCTION_COLS])
    energy["timestamp"] = energy["timestamp"].astype("int64")
    print(f"energy data: {len(energy)} rows")

    weather = build_weather(energy["timestamp"], args.refresh)
    df = energy.merge(weather, on="timestamp", how="left")
    df = add_targets(df)
    df = add_calendar(df)

    df.to_csv(DATASET_CSV, index=False, float_format="%.2f")
    print(f"\nwrote {DATASET_CSV.name}: {df.shape[0]} rows x {df.shape[1]} cols")
    print(f"  invalid (gap) rows:        {(df['valid'] == 0).sum()}")
    print(f"  rows with missing weather: {df[list(WEATHER_COLS.values())].isna().any(axis=1).sum()}")


if __name__ == "__main__":
    main()
