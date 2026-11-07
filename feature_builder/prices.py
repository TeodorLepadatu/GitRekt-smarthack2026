"""The day's day-ahead prices on a fixed interval grid (plan section 2, "Price API").

One :class:`PriceCurve` holds all 96 quarter-hour prices for the day and answers
every price question the feature builder asks: what it costs now, how that ranks
against the rest of the day, what the best later price is, and when the next
cheap refill period starts.

Everything here is pure arithmetic over the curve. No forecasting, no decisions.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence

from common import finite_float
from guard.goals import DAY_MIN, to_minutes

# A later interval counts as a "cheap refill" when its price is negative, or
# when it sits in the cheapest CHEAP_RANK percent of the day AND is cheaper
# than the price right now. Same threshold as prompt_assembly.situation.
CHEAP_RANK = 30


class PriceCurveError(ValueError):
    """The price curve cannot be used: empty, wrong length, or non-numeric."""


def hhmm(minutes: float) -> str:
    """Minutes since midnight -> 'HH:MM', wrapping at 24 h."""
    m = int(round(minutes)) % DAY_MIN
    return f"{m // 60:02d}:{m % 60:02d}"


class PriceCurve:
    """Day-ahead prices in EUR/MWh, one per interval, starting at 00:00.

    ``len(prices)`` sets the grid: 96 prices means 15-minute intervals, 24 means
    hourly. The length must divide the day exactly.
    """

    __slots__ = ("prices", "n", "interval_min", "_sorted")

    def __init__(self, prices: Sequence[float] | Iterable[float]) -> None:
        values: list[float] = []
        for i, raw in enumerate(prices):
            value = finite_float(raw)
            if value is None:
                raise PriceCurveError(f"price at index {i} is not a finite number: {raw!r}")
            values.append(value)
        if not values:
            raise PriceCurveError("price curve is empty")
        if DAY_MIN % len(values):
            raise PriceCurveError(f"{len(values)} prices do not divide a 24 h day evenly")
        self.prices: tuple[float, ...] = tuple(values)
        self.n = len(values)
        self.interval_min = DAY_MIN // self.n
        self._sorted = tuple(sorted(values))

    # --- grid -------------------------------------------------------------
    def index_at(self, time: object) -> int:
        """Index of the interval containing ``time`` ('HH:MM', time or datetime)."""
        minutes = to_minutes(time)
        if minutes is None:
            raise PriceCurveError(f"not a time: {time!r}")
        return min(self.n - 1, (minutes % DAY_MIN) // self.interval_min)

    def time_at(self, index: int) -> str:
        return hhmm(index * self.interval_min)

    def price_at(self, index: int) -> float:
        return self.prices[index]

    # --- whole-day statistics ---------------------------------------------
    @property
    def average(self) -> float:
        return sum(self.prices) / self.n

    def rank(self, index: int) -> int:
        """0 = cheapest interval of the day, 100 = most expensive. Ties share a rank."""
        if self.n == 1:
            return 50
        price = self.prices[index]
        below = sum(1 for p in self.prices if p < price)
        equal = sum(1 for p in self.prices if p == price)
        position = below + (equal - 1) / 2.0  # middle of the tied block
        return int(round(100.0 * position / (self.n - 1)))

    @property
    def cheap_price(self) -> float:
        """Price at the CHEAP_RANK percentile: at or below this counts as cheap."""
        return self._sorted[int(round(CHEAP_RANK / 100.0 * (self.n - 1)))]

    # --- the rest of the day ----------------------------------------------
    def later(self, index: int, end: Optional[int] = None) -> list[int]:
        """Indices strictly after ``index``, up to but not including ``end``."""
        return list(range(index + 1, self.n if end is None else min(end, self.n)))

    def extreme_later(self, index: int, *, highest: bool) -> Optional[tuple[int, float]]:
        """(index, price) of the highest or lowest later interval, earliest one on a tie."""
        rest = self.later(index)
        if not rest:
            return None
        pick = max(rest, key=lambda i: self.prices[i]) if highest else min(rest, key=lambda i: self.prices[i])
        best = self.prices[pick]
        first = next(i for i in rest if self.prices[i] == best)
        return first, best

    def kth_highest(self, indices: Sequence[int], k: int) -> Optional[float]:
        """The k-th highest price over ``indices`` (k = 1 is the maximum).

        Plan section 6, fix 2: one interval only sells ``P_max x 0.25 h``, so a
        battery holding several intervals' worth of energy cannot get the single
        best price for all of it. ``k`` is how many intervals it takes to empty.
        When there are fewer than ``k`` intervals left, the last one is used.
        """
        if not indices:
            return None
        ordered = sorted((self.prices[i] for i in indices), reverse=True)
        return ordered[min(max(k, 1), len(ordered)) - 1]

    def next_cheap_after(self, index: int) -> Optional[int]:
        """First later interval that is a cheap refill opportunity (plan section 6, fix 3)."""
        threshold, now = self.cheap_price, self.prices[index]
        for i in self.later(index):
            price = self.prices[i]
            if price < 0 or (price <= threshold and price < now):
                return i
        return None


def intervals_to_empty(deliverable_mwh: float, p_max_mw: float, interval_h: float) -> int:
    """How many intervals full-power discharge needs to deliver ``deliverable_mwh``.

    This is the ``k`` of the k-th highest price rule. Always at least 1.
    """
    per_interval = p_max_mw * interval_h
    if per_interval <= 0 or deliverable_mwh <= 0:
        return 1
    return max(1, math.ceil(deliverable_mwh / per_interval - 1e-9))
