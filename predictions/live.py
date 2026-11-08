"""Live data: scrape every 15 minutes, add the new intervals, retrain on everything.

Every tick (by default 5 minutes after each quarter hour, once ENTSO-E has
usually published the interval that just ended):

  1. Energy: Romania's actual total load and generation per type, from
       - the official ENTSO-E API when $ENTSOE_API_TOKEN is set (entsoe_api.py), else
       - the website export with the browser token in $ENTSOE_TOKEN
         (entsoe_export.py, the scrapers' code; that token expires after ~15 minutes), else
       - Transelectrica's public SEN chart (transelectrica.py): no login, the grid
         operator ENTSO-E's Romanian data comes from. Load, solar, wind, hydro and
         nuclear match the training data; coal and gas read ~120 / ~80 MW higher.
  2. Weather: the Open-Meteo Historical Forecast API at the same sites as the
     training data (predictions/build_dataset.py).
  3. Rows built exactly as the training rows (build_dataset.py, then
     predictions/build_dataset.py: total production, valid flag, calendar) and
     merged into predictions/live/dataset.csv, which starts as a copy of
     predictions/dataset.csv. An interval is added only once its load and every
     production type are published; recent intervals are re-read so revisions land.
  4. If any interval was added or revised, both models learn from it:
     - every tick, an update (BaseForecaster.update: every tree kept, its leaf
       values moved towards the latest 30 days; seconds), and
     - once a day, a full fit on all the data, 2026 included (trees rebuilt,
       early stopping, range recalibration; ~7 min).
     They go to predictions/live/models/.
  5. predictions/live/status.json records what the tick did.

The originals are never written: predictions/dataset.csv, predictions/models/
and predictions/reports/ keep the training, evaluation and plots in the README.
The app reads the live files only with PREDICTIONS_LIVE=1 (docker-compose.yml
sets it).

    python -m predictions.live              # run forever
    python -m predictions.live --once       # one tick, e.g. from Task Scheduler or cron
    python -m predictions.live --no-train   # only collect data
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import shutil
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from predictions import core
from predictions.build_dataset import (API_URL, ROLE_VARS, SITES, _get, add_calendar,
                                       add_targets, aggregate_sites)
from predictions.core import DATASET_CSV, MODELS_DIR, STEP, live_dir, replace_file

ENERGY_COLS = ["actual_total_load", "biomass", "energy_storage", "fossil_brown_coal/Lignite", "fossil_gas",
               "hydro_run_of_river_and_pondage", "hydro_water_reservoir", "nuclear", "solar", "wind_onshore"]
LOOKBACK = timedelta(days=2)        # re-read this much every tick: late publications and revisions
MAX_CATCH_UP = timedelta(days=30)   # after downtime, fetch at most this far back
WINDOW = timedelta(days=5)          # per request: the export pads a day each side, its limit is 7
FULL_FIT_EVERY = timedelta(days=1)
TICK_S, TICK_OFFSET_S = 900, 300
FORECASTERS = ("DemandForecaster", "ProductionForecaster")


def _log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z  {msg}", flush=True)


def _utc(ts: int | float) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# --------------------------------------------------------------------------- #
# 1. Energy: ENTSO-E (with a token) or Transelectrica (no login)
# --------------------------------------------------------------------------- #
def energy_source() -> str:
    if os.environ.get("ENTSOE_API_TOKEN", "").strip():
        return "api"
    if os.environ.get("ENTSOE_TOKEN", "").strip() or os.environ.get("ENTSOE_COOKIE", "").strip():
        return "export"
    return "transelectrica"


def _frame(load: dict, gen: dict) -> pd.DataFrame:
    """{UTC datetime: MW} + {UTC datetime: {column: MW}} -> one row per interval, ENERGY_COLS."""
    rows = []
    for t in sorted(set(load) | set(gen)):
        g = gen.get(t, {})
        rows.append([int(t.replace(tzinfo=timezone.utc).timestamp()), load.get(t),
                     *(g.get(c) for c in ENERGY_COLS[1:])])
    df = pd.DataFrame(rows, columns=["timestamp", *ENERGY_COLS])
    for c in ENERGY_COLS:   # the export gives strings, "" when missing
        df[c] = pd.to_numeric(df[c].replace("", np.nan), errors="coerce")
    df["timestamp"] = df["timestamp"].astype("int64")
    return df


def _fetch_api(start: datetime, end: datetime) -> pd.DataFrame:
    import entsoe_api
    token = os.environ["ENTSOE_API_TOKEN"].strip()
    load = entsoe_api.fetch_load(start, end, token)
    gen = entsoe_api.fetch_generation(start, end, token)
    naive = lambda d: {t.replace(tzinfo=None): v for t, v in d.items()}  # noqa: E731
    return _frame(naive(load), naive(gen))


def _fetch_export(start: datetime, end: datetime) -> pd.DataFrame:
    import build_dataset
    import entsoe_export
    entsoe_export.set_auth(os.environ.get("ENTSOE_TOKEN"), os.environ.get("ENTSOE_COOKIE"))
    # Whole days around the range: the load export's days are local (CET), generation's are UTC.
    from_day, to_day = (start - timedelta(days=1)).date(), (end + timedelta(days=1)).date()
    load_csv = entsoe_export.fetch_window(entsoe_export.build_payload(
        "load/total/dayAhead/export", entsoe_export.ROMANIA, "CET", from_day, to_day))
    gen_csv = entsoe_export.fetch_window(entsoe_export.build_payload(
        "generation/actual/perType/generation/export", entsoe_export.ROMANIA, "UTC", from_day, to_day))
    return _frame(build_dataset.parse_load(io.StringIO(load_csv)),
                  build_dataset.parse_generation(io.StringIO(gen_csv)))


def _fetch_transelectrica(start: datetime, end: datetime) -> pd.DataFrame:
    """Transelectrica's 15-min means in the dataset's columns.

    Hydro comes as one total (`hydro_total`; tick() splits it at the dataset's
    recent ratio). Storage has no series of its own: it is what the published
    total production holds beyond the listed types. Solar's night-time
    self-consumption (a few MW below 0) is 0, as in ENTSO-E's data.
    """
    import transelectrica
    t = transelectrica.fetch_intervals(start, end)
    types = ["coal", "gas", "hydro", "nuclear", "wind", "solar", "biomass"]
    t[types] = t[types].clip(lower=0)            # first, so the columns still add up to `production`
    listed = t[types].sum(axis=1)
    df = pd.DataFrame({
        "timestamp": t.timestamp.astype("int64"),
        "actual_total_load": t.consumption,
        "biomass": t.biomass,
        "energy_storage": (t.production - listed).clip(lower=0),
        "fossil_brown_coal/Lignite": t.coal,
        "fossil_gas": t.gas,
        "hydro_run_of_river_and_pondage": np.nan,
        "hydro_water_reservoir": np.nan,
        "nuclear": t.nuclear,
        "solar": t.solar,
        "wind_onshore": t.wind,
        "hydro_total": t.hydro,
    })
    for c in ENERGY_COLS + ["hydro_total"]:
        df[c] = df[c].clip(lower=0)
    return df


def split_hydro(energy: pd.DataFrame, base: pd.DataFrame) -> pd.DataFrame:
    """A single hydro total -> the dataset's two hydro columns, at the last week's ratio."""
    if "hydro_total" not in energy.columns:
        return energy
    recent = base[base.timestamp > base.timestamp.max() - 7 * 86_400]
    ror, res = recent["hydro_run_of_river_and_pondage"].sum(), recent["hydro_water_reservoir"].sum()
    share = ror / (ror + res) if ror + res > 0 else 0.5
    out = energy.copy()
    total = out.pop("hydro_total")
    out["hydro_run_of_river_and_pondage"] = total * share
    out["hydro_water_reservoir"] = total * (1 - share)
    return out


def fetch_energy(start: datetime, end: datetime) -> tuple[pd.DataFrame, str]:
    """Load + generation for [start, end), a few days per request."""
    source = energy_source()
    fetch = {"api": _fetch_api, "export": _fetch_export, "transelectrica": _fetch_transelectrica}[source]
    parts, lo = [], start
    while lo < end:
        hi = min(lo + WINDOW, end)
        parts.append(fetch(lo, hi))
        lo = hi
    df = pd.concat(parts, ignore_index=True).drop_duplicates("timestamp", keep="last")
    t0, t1 = int(start.timestamp()), int(end.timestamp())
    return df[(df.timestamp >= t0) & (df.timestamp < t1)].sort_values("timestamp").reset_index(drop=True), source


# --------------------------------------------------------------------------- #
# 2. Weather from Open-Meteo (same API and sites as the training data)
# --------------------------------------------------------------------------- #
def fetch_weather(timestamps: np.ndarray) -> pd.DataFrame:
    """National weather columns for these 15-min timestamps (a 'timestamp' column + WEATHER_COLS)."""
    timestamps = np.asarray(timestamps, dtype="int64")
    names = list(SITES)
    days = pd.to_datetime([timestamps.min(), timestamps.max()], unit="s")
    js = _get({
        "latitude": ",".join(str(SITES[n][0]) for n in names),
        "longitude": ",".join(str(SITES[n][1]) for n in names),
        "minutely_15": ",".join(sorted({v for vs in ROLE_VARS.values() for v in vs})),
        "start_date": days[0].date().isoformat(), "end_date": days[1].date().isoformat(),
        "timezone": "UTC", "timeformat": "unixtime",
    }, url=API_URL)
    js = js if isinstance(js, list) else [js]
    per_site = {n: pd.DataFrame(j["minutely_15"]).set_index("time").reindex(timestamps)
                for n, j in zip(names, js)}
    return aggregate_sites(per_site, timestamps)


# --------------------------------------------------------------------------- #
# 3. Rows and the merge
# --------------------------------------------------------------------------- #
def required_columns(base: pd.DataFrame) -> list[str]:
    """Energy columns an interval needs before it is added: those the last week of data always has.

    (energy_storage, say, was blank for years; a column that stops being
    published must not stop the dataset from growing.)
    """
    recent = base[base.timestamp > base.timestamp.max() - 7 * 86_400]
    return [c for c in ENERGY_COLS if recent[c].notna().mean() >= 0.99]


def build_rows(energy: pd.DataFrame, weather: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Energy + weather -> rows with every dataset column, as the training rows were built."""
    df = energy.merge(weather, on="timestamp", how="left")
    df = add_calendar(add_targets(df))
    return df[columns]


def merge(base: pd.DataFrame, rows: pd.DataFrame) -> tuple[pd.DataFrame, int, int]:
    """base with rows added or replacing the same timestamps -> (merged, added, revised).

    `revised` counts intervals whose energy values changed (ENTSO-E corrections);
    weather on its own does not count, so it never triggers a retrain.
    """
    if rows.empty:
        return base, 0, 0
    old = base.set_index("timestamp")
    new = rows.set_index("timestamp")
    added = new.index.difference(old.index)
    both = new.index.intersection(old.index)
    a, b = old.loc[both, ENERGY_COLS].to_numpy(float), new.loc[both, ENERGY_COLS].to_numpy(float)
    revised = int((~(np.isclose(a, b, atol=0.006) | (np.isnan(a) & np.isnan(b)))).any(axis=1).sum())
    merged = pd.concat([old.drop(both), new]).sort_index().reset_index()
    return merged[base.columns], len(added), revised


def read_dataset(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["timestamp"] = df["timestamp"].astype("int64")
    return df


def write_dataset(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_csv(tmp, index=False, float_format="%.2f")   # the format of predictions/dataset.csv
    replace_file(tmp, path)


# --------------------------------------------------------------------------- #
# 4. Training
# --------------------------------------------------------------------------- #
def train_models(data_path: Path, out_dir: Path, full: bool, samples_per_target: int = 4) -> dict:
    """Both forecasters learn from the dataset at data_path; saved to out_dir.

    full: a fit from scratch on all the data (minutes); otherwise an update
    from the newest data (update_models, seconds).
    """
    from predictions import DemandForecaster, ProductionForecaster
    if not full:
        return update_models(data_path, out_dir)
    data = core.load_data(data_path)
    t0 = time.time()
    for cls in (DemandForecaster, ProductionForecaster):
        cls.fit(data, samples_per_target=samples_per_target).save(out_dir / f"{cls.NAME}.pkl")
    return {"kind": "fit", "seconds": round(time.time() - t0), "trained_until": _utc(data.timestamp.max())}


def update_models(data_path: Path, out_dir: Path) -> dict:
    """Both forecasters learn from the newest data in seconds (BaseForecaster.update), saved to out_dir.

    Starts from out_dir's models if there are any, else from the originals.
    """
    from predictions import DemandForecaster, ProductionForecaster
    data = core.load_data(data_path)
    t0 = time.time()
    for cls in (DemandForecaster, ProductionForecaster):
        prev = out_dir / f"{cls.NAME}.pkl"
        model = cls.load(prev if prev.exists() else MODELS_DIR / f"{cls.NAME}.pkl", data=data).update()
        model.save(out_dir / f"{cls.NAME}.pkl")
    return {"kind": "update", "seconds": round(time.time() - t0, 1), "trained_until": _utc(data.timestamp.max())}


def _last_full_fit(status: dict) -> float:
    """When the models were last fully fitted: the live loop's, else the originals' training."""
    if status.get("last_full_fit_ts"):
        return float(status["last_full_fit_ts"])
    import pickle
    try:
        with open(MODELS_DIR / "demand_model.pkl", "rb") as f:
            return float(pickle.load(f)["trained_until"])
    except (OSError, KeyError, pickle.UnpicklingError):
        return 0.0


# --------------------------------------------------------------------------- #
# One tick
# --------------------------------------------------------------------------- #
def read_status(directory: Path) -> dict:
    try:
        return json.loads((directory / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_status(directory: Path, status: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / "status.json.tmp"
    tmp.write_text(json.dumps(status, indent=2), encoding="utf-8")
    replace_file(tmp, directory / "status.json")


def tick(now: datetime | None = None, *, directory: Path | None = None,
         get_energy: Callable = fetch_energy, get_weather: Callable = fetch_weather,
         train: Callable | None = train_models) -> dict:
    """Fetch, merge, retrain once. Returns (and saves) the status; raises nothing for a failed fetch."""
    directory = Path(directory or live_dir())
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    data_path = directory / "dataset.csv"
    status = read_status(directory)
    status.update(last_tick=now.strftime("%Y-%m-%d %H:%M:%S UTC"), error=None, added=0, revised=0)

    if not data_path.exists():
        directory.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DATASET_CSV, data_path)
        _log(f"started {data_path} from {DATASET_CSV}")
    base = read_dataset(data_path)
    last = int(base.timestamp.max())

    end = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 15)  # current interval start
    start = max(min(now - LOOKBACK, datetime.fromtimestamp(last + STEP, timezone.utc)), now - MAX_CATCH_UP)
    start = start.replace(second=0, microsecond=0) - timedelta(minutes=start.minute % 15)
    try:
        energy, source = get_energy(start, end)
        energy = split_hydro(energy, base)
        status["energy_source"] = source
        need = required_columns(base)
        complete = energy.dropna(subset=need)
        status["fetched_intervals"] = len(energy)
        status["published_intervals"] = len(complete)
        if source not in ("api", "export"):
            # Another source than the training data's: only extend the data, never replace ENTSO-E rows.
            complete = complete[~complete.timestamp.isin(base.timestamp)]
        if len(complete):
            rows = build_rows(complete, get_weather(complete.timestamp.to_numpy()), list(base.columns))
            merged, added, revised = merge(base, rows)
            status.update(added=added, revised=revised)
            if added or revised:
                write_dataset(merged, data_path)
                base = merged
        _log(f"{source}: {len(energy)} intervals fetched, {status['published_intervals']} complete, "
             f"{status['added']} added, {status['revised']} revised; data ends {_utc(base.timestamp.max())}")
    except Exception as exc:  # noqa: BLE001  (offline, expired token, rate limit: try again next tick)
        status["error"] = f"{type(exc).__name__}: {exc}"
        _log(f"fetch failed: {status['error']}")
        traceback.print_exc()
    status["data_ends"] = _utc(base.timestamp.max())
    status["rows"] = len(base)

    models_dir = directory / "models"
    # Until the first retrain the app keeps the original models (with the live history).
    pending = status.get("training_pending", False) or status["added"] > 0 or status["revised"] > 0
    if train is not None and pending:
        full = now.timestamp() - _last_full_fit(status) >= FULL_FIT_EVERY.total_seconds()
        status["training_pending"] = True
        write_status(directory, status)
        try:
            _log(f"training ({'full fit' if full else 'refit'}) on {len(base):,} intervals ...")
            status["trained"] = train(data_path, models_dir, full)
            status["training_pending"] = False
            if full:
                status["last_full_fit_ts"] = int(now.timestamp())
            _log(f"trained: {status['trained']}")
        except Exception as exc:  # noqa: BLE001
            status["error"] = f"training failed: {type(exc).__name__}: {exc}"
            _log(status["error"])
            traceback.print_exc()
    write_status(directory, status)
    return status


def run_forever(train: bool = True) -> None:
    _log(f"live loop: every {TICK_S // 60} min (+{TICK_OFFSET_S // 60} min), writing to {live_dir()}")
    while True:
        tick(train=train_models if train else None)
        now = time.time()
        nxt = math.floor((now - TICK_OFFSET_S) / TICK_S + 1) * TICK_S + TICK_OFFSET_S
        _log(f"next tick at {datetime.fromtimestamp(nxt, timezone.utc):%H:%M} UTC")
        time.sleep(max(1.0, nxt - time.time()))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m predictions.live", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--once", action="store_true", help="run one tick and exit")
    p.add_argument("--no-train", action="store_true", help="collect data only, never retrain")
    p.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 2) // 2),
                   help="LightGBM threads (default: half the cores, leaving room for the app and Ollama)")
    args = p.parse_args(argv)
    core.LGB_PARAMS["num_threads"] = args.threads
    if args.once:
        s = tick(train=None if args.no_train else train_models)
        return 1 if s.get("error") else 0
    try:
        run_forever(train=not args.no_train)
    except KeyboardInterrupt:
        _log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
