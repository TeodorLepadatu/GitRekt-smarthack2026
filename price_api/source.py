"""Where the mock price API's numbers come from.

Each 15-minute interval gets two prices:

    day-ahead   data/energy_price.csv (ENTSO-E, real)     else synthetic
    imbalance   synthetic_out/prices.csv (synth_market)   else synthetic

A synthetic day-ahead curve is built from the real file's own shape: the mean
price of each quarter-hour slot in that calendar month (the nearest month the
file covers when it does not cover this one), times a day-level factor, a
weekend discount and a little AR(1) noise. It is seeded by the date, so a date
always gets the same curve, on every machine and every run.

A synthetic imbalance pair uses the same multipliers ``synth_market.py`` does
(short 1.2-1.8 x, long 0.3-0.8 x the day-ahead price) but applied to the
price's magnitude, so it stays a penalty on negative-price intervals as well.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date as Date, datetime
from pathlib import Path
from threading import Lock
from typing import Mapping, Optional

from loop.real_day import IMBALANCE_CSV, LOCAL_TZ, N, PRICES_CSV, STEP_S, day_timestamps

REAL_DAY_AHEAD = "entsoe"
REAL_IMBALANCE = "synth_market"
SYNTHETIC = "synthetic"

# Used only when there is no price file at all: a plain Romanian-looking day,
# EUR/MWh per hour (night trough, morning ramp, solar dip, evening peak).
_FALLBACK_HOURLY = [95, 88, 84, 82, 84, 92, 110, 128, 120, 98, 75, 58,
                    50, 52, 62, 82, 112, 150, 185, 175, 150, 128, 112, 102]


@dataclass(frozen=True)
class IntervalPrice:
    timestamp: int
    day_ahead_eur_mwh: float
    imbalance_short_eur_mwh: float
    imbalance_long_eur_mwh: float
    day_ahead_source: str
    imbalance_source: str


class PriceSource:
    """Answers "what are the prices at this timestamp", real where it can be.

    ``day_ahead`` / ``imbalance`` inject the tables directly (tests); otherwise
    they are read from the two CSVs on first use. A missing file is not an
    error: everything it would have covered becomes synthetic.
    """

    def __init__(
        self,
        day_ahead: Optional[Mapping[int, float]] = None,
        imbalance: Optional[Mapping[int, tuple[float, float]]] = None,
        *,
        day_ahead_path: Path | str = PRICES_CSV,
        imbalance_path: Path | str = IMBALANCE_CSV,
    ) -> None:
        self._da = dict(day_ahead) if day_ahead is not None else None
        self._imb = dict(imbalance) if imbalance is not None else None
        self._da_path = Path(day_ahead_path)
        self._imb_path = Path(imbalance_path)
        self._profiles: dict[int, list[float]] | None = None
        self._synthetic: dict[str, tuple[list[float], list[tuple[float, float]]]] = {}
        self._lock = Lock()

    # --- loading ----------------------------------------------------------
    def _load(self) -> None:
        if self._profiles is not None:
            return
        with self._lock:
            if self._profiles is not None:
                return
            from loop.real_day import RealDayError, load_imbalance_prices, load_prices

            if self._da is None:
                try:
                    self._da = load_prices(self._da_path)
                except RealDayError:
                    self._da = {}
            if self._imb is None:
                try:
                    self._imb = {t: (p.short_eur_mwh, p.long_eur_mwh)
                                 for t, p in load_imbalance_prices(self._imb_path).items()}
                except RealDayError:
                    self._imb = {}
            self._profiles = _monthly_profiles(self._da)

    def coverage(self) -> dict[str, Optional[list[int]]]:
        """First and last real timestamp of each table, or None when it is empty."""
        self._load()
        return {
            "day_ahead": [min(self._da), max(self._da)] if self._da else None,
            "imbalance": [min(self._imb), max(self._imb)] if self._imb else None,
        }

    # --- prices -----------------------------------------------------------
    def at(self, ts: int, *, synthetic: bool = True) -> Optional[IntervalPrice]:
        """The prices of the interval starting at ``ts`` (snapped down to 15 min).

        ``synthetic=False``: None unless both prices are real.
        """
        self._load()
        ts = (int(ts) // STEP_S) * STEP_S
        da = self._da.get(ts)
        imb = self._imb.get(ts)
        if not synthetic and (da is None or imb is None):
            return None
        da_source = REAL_DAY_AHEAD if da is not None else SYNTHETIC
        imb_source = REAL_IMBALANCE if imb is not None else SYNTHETIC
        if da is None or imb is None:
            curve, spreads = self._synthetic_day(_local_date(ts))
            i = self._slot(ts)
            if da is None:
                da = curve[i]
            if imb is None:
                up, down = spreads[i]
                imb = (da + abs(da) * up, da - abs(da) * down)
        return IntervalPrice(
            timestamp=ts,
            day_ahead_eur_mwh=round(float(da), 2),
            imbalance_short_eur_mwh=round(float(imb[0]), 2),
            imbalance_long_eur_mwh=round(float(imb[1]), 2),
            day_ahead_source=da_source,
            imbalance_source=imb_source,
        )

    @staticmethod
    def _slot(ts: int) -> int:
        start = day_timestamps(_local_date(ts))[0]
        return min(N - 1, max(0, (ts - start) // STEP_S))

    def _synthetic_day(self, date: str) -> tuple[list[float], list[tuple[float, float]]]:
        cached = self._synthetic.get(date)
        if cached is not None:
            return cached
        d = Date.fromisoformat(date)
        rng = random.Random(f"price-api:{date}")  # str seeds are hashed with SHA-512: stable everywhere
        profile = _profile_for(self._profiles, d.month)
        level = min(1.5, max(0.6, rng.gauss(1.0, 0.15)))
        weekend = 0.85 if d.weekday() >= 5 else 1.0
        noise, curve = 0.0, []
        for base in profile:
            noise = 0.8 * noise + rng.gauss(0.0, 0.06)
            curve.append(round(base * level * weekend * (1.0 + noise), 2))
        spreads = [(rng.uniform(0.2, 0.8), rng.uniform(0.2, 0.7)) for _ in range(N)]
        self._synthetic[date] = (curve, spreads)
        return curve, spreads


# --------------------------------------------------------------------------- #


def _local_date(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), LOCAL_TZ).date().isoformat()


def _monthly_profiles(table: Mapping[int, float]) -> dict[int, list[float]]:
    """{month: 96 mean prices by local quarter-hour slot}, from the real file."""
    sums: dict[int, list[float]] = {}
    counts: dict[int, list[int]] = {}
    for ts, price in table.items():
        if not math.isfinite(price):
            continue
        local = datetime.fromtimestamp(ts, LOCAL_TZ)
        slot = local.hour * 4 + local.minute // 15
        s = sums.setdefault(local.month, [0.0] * N)
        c = counts.setdefault(local.month, [0] * N)
        s[slot] += price
        c[slot] += 1
    out: dict[int, list[float]] = {}
    for month, s in sums.items():
        c = counts[month]
        if all(c):  # a month with a gap in its profile would give a hole in the curve
            out[month] = [v / k for v, k in zip(s, c)]
    return out


def _profile_for(profiles: Mapping[int, list[float]], month: int) -> list[float]:
    if not profiles:
        return [float(_FALLBACK_HOURLY[i // 4]) for i in range(N)]
    if month in profiles:
        return profiles[month]
    # nearest month round the calendar: December borrows November (or January)
    nearest = min(profiles, key=lambda m: (min((m - month) % 12, (month - m) % 12), m))
    return profiles[nearest]
