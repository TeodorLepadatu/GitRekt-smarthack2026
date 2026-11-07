"""Checks that the real decision model makes sensible choices from the assembled prompt.

Skipped unless Ollama is running and the model is pulled:
    ollama pull qwen3:4b
    python -m pytest tests/test_prompt_assembly_llm.py -v
Set PROMPT_ASSEMBLY_TEST_MODEL to try another model (e.g. qwen3.5:4b).

None of these 19 situations is one of the solved examples. Each takes 5-15 s.
"""
import json
import os
import unittest
import urllib.request

from prompt_assembly import render_prompt

HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("PROMPT_ASSEMBLY_TEST_MODEL", "qwen3:4b")

# Same contract and default system message as llm/decision.py on feature/llm_agent.
SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "battery_fraction": {"type": "number", "minimum": -1, "maximum": 1},
        "curtail": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "explanation_for_operator": {"type": "string"},
    },
    "required": ["reasoning", "battery_fraction", "curtail", "confidence", "explanation_for_operator"],
}
SYSTEM = (
    "You are the battery dispatch agent for an energy site. You choose ONE action for the next 15-minute "
    "interval. All numbers in the prompt are already computed: compare them, do not recalculate them. "
    "Hard user goals come before profit. Respond ONLY with a JSON object with the keys "
    "reasoning, battery_fraction, curtail, confidence, explanation_for_operator."
)

_BASE = dict(min_soc_mwh=1.0, capacity_mwh=10.0, max_charge_mw=3.0, max_charge_fraction=1.0,
             max_discharge_mw=3.0, max_discharge_fraction=1.0, uncertainty="LOW")


def F(**kw):
    return {**_BASE, **kw}


# (name, features, goal_config, check on the decision)
CASES = [
    ("price far above break-even with a surplus: sell",
     F(time="19:00", demand_mw=3.0, production_mw=3.5, net_mw=0.5, net_label="SURPLUS", price_now=180,
       price_rank=95, price_avg=90, future_max_price=150, future_max_time="20:00", break_even=130,
       price_vs_breakeven="EUR 50 ABOVE", stored_mwh=7.0, matching_fraction=0.17),
     None, lambda d: d["battery_fraction"] < 0 and not d["curtail"]),
    ("negative price, small surplus: charge fully, nothing to curtail",
     F(time="12:00", demand_mw=2.0, production_mw=3.0, net_mw=1.0, net_label="SURPLUS", price_now=-5,
       price_rank=1, price_avg=70, future_max_price=140, future_max_time="19:00", break_even=121,
       price_vs_breakeven="EUR 126 BELOW", stored_mwh=2.0, matching_fraction=0.33),
     None, lambda d: d["battery_fraction"] >= 0.9 and not d["curtail"]),
    ("reserve goal behind: charge at least the goal pace",
     F(time="16:00", demand_mw=4.0, production_mw=3.0, net_mw=-1.0, net_label="DEFICIT", price_now=130,
       price_rank=80, price_avg=90, future_max_price=130, future_max_time="19:00", break_even=112,
       price_vs_breakeven="EUR 18 ABOVE", stored_mwh=3.0, matching_fraction=-0.33,
       goals=[{"id": "g1", "status": "behind", "need_mwh": 3.0, "hours_left": 1.5,
               "even_pace_fraction": 0.70, "force_from": "16:30"}]),
     {"objective": "max_profit", "goals": [{"id": "g1", "type": "reserve", "mwh": 6, "by": "17:30",
                                            "until": "17:30", "hard": True}]},
     lambda d: d["battery_fraction"] >= 0.69),
    ("near break-even: follow the matching fraction",
     F(time="11:00", demand_mw=2.0, production_mw=3.2, net_mw=1.2, net_label="SURPLUS", price_now=100,
       price_rank=50, price_avg=95, future_max_price=115, future_max_time="19:00", break_even=98.5,
       price_vs_breakeven="EUR 1.5 ABOVE", stored_mwh=5.0, matching_fraction=0.40),
     None, lambda d: abs(d["battery_fraction"] - 0.40) <= 0.05),
    ("own energy first: cover the deficit",
     F(time="20:00", demand_mw=3.0, production_mw=1.5, net_mw=-1.5, net_label="DEFICIT", price_now=90,
       price_rank=60, price_avg=85, future_max_price=120, future_max_time="21:00", break_even=103,
       price_vs_breakeven="EUR 13 BELOW", stored_mwh=6.0, matching_fraction=-0.50),
     {"objective": "self_consumption", "goals": []}, lambda d: abs(d["battery_fraction"] + 0.50) <= 0.05),
    ("minimum sell price blocked, battery nearly full: charge the limit and curtail",
     F(time="14:00", demand_mw=1.0, production_mw=4.0, net_mw=3.0, net_label="SURPLUS", price_now=30,
       price_rank=20, price_avg=70, future_max_price=120, future_max_time="19:00", break_even=103,
       price_vs_breakeven="EUR 73 BELOW", stored_mwh=9.8, max_charge_mw=0.84, max_charge_fraction=0.28,
       matching_fraction=1.0),
     {"objective": "max_profit", "goals": [{"id": "g2", "type": "min_sell_price", "eur_per_mwh": 50, "hard": True}]},
     lambda d: abs(d["battery_fraction"] - 0.28) <= 0.03 and d["curtail"]),
    ("HIGH uncertainty: moderate action",
     F(time="03:00", demand_mw=1.0, production_mw=1.6, net_mw=0.6, net_label="SURPLUS", uncertainty="HIGH",
       prod_low_mw=0.4, prod_high_mw=2.8, price_now=40, price_rank=10, price_avg=85, future_max_price=150,
       future_max_time="08:00", break_even=130, price_vs_breakeven="EUR 90 BELOW", stored_mwh=3.0,
       matching_fraction=0.20),
     None, lambda d: 0.15 <= d["battery_fraction"] <= 0.5),
    ("price a little above break-even, deficit: cover it",
     F(time="17:00", demand_mw=3.2, production_mw=2.0, net_mw=-1.2, net_label="DEFICIT", price_now=110,
       price_rank=75, price_avg=90, future_max_price=117, future_max_time="19:00", break_even=100.3,
       price_vs_breakeven="EUR 9.7 ABOVE", stored_mwh=6.0, matching_fraction=-0.40),
     None, lambda d: abs(d["battery_fraction"] + 0.40) <= 0.05 and not d["curtail"]),
    ("price below break-even, deficit, not cheap: stay idle",
     F(time="21:00", demand_mw=2.0, production_mw=1.1, net_mw=-0.9, net_label="DEFICIT", price_now=90,
       price_rank=55, price_avg=85, future_max_price=139, future_max_time="07:30", break_even=120.1,
       price_vs_breakeven="EUR 30.1 BELOW", stored_mwh=5.0, matching_fraction=-0.30),
     None, lambda d: abs(d["battery_fraction"]) <= 0.02),
    ("price far above break-even with a deficit: sell",
     F(time="19:30", demand_mw=4.0, production_mw=2.5, net_mw=-1.5, net_label="DEFICIT", price_now=175,
       price_rank=98, price_avg=90, future_max_price=150, future_max_time="20:30", break_even=130,
       price_vs_breakeven="EUR 45 ABOVE", stored_mwh=8.0, matching_fraction=-0.50),
     None, lambda d: d["battery_fraction"] <= -0.95),
    ("price above break-even with a surplus: idle, export the surplus",
     F(time="17:30", demand_mw=2.0, production_mw=2.9, net_mw=0.9, net_label="SURPLUS", price_now=112,
       price_rank=80, price_avg=90, future_max_price=114, future_max_time="19:00", break_even=97.6,
       price_vs_breakeven="EUR 14.4 ABOVE", stored_mwh=6.0, matching_fraction=0.30),
     None, lambda d: abs(d["battery_fraction"]) <= 0.02),
    ("MEDIUM uncertainty, price below break-even, not cheap: store the surplus",
     F(time="11:30", demand_mw=2.0, production_mw=3.35, net_mw=1.35, net_label="SURPLUS", uncertainty="MEDIUM",
       prod_low_mw=2.8, prod_high_mw=3.9, price_now=85, price_rank=45, price_avg=88, future_max_price=150,
       future_max_time="19:30", break_even=130, price_vs_breakeven="EUR 45 BELOW", stored_mwh=4.0,
       matching_fraction=0.45),
     None, lambda d: 0.3 <= d["battery_fraction"] <= 0.46 and not d["curtail"]),
    ("cheap refill before the peak: sell now (plan section 6, fix 3)",
     F(time="08:00", demand_mw=2.0, production_mw=1.4, net_mw=-0.6, net_label="DEFICIT", price_now=120,
       price_rank=85, price_avg=80, future_max_price=165, future_max_time="19:00", future_min_price=-8,
       future_min_time="13:00", break_even=143.5, price_vs_breakeven="EUR 23.5 BELOW",
       break_even_before_refill=95, next_refill_time="13:00", stored_mwh=7.0, matching_fraction=-0.20),
     None, lambda d: d["battery_fraction"] <= -0.95),
    ("two goals: reserve pace beats the surplus, sale allowed",
     F(time="14:00", demand_mw=3.0, production_mw=3.6, net_mw=0.6, net_label="SURPLUS", price_now=88,
       price_rank=60, price_avg=85, future_max_price=160, future_max_time="20:00", break_even=139,
       price_vs_breakeven="EUR 51 BELOW", stored_mwh=4.0, matching_fraction=0.20,
       goals=[{"id": "g1", "status": "on_track", "need_mwh": 4.0, "hours_left": 3.5,
               "even_pace_fraction": 0.40, "force_from": "16:45"}]),
     {"objective": "max_profit", "goals": [
         {"id": "g1", "type": "reserve", "mwh": 8, "by": "17:30", "until": "17:30", "hard": True},
         {"id": "g2", "type": "min_sell_price", "eur_per_mwh": 50, "hard": True}]},
     lambda d: abs(d["battery_fraction"] - 0.40) <= 0.03 and not d["curtail"]),
    ("own energy first but price is cheap: step 2 wins, charge fully",
     F(time="04:00", demand_mw=1.2, production_mw=1.5, net_mw=0.3, net_label="SURPLUS", price_now=45,
       price_rank=8, price_avg=85, future_max_price=150, future_max_time="08:00", break_even=130,
       price_vs_breakeven="EUR 85 BELOW", stored_mwh=3.0, matching_fraction=0.10),
     {"objective": "self_consumption", "goals": []}, lambda d: d["battery_fraction"] >= 0.95),
    ("HIGH uncertainty, price far above break-even: sell moderately",
     F(time="20:00", demand_mw=2.5, production_mw=2.0, net_mw=-0.5, net_label="DEFICIT", uncertainty="HIGH",
       prod_low_mw=0.8, prod_high_mw=3.2, price_now=170, price_rank=97, price_avg=90, future_max_price=150,
       future_max_time="21:00", break_even=130, price_vs_breakeven="EUR 40 ABOVE", stored_mwh=8.0,
       matching_fraction=-0.17),
     None, lambda d: -0.5 <= d["battery_fraction"] <= -0.3),
    ("negative price, battery nearly full but surplus fits: charge the limit, no curtail",
     F(time="13:30", demand_mw=1.5, production_mw=2.5, net_mw=1.0, net_label="SURPLUS", price_now=-3,
       price_rank=3, price_avg=75, future_max_price=140, future_max_time="20:00", break_even=121,
       price_vs_breakeven="EUR 124 BELOW", stored_mwh=9.6, max_charge_mw=1.69, max_charge_fraction=0.56,
       matching_fraction=0.33),
     None, lambda d: abs(d["battery_fraction"] - 0.56) <= 0.03 and not d["curtail"]),
    ("goal behind with HIGH uncertainty: the goal beats the 0.50 limit",
     F(time="16:15", demand_mw=2.0, production_mw=2.3, net_mw=0.3, net_label="SURPLUS", uncertainty="HIGH",
       prod_low_mw=1.0, prod_high_mw=3.6, price_now=100, price_rank=65, price_avg=90, future_max_price=150,
       future_max_time="20:00", break_even=130, price_vs_breakeven="EUR 30 BELOW", stored_mwh=3.0,
       matching_fraction=0.10,
       goals=[{"id": "g1", "status": "behind", "need_mwh": 3.0, "hours_left": 1.25,
               "even_pace_fraction": 0.84, "force_from": "16:30"}]),
     {"objective": "max_profit", "goals": [{"id": "g1", "type": "reserve", "mwh": 6, "by": "17:30",
                                            "until": "17:30", "hard": True}]},
     lambda d: d["battery_fraction"] >= 0.83),
    ("near break-even with a deficit: cover it",
     F(time="09:00", demand_mw=3.0, production_mw=2.4, net_mw=-0.6, net_label="DEFICIT", price_now=101,
       price_rank=55, price_avg=95, future_max_price=115, future_max_time="19:00", break_even=98.5,
       price_vs_breakeven="EUR 2.5 ABOVE", stored_mwh=6.0, matching_fraction=-0.20),
     None, lambda d: abs(d["battery_fraction"] + 0.20) <= 0.03),
]


def _model_available() -> bool:
    try:
        with urllib.request.urlopen(f"{HOST}/api/tags", timeout=2) as resp:
            names = {m["name"] for m in json.loads(resp.read()).get("models", [])}
    except Exception:
        return False
    return MODEL in names or f"{MODEL}:latest" in names


def decide(prompt: str) -> dict:
    body = {
        "model": MODEL, "stream": False, "format": SCHEMA, "think": False,
        # num_predict stops a model that starts looping in its reasoning
        "options": {"temperature": 0, "seed": 0, "num_ctx": 8192, "num_predict": 600},
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(f"{HOST}/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(json.loads(resp.read())["message"]["content"])


@unittest.skipUnless(_model_available(), f"Ollama model {MODEL} not available")
class RealModelTest(unittest.TestCase):
    def test_sensible_decisions(self):
        for name, features, goal_config, check in CASES:
            with self.subTest(name):
                d = decide(render_prompt(features, goal_config))
                self.assertTrue(check(d), f"{d['battery_fraction']=} {d['curtail']=}: {d['reasoning']}")


if __name__ == "__main__":
    unittest.main()
