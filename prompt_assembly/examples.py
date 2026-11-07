"""Section 7: the six solved examples.

Each situation has the same shape as the feature builder's output, and each is
rendered by the same code as the current situation, so the format always matches.
Each decision's reasoning walks through the steps of prompt section 4, because a
small model copies the examples more reliably than it follows rules.

All example numbers follow the plan's formulas, including its section 6 fixes
(battery 10 MWh, 3 MW, round trip 0.90 so eta = 0.949, minimum 1 MWh, wear EUR 5):
    break_even       = 0.9 x highest later price - 5
    max_charge_mw    = min(3, free / (eta x 0.25 h))
    max_discharge_mw = min(3, (stored - min) x eta / 0.25 h)
    matching         = net / 3
tests/test_prompt_assembly.py recomputes every one of them. When the feature
builder exists, the plan asks to regenerate these situations with its code.

Together the examples cover steps 1 and 3-6, the zones FAR ABOVE, ABOVE and
BELOW, negative prices with curtailment, and a curtail check in every example.
The plan has five examples; the ABOVE one was added because qwen3:4b mixed up
ABOVE with its neighbouring zones until it saw one.
"""
from __future__ import annotations

from typing import Any

_BATTERY = {"min_soc_mwh": 1.0, "capacity_mwh": 10.0}

SOLVED_EXAMPLES: list[dict[str, Any]] = [
    {
        "name": "cheap surplus, price far below break-even (steps 1-2)",
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
            "reasoning": "Step 1: zone BELOW with a SURPLUS gives the matching fraction 0.87. Step 2: objective profit, "
                         "no change. Step 3: price cheap YES (rank 15) and zone BELOW, so the charge limit 1.00. Step 4: "
                         "uncertainty LOW. Step 5: no goals. Step 6: within limits: 1.00. Curtail: surplus left NO, so "
                         "false.",
            "battery_fraction": 1.0, "curtail": False, "confidence": "high",
            "explanation_for_operator": "Charging at full power while energy is cheap; it is worth more at the 19:30 peak.",
        },
    },
    {
        "name": "evening peak, price far above break-even, small surplus (step 1, sign)",
        "features": {
            "time": "18:30", "demand_mw": 3.0, "production_mw": 3.3, "net_mw": 0.3, "net_label": "SURPLUS",
            "prod_low_mw": 3.0, "prod_high_mw": 3.6, "uncertainty": "LOW",
            "price_now": 150, "price_rank": 95, "price_avg": 85,
            "future_max_price": 140, "future_max_time": "19:00",
            "break_even": 121, "price_vs_breakeven": "EUR 29 ABOVE",
            "stored_mwh": 8.5, **_BATTERY,
            "max_charge_mw": 3.0, "max_charge_fraction": 1.0,
            "max_discharge_mw": 3.0, "max_discharge_fraction": 1.0,
            "matching_fraction": 0.10,
        },
        "goal_config": None,
        "decision": {
            "reasoning": "Step 1: zone FAR ABOVE gives the NEGATIVE discharge limit -1.00, even with a SURPLUS: sell "
                         "now. Step 2: objective profit. Step 3: price cheap NO. Step 4: uncertainty LOW. Step 5: no "
                         "goals. Step 6: within limits: -1.00. Curtail: surplus left NO, so false.",
            "battery_fraction": -1.0, "curtail": False, "confidence": "high",
            "explanation_for_operator": "Selling stored energy at full power during the evening price peak.",
        },
    },
    {
        "name": "evening deficit, price just above break-even (zone ABOVE; the plan's example 2 numbers)",
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
            "reasoning": "Step 1: zone ABOVE (never charge) with a DEFICIT gives the matching fraction -0.60, "
                         "covering the deficit. Step 2: objective profit. Step 3: price cheap NO. Step 4: "
                         "uncertainty LOW. Step 5: no goals. Step 6: within limits: -0.60. Curtail: surplus left "
                         "NO, so false.",
            "battery_fraction": -0.60, "curtail": False, "confidence": "medium",
            "explanation_for_operator": "Discharging to cover the site's own demand while the price is above break-even.",
        },
    },
    {
        "name": "negative price, battery almost full (steps 2, 6 and curtailment)",
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
            "reasoning": "Step 1: zone BELOW with a SURPLUS gives the matching fraction 2.10. Step 2: objective profit. "
                         "Step 3: price cheap YES (negative), so the charge limit 0.70. Step 4: uncertainty MEDIUM, no "
                         "change. Step 5: no goals. Step 6: 0.70 is the limit: 0.70. Curtail: surplus left YES and price "
                         "negative YES, so true.",
            "battery_fraction": 0.70, "curtail": True, "confidence": "medium",
            "explanation_for_operator": "Battery charging at its limit and extra production curtailed because the price is negative.",
        },
    },
    {
        "name": "cheap night energy, uncertain wind (steps 1, 2 and 5)",
        "features": {
            "time": "02:00", "demand_mw": 1.5, "production_mw": 1.0, "net_mw": -0.5, "net_label": "DEFICIT",
            "prod_low_mw": 0.3, "prod_high_mw": 1.9, "uncertainty": "HIGH",
            "price_now": 70, "price_rank": 25, "price_avg": 85,
            "future_max_price": 150, "future_max_time": "08:00",
            "break_even": 130, "price_vs_breakeven": "EUR 60 BELOW",
            "stored_mwh": 2.0, **_BATTERY,
            "max_charge_mw": 3.0, "max_charge_fraction": 1.0,
            "max_discharge_mw": 3.0, "max_discharge_fraction": 1.0,
            "matching_fraction": -0.17,
        },
        "goal_config": None,
        "decision": {
            "reasoning": "Step 1: zone BELOW with a DEFICIT gives 0.00 (keep stored energy). Step 2: objective profit. "
                         "Step 3: price cheap YES (rank 25) and zone BELOW, so the charge limit 1.00. Step 4: "
                         "uncertainty HIGH, so 1.00 becomes 0.50. Step 5: no goals. Step 6: within limits: 0.50. "
                         "Curtail: surplus left NO, so false.",
            "battery_fraction": 0.50, "curtail": False, "confidence": "low",
            "explanation_for_operator": "Charging at half power while night energy is cheap; the wind forecast is uncertain.",
        },
    },
    {
        "name": "reserve goal needs more than the surplus (step 4, plan section 5)",
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
            "reasoning": "Step 1: zone BELOW with a SURPLUS gives the matching fraction 0.13. Step 2: objective profit. "
                         "Step 3: price cheap NO (rank 70). Step 4: uncertainty LOW. Step 5: the 6 MWh goal needs "
                         "battery_fraction >= 0.29, so 0.29. Step 6: within limits: 0.29. Curtail: surplus left NO, so "
                         "false.",
            "battery_fraction": 0.29, "curtail": False, "confidence": "medium",
            "explanation_for_operator": "Charging at the goal pace to reach 6 MWh by 18:00.",
        },
    },
]
