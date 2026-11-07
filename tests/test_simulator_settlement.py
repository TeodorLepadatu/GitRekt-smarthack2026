"""Simulator and settlement together: one physics (simulator), money on top (settlement)."""

import json

import pytest

from battery import BatteryConfig
from goal_parser import parse_goals
from pipeline import run_cycle
from prompt_assembly import SOLVED_EXAMPLES
from settlement import DayLedger, settle, settle_interval
from simulator import SiteSimulator

B = BatteryConfig()
RESERVE = next(ex for ex in SOLVED_EXAMPLES if ex["goal_config"])  # plan section 5, 15:00


def cmd(setpoint, curtail=0.0):
    return {"battery_setpoint_mw": setpoint, "curtail_mw": curtail}


CASES = {
    "plan example": dict(soc=3.5, sp=0.87, cur=0.0, dem=4.6, prod=5.0, price=95),
    "guard rounding at the limit": dict(soc=9.5, sp=2.1082, cur=4.1918, dem=1.2, prod=7.5, price=-8),
    "cloud: less production than forecast": dict(soc=9.5, sp=2.1082, cur=4.1918, dem=1.2, prod=5.0, price=-8),
    "discharge at the minimum": dict(soc=1.1, sp=-3.0, cur=0.0, dem=3.0, prod=1.0, price=150),
}


@pytest.mark.parametrize("c", CASES.values(), ids=CASES.keys())
def test_settlement_uses_exactly_the_simulators_physics(c):
    step = SiteSimulator(B, c["soc"]).step(cmd(c["sp"], c["cur"]), c["dem"], c["prod"])
    s = settle_interval(time="12:00", price_eur_mwh=c["price"], soc_before_mwh=c["soc"], battery_setpoint_mw=c["sp"],
                        curtail_mw=c["cur"], demand_mw=c["dem"], production_mw=c["prod"], cfg=B)
    assert s.soc_after_mwh == round(step.stored_after_mwh, 4)
    assert s.battery_mw == round(step.setpoint_applied_mw, 4)
    assert s.curtail_mw == round(step.curtailed_mw, 4)
    assert (s.grid_import_mwh, s.grid_export_mwh) == (round(step.grid_import_mwh, 4), round(step.grid_export_mwh, 4))
    assert s.site_clipped == step.clipped
    assert s == settle(step, c["price"], time="12:00", cfg=B)


def test_cloud_case_no_longer_buys_energy_it_threw_away():
    s = settle_interval(time="12:00", price_eur_mwh=-8, soc_before_mwh=9.5, battery_setpoint_mw=2.1082,
                        curtail_mw=4.1918, demand_mw=1.2, production_mw=5.0, cfg=B)
    assert s.grid_import_mwh == 0.0                     # was 0.625 MWh bought at a negative price
    assert s.curtail_mw == pytest.approx(5.0 - 1.2 - B.max_charge_mw(9.5), abs=1e-4)


def test_guard_rounding_is_not_a_site_clip():
    s = settle_interval(time="12:00", price_eur_mwh=-8, soc_before_mwh=9.5, battery_setpoint_mw=2.1082,
                        curtail_mw=4.1918, demand_mw=1.2, production_mw=7.5, cfg=B)
    assert not s.site_clipped and s.warnings == []


def test_money_from_a_step():
    step = SiteSimulator(B, 3.5).step(cmd(0.87), 4.6, 5.0, time="15:00")
    s = settle(step, 95, cfg=B)
    assert (s.time, s.energy_cost_eur, s.wear_eur, s.profit_eur) == ("15:00", -11.16, -0.54, -11.71)


def test_ledger_settles_the_simulators_step_without_running_physics_again():
    goals = parse_goals("Store 6 MWh by 18:00").config
    features = RESERVE["features"]
    cycle = run_cycle(features, goals, llm_call=lambda p: json.dumps(RESERVE["decision"]), battery=B)
    step = SiteSimulator(B, features["stored_mwh"]).step(cycle.setpoint, 4.7, 4.9)

    rec = DayLedger(goals, battery=B).record(cycle, features, step)
    assert rec["soc_after_mwh"] == round(step.stored_after_mwh, 4)
    assert rec["grid_import_mwh"] == round(step.grid_import_mwh, 4)
    assert rec["forecast"]["demand_error_mw"] == pytest.approx(0.1)     # forecast errors stay in settlement
    assert rec["forecast"]["production_error_mw"] == pytest.approx(-0.1)
    assert rec["time"] == "15:00"


def test_ledger_with_measurements_still_works_and_never_raises():
    ledger = DayLedger(None, battery=B, start_soc_mwh=5.0)
    cycle = {"decision": {"source": "fallback"}, "setpoint": cmd(1.0)}
    rec = ledger.record(cycle, {"time": "10:00", "price_now": 60}, {"demand_mw": -2.0, "production_mw": 3.0})
    assert rec["demand_mw"] == 0.0 and any("negative" in w for w in rec["warnings"])
    rec = ledger.record(cycle, {"time": "10:15", "price_now": 60, "stored_mwh": 12.0}, {"demand_mw": 1, "production_mw": 1})
    assert rec["soc_before_mwh"] == B.e_max and any("outside the battery" in w for w in rec["warnings"])


def test_a_day_loop_keeps_simulator_and_ledger_in_step():
    """Pipeline -> guard -> simulator -> ledger for two hours; one state of charge for everyone."""
    goals = parse_goals("Store 6 MWh by 18:00").config
    features = dict(RESERVE["features"])
    sim = SiteSimulator(B, features["stored_mwh"])
    ledger = DayLedger(goals, battery=B)
    for i in range(8):
        t = f"{15 + i // 4:02d}:{(i % 4) * 15:02d}"
        features.update(time=t, stored_mwh=sim.stored_mwh,
                        max_charge_mw=B.max_charge_mw(sim.stored_mwh),
                        max_discharge_mw=B.max_discharge_mw(sim.stored_mwh))
        cycle = run_cycle(features, goals, llm_call=lambda p: json.dumps(RESERVE["decision"]), battery=B)
        step = sim.step(cycle.setpoint, features["demand_mw"], features["production_mw"], time=t)
        rec = ledger.record(cycle, features, step)
        assert rec["soc_after_mwh"] == round(sim.stored_mwh, 4)
    assert ledger.metrics()["energy_bought_mwh"] == pytest.approx(sum(r.grid_import_mwh for r in sim.history), abs=1e-3)
    assert ledger.goals.states["g1"].status in ("met", "on_track")
