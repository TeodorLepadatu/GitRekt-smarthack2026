"""The dashboard's payloads: Python objects -> the shapes `frontend/src/api/types.ts` declares.

The loop, the guard and the settlement each name things the way their own job
needs; the dashboard has one fixed vocabulary. This module is the single place
the two meet, so neither side has to bend to the other:

    interval_payload(record)        -> Interval          (also the SSE "cycle" event)
    forecast_payload(day, i, ...)   -> ForecastInterval  (an interval not yet decided)
    day_payload(runner)             -> DayResponse       GET /api/day
    state_payload(runner, ...)      -> StateSnapshot     GET /api/state
    metrics_payload(runner)         -> Metrics           GET /api/metrics
    goals_payload(runner)           -> Goal[]            GET /api/goals

Every function returns plain JSON-ready dicts and never raises on a partial or
broken record: a dashboard must not be able to stop the site.

What the names map to
---------------------
``soc_mwh`` is the energy stored at the *end* of the interval, the loop's
``soc_after_mwh``. The seven money fields the loop keeps flat move into a
nested ``settlement`` object. ``break_even`` comes from the feature builder,
and for intervals that have not run yet it is recomputed from the price curve
and the latest state of charge.

``demand_mw`` and ``production_mw`` are what was *measured*; ``solar_mw`` and
``wind_mw`` are only forecast separately, so the measured production is split
between them in the forecast's ratio. The dashboard's energy-flow diagram
needs ``solar_mw + wind_mw == production_mw`` to balance its arrows, and this
keeps that true while staying on the measured total.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from battery import BatteryConfig
from common import finite_float, goal_config_dict
from feature_builder import PriceCurve, SiteForecast, break_even_at

SOURCES = ("llm", "fallback", "operator")  # operator = an override set on the Day tab
CONFIDENCES = ("low", "medium", "high")
# settlement/goals.py statuses, which are already the dashboard's GoalStatus union
STATUSES = ("on_track", "behind", "met", "unreachable", "missed", "violated")

SETTLEMENT_FIELDS = ("grid_import_mwh", "grid_export_mwh", "energy_cost_eur", "wear_eur",
                     "profit_eur", "no_battery_profit_eur", "value_added_eur",
                     "deviation_mwh", "imbalance_eur")
# Goal fields that come from the goal definition, not from its running state.
GOAL_DEF_FIELDS = ("mwh", "by", "until", "eur_per_mwh")


def _num(value: Any, default: float = 0.0) -> float:
    v = finite_float(value)
    return default if v is None else float(v)


def _opt(value: Any) -> Optional[float]:
    v = finite_float(value)
    return None if v is None else float(v)


def _str(value: Any, default: str = "") -> str:
    return default if value is None else str(value)


def _one_of(value: Any, allowed: Sequence[str], default: str) -> str:
    v = None if value is None else str(value)
    return v if v in allowed else default


def _split(production_mw: float, solar: Optional[float], wind: Optional[float]) -> tuple[float, float]:
    """Attribute the measured production to solar and wind in the forecast's ratio.

    The two always add up to ``production_mw``: the diagram balances on that.
    With no forecast split to go by (a degraded interval, where the feature
    builder failed), it all goes to solar so the total still holds.
    """
    s, w = max(0.0, _num(solar)), max(0.0, _num(wind))
    total = s + w
    if production_mw <= 0:
        return 0.0, 0.0
    if total <= 0:
        return round(production_mw, 3), 0.0
    out_s = round(production_mw * s / total, 3)
    return out_s, round(production_mw - out_s, 3)


# --------------------------------------------------------------------------- #
# one interval


def interval_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    """One settled interval as the dashboard's ``Interval``.

    ``record`` is what :meth:`loop.DayRunner.step` returns (or a line of its
    JSONL log). This is also the payload of the SSE ``cycle`` event.
    """
    rec = record if isinstance(record, Mapping) else {}
    features = rec.get("features") if isinstance(rec.get("features"), Mapping) else {}
    decision = rec.get("decision") if isinstance(rec.get("decision"), Mapping) else {}
    guard = rec.get("guard") if isinstance(rec.get("guard"), Mapping) else {}
    site = rec.get("site") if isinstance(rec.get("site"), Mapping) else {}

    production = max(0.0, _num(rec.get("production_mw")))
    solar, wind = _split(production, features.get("solar_mw"), features.get("wind_mw"))

    return {
        "time": _str(rec.get("time"), "??:??"),
        "price_eur_mwh": _num(rec.get("price_eur_mwh")),
        "break_even": _num(features.get("break_even")),
        "demand_mw": max(0.0, _num(rec.get("demand_mw"))),
        "production_mw": production,
        "solar_mw": solar,
        "wind_mw": wind,
        "soc_mwh": _num(rec.get("soc_after_mwh")),
        "decision": {
            "battery_fraction": _num(decision.get("battery_fraction")),
            "curtail": bool(decision.get("curtail")),
            "source": _one_of(decision.get("source"), SOURCES, "fallback"),
            "confidence": _one_of(decision.get("confidence"), CONFIDENCES, "low"),
            "reasoning": _str(decision.get("reasoning")),
            "explanation_for_operator": _str(decision.get("explanation_for_operator")),
            "fallback_reason": None if decision.get("fallback_reason") is None
                               else _str(decision.get("fallback_reason")),
        },
        "guard": {
            # What the site actually did (the simulator's step, when the record has one): the
            # battery may cut a setpoint at its limits, and the export limiter sizes curtailment
            # on the measured surplus, so the guard's own numbers can differ from what happened.
            "battery_setpoint_mw": _num(site.get("setpoint_applied_mw", guard.get("battery_setpoint_mw"))),
            "curtail_mw": max(0.0, _num(site.get("curtailed_mw", guard.get("curtail_mw")))),
            "clipped": bool(guard.get("clipped")) or bool(site.get("clipped")),
            "applied_rules": [str(r) for r in (guard.get("applied_rules") or [])],
        },
        "settlement": {name: _num(rec.get(name)) for name in SETTLEMENT_FIELDS},
    }


def forecast_payload(
    forecast: SiteForecast | Mapping[str, Any],
    *,
    time: str,
    price_eur_mwh: float,
    break_even: float,
) -> dict[str, Any]:
    """An interval that has not run yet: the forecast half of ``Interval``.

    Here ``solar_mw``/``wind_mw``/``production_mw`` are all forecasts, so they
    agree without any splitting.
    """
    f = forecast if isinstance(forecast, SiteForecast) else SiteForecast.from_mapping(forecast or {})
    solar, wind = max(0.0, _num(f.solar_mw)), max(0.0, _num(f.wind_mw))
    production = _opt(f.production_mw)
    return {
        "time": time,
        "price_eur_mwh": _num(price_eur_mwh),
        "break_even": _num(break_even),
        "demand_mw": max(0.0, _num(f.demand_mw)),
        "production_mw": max(0.0, solar + wind if production is None else production),
        "solar_mw": solar,
        "wind_mw": wind,
    }


# --------------------------------------------------------------------------- #
# the whole day


def day_payload(runner: Any) -> dict[str, Any]:
    """``GET /api/day``: 96 slots, decided ones full, the rest forecast only."""
    records = list(getattr(runner, "records", []) or [])
    day = getattr(runner, "day", None)
    curve = getattr(runner, "curve", None)
    if curve is None and day is not None:
        curve = PriceCurve(day.prices)

    intervals: list[dict[str, Any]] = [interval_payload(r) for r in records]
    if day is not None and curve is not None:
        cfg = getattr(runner, "cfg", None) or BatteryConfig()
        stored = _num(getattr(getattr(runner, "sim", None), "stored_mwh", None), cfg.e_min_mwh)
        for i in range(len(intervals), day.n):
            intervals.append(forecast_payload(
                day.forecasts[i],
                time=curve.time_at(i),
                price_eur_mwh=curve.price_at(i),
                break_even=_safe_break_even(curve, i, stored, cfg),
            ))
    return {
        "date": _str(getattr(day, "date", "")),
        "now": intervals[len(records) - 1]["time"] if records else None,
        "intervals": intervals,
    }


def _safe_break_even(curve: Any, index: int, stored: float, cfg: BatteryConfig) -> float:
    try:
        return break_even_at(curve, index, stored, battery=cfg)
    except Exception:  # a dashboard number is never worth an exception
        return 0.0


def state_payload(runner: Any, *, running: bool = False, speed: float = 0.0) -> dict[str, Any]:
    """``GET /api/state``: battery size, today's profit and the latest interval."""
    cfg = getattr(runner, "cfg", None) or BatteryConfig()
    records = list(getattr(runner, "records", []) or [])
    ledger = getattr(runner, "ledger", None)
    profit = getattr(ledger, "profit_eur", None) if ledger is not None else None
    if profit is None:
        profit = records[-1].get("profit_today_eur") if records else 0.0
    return {
        "capacity_mwh": _num(cfg.capacity_mwh),
        "max_power_mw": _num(cfg.p_max_mw),
        "profit_today_eur": _num(profit),
        "current": interval_payload(records[-1]) if records else None,
        "sim": {"running": bool(running), "speed": _num(speed)},
    }


def metrics_payload(runner: Any) -> dict[str, Any]:
    """``GET /api/metrics``. The ledger already totals these; extra keys are kept."""
    ledger = getattr(runner, "ledger", None)
    try:
        metrics = dict(ledger.metrics())
    except Exception:
        metrics = {}
    for name in ("profit_today_eur", "energy_cost_eur", "wear_eur", "no_battery_profit_eur",
                 "value_added_eur", "energy_sold_mwh", "energy_bought_mwh", "curtailed_mwh"):
        metrics[name] = _num(metrics.get(name))
    for name in ("decisions_total", "decisions_llm", "decisions_fallback", "decisions_clipped",
                 "goals_met", "goals_total", "import_limit_exceeded_intervals"):
        metrics[name] = int(_num(metrics.get(name)))
    # Peak shaving: the limit stays null when no cap is set.
    for name in ("peak_import_mw", "peak_import_mw_no_battery"):
        metrics[name] = _num(metrics.get(name))
    metrics["import_limit_mw"] = _opt(metrics.get("import_limit_mw"))
    metrics["peak_import_time"] = (None if metrics.get("peak_import_time") is None
                                   else _str(metrics.get("peak_import_time")))
    return metrics


def goals_payload(runner: Any) -> list[dict[str, Any]]:
    """``GET /api/goals``: each goal's definition and how it is doing."""
    ledger = getattr(runner, "ledger", None)
    try:
        states = {str(s["id"]): s for s in (ledger.goal_summary() or []) if isinstance(s, Mapping)}
    except Exception:
        states = {}
    defs = goal_config_dict(getattr(runner, "goal_config", None)).get("goals") or []
    stored = _opt(getattr(getattr(runner, "sim", None), "stored_mwh", None))

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for goal in defs:
        if not isinstance(goal, Mapping) or goal.get("id") is None:
            continue
        gid = str(goal["id"])
        seen.add(gid)
        out.append(_goal(gid, goal, states.get(gid, {}), stored))
    for gid, state in states.items():  # a goal the user has since removed, still tracked today
        if gid not in seen:
            out.append(_goal(gid, {}, state, stored))
    return out


def _goal(gid: str, goal: Mapping[str, Any], state: Mapping[str, Any],
          stored_mwh: Optional[float]) -> dict[str, Any]:
    gtype = _str(goal.get("type") or state.get("type"), "max_profit")
    out: dict[str, Any] = {
        "id": gid,
        "type": gtype,
        "hard": (goal.get("hard", state.get("hard", True))) is not False,
        "source_text": _str(goal.get("source_text") or state.get("source_text")),
        "status": _one_of(state.get("status"), STATUSES, "on_track"),
        "message": _str(state.get("message")),
    }
    for name in GOAL_DEF_FIELDS:
        value = goal.get(name)
        if value is not None:
            out[name] = value if name in ("by", "until") else _opt(value)
    for name in ("need_mwh", "hours_left"):
        if state.get(name) is not None:
            out[name] = _opt(state.get(name))
    for name in ("reached_at", "violated_at"):
        out[name] = None if state.get(name) is None else _str(state.get(name))
    if gtype == "reserve" and stored_mwh is not None:
        out["stored_mwh"] = stored_mwh
    return out
