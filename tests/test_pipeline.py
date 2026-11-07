"""End to end: goal parser -> prompt assembly -> decision LLM (fake) -> validator -> guard."""

import json

from battery import BatteryConfig
from goal_parser import parse_goals
from llm import ChatResult, DecisionAgent, LLMError
from pipeline import run_cycle
from prompt_assembly import SOLVED_EXAMPLES

RESERVE = next(ex for ex in SOLVED_EXAMPLES if ex["goal_config"])  # plan section 5, 15:00: 3.5 of 6 MWh by 18:00
FEATURES = RESERVE["features"]
GOALS = parse_goals("I want to maximise profit. Store 6 MWh by 18:00. Never sell below 50 EUR/MWh").config


class FakeClient:
    model = "fake"

    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.prompts = reply, error, []

    def chat_json(self, messages, schema, *, tag=""):
        self.prompts.append(messages[-1]["content"])
        if self.error:
            raise self.error
        return ChatResult(json.dumps(self.reply), 0.0, self.model)


def test_plan_example_runs_end_to_end_with_goal_parser_object():
    client = FakeClient(RESERVE["decision"])
    r = run_cycle(FEATURES, GOALS, llm_call=DecisionAgent(client=client))

    assert "Store 6 MWh by 18:00" in client.prompts[0]
    assert r.decision.source == "llm" and r.decision.battery_fraction == 0.29
    assert r.setpoint.battery_setpoint_mw == 0.87
    assert "guard_error_hold" not in r.setpoint.applied_rules
    assert {"goal_g1_ok", "goal_g2_ok"} <= set(r.setpoint.applied_rules)


def test_object_and_dict_goal_config_give_the_same_result():
    call = DecisionAgent(client=FakeClient(RESERVE["decision"]))
    a = run_cycle(FEATURES, GOALS, llm_call=call).to_dict()
    b = run_cycle(FEATURES, GOALS.to_dict(), llm_call=call).to_dict()
    for d in (a, b):
        d["decision"]["latency_s"] = None
    assert a == b


def test_guard_enforces_goal_against_llm():
    # The LLM wants to sell everything at 17:00, but the 6 MWh reserve is due at 18:00.
    features = {**FEATURES, "time": "17:00"}
    sell = {**RESERVE["decision"], "battery_fraction": -1.0}
    r = run_cycle(features, GOALS, llm_call=DecisionAgent(client=FakeClient(sell)))
    assert r.decision.battery_fraction == -1.0
    assert r.setpoint.battery_setpoint_mw > 0 and "goal_g1_forced_charge" in r.setpoint.applied_rules


def test_ollama_down_falls_back_and_still_meets_goal_pace():
    r = run_cycle(FEATURES, GOALS, llm_call=DecisionAgent(client=FakeClient(error=LLMError("down"))))
    assert r.decision.source == "fallback" and r.decision.fallback_reason == "llm_error"
    assert r.setpoint.battery_setpoint_mw >= 0.29 * 3.0 - 1e-6


def test_broken_features_never_reach_the_llm():
    client = FakeClient(RESERVE["decision"])
    features = {k: v for k, v in FEATURES.items() if k != "break_even"}
    r = run_cycle(features, GOALS, llm_call=DecisionAgent(client=client))
    assert r.prompt is None and client.prompts == []
    assert r.decision.fallback_reason == "prompt_error"
    assert r.setpoint.applied_rules  # the guard still ran


def test_guard_uses_the_given_battery():
    r = run_cycle(FEATURES, None, llm_call=lambda p: json.dumps(RESERVE["decision"]),
                  battery=BatteryConfig(p_max_mw=1.0))
    assert r.setpoint.battery_setpoint_mw == 0.29
