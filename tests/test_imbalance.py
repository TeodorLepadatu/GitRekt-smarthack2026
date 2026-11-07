"""Imbalance: what deviating from the promised grid position costs.

Small synthetic days, so these are fast and need no models or CSV files. The
prices are the ones ``synthetic_out/prices.csv`` really holds around midday on
2026-06-15 (day-ahead 105.29, short 178.41, long 62.65).
"""
from __future__ import annotations

import pytest

from battery import BatteryConfig
from feature_builder import SiteForecast
from goal_parser import parse_goals
from loop import DayInput, DayRunner, LoopError
from loop.day import balancing_setpoint_mw
from settlement import ImbalancePrices, imbalance_cost, scheduled_position_mwh
from settlement.settle import settle_interval

def no_llm(prompt: str) -> str:
    """These tests check arithmetic, not the model: always take the rule-based path.

    ``run_cycle`` defaults ``llm_call`` to a real ``DecisionAgent``, so without
    this every interval would make an Ollama call.
    """
    raise RuntimeError("no LLM in these tests")


DA = 105.29
PRICES = ImbalancePrices(short_eur_mwh=178.41, long_eur_mwh=62.65)
CFG = BatteryConfig()
N = 96


# --------------------------------------------------------------------------- #
# the price of a deviation


def test_being_short_costs_more_than_the_plan():
    """Energy promised and not delivered is bought back above the day-ahead price."""
    cost = imbalance_cost(-0.175, DA, PRICES)
    assert cost == pytest.approx(-0.175 * (178.41 - DA))
    assert cost < 0


def test_being_long_earns_less_than_the_plan():
    cost = imbalance_cost(+0.175, DA, PRICES)
    assert cost == pytest.approx(0.175 * (62.65 - DA))
    assert cost < 0


def test_delivering_the_plan_is_free():
    assert imbalance_cost(0.0, DA, PRICES) == 0.0
    assert imbalance_cost(1e-12, DA, PRICES) == 0.0


def test_no_prices_means_no_charge():
    assert imbalance_cost(-5.0, DA, None) == 0.0


@pytest.mark.parametrize("deviation", [-2.0, -0.1, 0.1, 2.0])
def test_deviating_never_pays_better_than_the_plan(deviation):
    """Synthetic prices can cross the day-ahead price; a penalty must not become a bonus."""
    crossed = ImbalancePrices(short_eur_mwh=DA - 50, long_eur_mwh=DA + 50)
    assert imbalance_cost(deviation, DA, crossed) == pytest.approx(0.0)
    assert imbalance_cost(deviation, DA, PRICES) <= 0.0


def test_a_bigger_deviation_costs_more():
    costs = [imbalance_cost(-d, DA, PRICES) for d in (0.1, 0.5, 1.0)]
    assert costs[0] > costs[1] > costs[2]


# --------------------------------------------------------------------------- #
# the promise


def test_scheduled_position_is_the_forecast_balance():
    s = scheduled_position_mwh(forecast_demand_mw=2.4, forecast_production_mw=5.7,
                               battery_mw=0.0, cfg=CFG)
    assert s == pytest.approx((5.7 - 2.4) * 0.25)


def test_charging_lowers_the_promise():
    hold = scheduled_position_mwh(forecast_demand_mw=2.4, forecast_production_mw=5.7,
                                  battery_mw=0.0, cfg=CFG)
    charge = scheduled_position_mwh(forecast_demand_mw=2.4, forecast_production_mw=5.7,
                                    battery_mw=1.0, cfg=CFG)
    assert charge == pytest.approx(hold - 1.0 * 0.25)


def test_balancing_setpoint_is_the_promise_plus_the_error():
    """Holding the promise and absorbing the forecast error are the same action."""
    scheduled = scheduled_position_mwh(forecast_demand_mw=2.4, forecast_production_mw=5.7,
                                       battery_mw=0.0, cfg=CFG)
    # 0.7 MW less production than forecast -> discharge 0.7 MW to hold the promise
    assert balancing_setpoint_mw(scheduled_mwh=scheduled, demand_mw=2.4, production_mw=5.0,
                                 curtail_mw=0.0, cfg=CFG) == pytest.approx(-0.7)
    # production exactly as forecast -> nothing to do
    assert balancing_setpoint_mw(scheduled_mwh=scheduled, demand_mw=2.4, production_mw=5.7,
                                 curtail_mw=0.0, cfg=CFG) == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# settling one interval


def _settle(production_mw, forecast_production_mw, **kw):
    return settle_interval(
        time="11:00", price_eur_mwh=DA, soc_before_mwh=5.0, battery_setpoint_mw=0.0,
        curtail_mw=0.0, demand_mw=2.4, production_mw=production_mw, cfg=CFG,
        forecast_demand_mw=2.4, forecast_production_mw=forecast_production_mw, **kw)


def test_a_perfect_forecast_is_never_charged():
    s = _settle(5.0, 5.0, imbalance=PRICES)
    assert s.deviation_mwh == pytest.approx(0.0)
    assert s.imbalance_eur == 0.0


def test_a_shortfall_is_charged_at_the_short_price():
    s = _settle(5.0, 5.7, imbalance=PRICES)
    assert s.scheduled_grid_mwh == pytest.approx(0.825)
    assert s.deviation_mwh == pytest.approx(-0.175)
    assert s.imbalance_eur == pytest.approx(-0.175 * (178.41 - DA), abs=0.01)
    assert s.profit_eur == pytest.approx(s.energy_cost_eur + s.wear_eur + s.imbalance_eur, abs=0.01)


def test_without_prices_nothing_changes():
    """The old behaviour has to survive untouched: profit is energy + wear."""
    s = _settle(5.0, 5.7)
    assert (s.imbalance_eur, s.deviation_mwh, s.scheduled_grid_mwh) == (0.0, 0.0, 0.0)
    assert s.profit_eur == pytest.approx(s.energy_cost_eur + s.wear_eur, abs=0.01)


def test_the_counterfactual_bears_its_own_imbalance():
    """Without a battery the same error happens and nothing can absorb it."""
    priced = _settle(5.0, 5.7, imbalance=PRICES)
    plain = _settle(5.0, 5.7)
    assert priced.no_battery_profit_eur < plain.no_battery_profit_eur


# --------------------------------------------------------------------------- #
# a day


def _day(*, forecast_production=5.0, actual_production=5.0, demand=2.4, price=DA, imbalance=PRICES):
    """A flat 96-interval day; the gap between the two productions is the forecast error."""
    forecast = SiteForecast(demand_mw=demand, production_mw=forecast_production,
                            solar_mw=forecast_production, wind_mw=0.0,
                            prod_low_mw=max(0.0, forecast_production - 1), prod_high_mw=forecast_production + 1)
    return DayInput(
        prices=[price] * N,
        forecasts=[forecast] * N,
        actuals=[{"demand_mw": demand, "production_mw": actual_production}] * N,
        imbalance=None if imbalance is None else [imbalance] * N,
        date="2026-06-15",
    )


def test_day_rejects_the_wrong_number_of_imbalance_prices():
    with pytest.raises(LoopError, match="imbalance prices"):
        DayInput(prices=[DA] * N, forecasts=[SiteForecast(demand_mw=1.0)] * N, imbalance=[PRICES] * 10)


def test_a_day_without_imbalance_runs_exactly_as_before():
    runner = DayRunner(_day(imbalance=None), None, start_soc_mwh=5.0, llm_call=no_llm)
    runner.run(until="02:00")
    for rec in runner.records:
        assert rec["imbalance_eur"] == 0.0
        assert rec["profit_eur"] == pytest.approx(rec["energy_cost_eur"] + rec["wear_eur"], abs=0.01)
    assert runner.ledger.metrics()["imbalance_eur"] == 0.0


def test_the_battery_absorbs_the_error_and_is_not_charged():
    """With power left in the needed direction, the deviation is held at zero.

    At this price the rule-based policy discharges at full power, so there is no
    room to discharge *harder*; a surplus surprise is absorbed by discharging
    less, which it can do. ``test_a_full_power_battery_cannot_absorb_either``
    is the other side of this.
    """
    runner = DayRunner(_day(forecast_production=4.3, actual_production=5.0), None,
                       start_soc_mwh=5.0, llm_call=no_llm)
    runner.run(until="02:00")
    assert runner.records, "the day must produce records"
    for rec in runner.records:
        assert abs(rec["deviation_mwh"]) < 1e-6, f"{rec['time']} deviated: {rec['deviation_mwh']}"
        assert rec["imbalance_eur"] == 0.0


def test_the_battery_earns_its_keep_by_absorbing_the_error():
    """The no-battery case pays the penalty, so the battery's value shows up."""
    runner = DayRunner(_day(forecast_production=4.3, actual_production=5.0), None,
                       start_soc_mwh=5.0, llm_call=no_llm)
    runner.run(until="02:00")
    for rec in runner.records:
        assert rec["value_added_eur"] > 0, f"{rec['time']} added nothing"


def test_a_full_power_battery_cannot_absorb_either():
    """Already discharging at 3 MW, it cannot discharge harder to cover a shortfall.

    The power limit binds just as the state of charge does, and the leftover
    deviation is charged for. This is what makes keeping headroom worth money.
    """
    runner = DayRunner(_day(forecast_production=5.0, actual_production=4.3), None,
                       start_soc_mwh=5.0, llm_call=no_llm)
    rec = runner.step()
    assert rec["guard"]["battery_setpoint_mw"] == pytest.approx(-CFG.p_max_mw)
    assert rec["deviation_mwh"] == pytest.approx(-0.175)
    assert rec["imbalance_eur"] < 0


def test_an_empty_battery_cannot_absorb_and_the_deviation_is_charged():
    """No stored energy, so a shortfall cannot be covered: the penalty lands."""
    runner = DayRunner(_day(forecast_production=5.0, actual_production=4.3), None,
                       start_soc_mwh=CFG.e_min_mwh, llm_call=no_llm)
    runner.run(until="00:30")
    first = runner.records[0]
    assert first["deviation_mwh"] < -1e-6, "an empty battery must leave a deviation"
    assert first["imbalance_eur"] < 0, "and that deviation must be paid for"


def test_balancing_never_breaks_a_hard_goal():
    """The adjusted setpoint goes back through the guard, so a reserve still holds."""
    goals = parse_goals("Store 6 MWh by 18:00", battery_capacity_mwh=10).config
    # A shortfall all day would have the battery discharging to hold the promise.
    runner = DayRunner(_day(forecast_production=5.0, actual_production=4.0), goals,
                       start_soc_mwh=4.0, llm_call=no_llm)
    runner.run()
    assert runner.summary()["final_goal_status"] == {"g1": "met"}


def test_metrics_total_the_imbalance_and_the_drift():
    runner = DayRunner(_day(forecast_production=5.0, actual_production=4.3), None,
                       start_soc_mwh=CFG.e_min_mwh, llm_call=no_llm)
    runner.run(until="03:00")
    m = runner.ledger.metrics()
    assert m["imbalance_eur"] == pytest.approx(sum(r["imbalance_eur"] for r in runner.records), abs=0.01)
    assert m["deviation_mwh"] == pytest.approx(sum(abs(r["deviation_mwh"]) for r in runner.records), abs=1e-6)
    assert m["imbalance_eur"] < 0 and m["deviation_mwh"] > 0


def test_a_missing_forecast_does_not_stop_the_interval():
    """No forecast means no promise to measure against; the day must still run."""
    day = DayInput(prices=[DA] * N, forecasts=[{}] * N,
                   actuals=[{"demand_mw": 2.0, "production_mw": 3.0}] * N,
                   imbalance=[PRICES] * N)
    runner = DayRunner(day, None, start_soc_mwh=5.0, llm_call=no_llm)
    runner.run(until="00:30")
    rec = runner.records[0]
    assert rec["imbalance_eur"] == 0.0
    assert any("could not be priced" in w for w in rec["loop_warnings"])


def test_a_gap_in_the_imbalance_prices_is_not_charged():
    day = _day()
    holes = list(day.imbalance)
    holes[0] = None
    runner = DayRunner(DayInput(prices=day.prices, forecasts=day.forecasts, actuals=day.actuals,
                                imbalance=holes, date=day.date), None,
                       start_soc_mwh=5.0, llm_call=no_llm)
    runner.run(until="00:30")
    assert runner.records[0]["imbalance_eur"] == 0.0
