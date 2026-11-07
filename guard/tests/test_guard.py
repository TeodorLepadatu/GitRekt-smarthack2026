"""Tests for the guard layer.

Run from the repo root:   python -m unittest discover -s guard/tests -t .
"""

import unittest

from guard import BatteryConfig, guard
from guard.goals import in_window, to_minutes

# Plan section 5 example (15:00).
GOALS = {
    "objective": "max_profit",
    "goals": [
        {"id": "g1", "type": "reserve", "mwh": 6.0, "by": "18:00", "until": "18:00", "hard": True},
        {"id": "g2", "type": "min_sell_price", "eur_per_mwh": 50, "hard": True},
    ],
}
STATE = {"time": "15:00", "stored_mwh": 3.5, "net_mw": 0.4, "price_now": 95, "production_mw": 5.0}


def dec(fraction, curtail=False, source="llm"):
    return {"battery_fraction": fraction, "curtail": curtail, "source": source}


def st(**changes):
    s = dict(STATE)
    s.update(changes)
    return s


def only(*goals):
    return {"objective": "max_profit", "goals": list(goals)}


G1 = GOALS["goals"][0]
G2 = GOALS["goals"][1]


class TestPlanExample(unittest.TestCase):
    def test_plan_15_00(self):
        r = guard(dec(0.29), STATE, GOALS)
        self.assertAlmostEqual(r.battery_setpoint_mw, 0.87)
        self.assertEqual(r.curtail_mw, 0.0)
        self.assertFalse(r.clipped)
        self.assertEqual(r.source, "llm")
        for rule in ("soc_limits_ok", "goal_g1_ok", "goal_g2_ok"):
            self.assertIn(rule, r.applied_rules)
        self.assertAlmostEqual(r.expected_soc_after_mwh, 3.71, places=2)

    def test_to_dict_contract(self):
        d = guard(dec(0.29), STATE, GOALS).to_dict()
        for key in ("battery_setpoint_mw", "curtail_mw", "clipped", "applied_rules", "source"):
            self.assertIn(key, d)

    def test_accepts_validator_decision_object(self):
        from validator import validate
        d = validate({"battery_fraction": 0.29, "curtail": False})
        self.assertAlmostEqual(guard(d, STATE, GOALS).battery_setpoint_mw, 0.87)


class TestPhysicalLimits(unittest.TestCase):
    def test_full_power(self):
        self.assertEqual(guard(dec(1.0), STATE).battery_setpoint_mw, 3.0)
        self.assertEqual(guard(dec(-1.0), STATE).battery_setpoint_mw, -3.0)

    def test_nearly_full_battery_limits_charge(self):
        r = guard(dec(1.0), st(stored_mwh=9.9))
        # 0.1 MWh free / (0.949 * 0.25 h) = 0.42 MW
        self.assertAlmostEqual(r.battery_setpoint_mw, 0.4216, places=3)
        self.assertTrue(r.clipped)
        self.assertIn("soc_max_limit", r.applied_rules)
        self.assertLessEqual(r.expected_soc_after_mwh, 10.0 + 1e-6)

    def test_nearly_empty_battery_limits_discharge(self):
        r = guard(dec(-1.0), st(stored_mwh=1.2))
        # (1.2 - 1.0 min) MWh * 0.949 / 0.25 h = 0.76 MW
        self.assertAlmostEqual(r.battery_setpoint_mw, -0.7589, places=3)
        self.assertIn("soc_min_limit", r.applied_rules)
        self.assertGreaterEqual(r.expected_soc_after_mwh, -1e-6)

    def test_e_min_respected(self):
        cfg = BatteryConfig(e_min_mwh=1.0)
        r = guard(dec(-1.0), st(stored_mwh=1.2), cfg=cfg)
        self.assertGreaterEqual(r.expected_soc_after_mwh, 1.0 - 1e-6)

    def test_full_battery_no_charge(self):
        self.assertEqual(guard(dec(1.0), st(stored_mwh=10.0)).battery_setpoint_mw, 0.0)

    def test_fraction_out_of_range_clamped(self):
        r = guard(dec(2.5), STATE)
        self.assertEqual(r.battery_setpoint_mw, 3.0)
        self.assertIn("fraction_clamped", r.applied_rules)


class TestReserveGoal(unittest.TestCase):
    def test_not_forced_at_16_15(self):
        r = guard(dec(0.0), st(time="16:15"), only(G1))
        self.assertEqual(r.battery_setpoint_mw, 0.0)
        self.assertIn("goal_g1_ok", r.applied_rules)

    def test_forced_from_16_30(self):
        # Plan section 4: forced full charging from the 16:30 interval.
        r = guard(dec(0.0), st(time="16:30"), only(G1))
        self.assertEqual(r.battery_setpoint_mw, 3.0)
        self.assertIn("goal_g1_forced_charge", r.applied_rules)

    def test_discharge_that_breaks_goal_is_forced_to_charge(self):
        r = guard(dec(-1.0), st(time="16:15"), only(G1))
        self.assertEqual(r.battery_setpoint_mw, 3.0)
        self.assertIn("goal_g1_forced_charge", r.applied_rules)

    def test_early_discharge_allowed(self):
        r = guard(dec(-1.0), st(time="09:00", stored_mwh=5.0), only(G1))
        self.assertEqual(r.battery_setpoint_mw, -3.0)

    def test_goal_already_met_not_forced(self):
        r = guard(dec(0.0), st(time="17:30", stored_mwh=6.5), only(G1))
        self.assertEqual(r.battery_setpoint_mw, 0.0)

    def test_after_deadline_blocks_discharge_below_target(self):
        r = guard(dec(-1.0), st(time="19:00", stored_mwh=6.2), only(G1))
        # Only 0.2 MWh above target: 0.2 * 0.949 / 0.25 = 0.76 MW
        self.assertAlmostEqual(r.battery_setpoint_mw, -0.7589, places=3)
        self.assertIn("goal_g1_blocked_discharge", r.applied_rules)
        self.assertGreaterEqual(r.expected_soc_after_mwh, 6.0 - 1e-6)

    def test_after_deadline_below_target_no_discharge(self):
        r = guard(dec(-0.5), st(time="19:00", stored_mwh=5.9), only(G1))
        self.assertEqual(r.battery_setpoint_mw, 0.0)

    def test_after_deadline_charging_allowed(self):
        self.assertEqual(guard(dec(0.5), st(time="19:00", stored_mwh=5.9), only(G1)).battery_setpoint_mw, 1.5)

    def test_overnight_window(self):
        g = {"id": "g3", "type": "reserve", "mwh": 2.0, "by": "22:00", "until": "06:00", "hard": True}
        inside = guard(dec(-1.0), st(time="02:00", stored_mwh=2.5), only(g))
        self.assertAlmostEqual(inside.battery_setpoint_mw, -1.8974, places=3)
        self.assertIn("goal_g3_blocked_discharge", inside.applied_rules)
        outside = guard(dec(-1.0), st(time="07:00", stored_mwh=2.5), only(g))
        self.assertEqual(outside.battery_setpoint_mw, -3.0)

    def test_impossible_goal_charges_at_physical_max(self):
        g = {"id": "g9", "type": "reserve", "mwh": 12.0, "by": "18:00", "hard": True}
        r = guard(dec(0.0), st(time="17:00", stored_mwh=9.9), only(g))
        self.assertAlmostEqual(r.battery_setpoint_mw, 0.4216, places=3)
        self.assertIn("goal_g9_forced_charge", r.applied_rules)
        self.assertIn("goal_g9_unreachable", r.applied_rules)

    def test_behind_but_reachable_not_flagged(self):
        r = guard(dec(0.0), st(time="16:30"), only(G1))
        self.assertNotIn("goal_g1_unreachable", r.applied_rules)

    def test_soft_goal_ignored(self):
        g = dict(G1, hard=False)
        self.assertEqual(guard(dec(0.0), st(time="16:30"), only(g)).battery_setpoint_mw, 0.0)

    def test_missing_time_warns(self):
        r = guard(dec(0.0), st(time=None), only(G1))
        self.assertEqual(r.battery_setpoint_mw, 0.0)
        self.assertTrue(r.warnings)


class TestMinSellPrice(unittest.TestCase):
    def test_price_above_limit_allows_discharge(self):
        self.assertEqual(guard(dec(-1.0), STATE, only(G2)).battery_setpoint_mw, -3.0)

    def test_low_price_surplus_is_stored(self):
        r = guard(dec(-1.0), st(price_now=40, net_mw=1.0), only(G2))
        self.assertEqual(r.battery_setpoint_mw, 1.0)
        self.assertEqual(r.curtail_mw, 0.0)
        self.assertIn("goal_g2_blocked_export", r.applied_rules)

    def test_low_price_big_surplus_is_curtailed(self):
        r = guard(dec(0.0), st(price_now=40, net_mw=5.0), only(G2))
        self.assertEqual(r.battery_setpoint_mw, 3.0)
        self.assertEqual(r.curtail_mw, 2.0)
        self.assertIn("curtail_forced_min_sell", r.applied_rules)

    def test_low_price_deficit_discharge_only_covers_demand(self):
        r = guard(dec(-1.0), st(price_now=40, net_mw=-1.8), only(G2))
        self.assertEqual(r.battery_setpoint_mw, -1.8)

    def test_low_price_charging_still_allowed(self):
        self.assertEqual(guard(dec(1.0), st(price_now=40, net_mw=-1.8), only(G2)).battery_setpoint_mw, 3.0)


class TestCurtail(unittest.TestCase):
    def test_negative_price_curtails_remaining_surplus(self):
        r = guard(dec(1.0, curtail=True), st(price_now=-8, net_mw=6.3, production_mw=8.0))
        self.assertEqual(r.battery_setpoint_mw, 3.0)
        self.assertAlmostEqual(r.curtail_mw, 3.3)
        self.assertIn("curtail_applied", r.applied_rules)

    def test_positive_price_blocks_curtail(self):
        r = guard(dec(0.0, curtail=True), st(price_now=95, net_mw=2.0))
        self.assertEqual(r.curtail_mw, 0.0)
        self.assertIn("curtail_blocked_price_not_negative", r.applied_rules)

    def test_no_surplus_blocks_curtail(self):
        r = guard(dec(1.0, curtail=True), st(price_now=-8, net_mw=-1.0))
        self.assertEqual(r.curtail_mw, 0.0)
        self.assertIn("curtail_blocked_no_surplus", r.applied_rules)

    def test_no_discharge_while_curtailing(self):
        r = guard(dec(-0.5, curtail=True), st(price_now=-8, net_mw=2.0))
        self.assertEqual(r.battery_setpoint_mw, 0.0)
        self.assertEqual(r.curtail_mw, 2.0)
        self.assertIn("no_discharge_while_curtailing", r.applied_rules)

    def test_curtail_capped_by_production(self):
        r = guard(dec(0.0, curtail=True), st(price_now=-8, net_mw=4.0, production_mw=1.5))
        self.assertEqual(r.curtail_mw, 1.5)

    def test_no_curtail_requested(self):
        self.assertEqual(guard(dec(0.0), st(price_now=-8, net_mw=4.0)).curtail_mw, 0.0)


class TestRobustness(unittest.TestCase):
    def test_missing_soc_holds(self):
        r = guard(dec(1.0), st(stored_mwh=None), GOALS)
        self.assertEqual(r.battery_setpoint_mw, 0.0)
        self.assertIn("no_soc_hold", r.applied_rules)

    def test_garbage_decision_holds(self):
        for d in (None, {}, {"battery_fraction": "abc"}, 42):
            self.assertEqual(guard(d, STATE, GOALS).battery_setpoint_mw, 0.0)

    def test_garbage_goals_do_not_crash(self):
        for g in (None, {}, {"goals": 5}, {"goals": ["x", None, {"type": "reserve"}, {"type": "???"}]}):
            r = guard(dec(0.29), STATE, g)
            self.assertAlmostEqual(r.battery_setpoint_mw, 0.87)

    def test_empty_state(self):
        self.assertEqual(guard(dec(1.0), {}).battery_setpoint_mw, 0.0)


class TestTimeHelpers(unittest.TestCase):
    def test_to_minutes(self):
        self.assertEqual(to_minutes("16:30"), 990)
        self.assertEqual(to_minutes("24:00"), 1440)
        self.assertIsNone(to_minutes("25:00"))
        self.assertIsNone(to_minutes("soon"))

    def test_in_window(self):
        self.assertTrue(in_window(19 * 60, 18 * 60, 1440))
        self.assertTrue(in_window(2 * 60, 22 * 60, 6 * 60))
        self.assertFalse(in_window(12 * 60, 22 * 60, 6 * 60))


if __name__ == "__main__":
    unittest.main()
