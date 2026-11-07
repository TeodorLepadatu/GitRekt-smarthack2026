"""The clock: runs the whole cycle for every 15-minute interval of a day (plan section 2).

    for each interval:
        feature builder ─► pipeline (prompt → LLM → validator → guard)
            ─► site simulator (the battery runs the command)
            ─► settlement (the money and goal status)
        and the simulator's new stored energy feeds the next interval

It owns the one piece of state that crosses intervals: the battery, held by the
site simulator. Everything else is recomputed every interval.

It never stops part-way through a day: if the features cannot be built, the
interval falls back to the rule-based policy and the guard (which still
enforces the hard goals); if an actual measurement is missing or broken, the
forecast is used instead. Both are recorded in the interval's ``loop_warnings``.
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from battery import BatteryConfig
from common import finite_float
from feature_builder import PriceCurve, SiteForecast, build_features, hhmm
from guard import guard
from pipeline import CycleResult, run_cycle
from settlement import DayLedger
from simulator import SiteSimulator
from validator import fallback_decision
from validator.schema import DEFAULT_TIMEOUT_S

REASON_FEATURE_ERROR = "feature_error"  # the feature builder failed; the LLM was not called

FeatureBuilder = Callable[..., Mapping[str, Any]]


class LoopError(ValueError):
    """The day's input cannot be run (wrong number of intervals, bad start time, ...)."""


@dataclass(frozen=True)
class DayInput:
    """Everything known about one day, one entry per interval.

    ``prices``: day-ahead EUR/MWh. ``forecasts``: ML 1 + ML 2 per interval
    (``SiteForecast`` or a dict with ``demand_mw``, ``production_mw``, ...).
    ``actuals``: what really happened, ``{"demand_mw": ..., "production_mw": ...}``
    per interval; ``None`` (or a missing entry) means "use the forecast".
    """

    prices: Sequence[float]
    forecasts: Sequence[SiteForecast | Mapping[str, Any]]
    actuals: Optional[Sequence[Optional[Mapping[str, Any]]]] = None
    date: str = ""

    def __post_init__(self) -> None:
        n = len(self.prices)
        if len(self.forecasts) != n:
            raise LoopError(f"{len(self.forecasts)} forecasts for {n} prices")
        if self.actuals is not None and len(self.actuals) != n:
            raise LoopError(f"{len(self.actuals)} actuals for {n} prices")

    @property
    def n(self) -> int:
        return len(self.prices)


class DayRunner:
    """Runs one day, interval by interval.

        runner = DayRunner(day, goal_config, start_soc_mwh=4.0, llm_call=agent)
        runner.run()                      # the whole day, as fast as possible
        runner.run_realtime(speed=60)     # or 1 interval every 15 s (60x) for a live demo
        runner.summary()                  # profit, goals, LLM/fallback counts, ...
    """

    def __init__(
        self,
        day: DayInput,
        goal_config: Any = None,
        *,
        start_soc_mwh: Optional[float] = None,
        start_time: str = "00:00",
        battery: Optional[BatteryConfig] = None,
        llm_call: Optional[Callable[[str], Any]] = None,
        feature_builder: FeatureBuilder = build_features,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        on_interval: Optional[Callable[[dict], None]] = None,
        log_path: Optional[str] = None,
    ):
        self.cfg = battery or BatteryConfig()
        self.day = day
        expected = round(24 / self.cfg.interval_h)
        if day.n != expected:
            raise LoopError(f"a day has {expected} intervals of {self.cfg.interval_h * 60:g} min, got {day.n}")
        self.curve = PriceCurve(day.prices)
        self.index = self._index_of(start_time)

        self.goal_config = goal_config
        self.llm_call = llm_call
        self.feature_builder = feature_builder
        self.timeout_s = timeout_s
        self.on_interval = on_interval

        start = self.cfg.e_min_mwh if start_soc_mwh is None else start_soc_mwh
        self.sim = SiteSimulator(self.cfg, start)
        self.ledger = DayLedger(goal_config, battery=self.cfg, log_path=log_path)
        self.records: list[dict] = []

    # ------------------------------------------------------------------ control

    @property
    def done(self) -> bool:
        return self.index >= self.day.n

    @property
    def now(self) -> Optional[str]:
        """Start time of the next interval to run, or None once the day is over."""
        return None if self.done else self.curve.time_at(self.index)

    def set_goals(self, goal_config: Any) -> None:
        """The user changed their goals: they apply from the next interval on."""
        self.goal_config = goal_config
        self.ledger.set_goals(goal_config)

    def step(self) -> dict:
        """Run the next interval and return its record."""
        if self.done:
            raise LoopError("the day is over")
        i, t = self.index, self.curve.time_at(self.index)
        warnings: list[str] = []
        forecast = self.day.forecasts[i]

        features, cycle = self._decide(t, forecast, warnings)
        demand, production = self._actual(i, forecast, warnings)
        site = self.sim.step(cycle.setpoint, demand, production, time=t)
        record = self.ledger.record(cycle, features, site)

        record["features"] = dict(features)
        record["site"] = site.to_dict()
        record["loop_warnings"] = warnings
        self.records.append(record)
        self.index += 1
        if self.on_interval is not None:
            try:
                self.on_interval(record)
            except Exception as exc:  # a dashboard or logger failing must never stop the site
                warnings.append(f"on_interval callback failed: {exc!r}")
        return record

    def run(self, until: Optional[str] = None) -> list[dict]:
        """Run every remaining interval (or up to, not including, ``until``), as fast as possible."""
        stop = self.day.n if until is None else self._index_of(until)
        out = []
        while self.index < stop:
            out.append(self.step())
        return out

    def run_realtime(
        self,
        speed: float = 1.0,
        *,
        until: Optional[str] = None,
        sleep: Callable[[float], None] = _time.sleep,
        clock: Callable[[], float] = _time.monotonic,
    ) -> list[dict]:
        """Run like the real site: one interval every 15 minutes / ``speed``.

        ``speed=1`` is real time, ``speed=60`` one interval every 15 s, ``speed=600``
        every 1.5 s (the dashboard's demo speeds). If an interval takes longer than
        its slot (a slow LLM), the next one starts right away and the overrun is
        recorded in ``loop_warnings``.
        """
        if not speed > 0:
            raise LoopError(f"speed must be positive, got {speed!r}")
        slot_s = self.cfg.interval_h * 3600 / speed
        stop = self.day.n if until is None else self._index_of(until)
        out = []
        # Each interval has a fixed deadline counted from the start, so small delays
        # (sleep overshoot, slow intervals) never add up into drift.
        deadline = clock()
        while self.index < stop:
            started = clock()
            deadline += slot_s
            record = self.step()
            elapsed = clock() - started
            if elapsed > slot_s:
                record["loop_warnings"].append(f"interval took {elapsed:.1f} s, longer than its {slot_s:.1f} s slot")
            out.append(record)
            if self.index < stop:
                wait = deadline - clock()
                if wait > 0:
                    sleep(wait)
                else:
                    deadline = clock()  # behind schedule: start the next slot now instead of rushing to catch up
        return out

    def summary(self) -> dict:
        """Day totals and goal results. Goals are finalised once the day is over."""
        return {
            "date": self.day.date,
            "intervals_run": len(self.records),
            "intervals_total": self.day.n,
            "stored_mwh": self.sim.stored_mwh,
            "final_goal_status": self.ledger.finalize() if self.done else None,
            "goals": self.ledger.goal_summary(),
            "metrics": self.ledger.metrics(),
        }

    # ------------------------------------------------------------------ steps

    def _decide(self, t: str, forecast: Any, warnings: list[str]) -> tuple[Mapping[str, Any], Any]:
        """Features and the pipeline's decision. If the features cannot be built, the
        rule-based fallback decides and the guard still enforces the hard goals."""
        try:
            features = self.feature_builder(
                time=t, forecast=forecast, prices=self.curve, stored_mwh=self.sim.stored_mwh,
                goal_config=self.goal_config, battery=self.cfg,
            )
        except Exception as exc:  # the cycle must go on
            warnings.append(f"features could not be built ({exc}); rule-based fallback")
            features = self._minimal_features(t, forecast)
            decision = fallback_decision(features, REASON_FEATURE_ERROR, str(exc)[:200])
            return features, CycleResult(None, decision, guard(decision, features, self.goal_config, self.cfg))
        return features, run_cycle(features, self.goal_config, llm_call=self.llm_call,
                                   battery=self.cfg, timeout_s=self.timeout_s)

    def _minimal_features(self, t: str, forecast: Any) -> dict[str, Any]:
        """What is known without the feature builder: enough for the fallback and the guard."""
        demand, production = _forecast_values(forecast)
        f: dict[str, Any] = {"time": t, "stored_mwh": self.sim.stored_mwh,
                             "price_now": self.curve.prices[self.index]}
        if demand is not None and production is not None:
            f.update(demand_mw=demand, production_mw=production, net_mw=production - demand)
        return f

    def _actual(self, i: int, forecast: Any, warnings: list[str]) -> tuple[float, float]:
        """The interval's actual demand and production; the forecast stands in if they are missing."""
        actual = self.day.actuals[i] if self.day.actuals is not None else None
        f_demand, f_production = _forecast_values(forecast)
        values = []
        for key, fallback in (("demand_mw", f_demand), ("production_mw", f_production)):
            v = finite_float(actual.get(key)) if isinstance(actual, Mapping) else None
            if v is None or v < 0:
                if actual is not None:
                    warnings.append(f"actual {key} missing or invalid; used the forecast")
                v = fallback if fallback is not None and fallback >= 0 else 0.0
            values.append(v)
        return values[0], values[1]

    def _index_of(self, time: str) -> int:
        if time == "24:00":  # the end of the day
            return self.day.n
        try:
            i = self.curve.index_at(time)
        except Exception as exc:
            raise LoopError(f"time must be 'HH:MM', got {time!r}") from exc
        if self.curve.time_at(i) != time:
            raise LoopError(f"{time} is not the start of an interval")
        return i


def _forecast_values(forecast: Any) -> tuple[Optional[float], Optional[float]]:
    def get(key: str) -> Any:
        return forecast.get(key) if isinstance(forecast, Mapping) else getattr(forecast, key, None)

    demand = finite_float(get("demand_mw"))
    production = finite_float(get("production_mw"))
    if production is None:
        solar, wind = finite_float(get("solar_mw")), finite_float(get("wind_mw"))
        if solar is not None or wind is not None:
            production = (solar or 0.0) + (wind or 0.0)
    return demand, production
