"""Section 7: the five solved examples.

Each situation has the same shape as the feature builder's output, and each is
rendered by the same code as the current situation, so the format always matches.

All example numbers follow the plan's formulas, including its section 6 fixes
(battery 10 MWh, 3 MW, round trip 0.90 so eta = 0.949, minimum 1 MWh, wear EUR 5):
    break_even      = 0.9 x highest later price - 5
    max_charge_mw   = min(3, free / (eta x 0.25 h))
    max_discharge_mw= min(3, (stored - min) x eta / 0.25 h)
    matching        = net / 3
tests/test_prompt_assembly.py recomputes every one of them. When the feature
builder exists, the plan asks to regenerate these situations with its code.
"""
from __future__ import annotations

from typing import Any

_BATTERY = {"min_soc_mwh": 1.0, "capacity_mwh": 10.0}

SOLVED_EXAMPLES: list[dict[str, Any]] = [
    {
        "name": "cheap surplus, price far below break-even",
        "features": {
            "time": "10:00", "demand_mw": 2.0, "production_mw": 4.6, "net_mw": 2.6, "net_label": "SURPLUS",
            "prod_low_mw": 4.3, "prod_high_mw": 4.9, "uncertainty": "LOW",
            "price_now": 60, "price_rank": 15, "price_avg": 85,
            "future_max_price": 160, "future_max_time": "19:30",
            "break_even": 139, "price_vs_breakeven": "EUR 79 BELOW",
            "stored_mwh": 4.0, **_BATTERY,
            "max_charge_mw": 3.0, "max_charge_fraction": 1.0,
            "max_discharge_mw": 3.0, "max_discharge_fraction": 1.0,
            "matching_fraction": 0.87,
        },
        "goal_config": None,
        "decision": {
            "reasoning": "The price is EUR 79 below break-even and among the cheapest of the day (rank 15), "
                         "so energy is worth much more at the 19:30 peak. I store the surplus (0.87) and "
                         "buy a little more to charge at full power.",
            "battery_fraction": 1.0, "curtail": False, "confidence": "high",
            "explanation_for_operator": "Charging at full power while energy is cheap; it is worth more at the 19:30 peak.",
        },
    },
    {
        "name": "evening deficit, price just above break-even",
        "features": {
            "time": "18:30", "demand_mw": 5.0, "production_mw": 3.2, "net_mw": -1.8, "net_label": "DEFICIT",
            "prod_low_mw": 2.9, "prod_high_mw": 3.5, "uncertainty": "LOW",
            "price_now": 115, "price_rank": 85, "price_avg": 85,
            "future_max_price": 120, "future_max_time": "19:00",
            "break_even": 103, "price_vs_breakeven": "EUR 12 ABOVE",
            "stored_mwh": 8.5, **_BATTERY,
            "max_charge_mw": 3.0, "max_charge_fraction": 1.0,
            "max_discharge_mw": 3.0, "max_discharge_fraction": 1.0,
            "matching_fraction": -0.60,
        },
        "goal_config": None,
        "decision": {
            "reasoning": "The price is EUR 12 above break-even, so using stored energy now beats keeping it, "
                         "but by less than EUR 20. I discharge to cover the deficit (matching fraction -0.60) "
                         "without exporting.",
            "battery_fraction": -0.60, "curtail": False, "confidence": "medium",
            "explanation_for_operator": "Discharging to cover the site's own demand during the evening price peak.",
        },
    },
    {
        "name": "negative price, battery almost full",
        "features": {
            "time": "13:00", "demand_mw": 1.2, "production_mw": 7.5, "net_mw": 6.3, "net_label": "SURPLUS",
            "prod_low_mw": 6.8, "prod_high_mw": 8.1, "uncertainty": "MEDIUM",
            "price_now": -8, "price_rank": 2, "price_avg": 85,
            "future_max_price": 140, "future_max_time": "20:00",
            "break_even": 121, "price_vs_breakeven": "EUR 129 BELOW",
            "stored_mwh": 9.5, **_BATTERY,
            "max_charge_mw": 2.11, "max_charge_fraction": 0.70,
            "max_discharge_mw": 3.0, "max_discharge_fraction": 1.0,
            "matching_fraction": 2.10,
        },
        "goal_config": None,
        "decision": {
            "reasoning": "The price is negative and EUR 129 below break-even, so I charge as much as this "
                         "interval allows (0.70; the battery is almost full). The surplus (2.10) is far larger "
                         "and exporting would cost money, so I curtail the rest.",
            "battery_fraction": 0.70, "curtail": True, "confidence": "high",
            "explanation_for_operator": "Battery charging at its limit and extra production curtailed because the price is negative.",
        },
    },
    {
        "name": "night deficit, price below break-even, uncertain wind",
        "features": {
            "time": "02:00", "demand_mw": 1.5, "production_mw": 1.0, "net_mw": -0.5, "net_label": "DEFICIT",
            "prod_low_mw": 0.6, "prod_high_mw": 1.4, "uncertainty": "HIGH",
            "price_now": 105, "price_rank": 40, "price_avg": 85,
            "future_max_price": 150, "future_max_time": "08:00",
            "break_even": 130, "price_vs_breakeven": "EUR 25 BELOW",
            "stored_mwh": 2.0, **_BATTERY,
            "max_charge_mw": 3.0, "max_charge_fraction": 1.0,
            "max_discharge_mw": 3.0, "max_discharge_fraction": 1.0,
            "matching_fraction": -0.17,
        },
        "goal_config": None,
        "decision": {
            "reasoning": "The price is EUR 25 below break-even, so stored energy is worth more at the 08:00 "
                         "peak and I do not discharge. Rank 40 is not cheap enough to buy extra, and uncertainty "
                         "is HIGH, so I stay idle and let the grid cover the small deficit.",
            "battery_fraction": 0.0, "curtail": False, "confidence": "medium",
            "explanation_for_operator": "Holding the battery for the 08:00 peak; the grid covers the small shortfall.",
        },
    },
    {
        "name": "reserve goal needs more than the surplus (plan section 5)",
        "features": {
            "time": "15:00", "demand_mw": 4.6, "production_mw": 5.0, "net_mw": 0.4, "net_label": "SURPLUS",
            "prod_low_mw": 4.4, "prod_high_mw": 5.6, "uncertainty": "LOW",
            "price_now": 95, "price_rank": 70, "price_avg": 80,
            "future_max_price": 170, "future_max_time": "20:30",
            "break_even": 148, "price_vs_breakeven": "EUR 53 BELOW",
            "stored_mwh": 3.5, **_BATTERY,
            "max_charge_mw": 3.0, "max_charge_fraction": 1.0,
            "max_discharge_mw": 3.0, "max_discharge_fraction": 1.0,
            "matching_fraction": 0.13,
            "goals": [{"id": "g1", "status": "on_track", "need_mwh": 2.5, "hours_left": 3.0,
                       "even_pace_fraction": 0.29, "force_from": "16:30"}],
        },
        "goal_config": {
            "objective": "max_profit",
            "goals": [{"id": "g1", "type": "reserve", "mwh": 6.0, "by": "18:00", "until": "18:00", "hard": True}],
        },
        "decision": {
            "reasoning": "The price is EUR 53 below break-even, so stored energy is worth more tonight. The "
                         "6 MWh goal needs 0.29 at an even pace; the surplus alone (0.13) is not enough, so I "
                         "charge at the goal pace.",
            "battery_fraction": 0.29, "curtail": False, "confidence": "medium",
            "explanation_for_operator": "Charging at the goal pace to reach 6 MWh by 18:00.",
        },
    },
]
