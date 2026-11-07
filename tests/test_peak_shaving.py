"""Peak shaving: the grid import cap (max_import_mw) in the guard, the loop and the books.

Import convention (simulator/site.py): grid = production - curtailed - demand - battery,
import = max(0, -grid). With net = production - demand the guard projects
import = max(0, setpoint - net), so the cap allows any setpoint <= net + max_import_mw.
Battery setpoint > 0 = charging, < 0 = discharging.
"""

import json

import pytest

from backend import Backend
from battery import BatteryConfig
from config import Settings
from dashboard import metrics_payload
from feature_builder import SiteForecast
from goal_parser import GoalParser, GoalStore
from guard import guard
from loop import DayInput, DayRunner, LoopError, demo_day
from tests.test_dashboard_contract import _check_interface, contract  # noqa: F401  (types.ts reader, fixture)

B = BatteryConfig()


def dec(fraction, curtail=False):
    return {"battery_fraction": fraction, "curtail": curtail, "source": "llm"}


def state(**kw):
    s = {"time": "12:00", "stored_mwh": 6.0, "net_mw": -4.0, "price_now": 90.0, "production_mw": 1.0}
    s.update(kw)
    return s


def cap_rules(result):
    return [r for r in result.applied_rules if r.startswith("import_cap_")]


def llm_off(prompt):
    raise RuntimeError("off")


def llm_hold(prompt):
    return json.dumps({"reasoning": "r", "battery_fraction": 0.0, "curtail": False,
                       "confidence": "low", "explanation_for_operator": "hold"})


# --------------------------------------------------------------------------- guard


@pytest.mark.parametrize("fraction", [-1.0, -0.3, 0.0, 0.4, 1.0])
@pytest.mark.parametrize("net", [-4.0, -0.5, 0.0, 2.0])
def test_no_limit_leaves_the_guard_unchanged(fraction, net):
    """Without a cap the result is exactly what it was before the feature."""
    s = state(net_mw=net)
    plain = guard(dec(fraction), s).to_dict()
    explicit_none = guard(dec(fraction), s, max_import_mw=None).to_dict()
    assert plain == explicit_none
    assert plain["projected_import_mw"] is None
    assert not any(r.startswith("import_cap_") for r in plain["applied_rules"])


def test_import_over_the_limit_is_shaved_by_discharging():
    # 4 MW deficit, holding would import 4 MW; the cap is 2.5 MW -> discharge 1.5 MW.
    r = guard(dec(0.0), state(), max_import_mw=2.5)
    assert r.battery_setpoint_mw == pytest.approx(-1.5)
    assert "import_cap_discharge" in r.applied_rules and "import_cap_unmet" not in r.applied_rules
    assert r.projected_import_mw == pytest.approx(2.5)
    assert r.clipped


def test_charging_is_reduced_to_stay_under_the_limit():
    # 1 MW deficit; charging 3 MW would import 4 MW. Cap 2.5 -> charge at most 1.5 MW.
    r = guard(dec(1.0), state(net_mw=-1.0), max_import_mw=2.5)
    assert r.battery_setpoint_mw == pytest.approx(1.5)
    assert cap_rules(r) == ["import_cap_charge_limited"]
    assert r.projected_import_mw == pytest.approx(2.5)


def test_charging_from_a_surplus_is_limited_too():
    # 2 MW surplus, zero import allowed: charge only from the surplus.
    r = guard(dec(1.0), state(net_mw=2.0), max_import_mw=0.0)
    assert r.battery_setpoint_mw == pytest.approx(2.0)
    assert r.projected_import_mw == pytest.approx(0.0)


def test_within_the_limit_nothing_changes():
    base = guard(dec(-0.5), state())
    r = guard(dec(-0.5), state(), max_import_mw=3.0)   # import 4 - 1.5 = 2.5 <= 3
    assert r.battery_setpoint_mw == base.battery_setpoint_mw
    assert cap_rules(r) == ["import_cap_ok"]
    assert r.applied_rules[:-1] == base.applied_rules


def test_a_bigger_llm_discharge_is_kept():
    """The cap only ever lowers the setpoint; a deeper discharge the LLM asked for stands."""
    r = guard(dec(-1.0), state(), max_import_mw=2.5)
    assert r.battery_setpoint_mw == pytest.approx(-3.0)
    assert cap_rules(r) == ["import_cap_ok"]


def test_low_soc_cannot_meet_the_limit():
    stored = B.e_min_mwh + 0.05
    r = guard(dec(0.0), state(stored_mwh=stored), max_import_mw=2.0)
    max_d = B.max_discharge_mw(stored)
    assert r.battery_setpoint_mw == pytest.approx(-max_d, abs=1e-4)      # as much as SoC allows, no more
    assert r.expected_soc_after_mwh >= B.e_min_mwh - 1e-6
    assert "import_cap_discharge" in r.applied_rules and "import_cap_unmet" in r.applied_rules
    assert r.projected_import_mw > 2.0
    assert any("soc_min_limit" in w for w in r.warnings)


def test_power_limit_cannot_meet_the_limit():
    # 6 MW deficit, cap 2 MW would need a 4 MW discharge; P_max is 3 MW.
    r = guard(dec(0.0), state(net_mw=-6.0), max_import_mw=2.0)
    assert r.battery_setpoint_mw == pytest.approx(-B.p_max_mw)
    assert "import_cap_unmet" in r.applied_rules
    assert r.projected_import_mw == pytest.approx(3.0)


def test_reserve_goal_beats_the_import_cap():
    """Protection window of a hard reserve: no discharge below 6 MWh, whatever the import."""
    goals = {"goals": [{"id": "g1", "type": "reserve", "mwh": 6.0, "by": "18:00", "until": "23:00", "hard": True}]}
    r = guard(dec(0.0), state(time="19:00", stored_mwh=6.0), goals, max_import_mw=2.0)
    assert r.battery_setpoint_mw == pytest.approx(0.0)
    assert "import_cap_unmet" in r.applied_rules
    assert "import_cap_discharge" not in r.applied_rules
    assert any("goal_g1_blocked_discharge" in w for w in r.warnings)


def test_forced_charge_beats_the_import_cap():
    goals = {"goals": [{"id": "g1", "type": "reserve", "mwh": 9.0, "by": "13:00", "hard": True}]}
    r = guard(dec(0.0), state(stored_mwh=4.0), goals, max_import_mw=2.0)
    assert r.battery_setpoint_mw == pytest.approx(B.p_max_mw)          # the deadline wins
    assert "goal_g1_forced_charge" in r.applied_rules and "import_cap_unmet" in r.applied_rules


def test_missing_net_means_the_cap_is_not_checked():
    s = state()
    del s["net_mw"]
    r = guard(dec(0.0), s, max_import_mw=2.0)
    assert r.battery_setpoint_mw == 0.0
    assert cap_rules(r) == ["import_cap_unchecked"] and r.projected_import_mw is None


# --------------------------------------------------------------------------- the day


def flat_day(demand=4.0, actual_demand=5.0, production=0.0, price=90.0):
    """96 identical intervals; the actual demand is above the forecast."""
    f = SiteForecast(demand_mw=demand, production_mw=production, solar_mw=production, wind_mw=0.0,
                     prod_low_mw=production, prod_high_mw=production)
    return DayInput(prices=[price] * 96, forecasts=[f] * 96,
                    actuals=[{"demand_mw": actual_demand, "production_mw": production}] * 96, date="flat")


def run(day, limit, soc=9.0, goals=None):
    r = DayRunner(day, goals, start_soc_mwh=soc, llm_call=llm_off, max_import_mw=limit)
    r.run()
    return r


def test_no_limit_day_reports_peak_but_no_limit():
    r = run(demo_day(), None, soc=4.0)
    m = r.summary()["metrics"]
    assert m["import_limit_mw"] is None and m["import_limit_exceeded_intervals"] == 0
    assert m["peak_import_mw"] == pytest.approx(max(s.grid_import_mwh for s in r.sim.history) / B.interval_h,
                                                abs=1e-3)
    assert m["peak_import_mw_no_battery"] == pytest.approx(
        max(max(0.0, s.demand_mw - s.production_mw) for s in r.sim.history), abs=1e-3)
    assert not any(rule.startswith("import_cap_") for rec in r.records for rule in rec["guard"]["applied_rules"])
    assert all(rec["import_limit_exceeded"] is False for rec in r.records)


def test_no_limit_day_is_unchanged():
    """Passing max_import_mw=None runs exactly the same day as not passing it."""
    a = DayRunner(demo_day(), None, start_soc_mwh=4.0, llm_call=llm_off)
    b = DayRunner(demo_day(), None, start_soc_mwh=4.0, llm_call=llm_off, max_import_mw=None)
    a.run(), b.run()
    assert [x["battery_mw"] for x in a.records] == [x["battery_mw"] for x in b.records]
    assert a.ledger.profit_eur == b.ledger.profit_eur


def test_meter_check_shaves_the_forecast_error():
    # The LLM holds. Forecast 4 MW deficit -> guard discharges 1 MW; actual 5 MW -> the loop 2 MW.
    r = DayRunner(flat_day(), None, start_soc_mwh=9.0, llm_call=llm_hold, max_import_mw=3.0)
    rec = r.step()
    assert rec["guard"]["battery_setpoint_mw"] == pytest.approx(-2.0, abs=1e-3)   # the record shows the shave
    assert rec["grid_import_mw"] == pytest.approx(3.0, abs=1e-3)
    assert rec["battery_mw"] == pytest.approx(-2.0, abs=1e-3)
    assert "import_cap_realtime" in rec["guard"]["applied_rules"]
    assert "import_cap_discharge" in rec["guard"]["applied_rules"]
    assert rec["import_limit_exceeded"] is False and rec["import_limit_mw"] == 3.0


def test_when_the_battery_runs_out_the_limit_is_unmet_and_counted():
    r = run(flat_day(), 3.0)
    recs = r.records
    m = r.summary()["metrics"]
    over = [x for x in recs if x["import_limit_exceeded"]]
    assert over and m["import_limit_exceeded_intervals"] == len(over)
    assert all("import_cap_unmet" in x["guard"]["applied_rules"] for x in over)
    assert all(x["soc_after_mwh"] >= B.e_min_mwh - 1e-6 for x in recs)            # never below the floor
    assert recs[0]["import_limit_exceeded"] is False                               # it held while it could
    assert m["peak_import_mw"] == pytest.approx(5.0, abs=1e-3)                    # empty battery: full demand
    assert m["peak_import_mw_no_battery"] == pytest.approx(5.0)
    assert "IMPORT" in over[0]["dashboard_line"]


def test_demo_day_with_a_limit_imports_less():
    limit = 0.5
    free, capped = run(demo_day(), None, soc=4.0), run(demo_day(), limit, soc=4.0)

    def over(r):
        return sum(1 for x in r.records if x["grid_import_mw"] > limit + 1e-3)

    assert over(free) > over(capped) > 0      # fewer intervals over; the rest: battery already empty
    m = capped.summary()["metrics"]
    assert m["import_limit_mw"] == limit and m["import_limit_exceeded_intervals"] == over(capped)
    assert m["energy_bought_mwh"] < free.summary()["metrics"]["energy_bought_mwh"]
    # Morning: the fallback charges on cheap power, the cap trims it to what keeps the import at the limit.
    trimmed = [x for x in capped.records if "import_cap_charge_limited" in x["guard"]["applied_rules"]]
    assert trimmed and all(x["grid_import_mw"] <= limit + 1e-3 and x["battery_mw"] >= 0 for x in trimmed)


def test_a_bad_limit_is_refused():
    with pytest.raises(LoopError):
        DayRunner(demo_day(), None, llm_call=llm_off, max_import_mw=-1)


# --------------------------------------------------------------------------- config, contract, backend


def test_settings_read_the_limit(monkeypatch):
    monkeypatch.delenv("ENERGY_AGENT_MAX_IMPORT_MW", raising=False)
    assert Settings().max_import_mw is None
    monkeypatch.setenv("ENERGY_AGENT_MAX_IMPORT_MW", "2.5")
    assert Settings().max_import_mw == 2.5
    monkeypatch.setenv("ENERGY_AGENT_MAX_IMPORT_MW", "off")
    assert Settings().max_import_mw is None
    monkeypatch.setenv("ENERGY_AGENT_MAX_IMPORT_MW", "-1")
    with pytest.raises(ValueError):
        Settings()


def test_metrics_payload_carries_the_peak_fields(contract):
    r = DayRunner(flat_day(), None, start_soc_mwh=9.0, llm_call=llm_off, max_import_mw=3.0)
    r.run(until="06:00")
    m = metrics_payload(r)
    _check_interface(m, "Metrics", contract)
    assert m["import_limit_mw"] == 3.0
    assert isinstance(m["import_limit_exceeded_intervals"], int)
    assert m["peak_import_mw"] >= 3.0 and m["peak_import_time"] is not None


def test_backend_passes_the_limit_to_the_day():
    store = GoalStore(GoalParser(None))
    b = Backend(store, llm_call=llm_off, max_import_mw=2.0)
    try:
        b.advance(4)
        m = b.metrics()
        assert m["import_limit_mw"] == 2.0 and b._runner.max_import_mw == 2.0
    finally:
        b.close()
