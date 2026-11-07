"""Feature builder: the plan's worked example, section 6 fixes 1-3, and the
contract with prompt assembly, the guard and the fallback.
"""
import json
import unittest

from battery import BatteryConfig
from feature_builder import (
    PriceCurve,
    PriceCurveError,
    SiteForecast,
    build_features,
    intervals_to_empty,
)
from feature_builder.builder import (
    CHEAP_RANK,
    FAR_ABOVE_EUR,
    NEAR_EUR,
    FeatureBuilderError,
)
from goal_parser import parse_goals
from pipeline import run_cycle
from prompt_assembly import assemble_prompt
from prompt_assembly import situation as pa_situation

B = BatteryConfig()


def day_prices():
    """A realistic day: cheap night, midday solar glut that goes negative,
    an afternoon plateau and an evening peak of EUR 170 starting at 20:30."""
    out = []
    for i in range(96):
        h = i * 15 / 60
        if h < 6:
            v = 55
        elif h < 10:
            v = 95
        elif h < 12:
            v = 45
        elif h < 13:
            v = -8
        elif h < 15:
            v = 45
        elif h < 18:
            v = 95
        elif h < 20.5:
            v = 140
        elif h < 21.5:
            v = 170
        else:
            v = 90
        out.append(float(v))
    return out


PRICES = day_prices()
# The plan's section 5 site at 15:00.
FORECAST = SiteForecast(demand_mw=4.6, production_mw=5.0, prod_low_mw=4.4, prod_high_mw=5.6)
GOALS = parse_goals("Maximise profit. Store 6 MWh by 18:00. Never sell below 50 EUR/MWh").config


def features(time="15:00", stored_mwh=3.5, forecast=FORECAST, goal_config=None, **kw):
    return build_features(time=time, forecast=forecast, prices=PRICES,
                          stored_mwh=stored_mwh, goal_config=goal_config, **kw)


class PriceCurveTest(unittest.TestCase):
    def setUp(self):
        self.c = PriceCurve(PRICES)

    def test_grid_is_derived_from_the_number_of_prices(self):
        self.assertEqual((self.c.n, self.c.interval_min), (96, 15))
        self.assertEqual(PriceCurve([1.0] * 24).interval_min, 60)

    def test_index_and_time_round_trip(self):
        for t in ("00:00", "09:15", "15:00", "23:45"):
            self.assertEqual(self.c.time_at(self.c.index_at(t)), t)

    def test_a_time_inside_an_interval_maps_to_its_start(self):
        self.assertEqual(self.c.time_at(self.c.index_at("15:07")), "15:00")

    def test_rank_stays_in_range_and_follows_price(self):
        ranks = [self.c.rank(i) for i in range(self.c.n)]
        self.assertTrue(all(0 <= r <= 100 for r in ranks))
        by_price = sorted(range(self.c.n), key=lambda i: self.c.prices[i])
        self.assertEqual([ranks[i] for i in by_price], sorted(ranks))

    def test_intervals_at_the_same_price_share_a_rank(self):
        same = [i for i, p in enumerate(self.c.prices) if p == 95.0]
        self.assertGreater(len(same), 1)
        self.assertEqual(len({self.c.rank(i) for i in same}), 1)

    def test_a_unique_cheapest_and_dearest_reach_0_and_100(self):
        curve = PriceCurve([float(p) for p in range(96)])
        self.assertEqual((curve.rank(0), curve.rank(95)), (0, 100))

    def test_extreme_later_looks_only_at_later_intervals(self):
        at, price = self.c.extreme_later(self.c.index_at("15:00"), highest=True)
        self.assertEqual((self.c.time_at(at), price), ("20:30", 170.0))
        self.assertIsNone(self.c.extreme_later(self.c.n - 1, highest=True))

    def test_kth_highest_falls_back_to_the_last_available(self):
        idx = [0, 1, 2]
        prices = PriceCurve([10.0, 30.0, 20.0] + [0.0] * 93)
        self.assertEqual(prices.kth_highest(idx, 1), 30.0)
        self.assertEqual(prices.kth_highest(idx, 3), 10.0)
        self.assertEqual(prices.kth_highest(idx, 99), 10.0)
        self.assertIsNone(prices.kth_highest([], 1))

    def test_bad_curves_are_rejected(self):
        for bad in ([], [1.0, "x"], [1.0] * 7):
            with self.subTest(bad=bad):
                self.assertRaises(PriceCurveError, PriceCurve, bad)

    def test_intervals_to_empty(self):
        # 3 MW over 15 min delivers 0.75 MWh, so 2.37 MWh needs 4 intervals.
        self.assertEqual(intervals_to_empty(2.3717, 3.0, 0.25), 4)
        self.assertEqual(intervals_to_empty(0.75, 3.0, 0.25), 1)
        self.assertEqual(intervals_to_empty(0.0, 3.0, 0.25), 1)


class PlanWorkedExampleTest(unittest.TestCase):
    """Plan section 5, "Feature builder -> prompt assembly", at 15:00."""

    def setUp(self):
        self.f = features(goal_config=GOALS)

    def test_site_balance(self):
        f = self.f
        self.assertEqual(f["time"], "15:00")
        self.assertEqual((f["demand_mw"], f["production_mw"]), (4.6, 5.0))
        self.assertAlmostEqual(f["net_mw"], 0.4)
        self.assertEqual(f["net_label"], "SURPLUS")
        self.assertEqual(f["matching_fraction"], 0.13)

    def test_prices_and_break_even(self):
        f = self.f
        self.assertEqual(f["price_now"], 95.0)
        self.assertEqual((f["future_max_price"], f["future_max_time"]), (170.0, "20:30"))
        self.assertEqual(f["break_even"], 148.0)
        self.assertEqual(f["price_vs_breakeven"], "EUR 53 BELOW")
        self.assertEqual(f["price_zone"], "BELOW")

    def test_battery_limits(self):
        f = self.f
        self.assertEqual((f["stored_mwh"], f["max_charge_mw"], f["max_discharge_mw"]), (3.5, 3.0, 3.0))
        self.assertEqual((f["min_soc_mwh"], f["capacity_mwh"]), (1.0, 10.0))

    def test_goal_progress_matches_the_plan(self):
        g = self.f["goals"][0]
        self.assertEqual(g["id"], "g1")
        self.assertEqual(g["status"], "on_track")
        self.assertAlmostEqual(g["need_mwh"], 2.5)
        self.assertAlmostEqual(g["hours_left"], 3.0)
        self.assertEqual(g["even_pace_fraction"], 0.29)
        self.assertEqual(g["force_from"], "16:30")

    def test_the_whole_cycle_reaches_the_plan_setpoint(self):
        reply = json.dumps({"reasoning": "goal pace", "battery_fraction": 0.29, "curtail": False,
                            "confidence": "medium", "explanation_for_operator": "charging"})
        r = run_cycle(self.f, GOALS, llm_call=lambda prompt: reply)
        self.assertEqual(r.decision.source, "llm")
        self.assertEqual(r.setpoint.battery_setpoint_mw, 0.87)
        self.assertAlmostEqual(r.setpoint.expected_soc_after_mwh, 3.7063, places=3)
        self.assertNotIn("guard_error_hold", r.setpoint.applied_rules)


class BatteryLimitsTest(unittest.TestCase):
    """Plan section 6, fix 1: limits on a 15-minute basis, from battery.py."""

    def test_limits_come_from_the_shared_battery_config(self):
        for stored in (1.0, 2.0, 3.5, 9.5, 10.0):
            with self.subTest(stored=stored):
                f = features(stored_mwh=stored)
                self.assertEqual(f["max_charge_mw"], round(B.max_charge_mw(stored), 2))
                self.assertEqual(f["max_discharge_mw"], round(B.max_discharge_mw(stored), 2))
                self.assertEqual(f["max_charge_fraction"], round(f["max_charge_mw"] / B.p_max_mw, 2))
                self.assertEqual(f["max_discharge_fraction"], round(f["max_discharge_mw"] / B.p_max_mw, 2))

    def test_an_almost_full_battery_cannot_charge_at_full_power(self):
        self.assertEqual(features(stored_mwh=9.5)["max_charge_mw"], 2.11)
        self.assertEqual(features(stored_mwh=10.0)["max_charge_mw"], 0.0)

    def test_an_empty_battery_cannot_discharge(self):
        self.assertEqual(features(stored_mwh=1.0)["max_discharge_mw"], 0.0)

    def test_a_different_battery_is_honoured(self):
        f = features(battery=BatteryConfig(capacity_mwh=4.0, p_max_mw=1.0))
        self.assertEqual(f["capacity_mwh"], 4.0)
        self.assertEqual(f["matching_fraction"], 0.4)  # net 0.4 MW over 1 MW rated


class BreakEvenTest(unittest.TestCase):
    """Plan section 6, fixes 2 and 3."""

    def test_k_grows_with_stored_energy(self):
        # One interval sells P_max x 0.25 h, so a fuller battery needs more of them.
        ks = [features(stored_mwh=s)["break_even_k"] for s in (1.0, 3.5, 5.0, 10.0)]
        self.assertEqual(ks, sorted(ks))
        self.assertEqual(ks[0], 1)
        self.assertGreater(ks[-1], ks[0])

    def test_a_fuller_battery_values_its_energy_lower(self):
        near_empty = features(stored_mwh=1.0)
        fairly_full = features(stored_mwh=8.0)
        self.assertEqual(near_empty["break_even"], 148.0)   # the single best later price
        self.assertLess(fairly_full["break_even"], near_empty["break_even"])

    def test_break_even_follows_the_formula_on_the_price_it_used(self):
        for stored in (1.0, 3.5, 8.0):
            with self.subTest(stored=stored):
                f = features(stored_mwh=stored)
                self.assertAlmostEqual(
                    f["break_even"], round(B.round_trip_eff * f["break_even_price"] - 5.0, 1))

    def test_a_cheap_refill_lowers_the_bar_and_flips_the_zone(self):
        # 09:00 at EUR 95: the evening peak alone says hold, but a cheap midday
        # refill comes first, so selling now and buying back wins.
        f = features(time="09:00", stored_mwh=7.0)
        self.assertEqual(f["break_even"], 121.0)
        self.assertEqual(f["break_even_before_refill"], 80.5)
        self.assertEqual(f["next_refill_time"], "10:00")
        self.assertEqual(f["price_zone"], "ABOVE")

    def test_no_refill_field_when_nothing_cheap_is_coming(self):
        f = features(time="15:00", stored_mwh=3.5)
        self.assertNotIn("break_even_before_refill", f)
        self.assertNotIn("next_refill_time", f)

    def test_the_last_interval_of_the_day_has_nothing_to_hold_for(self):
        f = features(time="23:45", stored_mwh=5.0)
        self.assertNotIn("future_max_price", f)
        self.assertLess(f["break_even"], f["price_now"])
        self.assertEqual(f["price_zone"], "ABOVE")


class GoalProgressTest(unittest.TestCase):
    """Plan section 4: even pace tells the LLM how urgent, latest start is the guard's line."""

    def goal(self, time, stored, config=GOALS):
        return features(time=time, stored_mwh=stored, goal_config=config)["goals"][0]

    def test_even_pace_and_latest_start(self):
        g = self.goal("15:00", 3.5)
        self.assertEqual(g["even_pace_fraction"],
                         round(2.5 / 3.0 / B.eta_c / B.p_max_mw, 2))
        self.assertEqual(g["force_from"], "16:30")

    def test_status_turns_behind_at_the_latest_start(self):
        self.assertEqual(self.goal("16:15", 3.5)["status"], "on_track")
        self.assertEqual(self.goal("16:30", 3.5)["status"], "behind")
        self.assertEqual(self.goal("17:00", 3.5)["status"], "behind")

    def test_pace_rises_as_time_runs_out(self):
        paces = [self.goal(t, 3.5)["even_pace_fraction"] for t in ("15:00", "16:00", "17:00")]
        self.assertEqual(paces, sorted(paces))
        self.assertLessEqual(max(paces), 1.0)

    def test_a_reached_goal_is_met_and_needs_nothing(self):
        g = self.goal("15:00", 6.0)
        self.assertEqual((g["status"], g["need_mwh"]), ("met", 0.0))
        self.assertNotIn("even_pace_fraction", g)

    def test_inside_the_protection_window_a_shortfall_is_behind(self):
        self.assertEqual(self.goal("18:30", 6.5)["status"], "met")
        self.assertEqual(self.goal("18:30", 4.0)["status"], "behind")

    def test_an_impossible_goal_is_flagged_unreachable(self):
        g = self.goal("17:55", 1.0)
        self.assertEqual(g["status"], "behind")
        self.assertFalse(g["reachable"])

    def test_an_overnight_window_wraps_midnight(self):
        overnight = parse_goals("Keep 2 MWh backup overnight").config  # 22:00 until 06:00
        self.assertEqual(self.goal("23:00", 3.0, overnight)["status"], "met")
        inside = self.goal("23:00", 1.5, overnight)
        self.assertEqual(inside["status"], "behind")
        self.assertAlmostEqual(inside["hours_left"], 7.0)   # to 06:00
        self.assertNotIn("force_from", inside)              # the guard blocks, it does not force
        before = self.goal("20:00", 0.5, overnight)
        self.assertEqual(before["status"], "on_track")
        self.assertEqual(before["force_from"], "20:45")

    def test_only_reserve_goals_get_progress(self):
        # The goal set also holds a min_sell_price goal, judged from price_now.
        ids = [g["id"] for g in features(goal_config=GOALS)["goals"]]
        self.assertEqual(ids, ["g1"])

    def test_no_goals_means_an_empty_list(self):
        self.assertEqual(features()["goals"], [])

    def test_the_fallback_reads_the_pace(self):
        # validator.fallback raises its fraction to an open hard goal's pace.
        from validator import fallback_decision
        f = features(goal_config=GOALS)
        d = fallback_decision(f, "timeout")
        self.assertGreaterEqual(d.battery_fraction, f["goals"][0]["even_pace_fraction"])


class ContractTest(unittest.TestCase):
    """What prompt assembly, the guard and the fallback expect to find."""

    CASES = [(t, s) for t in ("00:00", "09:00", "12:00", "15:00", "20:30", "23:45")
             for s in (1.0, 3.5, 7.0, 10.0)]

    def test_every_required_prompt_field_is_present_and_numeric(self):
        for time, stored in self.CASES:
            with self.subTest(time=time, stored=stored):
                f = features(time=time, stored_mwh=stored, goal_config=GOALS)
                for name in pa_situation.REQUIRED_FIELDS:
                    self.assertIn(name, f)
                    self.assertIsNotNone(f[name])

    def test_the_prompt_assembles_without_a_single_warning(self):
        for time, stored in self.CASES:
            with self.subTest(time=time, stored=stored):
                f = features(time=time, stored_mwh=stored, goal_config=GOALS)
                self.assertEqual(assemble_prompt(f, GOALS).warnings, [])

    def test_labels_match_what_prompt_assembly_would_derive(self):
        """The two must never drift: the feature builder is the source, but
        prompt assembly falls back to its own copy when a field is missing."""
        derived_keys = ("price_zone", "price_cheap", "surplus_left")
        for time, stored in self.CASES:
            with self.subTest(time=time, stored=stored):
                f = features(time=time, stored_mwh=stored)
                without = {k: v for k, v in f.items() if k not in derived_keys}
                theirs = pa_situation.labels(without)
                theirs.pop("price_negative")
                self.assertEqual({k: f[k] for k in derived_keys}, theirs)

    def test_label_thresholds_are_the_same_constants(self):
        self.assertEqual(NEAR_EUR, pa_situation.NEAR_EUR)
        self.assertEqual(FAR_ABOVE_EUR, pa_situation.FAR_ABOVE_EUR)
        self.assertEqual(CHEAP_RANK, pa_situation.CHEAP_RANK)

    def test_price_vs_breakeven_matches_the_format_prompt_assembly_accepts(self):
        for time, stored in self.CASES:
            with self.subTest(time=time, stored=stored):
                f = features(time=time, stored_mwh=stored)
                self.assertRegex(f["price_vs_breakeven"], pa_situation._PRICE_VS_BE_RE)

    def test_the_guard_finds_the_state_it_reads(self):
        f = features(goal_config=GOALS)
        for name in ("time", "stored_mwh", "net_mw", "price_now", "production_mw"):
            self.assertIn(name, f)

    def test_settlement_gets_the_interval_and_the_wear_rate(self):
        f = features()
        self.assertEqual(f["interval_h"], B.interval_h)
        # One wear rate for the whole project: settlement charges the same one.
        self.assertEqual(f["wear_eur_per_mwh"], B.wear_eur_per_mwh)

    def test_a_custom_wear_rate_moves_break_even(self):
        pricey = BatteryConfig(wear_eur_per_mwh=20.0)
        f = features(battery=pricey)
        self.assertEqual(f["wear_eur_per_mwh"], 20.0)
        self.assertAlmostEqual(
            f["break_even"], round(B.round_trip_eff * f["break_even_price"] - 40.0, 1))
        self.assertLess(f["break_even"], features()["break_even"])


class SiteForecastTest(unittest.TestCase):
    def test_production_defaults_to_solar_plus_wind(self):
        f = features(forecast=SiteForecast(demand_mw=2.0, solar_mw=3.0, wind_mw=1.5))
        self.assertEqual(f["production_mw"], 4.5)
        self.assertEqual((f["solar_mw"], f["wind_mw"]), (3.0, 1.5))
        self.assertAlmostEqual(f["net_mw"], 2.5)

    def test_a_mapping_works_as_well_as_the_dataclass(self):
        a = features(forecast={"demand_mw": 4.6, "production_mw": 5.0})
        b = features(forecast=SiteForecast(demand_mw=4.6, production_mw=5.0))
        self.assertEqual(a, b)

    def test_uncertainty_comes_from_the_forecast_band(self):
        def unc(low, high, production=5.0):
            return features(forecast=SiteForecast(demand_mw=4.6, production_mw=production,
                                                  prod_low_mw=low, prod_high_mw=high))["uncertainty"]
        self.assertEqual(unc(4.7, 5.3), "LOW")      # 0.6 MW over 3 MW rated
        self.assertEqual(unc(4.4, 5.6), "MEDIUM")   # 1.2 MW
        self.assertEqual(unc(3.0, 7.0), "HIGH")     # 4.0 MW

    def test_a_missing_band_means_no_uncertainty_label(self):
        f = features(forecast=SiteForecast(demand_mw=4.6, production_mw=5.0))
        for name in ("prod_low_mw", "prod_high_mw", "uncertainty"):
            self.assertNotIn(name, f)

    def test_a_swapped_band_is_put_back_in_order_and_covers_production(self):
        f = features(forecast=SiteForecast(demand_mw=4.6, production_mw=5.0,
                                           prod_low_mw=5.6, prod_high_mw=4.4))
        self.assertLessEqual(f["prod_low_mw"], f["production_mw"])
        self.assertLessEqual(f["production_mw"], f["prod_high_mw"])

    def test_a_balanced_site_is_labelled_balanced(self):
        f = features(forecast=SiteForecast(demand_mw=4.6, production_mw=4.6))
        self.assertEqual(f["net_label"], "BALANCED")
        self.assertEqual(f["matching_fraction"], 0.0)


class BadInputTest(unittest.TestCase):
    def test_unusable_inputs_raise(self):
        cases = {
            "bad time": dict(time="half past three"),
            "state of charge above capacity": dict(stored_mwh=99.0),
            "state of charge not a number": dict(stored_mwh=float("nan")),
            "no demand": dict(forecast=SiteForecast(demand_mw=None, production_mw=5.0)),
            "no production at all": dict(forecast=SiteForecast(demand_mw=4.6)),
        }
        for name, override in cases.items():
            with self.subTest(name):
                self.assertRaises(FeatureBuilderError, features, **override)

    def test_a_broken_goal_is_skipped_not_fatal(self):
        config = {"objective": "max_profit",
                  "goals": [{"id": "g1", "type": "reserve", "mwh": None, "by": "18:00", "hard": True},
                            {"id": "g2", "type": "min_sell_price", "eur_per_mwh": 50, "hard": True}]}
        self.assertEqual(features(goal_config=config)["goals"], [])

    def test_a_prompt_error_still_leaves_the_cycle_running(self):
        # The pipeline must never raise, even if the feature set is wrecked later.
        f = features(goal_config=GOALS)
        del f["break_even"]
        r = run_cycle(f, GOALS, llm_call=lambda p: "{}")
        self.assertIsNone(r.prompt)
        self.assertEqual(r.decision.source, "fallback")
        self.assertTrue(r.setpoint.applied_rules)


class WholeDayTest(unittest.TestCase):
    def test_every_interval_of_the_day_builds_and_runs(self):
        reply = json.dumps({"reasoning": "r", "battery_fraction": 0.1, "curtail": False,
                            "confidence": "low", "explanation_for_operator": "e"})
        stored = 3.5
        for i in range(96):
            time = PriceCurve(PRICES).time_at(i)
            with self.subTest(time=time):
                f = build_features(time=time, forecast=FORECAST, prices=PRICES,
                                   stored_mwh=stored, goal_config=GOALS)
                self.assertEqual(assemble_prompt(f, GOALS).warnings, [])
                r = run_cycle(f, GOALS, llm_call=lambda p: reply)
                self.assertFalse(r.setpoint.warnings, r.setpoint.warnings)
                stored = r.setpoint.expected_soc_after_mwh
                self.assertTrue(B.e_min_mwh - 1e-6 <= stored <= B.e_max + 1e-6)


if __name__ == "__main__":
    unittest.main()
