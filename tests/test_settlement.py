"""Settlement: money per interval, goal status, day totals."""

import json

import pytest

from battery import BatteryConfig
from goal_parser import parse_goals
from pipeline import run_cycle
from prompt_assembly import SOLVED_EXAMPLES
from settlement import DayLedger, GoalTracker, goal_costs, settle_interval

CFG = BatteryConfig()
RESERVE = next(ex for ex in SOLVED_EXAMPLES if ex["goal_config"])  # plan section 5, 15:00
FEATURES = RESERVE["features"]
GOALS = parse_goals("I want to maximise profit. Store 6 MWh by 18:00. Never sell below 50 EUR/MWh").config
G1 = {"id": "g1", "type": "reserve", "mwh": 6.0, "by": "18:00", "until": "18:00", "hard": True}
G2 = {"id": "g2", "type": "min_sell_price", "eur_per_mwh": 50, "hard": True}


def settle(time="15:00", price=95, soc=3.5, mw=0.87, curtail=0.0, demand=4.6, production=5.0):
    return settle_interval(time=time, price_eur_mwh=price, soc_before_mwh=soc, battery_setpoint_mw=mw,
                           curtail_mw=curtail, demand_mw=demand, production_mw=production, cfg=CFG)


# ----------------------------------------------------------------- money
def test_plan_section5_settlement_numbers():
    s = settle()
    assert s.grid_import_mwh == 0.1175 and s.grid_export_mwh == 0.0
    assert s.energy_cost_eur == -11.16
    assert s.wear_eur == -0.54
    assert s.profit_eur == -11.71
    assert round(s.soc_after_mwh, 2) == 3.71


def test_discharge_sells_surplus_and_battery_energy():
    s = settle(time="19:00", price=150, soc=8.0, mw=-3.0, demand=3.0, production=3.3)
    assert s.grid_export_mwh == pytest.approx((0.3 + 3.0) * 0.25)
    assert s.energy_cost_eur == pytest.approx(0.825 * 150, abs=0.01)
    assert s.wear_eur == pytest.approx(-2.5 * 0.75, abs=0.01)
    assert s.soc_after_mwh == pytest.approx(8.0 - 0.75 / CFG.eta_d, abs=1e-4)
    assert s.value_added_eur == pytest.approx(s.profit_eur - 0.3 * 0.25 * 150, abs=0.01)


def test_curtailment_at_negative_price_avoids_paying_to_export():
    # 6.3 MW surplus, battery takes 3, 3.3 curtailed -> no export at -8.
    s = settle(time="12:00", price=-8, soc=5.0, mw=3.0, curtail=3.3, demand=1.0, production=7.3)
    assert s.grid_export_mwh == 0.0 and s.grid_import_mwh == 0.0
    assert s.energy_cost_eur == 0.0
    assert s.no_battery_profit_eur == pytest.approx(6.3 * 0.25 * -8, abs=0.01)
    assert s.value_added_eur > 0


def test_site_cuts_setpoint_the_actual_soc_cannot_deliver():
    s = settle(time="20:00", price=150, soc=1.1, mw=-3.0)
    assert s.site_clipped and s.warnings
    assert s.battery_mw == pytest.approx(-0.1 * CFG.eta_d / 0.25, abs=1e-4)
    assert s.soc_after_mwh == pytest.approx(1.0, abs=1e-4)


def test_energy_balance_holds():
    s = settle(time="12:00", price=-8, soc=5.0, mw=2.0, curtail=1.0, demand=1.0, production=7.3)
    lhs = (s.production_mw - s.demand_mw) * 0.25
    rhs = s.battery_mw * 0.25 + s.grid_export_mwh - s.grid_import_mwh + s.curtail_mw * 0.25
    assert lhs == pytest.approx(rhs, abs=1e-6)


# ----------------------------------------------------------------- goals
def run_reserve(start_soc, start_time, mw_per_interval, n):
    tracker = GoalTracker({"goals": [G1]}, CFG)
    soc, (h, m) = start_soc, map(int, start_time.split(":"))
    t = h * 60 + m
    for _ in range(n):
        s = settle(time=f"{t // 60:02d}:{t % 60:02d}", soc=soc, mw=mw_per_interval)
        tracker.update(s)
        soc, t = s.soc_after_mwh, t + 15
    return tracker.states["g1"]


def test_reserve_reached_time_is_recorded():
    st = run_reserve(3.5, "15:00", 3.0, 4)  # 3.5 + 4 x 0.71 = 6.35 at 16:00
    assert st.status == "met" and st.reached_at == "16:00"
    assert "6 MWh reached at 16:00" in st.events


@pytest.mark.parametrize("time,status", [("16:00", "on_track"), ("16:30", "behind"), ("17:00", "unreachable")])
def test_reserve_on_track_behind_unreachable(time, status):
    assert run_reserve(3.5, time, 0.0, 1).status == status


def test_reserve_missed_at_deadline_is_final():
    st = run_reserve(3.5, "15:00", 0.0, 12)  # holds until 18:00
    assert st.status == "missed"
    # Charging after the deadline does not undo it.
    tracker = GoalTracker({"goals": [G1]}, CFG)
    tracker.states["g1"] = st
    tracker.update(settle(time="18:00", soc=5.8, mw=3.0))
    assert tracker.states["g1"].status == "missed"


def test_reserve_discharge_below_target_after_deadline_is_violation():
    tracker = GoalTracker({"goals": [G1]}, CFG)
    tracker.update(settle(time="17:45", soc=6.2, mw=0.0))
    assert tracker.states["g1"].status == "met"
    tracker.update(settle(time="18:15", soc=6.2, mw=-3.0))
    st = tracker.states["g1"]
    assert st.status == "violated" and st.violated_at == "18:15"


def test_min_sell_violation_and_end_of_day_met():
    tracker = GoalTracker({"goals": [G2]}, CFG)
    tracker.update(settle(time="11:00", price=40, soc=5.0, mw=0.4))  # surplus stored, no export
    assert tracker.states["g2"].status == "on_track" and tracker.states["g2"].blocked_intervals == 1
    assert tracker.finalize()["g2"] == "met"

    tracker = GoalTracker({"goals": [G2]}, CFG)
    tracker.update(settle(time="11:00", price=40, soc=5.0, mw=0.0))  # 0.4 MW exported at 40
    assert tracker.states["g2"].status == "violated"
    assert tracker.finalize()["g2"] == "violated"


def test_goal_history_survives_a_goal_change():
    tracker = GoalTracker({"goals": [G1]}, CFG)
    tracker.update(settle(time="17:45", soc=6.2, mw=0.0))
    tracker.set_goals({"goals": [G1, G2]})
    assert tracker.states["g1"].reached_at == "18:00" and tracker.states["g2"].status == "on_track"


# ----------------------------------------------------------------- ledger
def test_ledger_settles_a_pipeline_cycle(tmp_path):
    log = tmp_path / "settlement.jsonl"
    ledger = DayLedger(GOALS, start_soc_mwh=3.5, log_path=str(log))
    cycle = run_cycle(FEATURES, GOALS, llm_call=lambda p: json.dumps(RESERVE["decision"]))
    rec = ledger.record(cycle, FEATURES, {"demand_mw": 4.6, "production_mw": 5.0})

    assert rec["profit_eur"] == -11.71 and rec["wear_eur"] == -0.54
    assert rec["goal_g1"] == "on_track" and rec["goal_g2"] == "on_track"
    assert rec["decision"]["source"] == "llm"
    assert rec["decision"]["explanation_for_operator"].startswith("Charging")
    assert "15:00 | charge 0.87 MW | buy 0.1175 MWh @ EUR 95" in rec["dashboard_line"]
    assert ledger.soc_mwh == rec["soc_after_mwh"]

    m = ledger.metrics()
    assert m["profit_today_eur"] == -11.71 and m["energy_bought_mwh"] == 0.1175
    assert m["decisions_llm"] == 1 and m["hard_goals_total"] == 2
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[0])["profit_eur"] == -11.71


def test_ledger_logs_forecast_errors_and_uses_its_own_soc():
    ledger = DayLedger(None, start_soc_mwh=5.0)
    features = {"time": "10:00", "price_now": 60, "demand_mw": 2.0, "production_mw": 4.6,
                "prod_low_mw": 4.3, "prod_high_mw": 4.9}
    cycle = {"decision": {"source": "fallback"}, "setpoint": {"battery_setpoint_mw": 2.6, "curtail_mw": 0.0}}
    rec = ledger.record(cycle, features, {"demand_mw": 2.2, "production_mw": 5.1})
    assert rec["soc_before_mwh"] == 5.0
    assert rec["forecast"] == {"demand_error_mw": 0.2, "production_error_mw": 0.5, "production_in_range": False}
    rec2 = ledger.record(cycle, {**features, "time": "10:15"})  # no actuals -> forecast, with a warning
    assert rec2["soc_before_mwh"] == rec["soc_after_mwh"] and rec2["warnings"]
    m = ledger.metrics()
    assert m["production_range_coverage"] == 0.5 and m["fallback_rate"] == 1.0


def test_goal_costs_compares_with_profit_only_run():
    profits = {(): 100.0, ("g1",): 70.0, ("g2",): 95.0, ("g1", "g2"): 60.0}

    def run_day(cfg):
        ledger = DayLedger(cfg)
        ledger.records.append({"profit_eur": profits[tuple(g["id"] for g in cfg["goals"])]})
        return ledger

    out = goal_costs(run_day, {"objective": "max_profit", "goals": [G1, G2]})
    assert out["total_cost_eur"] == 40.0
    assert out["per_goal"]["g1"]["cost_eur"] == 35.0  # 95 without g1 vs 60
    assert out["per_goal"]["g2"]["cost_eur"] == 10.0
