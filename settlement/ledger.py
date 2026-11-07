"""The day's books: one settled record per cycle, running totals, metrics and the log.

    ledger = DayLedger(goal_config, battery=cfg, start_soc_mwh=4.0, log_path="logs/settlement.jsonl")
    cycle = run_cycle(features, goal_config, ...)               # pipeline
    record = ledger.record(cycle, features, actual)             # actual = site/simulator measurements
    record["dashboard_line"], ledger.metrics(), ledger.goal_summary()

``actual`` holds what really happened in the interval:
    {"demand_mw": 4.7, "production_mw": 4.9, "stored_mwh": 3.5 (sensor, optional), "price_now": 95 (optional)}
Missing actual values fall back to the forecast in ``features`` (and say so in
``warnings``). The SoC is taken from ``actual``, then ``features["stored_mwh"]``
(the sensor value the guard used), then the ledger's own SoC after the last interval.

Goal cost (plan section 2): profit given up versus a profit-only plan. That
needs the same day run without the goals, so ``goal_costs`` takes a function
that runs a whole day for a given goal config and returns its ledger.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Mapping, Optional

from battery import BatteryConfig
from common import finite_float, goal_config_dict

from .goals import GoalTracker
from .settle import IntervalSettlement, forecast_errors, settle_interval


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _action(mw: float) -> str:
    if mw > 1e-6:
        return f"charge {mw:.2f} MW"
    if mw < -1e-6:
        return f"discharge {-mw:.2f} MW"
    return "hold"


class DayLedger:
    def __init__(
        self,
        goal_config: Any = None,
        *,
        battery: Optional[BatteryConfig] = None,
        start_soc_mwh: Optional[float] = None,
        log_path: Optional[str] = None,
    ):
        self.cfg = battery or BatteryConfig()
        self.goals = GoalTracker(goal_config, self.cfg)
        self.soc_mwh = start_soc_mwh
        self.log_path = log_path
        self.records: list[dict] = []

    def set_goals(self, goal_config: Any) -> None:
        self.goals.set_goals(goal_config)

    # ------------------------------------------------------------------ record
    def record(self, cycle: Any, features: Mapping[str, Any], actual: Optional[Mapping[str, Any]] = None) -> dict:
        """Settle one cycle (pipeline.CycleResult or its dict) with the actual site values."""
        actual = actual or {}
        decision = _get(cycle, "decision") or {}
        setpoint = _get(cycle, "setpoint") or {}
        warnings: list[str] = []

        def pick(key: str, *fallbacks: Any) -> Optional[float]:
            for src in (actual, *fallbacks):
                v = finite_float(src.get(key)) if isinstance(src, Mapping) else finite_float(src)
                if v is not None:
                    return v
            return None

        demand = pick("demand_mw")
        if demand is None:
            demand = pick("demand_mw", features) or 0.0
            warnings.append("actual demand missing: settled on the forecast")
        production = pick("production_mw")
        if production is None:
            production = pick("production_mw", features) or 0.0
            warnings.append("actual production missing: settled on the forecast")
        price = pick("price_now", features)
        if price is None:
            price = 0.0
            warnings.append("price missing: settled at EUR 0")
        soc = pick("stored_mwh", features, self.soc_mwh)  # sensor, then the ledger's own SoC
        if soc is None:
            soc = self.cfg.e_min_mwh
            warnings.append("state of charge unknown: assumed the minimum")

        s: IntervalSettlement = settle_interval(
            time=str(features.get("time") or actual.get("time") or "??:??"),
            price_eur_mwh=price,
            soc_before_mwh=soc,
            battery_setpoint_mw=finite_float(_get(setpoint, "battery_setpoint_mw")) or 0.0,
            curtail_mw=finite_float(_get(setpoint, "curtail_mw")) or 0.0,
            demand_mw=demand,
            production_mw=production,
            cfg=self.cfg,
        )
        s.warnings = warnings + s.warnings
        statuses = self.goals.update(s)
        self.soc_mwh = s.soc_after_mwh

        rec = s.to_dict()
        rec.update({f"goal_{gid}": status for gid, status in statuses.items()})
        rec["goals"] = {st.id: st.message for st in self.goals.states.values()}
        rec["profit_today_eur"] = round(sum(r["profit_eur"] for r in self.records) + s.profit_eur, 2)
        rec["forecast"] = forecast_errors(dict(features), demand, production)
        rec["decision"] = {
            "source": _get(decision, "source"),
            "fallback_reason": _get(decision, "fallback_reason"),
            "battery_fraction": _get(decision, "battery_fraction"),
            "curtail": _get(decision, "curtail"),
            "confidence": _get(decision, "confidence"),
            "reasoning": _get(decision, "reasoning"),
            "explanation_for_operator": _get(decision, "explanation_for_operator"),
        }
        rec["guard"] = {
            "battery_setpoint_mw": _get(setpoint, "battery_setpoint_mw"),
            "curtail_mw": _get(setpoint, "curtail_mw"),
            "clipped": bool(_get(setpoint, "clipped", False)),
            "applied_rules": list(_get(setpoint, "applied_rules", []) or []),
        }
        rec["dashboard_line"] = self._line(s, rec)
        self.records.append(rec)
        self._log(rec)
        return rec

    def _line(self, s: IntervalSettlement, rec: dict) -> str:
        if s.grid_export_mwh > 0:
            grid = f"sell {s.grid_export_mwh:.4f} MWh"
        elif s.grid_import_mwh > 0:
            grid = f"buy {s.grid_import_mwh:.4f} MWh"
        else:
            grid = "no grid trade"
        parts = [
            s.time,
            _action(s.battery_mw),
            f"{grid} @ EUR {s.price_eur_mwh:g}",
            f"profit EUR {s.profit_eur:+.2f} (day EUR {rec['profit_today_eur']:+.2f})",
            f"SoC {s.soc_after_mwh:.2f} MWh",
        ]
        if s.curtail_mw > 0:
            parts.insert(2, f"curtail {s.curtail_mw:.2f} MW")
        parts += [f"{gid}: {msg}" for gid, msg in rec["goals"].items()]
        return " | ".join(parts)

    def _log(self, rec: dict) -> None:
        if not self.log_path:
            return
        try:
            os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass  # logging must never stop the site

    # ----------------------------------------------------------------- totals
    def finalize(self) -> dict[str, str]:
        return self.goals.finalize()

    @property
    def profit_eur(self) -> float:
        return round(sum(r["profit_eur"] for r in self.records), 2)

    def goal_summary(self) -> list[dict]:
        return self.goals.summary()

    def metrics(self) -> dict:
        """Day totals. Keys match the dashboard's /api/metrics plus the plan's evaluation metrics."""
        recs = self.records
        dt = self.cfg.interval_h
        n = len(recs)

        def total(key: str, nd: int = 4) -> float:
            return round(sum(r[key] for r in recs), nd)

        dem_err = [abs(r["forecast"]["demand_error_mw"]) for r in recs if r["forecast"]["demand_error_mw"] is not None]
        prod_err = [abs(r["forecast"]["production_error_mw"]) for r in recs
                    if r["forecast"]["production_error_mw"] is not None]
        in_range = [r["forecast"]["production_in_range"] for r in recs
                    if r["forecast"]["production_in_range"] is not None]
        states = list(self.goals.states.values())
        hard = [st for st in states if st.hard]
        llm = sum(1 for r in recs if r["decision"]["source"] == "llm")
        fallback = sum(1 for r in recs if r["decision"]["source"] == "fallback")
        clipped = sum(1 for r in recs if r["guard"]["clipped"])
        return {
            "profit_today_eur": self.profit_eur,
            "energy_cost_eur": total("energy_cost_eur", 2),
            "wear_eur": total("wear_eur", 2),
            "no_battery_profit_eur": total("no_battery_profit_eur", 2),
            "value_added_eur": total("value_added_eur", 2),
            "energy_sold_mwh": total("grid_export_mwh"),
            "energy_bought_mwh": total("grid_import_mwh"),
            "curtailed_mwh": round(sum(r["curtail_mw"] for r in recs) * dt, 4),
            "battery_throughput_mwh": round(sum(abs(r["battery_mw"]) for r in recs) * dt, 4),
            "soc_mwh": self.soc_mwh,
            "decisions_total": n,
            "decisions_llm": llm,
            "decisions_fallback": fallback,
            "decisions_clipped": clipped,
            "fallback_rate": round(fallback / n, 4) if n else 0.0,
            "clip_rate": round(clipped / n, 4) if n else 0.0,
            "goals_met": sum(1 for st in states if st.status == "met"),
            "goals_total": len(states),
            "hard_goals_met": sum(1 for st in hard if st.status == "met"),
            "hard_goals_total": len(hard),
            "hard_goals_violated": sum(1 for st in hard if st.status in ("missed", "violated")),
            "demand_mae_mw": round(sum(dem_err) / len(dem_err), 4) if dem_err else None,
            "production_mae_mw": round(sum(prod_err) / len(prod_err), 4) if prod_err else None,
            "production_range_coverage": round(sum(in_range) / len(in_range), 4) if in_range else None,
        }


def goal_costs(run_day: Callable[[dict], DayLedger], goal_config: Any) -> dict:
    """Profit given up for the user's goals, versus a profit-only plan.

    ``run_day(goal_config_dict) -> DayLedger`` must run the same day (same
    prices, forecasts, actuals and start SoC) for the given goals.

    total_cost_eur   = profit(profit-only) - profit(with goals)
    per goal cost    = profit(all goals except this one) - profit(with goals)
    (per-goal costs need not add up to the total: goals can overlap.)
    """
    cfg = dict(goal_config_dict(goal_config))
    goals = list(cfg.get("goals") or [])
    with_goals = run_day({**cfg, "goals": goals})
    profit_only = run_day({"objective": "max_profit", "goals": []})
    per_goal = {}
    for g in goals:
        others = [o for o in goals if o is not g]
        without = run_day({**cfg, "goals": others})
        per_goal[str(g.get("id"))] = {
            "source_text": g.get("source_text"),
            "cost_eur": round(without.profit_eur - with_goals.profit_eur, 2),
        }
    return {
        "profit_with_goals_eur": with_goals.profit_eur,
        "profit_only_eur": profit_only.profit_eur,
        "total_cost_eur": round(profit_only.profit_eur - with_goals.profit_eur, 2),
        "per_goal": per_goal,
    }
