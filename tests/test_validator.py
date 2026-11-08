"""Tests for validator, fallback and decide.

Run from the repo root:   python -m pytest tests/test_validator.py
"""

import json
import time
import unittest

from validator import decide, fallback_decision, validate
from validator.schema import ValidationError

# Example LLM output from the plan, section 5 (15:00).
PLAN_OUTPUT = {
    "reasoning": "The price is EUR 53 below break-even, so stored energy is worth more tonight.",
    "battery_fraction": 0.29,
    "curtail": False,
    "confidence": "medium",
    "explanation_for_operator": "Charging 0.87 MW to reach the 6 MWh goal by 18:00.",
}

# Example features from the plan, section 5 (15:00).
PLAN_FEATURES = {
    "time": "15:00", "demand_mw": 4.6, "production_mw": 5.0, "net_mw": 0.4,
    "price_now": 95, "break_even": 148.0, "stored_mwh": 3.5,
    "max_charge_mw": 3.0, "max_discharge_mw": 3.0, "matching_fraction": 0.13,
    "goals": [{"id": "g1", "status": "on_track", "need_mwh": 2.5, "hours_left": 3.0,
               "even_pace_fraction": 0.29, "force_from": "16:30"}],
}


def with_(**changes):
    out = dict(PLAN_OUTPUT)
    out.update(changes)
    return json.dumps(out)


class TestValidatorAccepts(unittest.TestCase):
    def test_plan_example(self):
        d = validate(json.dumps(PLAN_OUTPUT))
        self.assertEqual(d.battery_fraction, 0.29)
        self.assertFalse(d.curtail)
        self.assertEqual(d.source, "llm")
        self.assertEqual(d.confidence, "medium")
        self.assertEqual(d.warnings, [])

    def test_already_a_dict(self):
        self.assertEqual(validate(PLAN_OUTPUT).battery_fraction, 0.29)

    def test_code_fence(self):
        raw = "```json\n" + json.dumps(PLAN_OUTPUT) + "\n```"
        self.assertEqual(validate(raw).battery_fraction, 0.29)

    def test_text_around_json(self):
        raw = "Here is my decision: " + json.dumps(PLAN_OUTPUT) + " Hope it helps."
        self.assertEqual(validate(raw).battery_fraction, 0.29)

    def test_qwen_think_block(self):
        raw = "<think>price {is} low...</think>\n" + json.dumps(PLAN_OUTPUT)
        self.assertEqual(validate(raw).battery_fraction, 0.29)

    def test_integer_fraction(self):
        self.assertEqual(validate(with_(battery_fraction=-1)).battery_fraction, -1.0)

    def test_bounds_inclusive(self):
        self.assertEqual(validate(with_(battery_fraction=1.0)).battery_fraction, 1.0)
        self.assertEqual(validate(with_(battery_fraction=-1.0)).battery_fraction, -1.0)

    def test_small_overshoot_is_clamped(self):
        d = validate(with_(battery_fraction=1.03))
        self.assertEqual(d.battery_fraction, 1.0)
        self.assertTrue(d.warnings)

    def test_curtail_as_string(self):
        d = validate(with_(curtail="true"))
        self.assertTrue(d.curtail)
        self.assertTrue(d.warnings)

    def test_confidence_normalised(self):
        self.assertEqual(validate(with_(confidence=" HIGH ")).confidence, "high")

    def test_bad_confidence_defaults_to_low(self):
        d = validate(with_(confidence="very sure"))
        self.assertEqual(d.confidence, "low")
        self.assertTrue(d.warnings)

    def test_missing_text_fields_only_warn(self):
        raw = json.dumps({"battery_fraction": 0.5, "curtail": False})
        d = validate(raw)
        self.assertEqual(d.battery_fraction, 0.5)
        self.assertEqual(d.explanation_for_operator, "")
        self.assertEqual(len(d.warnings), 3)  # confidence + 2 text fields


class TestValidatorRejects(unittest.TestCase):
    def assertReason(self, raw, reason):
        with self.assertRaises(ValidationError) as ctx:
            validate(raw)
        self.assertEqual(ctx.exception.reason, reason)

    def test_empty(self):
        self.assertReason("", "empty_output")
        self.assertReason("   ", "empty_output")
        self.assertReason(None, "empty_output")
        self.assertReason("<think>hmm</think>", "empty_output")

    def test_not_json(self):
        self.assertReason("I think we should charge.", "bad_json")

    def test_truncated_json(self):
        self.assertReason('{"battery_fraction": 0.3, "curtail": fa', "bad_json")

    def test_json_array(self):
        self.assertReason("[0.3, false]", "not_a_json_object")

    def test_missing_fraction(self):
        self.assertReason(json.dumps({"curtail": False}), "missing_field")

    def test_missing_curtail(self):
        self.assertReason(json.dumps({"battery_fraction": 0.2}), "missing_field")

    def test_fraction_as_string(self):
        self.assertReason(with_(battery_fraction="0.29"), "bad_type")

    def test_fraction_as_bool(self):
        self.assertReason(with_(battery_fraction=True), "bad_type")

    def test_fraction_null(self):
        self.assertReason(with_(battery_fraction=None), "bad_type")

    def test_fraction_too_large(self):
        self.assertReason(with_(battery_fraction=3.15), "out_of_range")
        self.assertReason(with_(battery_fraction=-1.5), "out_of_range")

    def test_fraction_nan(self):
        # json.dumps writes NaN, which Python's json.loads accepts.
        self.assertReason(with_(battery_fraction=float("nan")), "out_of_range")
        self.assertReason(with_(battery_fraction=float("inf")), "out_of_range")

    def test_curtail_bad_type(self):
        self.assertReason(with_(curtail="maybe"), "bad_type")
        self.assertReason(with_(curtail=1), "bad_type")


class TestFallback(unittest.TestCase):
    def f(self, **changes):
        feats = dict(PLAN_FEATURES, goals=[])
        feats.update(changes)
        return fallback_decision(feats, "test")

    def test_source_and_reason(self):
        d = fallback_decision(PLAN_FEATURES, "bad_json", "detail")
        self.assertEqual(d.source, "fallback")
        self.assertEqual(d.fallback_reason, "bad_json")
        self.assertEqual(d.confidence, "low")
        self.assertTrue(d.explanation_for_operator)

    def test_negative_price_charges_full(self):
        d = self.f(price_now=-8, net_mw=1.0)
        self.assertEqual(d.battery_fraction, 1.0)
        self.assertFalse(d.curtail)  # 1.0 MW surplus fits in 3 MW charging

    def test_negative_price_curtails_excess_surplus(self):
        d = self.f(price_now=-8, net_mw=6.3, max_charge_mw=2.0)
        self.assertEqual(d.battery_fraction, 1.0)
        self.assertTrue(d.curtail)

    def test_negative_price_deficit_no_curtail(self):
        d = self.f(price_now=-8, net_mw=-1.0)
        self.assertEqual(d.battery_fraction, 1.0)
        self.assertFalse(d.curtail)

    def test_price_above_break_even_discharges(self):
        d = self.f(price_now=165, break_even=148.0)
        self.assertEqual(d.battery_fraction, -1.0)
        self.assertFalse(d.curtail)

    def test_price_equal_break_even_discharges(self):
        self.assertEqual(self.f(price_now=148.0, break_even=148.0).battery_fraction, -1.0)

    def test_below_break_even_follows_matching(self):
        self.assertEqual(self.f().battery_fraction, 0.13)
        self.assertEqual(self.f(net_mw=-1.8, matching_fraction=-0.60).battery_fraction, -0.6)

    def test_matching_fraction_clamped(self):
        self.assertEqual(self.f(matching_fraction=3.15).battery_fraction, 1.0)

    def test_open_goal_raises_fraction(self):
        d = fallback_decision(PLAN_FEATURES, "test")  # matching 0.13, goal pace 0.29
        self.assertEqual(d.battery_fraction, 0.29)

    def test_open_goal_overrides_discharge(self):
        feats = dict(PLAN_FEATURES, price_now=165)
        self.assertEqual(fallback_decision(feats, "test").battery_fraction, 0.29)

    def test_met_goal_ignored(self):
        feats = dict(PLAN_FEATURES, goals=[{"status": "met", "even_pace_fraction": 0.9}])
        self.assertEqual(fallback_decision(feats, "test").battery_fraction, 0.13)

    def test_highest_goal_pace_wins(self):
        feats = dict(PLAN_FEATURES, goals=[
            {"status": "on_track", "even_pace_fraction": 0.29},
            {"status": "behind", "even_pace_fraction": 0.55},
        ])
        self.assertEqual(fallback_decision(feats, "test").battery_fraction, 0.55)

    def test_no_features_holds(self):
        for feats in (None, {}, {"price_now": "abc"}):
            d = fallback_decision(feats, "test")
            self.assertEqual(d.battery_fraction, 0.0)
            self.assertFalse(d.curtail)

    def test_garbage_goals_do_not_crash(self):
        feats = dict(PLAN_FEATURES, goals=["x", None, {"even_pace_fraction": "y"}])
        self.assertEqual(fallback_decision(feats, "test").battery_fraction, 0.13)

    def test_non_list_goals_do_not_crash(self):
        d = fallback_decision(dict(PLAN_FEATURES, goals=42), "test")
        self.assertEqual(d.source, "fallback")


class TestDecide(unittest.TestCase):
    def test_valid_llm_output(self):
        d = decide("prompt", PLAN_FEATURES, llm_call=lambda p: json.dumps(PLAN_OUTPUT))
        self.assertEqual(d.source, "llm")
        self.assertEqual(d.battery_fraction, 0.29)
        self.assertIsNotNone(d.latency_s)

    def test_invalid_output_falls_back(self):
        d = decide("prompt", PLAN_FEATURES, llm_call=lambda p: "not json")
        self.assertEqual(d.source, "fallback")
        self.assertEqual(d.fallback_reason, "bad_json")
        self.assertEqual(d.battery_fraction, 0.29)

    def test_llm_exception_falls_back(self):
        def boom(p):
            raise ConnectionError("ollama not running")
        d = decide("prompt", PLAN_FEATURES, llm_call=boom)
        self.assertEqual(d.source, "fallback")
        self.assertEqual(d.fallback_reason, "llm_error")

    def test_timeout_falls_back_quickly(self):
        def slow(p):
            time.sleep(2)
            return json.dumps(PLAN_OUTPUT)
        start = time.perf_counter()
        d = decide("prompt", PLAN_FEATURES, llm_call=slow, timeout_s=0.2)
        self.assertLess(time.perf_counter() - start, 1.0)
        self.assertEqual(d.source, "fallback")
        self.assertEqual(d.fallback_reason, "timeout")

    def test_logger_receives_record(self):
        records = []
        decide("p", PLAN_FEATURES, llm_call=lambda p: json.dumps(PLAN_OUTPUT), logger=records.append)
        self.assertEqual(records[0]["prompt"], "p")
        self.assertEqual(records[0]["decision"]["source"], "llm")

    def test_broken_logger_does_not_crash(self):
        def bad_logger(rec):
            raise RuntimeError("disk full")
        d = decide("p", PLAN_FEATURES, llm_call=lambda p: json.dumps(PLAN_OUTPUT), logger=bad_logger)
        self.assertEqual(d.source, "llm")

    def test_to_dict_has_guard_fields(self):
        d = decide("p", PLAN_FEATURES, llm_call=lambda p: json.dumps(PLAN_OUTPUT)).to_dict()
        for key in ("battery_fraction", "curtail", "source"):
            self.assertIn(key, d)


if __name__ == "__main__":
    unittest.main()
