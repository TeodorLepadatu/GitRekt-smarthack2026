"""A real day at the site: ML forecasts + ENTSO-E day-ahead prices (plan section 2).

The real counterpart to :mod:`loop.demo`. Where ``demo_day()`` makes up a
believable day, ``real_day()`` builds the same :class:`~loop.day.DayInput` from
the three real feeds the project already has:

    predictions/models/*.pkl  ML 1 + ML 2  -> forecasts (with their 10-90 % band)
    data/energy_price.csv     ENTSO-E      -> day-ahead prices, already 15-min
                                             (or the mock price API: ``price_api=URL``)
    predictions/dataset.csv   ENTSO-E      -> what really happened (the actuals)

    from loop import DayRunner, real_day
    day = real_day("2026-06-15")
    DayRunner(day, goals).run()

Replaying a past day is an honest backtest: the forecasts are issued at the
day-ahead gate (12:00 local the day before), so the models only see history
that was available then, exactly as in ``predictions/train_and_evaluate.ipynb``.

National figures, site figures
------------------------------
The models forecast *Romania* (demand around 6 GW); the site is a 10 MWh /
3 MW battery with demand in the single-digit MW. :class:`SiteScale` maps one to
the other by scaling each series so its national reference peak becomes the
site's peak. Demand, solar and wind are scaled separately, because the site's
mix is not the country's. The defaults reproduce the scale ``demo.py`` uses
(demand 1.5-4.5 MW, solar up to about 6 MW, some wind), so a real day and a
demo day are directly comparable.

Scaling is linear, so each series' 10-90 % band scales with it; the site's
production band is then recombined from the scaled solar and wind bands the
same way :func:`predictions.core.combine` builds the national one.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from feature_builder import SiteForecast
from settlement import ImbalancePrices

from .day import DayInput, LoopError

log = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("Europe/Bucharest")  # same zone predictions/core.py builds its calendar in
STEP_S = 15 * 60
N = 96

PRICES_CSV = Path("data/energy_price.csv")
PRICE_COLUMN = "Day-ahead Price (EUR/MWh)"

# synth_market.py's imbalance prices: what deviating from the plan costs.
IMBALANCE_CSV = Path("synthetic_out/prices.csv")

# 99th percentile of each national series over the four years in predictions/dataset.csv.
# The p99 rather than the max: one freak hour must not shrink every other day.
NATIONAL_P99_MW = {"demand": 8429.0, "solar": 2420.0, "wind": 2571.0}


class RealDayError(LoopError):
    """The day cannot be built: no prices, no models, or a date out of range."""


@dataclass(frozen=True)
class SiteScale:
    """The site's peaks, in MW. Each national series is scaled to reach these.

    The defaults match the site ``demo.py`` describes, so switching a run from
    ``demo_day()`` to ``real_day()`` changes the *shape* of the day, not its size.
    """

    demand_peak_mw: float = 4.5
    solar_peak_mw: float = 6.2
    wind_peak_mw: float = 1.5

    def __post_init__(self) -> None:
        for name in ("demand_peak_mw", "solar_peak_mw", "wind_peak_mw"):
            v = getattr(self, name)
            if not (isinstance(v, (int, float)) and math.isfinite(v)) or v < 0:
                raise RealDayError(f"{name} must be a non-negative number, got {v!r}")

    @property
    def factors(self) -> dict[str, float]:
        """National MW -> site MW, one factor per series."""
        return {
            "demand": self.demand_peak_mw / NATIONAL_P99_MW["demand"],
            "solar": self.solar_peak_mw / NATIONAL_P99_MW["solar"],
            "wind": self.wind_peak_mw / NATIONAL_P99_MW["wind"],
        }


# --------------------------------------------------------------------------- #
# the day's 96 timestamps


def day_timestamps(date: str) -> list[int]:
    """The 96 unix seconds of a local day, starting at local midnight.

    On the two DST days a local day is 23 or 25 hours; a day is always 96
    intervals here, so such a day runs 24 hours from local midnight and the
    clock labels after the change are off by an hour.
    """
    try:
        d = datetime.strptime(date, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise RealDayError(f"date must be YYYY-MM-DD, got {date!r}") from exc
    start = datetime.combine(d, time(0, 0), tzinfo=LOCAL_TZ)
    base = int(start.timestamp())
    return [base + i * STEP_S for i in range(N)]


def gate_closure(date: str) -> int:
    """The day-ahead gate: 12:00 local on the day before, as unix seconds.

    Prices for a day are published just after this, so it is the latest moment
    a plan for that day could honestly have been made.
    """
    d = datetime.strptime(date, "%Y-%m-%d").date() - timedelta(days=1)
    return int(datetime.combine(d, time(12, 0), tzinfo=LOCAL_TZ).timestamp())


# --------------------------------------------------------------------------- #
# prices


def load_prices(path: Path | str = PRICES_CSV) -> dict[int, float]:
    """``data/energy_price.csv`` -> {unix seconds: EUR/MWh}, snapped to the 15-min grid.

    Hourly rows (ENTSO-E published hourly before 2025) are spread over the four
    quarters of their hour, so the result is always on the 15-minute grid.
    """
    import pandas as pd

    p = Path(path)
    if not p.exists():
        raise RealDayError(f"{p} not found - run `python energy_price.py` to download prices")
    df = pd.read_csv(p)
    if "timestamp" not in df.columns:
        raise RealDayError(f"{p} has no `timestamp` column (columns: {list(df.columns)})")
    col = PRICE_COLUMN if PRICE_COLUMN in df.columns else df.columns[-1]
    ts = (df["timestamp"].astype("int64") // STEP_S) * STEP_S
    price = pd.to_numeric(df[col], errors="coerce")
    out = {int(t): float(v) for t, v in zip(ts, price) if v == v}
    # Hourly data: fill the three missing quarters of each hour.
    for t, v in list(out.items()):
        for k in (1, 2, 3):
            out.setdefault(t + k * STEP_S, v)
    return out


def load_imbalance_prices(path: Path | str = IMBALANCE_CSV) -> dict[int, ImbalancePrices]:
    """``synthetic_out/prices.csv`` -> {unix seconds: ImbalancePrices}.

    Built by ``synth_market.py`` from the real load and generation mix, so the
    penalties are synthetic even on a real day; the day-ahead price keeps coming
    from the real ENTSO-E file. :meth:`ImbalancePrices.penalty_for` clamps the
    pair to the day-ahead price, so the two sources crossing cannot turn a
    penalty into a bonus.
    """
    import pandas as pd

    p = Path(path)
    if not p.exists():
        raise RealDayError(
            f"{p} not found - run `python synth_market.py predictions/dataset.csv synthetic_out` "
            "to generate the market prices")
    df = pd.read_csv(p)
    missing = {"time", "imb_short_price", "imb_long_price"} - set(df.columns)
    if missing:
        raise RealDayError(f"{p} has no {sorted(missing)} column(s) (columns: {list(df.columns)})")
    # Subtracting the epoch and dividing by 1 s keeps this right whatever
    # resolution pandas picked for the column (s, ms or ns).
    seconds = (pd.to_datetime(df["time"], utc=True) - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta("1s")
    ts = (seconds // STEP_S) * STEP_S
    short = pd.to_numeric(df["imb_short_price"], errors="coerce")
    long = pd.to_numeric(df["imb_long_price"], errors="coerce")
    return {int(t): ImbalancePrices(short_eur_mwh=float(a), long_eur_mwh=float(b))
            for t, a, b in zip(ts, short, long) if a == a and b == b}


# --------------------------------------------------------------------------- #
# forecasts and actuals


def _load_models(models: Optional[Mapping[str, Any]], data: Any = None) -> tuple[Any, Any]:
    """The two forecasters, sharing one copy of the history (dataset.csv is 19 MB)."""
    if models:
        try:
            return models["demand"], models["production"]
        except KeyError as exc:
            raise RealDayError("models must hold both 'demand' and 'production'") from exc
    try:
        from predictions import DemandForecaster, ProductionForecaster
    except ImportError as exc:  # lightgbm / pandas missing
        raise RealDayError(f"the prediction models cannot be imported ({exc})") from exc
    data = load_dataset() if data is None else data
    try:
        return (DemandForecaster.load(data=data), ProductionForecaster.load(data=data))
    except FileNotFoundError as exc:
        raise RealDayError(
            f"{exc} - run predictions/train_and_evaluate.ipynb to train and save the models"
        ) from exc


def load_dataset() -> Any:
    """``predictions/dataset.csv``, with the invalid records already blanked out."""
    try:
        from predictions.core import load_data
    except ImportError as exc:
        raise RealDayError(f"predictions cannot be imported ({exc})") from exc
    try:
        return load_data()
    except FileNotFoundError as exc:
        raise RealDayError(f"{exc} - run predictions/build_dataset.py to build it") from exc


def _combine_band(parts: Sequence[tuple[float, float, float]]) -> tuple[float, float]:
    """Low/high of a sum of series, errors assumed independent (as predictions.core.combine)."""
    mid = sum(p[1] for p in parts)
    down = math.sqrt(sum((p[1] - p[0]) ** 2 for p in parts))
    up = math.sqrt(sum((p[2] - p[1]) ** 2 for p in parts))
    return max(0.0, mid - down), mid + up


def site_forecasts(
    timestamps: Sequence[int],
    *,
    issued_at: int,
    scale: SiteScale,
    models: Optional[Mapping[str, Any]] = None,
    data: Any = None,
    allow_fetch: bool = True,
) -> list[SiteForecast]:
    """ML 1 + ML 2 for these timestamps, scaled to the site."""
    demand_model, production_model = _load_models(models, data)
    f = scale.factors
    last = int(getattr(demand_model, "last_known_ts", 0))
    issued = min(int(issued_at), last + STEP_S) if last else int(issued_at)

    try:
        dem = demand_model.predict(list(timestamps), issued_at=issued, allow_fetch=allow_fetch)
        prod = production_model.predict(list(timestamps), issued_at=issued, allow_fetch=allow_fetch)
    except ValueError as exc:
        raise RealDayError(
            f"the models cannot forecast this day from {_iso(issued)} ({exc}); "
            "they reach 7 days past the end of predictions/dataset.csv"
        ) from exc

    out: list[SiteForecast] = []
    for i in range(len(timestamps)):
        d = dem.iloc[i]
        p = prod.iloc[i]
        solar = (float(p["solar_low_mw"]) * f["solar"], float(p["solar_mw"]) * f["solar"],
                 float(p["solar_high_mw"]) * f["solar"])
        wind = (float(p["wind_low_mw"]) * f["wind"], float(p["wind_mw"]) * f["wind"],
                float(p["wind_high_mw"]) * f["wind"])
        low, high = _combine_band([solar, wind])
        out.append(SiteForecast(
            demand_mw=round(max(0.0, float(d["demand_mw"]) * f["demand"]), 3),
            production_mw=round(max(0.0, solar[1] + wind[1]), 3),
            solar_mw=round(max(0.0, solar[1]), 3),
            wind_mw=round(max(0.0, wind[1]), 3),
            prod_low_mw=round(max(0.0, low), 3),
            prod_high_mw=round(max(0.0, high), 3),
        ))
    return out


def site_actuals(
    timestamps: Sequence[int],
    *,
    scale: SiteScale,
    data: Any = None,
) -> list[Optional[dict[str, float]]]:
    """What really happened, scaled to the site: ``None`` where the day is still ahead.

    Reads ``predictions/dataset.csv`` (via :func:`predictions.core.load_data`,
    which already blanks the 48 invalid records).
    """
    if data is None:
        data = load_dataset()
    f = scale.factors
    rows = data.set_index("timestamp") if "timestamp" in getattr(data, "columns", []) else data

    out: list[Optional[dict[str, float]]] = []
    for t in timestamps:
        try:
            row = rows.loc[int(t)]
        except KeyError:
            out.append(None)
            continue
        demand = row.get("actual_total_load")
        solar, wind = row.get("solar"), row.get("wind_onshore")
        if any(v is None or v != v for v in (demand, solar, wind)):  # NaN = invalid record
            out.append(None)
            continue
        out.append({
            "demand_mw": round(max(0.0, float(demand) * f["demand"]), 3),
            "production_mw": round(max(0.0, float(solar) * f["solar"] + float(wind) * f["wind"]), 3),
        })
    return out


# --------------------------------------------------------------------------- #


def real_day(
    date: str,
    *,
    scale: SiteScale = SiteScale(),
    issued_at: Optional[int] = None,
    prices: Optional[Mapping[int, float] | Sequence[float]] = None,
    prices_path: Path | str = PRICES_CSV,
    models: Optional[Mapping[str, Any]] = None,
    data: Any = None,
    with_actuals: bool = True,
    imbalance: bool = False,
    imbalance_path: Path | str = IMBALANCE_CSV,
    allow_fetch: bool = True,
    price_api: Any = None,
) -> DayInput:
    """One real day at the site, ready for :class:`~loop.day.DayRunner`.

    ``date``: a local calendar day, ``"YYYY-MM-DD"``.
    ``issued_at``: when the forecasts are made; default is the day-ahead gate,
    12:00 local the day before, clamped to the end of the models' data.
    ``prices``: 96 values or {timestamp: price}; by default read from
    ``prices_path``. ``with_actuals=False`` leaves the day unsettled against
    measurements, so the forecasts are used instead (a plan, not a backtest).
    ``imbalance=True`` charges for deviating from the promised grid position and
    lets the battery defend it (see :class:`~loop.day.DayInput`); it needs the
    actuals, since without a measurement there is no deviation.
    ``price_api``: the mock price API's base URL (or an ``httpx.Client`` for it).
    When set and ``prices`` is not given, both the day-ahead and the imbalance
    prices come from the API; if it cannot be reached, from the CSV files.
    """
    stamps = day_timestamps(date)
    if imbalance and not with_actuals:
        raise RealDayError("imbalance needs the actuals: a forecast cannot deviate from itself")
    fetched = _fetch_prices(date, stamps, price_api) if prices is None and price_api is not None else None
    curve = fetched.day_ahead if fetched else _prices_for(stamps, prices, prices_path, date)
    # One copy of dataset.csv for the models and the actuals: it is read three
    # times otherwise, once per forecaster and once here.
    if data is None and not models:
        data = load_dataset()
    forecasts = site_forecasts(
        stamps, issued_at=gate_closure(date) if issued_at is None else int(issued_at),
        scale=scale, models=models, data=data, allow_fetch=allow_fetch,
    )
    actuals = site_actuals(stamps, scale=scale, data=data) if with_actuals else None
    imb = None
    if imbalance and fetched:
        imb = list(fetched.imbalance)
    elif imbalance:
        table = load_imbalance_prices(imbalance_path)
        imb = [table.get(t) for t in stamps]
        if not any(x is not None for x in imb):
            raise RealDayError(
                f"no imbalance price for any interval of {date}; {Path(imbalance_path)} covers "
                f"{_iso(min(table))} .. {_iso(max(table))}" if table else
                f"{Path(imbalance_path)} holds no usable imbalance prices")
    return DayInput(prices=curve, forecasts=forecasts, actuals=actuals, date=date, imbalance=imb)


def _fetch_prices(date: str, stamps: Sequence[int], api: Any):
    """The day from the mock price API, or None (logged) to fall back to the CSVs."""
    from price_api import PriceApiError, fetch_day

    try:
        day = fetch_day(date, api)
    except PriceApiError as exc:
        log.warning("%s; reading the price CSVs instead", exc)
        return None
    if list(day.timestamps) != list(stamps):
        log.warning("price API returned other intervals than %s; reading the price CSVs instead", date)
        return None
    if day.synthetic:
        log.info("prices for %s come partly or fully from the generator: %s", date, day.source)
    return day


def _prices_for(
    stamps: Sequence[int],
    prices: Optional[Mapping[int, float] | Sequence[float]],
    prices_path: Path | str,
    date: str,
) -> list[float]:
    if prices is not None and not isinstance(prices, Mapping):
        got = list(prices)
        if len(got) != N:
            raise RealDayError(f"{len(got)} prices given for a {N}-interval day")
        return [float(v) for v in got]
    table = dict(prices) if isinstance(prices, Mapping) else load_prices(prices_path)
    missing = [t for t in stamps if t not in table]
    if missing:
        known = (f"{_iso(min(table))} .. {_iso(max(table))}" if table else "nothing")
        raise RealDayError(
            f"no day-ahead price for {len(missing)} of the {N} intervals of {date} "
            f"(first {_iso(missing[0])}); the price file covers {known}. Day-ahead prices "
            "exist only up to tomorrow - pass prices=... to plan a later day."
        )
    return [float(table[t]) for t in stamps]


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), LOCAL_TZ).isoformat(timespec="minutes")
