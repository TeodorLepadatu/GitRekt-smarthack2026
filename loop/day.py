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

Peak shaving (``max_import_mw``): the guard caps the grid import on the
forecast when it decides, and once the interval's actual demand and production
are known the loop checks the cap again on those (a real site's controller reads
the meter), sending any extra discharge back through the guard. Physics and the
hard goals still have the last word; an import the battery cannot cover is
recorded as ``import_cap_unmet`` and counted by the settlement.

Operator control (``control``, a ``control.OperatorControl``): before the AI is
asked, the operator's command for the interval is checked.

    emergency hold  battery idle, the exportable surplus switched off; the AI,
                    the hard goals and the balancing are all skipped
    override        the operator's setpoint, through the guard like any decision
                    (physics, hard goals and the import cap still apply)
    otherwise       the AI decides; if the autonomy policy asks for it, the loop
                    waits for the operator's approval, and holds (through the
                    guard) on a rejection or a timeout

Each record's ``control`` says which of these happened (``action``), under
which autonomy ``mode``, and what the AI proposed when it had to wait
(``proposed_mw``). Only an AI decision that runs is balanced against the
forecast error; an operator's setpoint is run as given, and any deviation it
causes is settled as usual.
"""
from __future__ import annotations

import dataclasses
import time as _time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Optional, Sequence

from battery import BatteryConfig
from common import finite_float
from control import APPROVED, REJECTED, SUPERSEDED, TIMEOUT, Command, OperatorControl
from feature_builder import PriceCurve, SiteForecast, build_features, hhmm
from guard import GuardResult, guard
from pipeline import CycleResult, run_cycle
from settlement import DayLedger
from settlement.settle import ImbalancePrices, scheduled_position_mwh
from simulator import SiteSimulator
from validator import Decision, fallback_decision
from validator.schema import DEFAULT_TIMEOUT_S, REASON_APPROVAL_TIMEOUT, SOURCE_FALLBACK, SOURCE_OPERATOR

REASON_FEATURE_ERROR = "feature_error"  # the feature builder failed; the LLM was not called
RULE_EMERGENCY_HOLD = "emergency_hold"
# control actions after which the AI's decision runs (and is balanced against the forecast error)
AI_ACTIONS = ("auto", "approved")

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
    ``imbalance``: the price of deviating from the plan, per interval. Given, the
    battery also defends the promised grid position and the leftover deviation is
    charged for; left out, the day runs and settles exactly as it did before.
    """

    prices: Sequence[float]
    forecasts: Sequence[SiteForecast | Mapping[str, Any]]
    actuals: Optional[Sequence[Optional[Mapping[str, Any]]]] = None
    date: str = ""
    imbalance: Optional[Sequence[Optional[ImbalancePrices]]] = None

    def __post_init__(self) -> None:
        n = len(self.prices)
        if len(self.forecasts) != n:
            raise LoopError(f"{len(self.forecasts)} forecasts for {n} prices")
        if self.actuals is not None and len(self.actuals) != n:
            raise LoopError(f"{len(self.actuals)} actuals for {n} prices")
        if self.imbalance is not None and len(self.imbalance) != n:
            raise LoopError(f"{len(self.imbalance)} imbalance prices for {n} prices")

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
        max_import_mw: Optional[float] = None,
        control: Optional[OperatorControl] = None,
    ):
        self.cfg = battery or BatteryConfig()
        self.control = control  # None = no operator: the AI decides every interval
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
        limit = finite_float(max_import_mw)
        if max_import_mw is not None and (limit is None or limit < 0):
            raise LoopError(f"max_import_mw must be a number >= 0, got {max_import_mw!r}")
        self.max_import_mw = limit  # None = no grid import cap

        start = self.cfg.e_min_mwh if start_soc_mwh is None else start_soc_mwh
        self.sim = SiteSimulator(self.cfg, start)
        self.ledger = DayLedger(goal_config, battery=self.cfg, log_path=log_path,
                                import_limit_mw=self.max_import_mw)
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

        features, cycle, control = self._decide(t, forecast, warnings)
        demand, production = self._actual(i, forecast, warnings)
        imbalance, scheduled, setpoint = self._balance(i, cycle, forecast, demand, production, t, warnings,
                                                       adjust=control["action"] in AI_ACTIONS)
        if control["action"] == "hold":
            # Idle, nothing exported: asked to curtail, the simulator switches off exactly the
            # exportable surplus it measures. The import cap is not chased: that would move the battery.
            setpoint = SimpleNamespace(battery_setpoint_mw=0.0, curtail_mw=max(0.0, production - demand))
        else:
            cycle, setpoint = self._shave(i, cycle, setpoint, demand, production, t, warnings)
        site = self.sim.step(setpoint, demand, production, time=t)
        record = self.ledger.record(cycle, features, site, imbalance=imbalance, scheduled_mwh=scheduled)

        record["features"] = dict(features)
        record["site"] = site.to_dict()
        record["control"] = control
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

    def _decide(self, t: str, forecast: Any, warnings: list[str]) -> tuple[Mapping[str, Any], Any, dict]:
        """Features, the interval's decision, and who made it (the record's ``control``).

        The operator's command comes first; without one the AI decides, and the
        autonomy policy may make that decision wait for the operator's approval.
        """
        features, error = self._features(t, forecast, warnings)
        if self.control is None:
            return features, self._ai_cycle(features, error), _control("auto", "autonomous")
        mode = self.control.autonomy.mode
        command = self.control.command_for(t)
        if command is not None:
            return features, self._command_cycle(command, features), _control(command.kind, mode)

        cycle = self._ai_cycle(features, error)
        proposed = finite_float(getattr(cycle.setpoint, "battery_setpoint_mw", None)) or 0.0
        if not self.control.autonomy.needs_review(proposed, cycle.decision.confidence):
            return features, cycle, _control("auto", mode)
        verdict = self.control.review(time=t, setpoint_mw=proposed, source=cycle.decision.source,
                                      confidence=cycle.decision.confidence,
                                      explanation_for_operator=cycle.decision.explanation_for_operator)
        if verdict == APPROVED:
            return features, cycle, _control(APPROVED, mode, proposed)
        if verdict == SUPERSEDED:
            command = self.control.command_for(t)
            if command is not None:
                return features, self._command_cycle(command, features), _control(command.kind, mode, proposed)
        # Rejected, or nobody answered: hold. Through the guard, so the hard goals still hold too.
        action = REJECTED if verdict == REJECTED else TIMEOUT
        if action == REJECTED:
            decision = _operator_decision(0.0, self.cfg, f"The operator rejected the AI's proposal to "
                                                         f"{_verb(proposed)}: battery held.")
        else:
            decision = Decision(0.0, False, SOURCE_FALLBACK, fallback_reason=REASON_APPROVAL_TIMEOUT,
                                explanation_for_operator=f"No answer to the AI's proposal to {_verb(proposed)} "
                                                         f"in time: battery held.")
        return features, CycleResult(cycle.prompt, decision, self._guard(decision, features)), \
            _control(action, mode, proposed)

    def _features(self, t: str, forecast: Any, warnings: list[str]) -> tuple[Mapping[str, Any], Optional[str]]:
        """The feature builder's output, or a minimal stand-in and the error when it fails."""
        try:
            prices = self.day.imbalance[self.index] if self.day.imbalance is not None else None
            return self.feature_builder(
                time=t, forecast=forecast, prices=self.curve, stored_mwh=self.sim.stored_mwh,
                goal_config=self.goal_config, battery=self.cfg,
                imbalance_short_eur_mwh=None if prices is None else prices.short_eur_mwh,
                imbalance_long_eur_mwh=None if prices is None else prices.long_eur_mwh,
            ), None
        except Exception as exc:  # the cycle must go on
            warnings.append(f"features could not be built ({exc}); rule-based fallback")
            return self._minimal_features(t, forecast), str(exc)[:200]

    def _ai_cycle(self, features: Mapping[str, Any], error: Optional[str]) -> CycleResult:
        """The pipeline's decision. If the features could not be built, the rule-based
        fallback decides and the guard still enforces the hard goals."""
        if error is not None:
            decision = fallback_decision(features, REASON_FEATURE_ERROR, error)
            return CycleResult(None, decision, self._guard(decision, features))
        return run_cycle(features, self.goal_config, llm_call=self.llm_call,
                         battery=self.cfg, timeout_s=self.timeout_s,
                         max_import_mw=self.max_import_mw)

    def _command_cycle(self, command: Command, features: Mapping[str, Any]) -> CycleResult:
        """The operator's command as a cycle; the AI is not asked."""
        if command.kind == "hold":
            decision = _operator_decision(0.0, self.cfg, "Emergency hold: the battery is idle and nothing is "
                                                         "exported until the operator releases it.")
            stored = self.sim.stored_mwh
            surplus = max(0.0, finite_float(features.get("net_mw")) or 0.0)
            return CycleResult(None, decision, GuardResult(
                battery_setpoint_mw=0.0, curtail_mw=round(surplus, 4), clipped=False,
                applied_rules=[RULE_EMERGENCY_HOLD], source=SOURCE_OPERATOR,
                expected_soc_after_mwh=round(stored, 4),
            ))
        decision = _operator_decision(command.setpoint_mw, self.cfg,
                                      f"Operator override: {_verb(command.setpoint_mw)} until {command.until}.")
        return CycleResult(None, decision, self._guard(decision, features))

    def _guard(self, decision: Any, features: Mapping[str, Any]) -> GuardResult:
        return guard(decision, features, self.goal_config, self.cfg, max_import_mw=self.max_import_mw)

    def _balance(
        self, i: int, cycle: Any, forecast: Any, demand: float, production: float,
        t: str, warnings: list[str], *, adjust: bool = True,
    ) -> tuple[Optional[ImbalancePrices], Optional[float], Any]:
        """Hold the promised grid position, so far as the guard and the battery allow.

        The setpoint was chosen on the forecast and the grid balances whatever
        really happens, so once a deviation is charged for, the battery's job is
        to absorb the forecast error. The adjusted setpoint goes back through the
        guard: the hard goals and the battery limits still have the last word, and
        whatever they refuse stays as a deviation and is paid for.

        Returns the interval's imbalance prices, the promised position, and the
        setpoint to run. Without imbalance prices, or with ``adjust`` off (an
        operator's setpoint, run as given), nothing is adjusted.
        """
        prices = self.day.imbalance[i] if self.day.imbalance is not None else None
        if prices is None:
            return None, None, cycle.setpoint
        f_demand, f_production = _forecast_values(forecast)
        if f_demand is None or f_production is None:
            warnings.append("no forecast for this interval; the promise could not be priced")
            return None, None, cycle.setpoint

        commanded = finite_float(getattr(cycle.setpoint, "battery_setpoint_mw", None)) or 0.0
        curtail = finite_float(getattr(cycle.setpoint, "curtail_mw", None)) or 0.0
        scheduled = scheduled_position_mwh(
            forecast_demand_mw=f_demand, forecast_production_mw=f_production,
            battery_mw=commanded, curtail_mw=curtail, cfg=self.cfg,
        )
        if not adjust:
            return prices, scheduled, cycle.setpoint
        wanted = balancing_setpoint_mw(
            scheduled_mwh=scheduled, demand_mw=demand, production_mw=production,
            curtail_mw=curtail, cfg=self.cfg,
        )
        if abs(wanted - commanded) < 1e-9:
            return prices, scheduled, cycle.setpoint

        p_max = self.cfg.p_max_mw
        fraction = max(-1.0, min(1.0, wanted / p_max)) if p_max > 0 else 0.0
        state = {"time": t, "stored_mwh": self.sim.stored_mwh, "price_now": self.curve.prices[i],
                 "demand_mw": demand, "production_mw": production, "net_mw": production - demand}
        try:
            checked = guard({"battery_fraction": fraction, "curtail": curtail > 0},
                            state, self.goal_config, self.cfg, max_import_mw=self.max_import_mw)
        except Exception as exc:  # the interval must go on with the original setpoint
            warnings.append(f"balancing could not be checked ({exc}); kept the planned setpoint")
            return prices, scheduled, cycle.setpoint
        # The guard decides the battery; the curtailment was a deliberate choice, so it stands.
        return prices, scheduled, SimpleNamespace(
            battery_setpoint_mw=checked.battery_setpoint_mw, curtail_mw=curtail,
        )

    def _shave(
        self, i: int, cycle: Any, setpoint: Any, demand: float, production: float,
        t: str, warnings: list[str],
    ) -> tuple[Any, Any]:
        """Hold the grid import cap on the ACTUAL demand and production.

        The guard capped the import on the forecast; if the measured net load
        would still push the import over ``max_import_mw``, the setpoint is sent
        back through the guard (with the measured state) and lowered by what it
        allows. It only ever lowers the setpoint, so it cannot undo a hard goal's
        forced charge; what it cannot cover is recorded as ``import_cap_unmet``.

        Returns the cycle (its guard result updated when the cap acted, so the
        record shows why) and the command to run.
        """
        if self.max_import_mw is None:
            return cycle, setpoint
        battery = finite_float(getattr(setpoint, "battery_setpoint_mw", None)) or 0.0
        curtail = finite_float(getattr(setpoint, "curtail_mw", None)) or 0.0
        net = production - demand
        if battery - net <= self.max_import_mw + 1e-6:   # the meter agrees: within the cap
            return cycle, setpoint
        p_max = self.cfg.p_max_mw
        state = {"time": t, "stored_mwh": self.sim.stored_mwh, "price_now": self.curve.prices[i],
                 "demand_mw": demand, "production_mw": production, "net_mw": net}
        try:
            checked = guard({"battery_fraction": max(-1.0, min(1.0, battery / p_max)) if p_max > 0 else 0.0,
                             "curtail": curtail > 0},
                            state, self.goal_config, self.cfg, max_import_mw=self.max_import_mw)
        except Exception as exc:  # the interval must go on with the setpoint it has
            warnings.append(f"import cap could not be checked on the actuals ({exc}); kept the setpoint")
            return cycle, setpoint
        shaved = min(battery, checked.battery_setpoint_mw)
        cap_rules = [r for r in checked.applied_rules if r.startswith("import_cap_")]
        if shaved < battery - 1e-6:
            warnings.append(f"import cap: actual net load {-net:.3f} MW, setpoint {battery:+.3f} -> "
                            f"{shaved:+.3f} MW")
        old = cycle.setpoint
        if isinstance(old, GuardResult):
            kept = [r for r in old.applied_rules if r not in ("import_cap_ok", "import_cap_unchecked")]
            rules = kept + [r for r in cap_rules + ["import_cap_realtime"] if r not in kept]
            new_guard = dataclasses.replace(
                old, battery_setpoint_mw=round(shaved, 4), applied_rules=rules,
                clipped=old.clipped or shaved < battery - 1e-3,
                projected_import_mw=round(max(0.0, shaved - net), 4),
                warnings=list(old.warnings) + [w for w in checked.warnings if w.startswith("import cap")],
            )
            cycle = CycleResult(getattr(cycle, "prompt", None), cycle.decision, new_guard)
        return cycle, SimpleNamespace(battery_setpoint_mw=round(shaved, 4), curtail_mw=curtail)

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


def _control(action: str, mode: str, proposed_mw: Optional[float] = None) -> dict:
    """A record's ``control``: who set the battery, under which autonomy mode."""
    return {"action": action, "mode": mode,
            "proposed_mw": None if proposed_mw is None else round(proposed_mw, 4)}


def _operator_decision(setpoint_mw: float, cfg: BatteryConfig, explanation: str) -> Decision:
    """The operator's setpoint, in the shape the guard and the ledger expect from the AI."""
    fraction = setpoint_mw / cfg.p_max_mw if cfg.p_max_mw > 0 else 0.0
    # Not rounded: 2 MW of 3 must come out of the guard as 2 MW, not 2.0001.
    return Decision(battery_fraction=max(-1.0, min(1.0, fraction)), curtail=False,
                    source=SOURCE_OPERATOR, confidence="high", explanation_for_operator=explanation)


def _verb(mw: float) -> str:
    """'charge 2.00 MW' / 'discharge 1.50 MW' / 'hold'."""
    if mw > 0.005:
        return f"charge {mw:.2f} MW"
    if mw < -0.005:
        return f"discharge {-mw:.2f} MW"
    return "hold"


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


def balancing_setpoint_mw(
    *,
    scheduled_mwh: float,
    demand_mw: float,
    production_mw: float,
    curtail_mw: float,
    cfg: BatteryConfig,
) -> float:
    """The battery power that would hold the promised grid exchange exactly.

    Rearranged from the site's energy balance
    (``grid = production - curtailed - demand - battery``), on what really
    happened rather than the forecast. Equivalent to the promised setpoint plus
    the net forecast error, so defending the promise and absorbing the error are
    the same action. The guard and the battery's own limits decide how much of
    it actually happens.
    """
    return production_mw - curtail_mw - demand_mw - scheduled_mwh / cfg.interval_h
