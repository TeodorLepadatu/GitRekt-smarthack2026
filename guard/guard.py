"""Guard layer: the last check before the battery (plan section 2).

guard(decision, state, goal_config) -> GuardResult

    1. fraction x P_max -> requested MW
    2. physical limits: SoC limits and max power (plan section 6 formulas)
    3. hard goals: reserve (forced charge / blocked discharge), min sell price
    4. physical limits always win over goals (you can't charge a full battery)
    5. curtailment: only when price < 0 and surplus remains,
       or when a min-sell goal blocks exporting a surplus

Input `decision` is the validator output (validator.Decision or its dict):
    {"battery_fraction": float in [-1, 1], "curtail": bool, "source": "llm"|"fallback"}
Input `state` (feature builder / sensors for this interval):
    {"time": "15:00", "stored_mwh": 3.5, "net_mw": 0.4, "price_now": 95,
     "production_mw": 5.0 (optional, caps curtailment)}

Output (plan section 5 "Guard -> site"):
    {"battery_setpoint_mw", "curtail_mw", "clipped", "applied_rules", "source", ...}

Sign convention: setpoint > 0 = CHARGE, < 0 = DISCHARGE. Never raises.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional

from .config import BatteryConfig
from .goals import Bounds, apply_min_sell, apply_reserve, hard_goals, to_minutes

EPS = 1e-9


@dataclass
class GuardResult:
    battery_setpoint_mw: float
    curtail_mw: float
    clipped: bool
    applied_rules: list[str]
    source: str
    requested_setpoint_mw: float = 0.0
    expected_soc_after_mwh: Optional[float] = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def guard(
    decision: Any,
    state: Mapping[str, Any],
    goal_config: Optional[Mapping[str, Any]] = None,
    cfg: BatteryConfig = BatteryConfig(),
) -> GuardResult:
    """Turn a validated decision into safe setpoints. Never raises."""
    try:
        return _guard(decision, state or {}, goal_config, cfg)
    except Exception as exc:  # last line of defence: hold the battery
        return GuardResult(
            battery_setpoint_mw=0.0,
            curtail_mw=0.0,
            clipped=True,
            applied_rules=["guard_error_hold"],
            source=str(_get(decision, "source", "unknown")),
            warnings=[repr(exc)[:200]],
        )


def _guard(decision: Any, state: Mapping[str, Any], goal_config, cfg: BatteryConfig) -> GuardResult:
    rules: list[str] = []
    warnings: list[str] = []
    source = str(_get(decision, "source", "unknown"))

    stored = _num(state.get("stored_mwh"))
    if stored is None:
        # Without the state of charge nothing can be checked safely.
        return GuardResult(0.0, 0.0, True, ["no_soc_hold"], source,
                           warnings=["stored_mwh missing or invalid"])
    net = _num(state.get("net_mw"))
    price = _num(state.get("price_now"))
    production = _num(state.get("production_mw"))
    now = to_minutes(state.get("time"))

    # 1. fraction -> MW (the validator already checked the range; re-check anyway)
    fraction = _num(_get(decision, "battery_fraction"))
    if fraction is None:
        fraction = 0.0
        rules.append("invalid_fraction_hold")
    elif not -1.0 <= fraction <= 1.0:
        fraction = max(-1.0, min(1.0, fraction))
        rules.append("fraction_clamped")
    requested = fraction * cfg.p_max_mw

    # 2. physical limits
    max_c = cfg.max_charge_mw(stored)
    max_d = cfg.max_discharge_mw(stored)
    bounds = Bounds(lo=-max_d, hi=max_c, lo_rule="soc_min_limit", hi_rule="soc_max_limit")

    # 3. hard goals
    for goal in hard_goals(goal_config):
        gtype = goal.get("type")
        if gtype == "reserve":
            if now is None:
                warnings.append(f"time missing: reserve goal {goal.get('id')} not checked")
                continue
            proposed = min(bounds.hi, max(bounds.lo, requested))
            apply_reserve(goal, bounds, now=now, stored_mwh=stored, proposed_mw=proposed, cfg=cfg)
        elif gtype == "min_sell_price":
            apply_min_sell(goal, bounds, price_now=price, net_mw=net)
        else:
            bounds.notes.append(f"goal_{goal.get('id', '?')}_unknown_type")

    # 4. physics beats goals
    if bounds.lo > bounds.hi:
        warnings.append(f"{bounds.lo_rule} limited by {bounds.hi_rule}")
        bounds.lo = bounds.hi

    setpoint = requested
    if setpoint > bounds.hi + EPS:
        setpoint = bounds.hi
        rules.append(bounds.hi_rule)
    elif setpoint < bounds.lo - EPS:
        setpoint = bounds.lo
        rules.append(bounds.lo_rule)
    elif not any(r.startswith("soc_") for r in rules):
        rules.append("soc_limits_ok")

    # 5. curtailment
    curtail_mw = 0.0
    wants_curtail = bool(_get(decision, "curtail", False))
    surplus_left = max(0.0, (net or 0.0) - max(setpoint, 0.0))
    if bounds.force_curtail_export and surplus_left > EPS:
        curtail_mw = surplus_left
        rules.append("curtail_forced_min_sell")
    elif wants_curtail:
        if price is None or price >= 0:
            rules.append("curtail_blocked_price_not_negative")
        elif surplus_left <= EPS:
            rules.append("curtail_blocked_no_surplus")
        else:
            curtail_mw = surplus_left
            rules.append("curtail_applied")
            if setpoint < 0:  # don't discharge into the grid while throwing energy away
                setpoint = 0.0
                rules.append("no_discharge_while_curtailing")
    if production is not None:
        curtail_mw = min(curtail_mw, max(0.0, production))

    rules.extend(bounds.notes)
    setpoint = round(setpoint, 4)
    return GuardResult(
        battery_setpoint_mw=setpoint,
        curtail_mw=round(curtail_mw, 4),
        clipped=abs(setpoint - requested) > 1e-3,
        applied_rules=rules,
        source=source,
        requested_setpoint_mw=round(requested, 4),
        expected_soc_after_mwh=round(cfg.energy_after(stored, setpoint), 4),
        warnings=warnings,
    )
