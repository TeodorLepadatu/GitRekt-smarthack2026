"""Rule-based fallback policy.

Used when the LLM output is invalid or the LLM timed out. It mirrors the
break-even logic the prompt teaches the LLM, but in plain code. It must be
safe, predictable and never raise. The guard layer still checks its output.

Features read (names from the feature builder contract, plan section 5):
    price_now, break_even, net_mw, matching_fraction, max_charge_mw,
    goals: [{"status": "on_track"|"behind"|"met", "even_pace_fraction": float}]

Rules, in order:
    1. price_now < 0          -> charge at full power; curtail if the surplus
                                 is still larger than what the battery can take
    2. price_now >= break_even -> discharge at full power (selling now beats
                                  holding; the guard enforces min-sell / reserve)
    3. otherwise               -> follow matching_fraction (store surplus,
                                  cover deficit)
    4. any open hard goal     -> battery_fraction >= even_pace_fraction
    Missing/broken features   -> hold (0.0), the safest action.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional

from .schema import FRACTION_MAX, FRACTION_MIN, SOURCE_FALLBACK, Decision


def _num(features: Mapping[str, Any], key: str) -> Optional[float]:
    value = features.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _clamp(x: float) -> float:
    return min(FRACTION_MAX, max(FRACTION_MIN, x))


def _goal_floor(features: Mapping[str, Any]) -> Optional[float]:
    """Highest even-pace fraction over all open hard goals, if any."""
    floor = None
    for goal in features.get("goals") or []:
        if not isinstance(goal, Mapping):
            continue
        if goal.get("status") == "met" or goal.get("hard") is False:
            continue
        pace = _num(goal, "even_pace_fraction")
        if pace is not None:
            floor = pace if floor is None else max(floor, pace)
    return floor


def fallback_decision(
    features: Optional[Mapping[str, Any]],
    reason: str,
    detail: str = "",
) -> Decision:
    """Return a safe rule-based Decision. Never raises."""
    try:
        return _decide(features or {}, reason, detail)
    except Exception as exc:  # last line of defence: never crash the cycle
        return Decision(
            battery_fraction=0.0,
            curtail=False,
            source=SOURCE_FALLBACK,
            reasoning=f"Fallback crashed ({exc!r}); holding.",
            explanation_for_operator="Holding the battery (automatic safe mode).",
            fallback_reason=reason,
            fallback_detail=detail,
        )


def _decide(features: Mapping[str, Any], reason: str, detail: str) -> Decision:
    price = _num(features, "price_now")
    break_even = _num(features, "break_even")
    net = _num(features, "net_mw")
    matching = _num(features, "matching_fraction")
    max_charge = _num(features, "max_charge_mw")

    fraction = 0.0
    curtail = False

    if price is not None and price < 0:
        fraction = 1.0
        rule = f"negative price ({price:g} EUR/MWh): charge at full power"
        # Surplus left after charging at full power -> curtail it.
        if net is not None and net > 0 and (max_charge is None or net > max_charge):
            curtail = True
            rule += " and curtail the remaining surplus"
    elif price is not None and break_even is not None and price >= break_even:
        fraction = -1.0
        rule = f"price {price:g} >= break-even {break_even:g}: discharge"
    elif matching is not None:
        fraction = _clamp(matching)
        rule = f"follow matching fraction {matching:g}"
    else:
        rule = "features missing: hold"

    floor = _goal_floor(features)
    if floor is not None and fraction < floor:
        fraction = _clamp(floor)
        rule += f"; raised to {fraction:g} for an open hard goal"

    fraction = round(_clamp(fraction), 4)

    if fraction > 0:
        action = f"Charging at {fraction:.0%} of max power"
    elif fraction < 0:
        action = f"Discharging at {-fraction:.0%} of max power"
    else:
        action = "Holding the battery"
    if curtail:
        action += ", curtailing surplus"

    return Decision(
        battery_fraction=fraction,
        curtail=curtail,
        source=SOURCE_FALLBACK,
        confidence="low",
        reasoning=f"Rule-based fallback ({reason}): {rule}.",
        explanation_for_operator=f"{action} (automatic rule, AI decision unavailable).",
        fallback_reason=reason,
        fallback_detail=detail or None,
    )
