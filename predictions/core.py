"""Shared forecasting machinery for DemandForecaster and ProductionForecaster.

Each forecaster models one or more 15-min series with LightGBM quantile
regression (10 / 50 / 90 %). A sample is a pair (target time T, issue time
issued_at); only data strictly before issued_at is used, so one model serves
every lead time from 15 min to 7 days (`lead_hours` is a feature).

Per series the model predicts a correction to a base forecast that blends from
the last known value (short leads) to a seasonal value (long leads). Values
are divided by the series' recent level (so forecasts follow capacity growth)
and corrections by the typical error at that lead (so short-lead corrections
stay small). Early stopping and range calibration use the 3 months before the
training cut-off.
"""

from __future__ import annotations

import os
import pickle
import time
from pathlib import Path

import holidays
import lightgbm as lgb
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
DATASET_CSV = HERE / "dataset.csv"     # the original training data: never written by the live loop
MODELS_DIR = HERE / "models"           # the original models, behind the README results and plots

STEP, DAY = 900, 86_400
WEEK = 7 * DAY
MAX_LEAD_STEPS = 7 * 96
LOCAL_TZ = "Europe/Bucharest"

ROLLING = {  # name: (window in steps, min_periods, statistic)
    "mean_1d": (96, 48, "mean"), "mean_7d": (672, 288, "mean"),
    "max_30d": (2880, 960, "max"), "max_365d": (35040, 2880, "max"),
}
WEATHER = ["temperature_2m", "precipitation", "shortwave_radiation", "cloud_cover", "wind_speed_80m"]

TAU_H = 3.0                                          # hours: base moves from last value to seasonal
QUANTILES = (0.1, 0.5, 0.9)
LEAD_BUCKETS = np.array([0, 1, 6, 24, 72, np.inf])   # hours, for range calibration
VAL_DAYS = 92
MIN_HISTORY_DAYS = 30                                # lag/level features need this much data before issued_at
LIVE_WEATHER_TTL_S = 3600                            # refetch live weather forecasts after 1 h
LGB_PARAMS = dict(learning_rate=0.05, num_leaves=31, min_data_in_leaf=1000, lambda_l2=1.0,
                  bagging_fraction=0.7, bagging_freq=1, feature_fraction=0.8, verbose=-1)


# --------------------------------------------------------------------------- #
# Data helpers
# --------------------------------------------------------------------------- #
def load_data(path: Path = DATASET_CSV) -> pd.DataFrame:
    """Read dataset.csv, derive other_production / renewables, blank out invalid rows."""
    df = pd.read_csv(path)
    df["timestamp"] = df["timestamp"].astype("int64")
    df["other_production"] = df["total_production"] - df["solar"] - df["wind_onshore"]
    df["renewables"] = df["solar"] + df["wind_onshore"]
    cols = ["actual_total_load", "total_production", "solar", "wind_onshore",
            "other_production", "renewables"]
    df.loc[df["valid"] == 0, cols] = np.nan
    return df


# --------------------------------------------------------------------------- #
# Live data (predictions/live.py): a growing copy of the dataset and models retrained on it.
# Opt-in with PREDICTIONS_LIVE=1, so the notebook, the tests and the original results
# keep using dataset.csv and models/.
# --------------------------------------------------------------------------- #
def live_dir() -> Path:
    return Path(os.environ.get("PREDICTIONS_LIVE_DIR") or HERE / "live")


def live_enabled() -> bool:
    return os.environ.get("PREDICTIONS_LIVE", "").strip().lower() in ("1", "true", "yes", "on")


def active_dataset_path() -> Path:
    """The live dataset when live mode is on and it exists, else the original dataset.csv."""
    p = live_dir() / "dataset.csv"
    return p if live_enabled() and p.exists() else DATASET_CSV


def replace_file(src: Path, dst: Path, attempts: int = 10) -> None:
    """os.replace, retried: on Windows it fails while another process has dst open."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.5)


def active_model_path(name: str) -> Path:
    """The live model when live mode is on and it exists, else the original in models/."""
    p = live_dir() / "models" / f"{name}.pkl"
    return p if live_enabled() and p.exists() else MODELS_DIR / f"{name}.pkl"


class History:
    """One series on a gap-free 15-min grid, with O(1) lookups by timestamp."""

    def __init__(self, timestamps, values):
        ts = np.asarray(timestamps, dtype="int64")
        vals = np.asarray(values, dtype=float)
        self.t0 = int(ts.min())
        self.values = np.full(int((ts.max() - self.t0) // STEP) + 1, np.nan)
        self.values[(ts - self.t0) // STEP] = vals
        self.last_ts = int(ts[~np.isnan(vals)].max())
        s = pd.Series(self.values)
        self.stats = {k: getattr(s.rolling(w, min_periods=m), how)().to_numpy()
                      for k, (w, m, how) in ROLLING.items()}

    def _lookup(self, arr, ts):
        idx = (np.asarray(ts, dtype="int64") - self.t0) // STEP
        ok = (idx >= 0) & (idx < len(arr))
        out = np.full(idx.shape, np.nan)
        out[ok] = arr[idx[ok]]
        return out

    def value(self, ts):
        return self._lookup(self.values, ts)

    def stat(self, name, ts):
        return self._lookup(self.stats[name], ts)


RO_HOLIDAYS = holidays.Romania(years=range(2015, 2041))


def calendar_features(ts) -> pd.DataFrame:
    local = pd.to_datetime(ts, unit="s", utc=True).tz_convert(LOCAL_TZ)
    doy = local.dayofyear.to_numpy() / 365.25
    weekday = local.weekday.to_numpy()
    return pd.DataFrame({
        "quarter_of_day": (local.hour * 4 + local.minute // 15).to_numpy(),
        "weekday": weekday,
        "month": local.month.to_numpy(),
        "doy_sin": np.sin(2 * np.pi * doy), "doy_cos": np.cos(2 * np.pi * doy),
        "is_weekend": (weekday >= 5).astype(int),
        "is_holiday": np.array([d in RO_HOLIDAYS for d in local.date], dtype=int),
    })


def sun_elevation(ts, lat=45.0, lon=25.0):
    """Approximate solar elevation (deg) at the middle of each interval, central Romania."""
    t = pd.to_datetime(np.asarray(ts) + STEP // 2, unit="s", utc=True)
    decl = np.deg2rad(23.44) * np.sin(2 * np.pi * (284 + t.dayofyear.to_numpy()) / 365)
    hour_angle = np.deg2rad(15 * ((t.hour + t.minute / 60).to_numpy() + lon / 15 - 12))
    phi = np.deg2rad(lat)
    s = np.sin(phi) * np.sin(decl) + np.cos(phi) * np.cos(decl) * np.cos(hour_angle)
    return np.rad2deg(np.arcsin(np.clip(s, -1, 1)))


def lead_sigma(lead_h):
    """Typical error size by lead (relative): small for the next interval, flat after 1 day."""
    return np.sqrt(np.minimum(np.asarray(lead_h, dtype=float), 24) / 24)


def build_features(T, issued, hist: History, weather: pd.DataFrame, spec: dict):
    """Features, scale and base forecast for each (T[i], issued[i]) pair."""
    T = np.asarray(T, dtype="int64")
    issued = np.broadcast_to(np.asarray(issued, dtype="int64"), T.shape)
    gap = T - issued
    if (gap < 0).any():
        raise ValueError("target timestamps must be at or after issued_at")
    known = issued - STEP                       # last interval with data
    lead_h = gap / 3600 + 0.25

    scale = hist.stat(spec["scale"], known)
    scale = np.where(scale > 1.0, scale, np.nan)
    f = {
        "last_known": hist.value(known),
        "lag_day": hist.value(T - (gap // DAY + 1) * DAY),       # latest known same time of day
        "lag_week": hist.value(T - (gap // WEEK + 1) * WEEK),    # latest known same weekday + time
        "mean_1d": hist.stat("mean_1d", known),
        "ramp_15m": hist.value(known) - hist.value(known - STEP),
        "ramp_1h": hist.value(known) - hist.value(known - 4 * STEP),
    }
    f = {k: v / scale for k, v in f.items()}

    seasonal = np.where(np.isnan(f[spec["seasonal"]]), f["mean_1d"], f[spec["seasonal"]])
    short = np.where(np.isnan(f["last_known"]), seasonal, f["last_known"])
    seasonal = np.where(np.isnan(seasonal), short, seasonal)
    w = np.exp(-(lead_h - 0.25) / TAU_H)        # exactly 1 for the next interval
    base = w * short + (1 - w) * seasonal

    X = calendar_features(T)
    X["lead_hours"] = lead_h
    X["sun_elevation"] = sun_elevation(T)
    for k, v in f.items():
        X[k] = v
    X["base"] = base
    wx = weather.reindex(T)[WEATHER].to_numpy()
    for i, col in enumerate(WEATHER):
        X[col] = wx[:, i]
    return X, scale, base


def sample_leads(n, rng):
    """Lead in steps (1 = next interval): half log-uniform, half uniform over 7 days."""
    log_part = np.exp(rng.uniform(0, np.log(MAX_LEAD_STEPS), n))
    uni_part = rng.uniform(1, MAX_LEAD_STEPS, n)
    return np.clip(np.where(rng.random(n) < 0.5, log_part, uni_part).astype(int), 1, MAX_LEAD_STEPS)


def combine(parts):
    """Sum of series; ranges combined assuming independent errors."""
    mid = sum(p[:, 1] for p in parts)
    down = np.sqrt(sum((p[:, 1] - p[:, 0]) ** 2 for p in parts))
    up = np.sqrt(sum((p[:, 2] - p[:, 1]) ** 2 for p in parts))
    return np.column_stack([np.clip(mid - down, 0, None), mid, mid + up])


def fetch_live_forecast(start_ts: int, end_ts: int) -> pd.DataFrame:
    """Open-Meteo live 15-min forecast for the dataset's sites, aggregated nationally."""
    from predictions.build_dataset import ROLE_VARS, SITES, _get, aggregate_sites
    names = list(SITES)
    js = _get({
        "latitude": ",".join(str(SITES[n][0]) for n in names),
        "longitude": ",".join(str(SITES[n][1]) for n in names),
        "minutely_15": ",".join(sorted({v for vs in ROLE_VARS.values() for v in vs})),
        "start_date": pd.to_datetime(start_ts, unit="s").date().isoformat(),
        "end_date": pd.to_datetime(end_ts, unit="s").date().isoformat(),
        "timezone": "UTC", "timeformat": "unixtime",
    }, url="https://api.open-meteo.com/v1/forecast")
    js = js if isinstance(js, list) else [js]
    per_site = {n: pd.DataFrame(j["minutely_15"]).set_index("time") for n, j in zip(names, js)}
    index = next(iter(per_site.values())).index.to_numpy()
    return aggregate_sites({n: d.reindex(index) for n, d in per_site.items()}, index).set_index("timestamp")


# --------------------------------------------------------------------------- #
# Base forecaster
# --------------------------------------------------------------------------- #
class BaseForecaster:
    """Subclasses set NAME, SPECS (target -> scale/seasonal), SERIES_COL and implement
    _series() (raw target forecasts -> output series) and _columns() (output frame)."""

    NAME: str = ""
    SPECS: dict[str, dict] = {}
    SERIES_COL: dict[str, str] = {}   # output series -> dataset column (for calibration / evaluation)

    def __init__(self, models: dict | None = None, calibration: dict | None = None,
                 data: pd.DataFrame | None = None):
        self.models = models or {}            # target -> {"features", "boosters": {q: Booster}, "rounds"}
        self.calibration = calibration or {}  # series -> factor per LEAD_BUCKETS bucket
        self.set_data(load_data() if data is None else data)

    @classmethod
    def default_path(cls) -> Path:
        return MODELS_DIR / f"{cls.NAME}.pkl"

    # -- to implement -------------------------------------------------------- #
    def _series(self, p: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        raise NotImplementedError

    def _columns(self, fc: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        raise NotImplementedError

    # -- history / weather --------------------------------------------------- #
    def set_data(self, data: pd.DataFrame) -> None:
        """(Re)load the history used for lag features, e.g. after new sensor readings."""
        self.data = data
        cols = set(self.SPECS) | set(self.SERIES_COL.values())
        self.histories = {c: History(data.timestamp, data[c]) for c in cols}
        self.archive = data.set_index("timestamp")[WEATHER]
        cal = calendar_features(self.archive.index.to_numpy())
        self.climatology = (self.archive.assign(month=cal.month.to_numpy(), qod=cal.quarter_of_day.to_numpy())
                            .groupby(["month", "qod"]).mean())
        self._live_cache: pd.DataFrame | None = None
        self._live_fetched_at = 0.0

    @property
    def last_known_ts(self) -> int:
        """Start of the latest interval with data for every modelled series."""
        return min(self.histories[t].last_ts for t in self.SPECS)

    def get_weather(self, T, allow_fetch: bool = True) -> tuple[pd.DataFrame, pd.Series]:
        """Weather per timestamp: archive -> live forecast (<= 16 days) -> climatology."""
        T = np.unique(np.asarray(T, dtype="int64"))
        w = self.archive.reindex(T)
        source = pd.Series(np.where(w.notna().all(axis=1), "archive", ""), index=T)
        missing = T[source.to_numpy() == ""]
        if len(missing) and allow_fetch:
            try:
                c = self._live_cache
                stale = time.time() - self._live_fetched_at > LIVE_WEATHER_TTL_S
                if c is None or stale or missing.min() < c.index.min() or missing.max() > c.index.max():
                    self._live_cache = c = fetch_live_forecast(int(missing.min()), int(missing.max()))
                    self._live_fetched_at = time.time()
                live = c.reindex(missing).dropna()
                w.loc[live.index] = live[WEATHER]
                source[live.index] = "forecast"
            except Exception as exc:  # offline, rate limit, too far ahead...
                print(f"live weather forecast unavailable ({exc}); using climatology")
        missing = T[source.to_numpy() == ""]
        if len(missing):
            c = calendar_features(missing)
            keys = pd.MultiIndex.from_arrays([c.month, c.quarter_of_day])
            w.loc[missing] = self.climatology.reindex(keys).to_numpy()
            source[missing] = "climatology"
        return w, source

    # -- training ------------------------------------------------------------ #
    @classmethod
    def fit(cls, data: pd.DataFrame | None = None, train_end: int | None = None,
            samples_per_target: int = 4, max_rounds: int = 2000, seed: int = 0,
            verbose: bool = True):
        """Train on all intervals before train_end (default: all data).

        The last VAL_DAYS before train_end are used for early stopping and range
        calibration, then the models are refit on everything before train_end.
        """
        self = cls(data=data)
        end = int(self.data.timestamp.max()) + STEP if train_end is None else int(train_end)
        early = {}
        for target in self.SPECS:
            t0 = time.time()
            early[target] = self._fit_target(target, end, samples_per_target, max_rounds, seed)
            if verbose:
                m = self.models[target]
                print(f"[{cls.NAME}] {target:<18} {m['n_samples']:>9,} samples   trees/quantile "
                      f"{list(m['rounds'].values())}   {time.time() - t0:5.0f} s")
        self.calibration = self._calibrate(end, seed, early)
        return self

    def refit(self, data: pd.DataFrame | None = None, samples_per_target: int = 4, seed: int = 0,
              verbose: bool = True):
        """A new forecaster trained on all of `data` (default: this one's history).

        Keeps this model's trees per quantile and range calibration instead of
        early-stopping and recalibrating, so it takes about half the time of fit().
        The live loop uses it to learn from every new interval and runs fit() daily.
        """
        new = type(self)(calibration={k: list(v) for k, v in self.calibration.items()},
                         data=self.data if data is None else data)
        t_min, end = int(new.data.timestamp.min()), int(new.data.timestamp.max()) + STEP
        for target in self.SPECS:
            t0 = time.time()
            rounds = dict(self.models[target]["rounds"])
            X_all, y_all = new._xy(target, *new._pairs(t_min, end, samples_per_target, seed))
            final = {q: lgb.train(dict(LGB_PARAMS, objective="quantile", alpha=q, seed=seed),
                                  lgb.Dataset(X_all, y_all), num_boost_round=rounds[q]) for q in QUANTILES}
            new.models[target] = {"features": list(X_all.columns), "boosters": final,
                                  "rounds": rounds, "n_samples": len(y_all)}
            if verbose:
                print(f"[{self.NAME}] refit {target:<18} {len(y_all):>9,} samples   {time.time() - t0:5.0f} s")
        return new

    def update(self, data: pd.DataFrame | None = None, days: int = 30, decay_rate: float = 0.9,
               samples_per_target: int = 4, seed: int = 0):
        """A new forecaster that has learnt from the last `days` of `data`, in seconds.

        LightGBM's Booster.refit keeps every tree and moves each leaf value towards
        what the new data says: new = decay_rate * old + (1 - decay_rate) * fitted.
        refit() and fit() rebuild the trees from all the data instead (minutes).
        """
        new = type(self)(calibration={k: list(v) for k, v in self.calibration.items()},
                         data=self.data if data is None else data)
        end = int(new.data.timestamp.max()) + STEP
        for target in self.SPECS:
            m = self.models[target]
            X, y = new._xy(target, *new._pairs(end - days * DAY, end, samples_per_target, seed))
            boosters = {q: m["boosters"][q].refit(X[m["features"]], y, decay_rate=decay_rate) for q in QUANTILES}
            new.models[target] = {**m, "boosters": boosters}
        return new

    def _pairs(self, t_from, t_to, n_per, seed):
        ts = self.data.timestamp[(self.data.timestamp >= t_from) & (self.data.timestamp < t_to)].to_numpy()
        T = np.repeat(ts, n_per)
        return T, T - (sample_leads(len(T), np.random.default_rng(seed)) - 1) * STEP

    def _xy(self, target, T, issued):
        hist = self.histories[target]
        X, scale, base = build_features(T, issued, hist, self.archive, self.SPECS[target])
        y = (hist.value(T) / scale - base) / lead_sigma(X["lead_hours"])
        keep = ~np.isnan(y)
        return X[keep], y[keep]

    def _fit_target(self, target, end, samples_per_target, max_rounds, seed):
        val_start, t_min = end - VAL_DAYS * DAY, int(self.data.timestamp.min())
        X_tr, y_tr = self._xy(target, *self._pairs(t_min, val_start, samples_per_target, seed))
        X_va, y_va = self._xy(target, *self._pairs(val_start, end, 1, seed + 1))
        early, final, rounds = {}, {}, {}
        for q in QUANTILES:
            params = dict(LGB_PARAMS, objective="quantile", alpha=q, seed=seed)
            early[q] = lgb.train(params, lgb.Dataset(X_tr, y_tr), num_boost_round=max_rounds,
                                 valid_sets=[lgb.Dataset(X_va, y_va)],
                                 callbacks=[lgb.early_stopping(50, verbose=False)])
            rounds[q] = max(early[q].best_iteration, 50)
        X_all, y_all = self._xy(target, *self._pairs(t_min, end, samples_per_target, seed))
        for q in QUANTILES:
            params = dict(LGB_PARAMS, objective="quantile", alpha=q, seed=seed)
            final[q] = lgb.train(params, lgb.Dataset(X_all, y_all), num_boost_round=rounds[q])
        self.models[target] = {"features": list(X_all.columns), "boosters": final,
                               "rounds": rounds, "n_samples": len(y_all)}
        return early

    def _calibrate(self, end, seed, early):
        """Factor k per series and lead bucket so that mid ± k·half-width covers 80 %
        of the held-out months, predicted by the early-stopped models (never trained on them)."""
        T, issued = self._pairs(end - VAL_DAYS * DAY, end, 2, seed + 7)
        fc = self.forecast(T, issued, calibrated=False, boosters=early)
        bucket = np.digitize((T - issued) / 3600 + 0.25, LEAD_BUCKETS) - 1
        out = {}
        for name, p in fc.items():
            y = self.histories[self.SERIES_COL[name]].value(T)
            up = np.maximum(p[:, 2] - p[:, 1], 1e-6)
            down = np.maximum(p[:, 1] - p[:, 0], 1e-6)
            u = np.where(y >= p[:, 1], (y - p[:, 1]) / up, (p[:, 1] - y) / down)
            out[name] = [float(np.nanquantile(u[bucket == b], 0.8)) for b in range(len(LEAD_BUCKETS) - 1)]
        return out

    # -- prediction ---------------------------------------------------------- #
    def _predict_target(self, target, T, issued, weather, boosters=None):
        m = self.models[target]
        boosters = m["boosters"] if boosters is None else boosters[target]
        X, scale, base = build_features(T, issued, self.histories[target], weather, self.SPECS[target])
        z = np.column_stack([boosters[q].predict(X[m["features"]]) for q in QUANTILES])
        z *= lead_sigma(X["lead_hours"])[:, None]
        return np.clip(np.sort(z + base[:, None], axis=1) * scale[:, None], 0, None)

    def forecast(self, T, issued, weather: pd.DataFrame | None = None, calibrated: bool = True,
                 boosters: dict | None = None) -> dict[str, np.ndarray]:
        """Low-level, vectorised: series -> (n, 3) array of low / mid / high (MW).

        T and issued are arrays of unix seconds on the 15-min grid (issued may be a scalar).
        """
        weather = self.archive if weather is None else weather
        p = {t: self._predict_target(t, T, issued, weather, boosters) for t in self.SPECS}
        fc = self._series(p)
        if calibrated:
            bucket = np.digitize((np.asarray(T) - issued) / 3600 + 0.25, LEAD_BUCKETS) - 1
            for name, a in fc.items():
                k = np.asarray(self.calibration[name])[bucket]
                a[:, 0] = np.clip(a[:, 1] - k * (a[:, 1] - a[:, 0]), 0, None)
                a[:, 2] = a[:, 1] + k * (a[:, 2] - a[:, 1])
        return fc

    def predict(self, timestamps, issued_at: int | None = None, allow_fetch: bool = True) -> pd.DataFrame:
        """Forecast for arbitrary timestamps (unix seconds, any order, snapped to 15 min).

        issued_at: the "now" of the forecast; defaults to just after the latest known data.
        Timestamps must lie in [issued_at, issued_at + 7 days). Values in MW.
        """
        T = (np.asarray(timestamps, dtype="int64").ravel() // STEP) * STEP
        issued = (int(self.last_known_ts + STEP if issued_at is None else issued_at) // STEP) * STEP
        earliest, latest = int(self.data.timestamp.min()) + MIN_HISTORY_DAYS * DAY, self.last_known_ts + STEP
        if not earliest <= issued <= latest:
            raise ValueError(
                f"issued_at must lie between {earliest} and {latest} (the data ends at {self.last_known_ts}); "
                "add newer data with set_data() to forecast from a later moment")
        lead = (T - issued) // STEP + 1
        if (lead < 1).any() or (lead > MAX_LEAD_STEPS).any():
            raise ValueError(
                f"timestamps must lie between issued_at ({issued}) and issued_at + 7 days "
                f"({issued + MAX_LEAD_STEPS * STEP - STEP})")
        weather, source = self.get_weather(T, allow_fetch)
        fc = self.forecast(T, issued, weather)
        out = {"timestamp": T, "time_utc": pd.to_datetime(T, unit="s"),
               "lead_hours": (T - issued) / 3600 + 0.25}
        out.update(self._columns(fc))
        out["weather_source"] = source.reindex(T).to_numpy()
        return pd.DataFrame(out).round(1)

    # -- persistence --------------------------------------------------------- #
    def save(self, path: Path | str | None = None) -> Path:
        path = Path(path or self.default_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as f:   # write then rename: a reader never sees half a file
            pickle.dump({"kind": self.NAME, "models": self.models, "calibration": self.calibration,
                         "trained_until": int(self.data.timestamp.max())}, f)
        replace_file(tmp, path)
        return path

    @classmethod
    def load(cls, path: Path | str | None = None, data: pd.DataFrame | None = None):
        """Load trained models; history/weather come from `data` (default: dataset.csv)."""
        with open(path or cls.default_path(), "rb") as f:
            state = pickle.load(f)
        if state.get("kind") != cls.NAME:
            raise ValueError(f"{path} holds a '{state.get('kind')}' model, not '{cls.NAME}'")
        return cls(state["models"], state["calibration"], data)
