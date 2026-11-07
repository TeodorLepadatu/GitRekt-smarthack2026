"""Site simulator: battery physics, grid trade, curtailment, and the full cycle with the pipeline."""

import itertools
import json
import math

import pytest

from battery import BatteryConfig
from goal_parser import parse_goals
from pipeline import run_cycle
from prompt_assembly import SOLVED_EXAMPLES
from simulator import SimulatorError, SiteSimulator

B = BatteryConfig()  # 10 MWh, 3 MW, round trip 0.90, minimum 1 MWh
DT = B.interval_h
RESERVE = next(ex for ex in SOLVED_EXAMPLES if ex["goal_config"])  # plan section 5, 15:00


def cmd(setpoint, curtail=0.0):
    return {"battery_setpoint_mw": setpoint, "curtail_mw": curtail}


def test_plan_settlement_example():
    """Plan section 5: 3.5 MWh, charge 0.87 MW, surplus 0.4 MW -> 3.71 MWh, import 0.1175 MWh."""
    sim = SiteSimulator(B, stored_mwh=3.5)
    r = sim.step(cmd(0.87), demand_mw=4.6, production_mw=5.0, time="15:00")
    assert round(r.stored_after_mwh, 2) == 3.71
    assert r.grid_import_mwh == pytest.approx(0.1175)
    assert r.grid_export_mwh == 0.0
    assert r.battery_throughput_mwh == pytest.approx(0.87 * DT)   # x EUR 2.5 = EUR 0.54 wear
    assert not r.clipped and r.warnings == []
    assert sim.stored_mwh == r.stored_after_mwh


@pytest.mark.parametrize("stored, setpoint, demand, production, curtail", itertools.product(
    [1.0, 3.5, 9.8], [-3.0, -0.5, 0.0, 0.87, 3.0], [0.0, 2.0, 4.6], [0.0, 1.5, 7.5], [0.0, 2.0]))
def test_energy_always_balances(stored, setpoint, demand, production, curtail):
    r = SiteSimulator(B, stored).step(cmd(setpoint, curtail), demand, production)
    # production - curtailed - demand = battery + grid  (plan section 6, fix 4)
    grid_mw = (r.grid_export_mwh - r.grid_import_mwh) / DT
    assert production - r.curtailed_mw - demand == pytest.approx(r.setpoint_applied_mw + grid_mw)
    assert B.e_min_mwh - 1e-9 <= r.stored_after_mwh <= B.e_max + 1e-9
    assert abs(r.setpoint_applied_mw) <= B.p_max_mw + 1e-9
    assert r.grid_import_mwh == 0 or r.grid_export_mwh == 0
    assert 0 <= r.curtailed_mw <= min(curtail, production)   # only production is ever curtailed
    assert r.stored_after_mwh == pytest.approx(B.energy_after(stored, r.setpoint_applied_mw))


def test_discharge_sells_and_loses_efficiency():
    r = SiteSimulator(B, 5.0).step(cmd(-2.0), demand_mw=1.0, production_mw=1.0)
    assert r.grid_export_mwh == pytest.approx(2.0 * DT)
    assert r.stored_after_mwh == pytest.approx(5.0 - 2.0 * DT / B.eta_d)
    assert r.losses_mwh == pytest.approx(2.0 * DT / B.eta_d - 2.0 * DT)


def test_round_trip_returns_ninety_percent():
    sim = SiteSimulator(B, 5.0)
    charged = sim.step(cmd(2.0), 0.0, 0.0).grid_import_mwh          # bought from the grid
    gained = sim.stored_mwh - 5.0
    delivered = sim.step(cmd(-gained * B.eta_d / DT), 0.0, 0.0).grid_export_mwh
    assert sim.stored_mwh == pytest.approx(5.0)
    assert delivered / charged == pytest.approx(B.round_trip_eff)


@pytest.mark.parametrize("stored, setpoint, applied", [
    (9.8, 3.0, B.max_charge_mw(9.8)),           # almost full
    (10.0, 1.0, 0.0),                           # full
    (1.2, -3.0, -B.max_discharge_mw(1.2)),      # near the minimum
    (1.0, -1.0, 0.0),                           # at the minimum
    (5.0, 4.0, 3.0),                            # over rated power
    (5.0, -4.0, -3.0),
])
def test_battery_never_goes_past_its_limits(stored, setpoint, applied):
    r = SiteSimulator(B, stored).step(cmd(setpoint), 0.0, 0.0)
    assert r.clipped and r.setpoint_applied_mw == pytest.approx(applied)
    assert r.setpoint_requested_mw == setpoint
    assert any("clipped" in w for w in r.warnings)


def test_guard_rounding_is_not_reported_as_clipping():
    # the guard sends 2.1082 MW (4 decimals) for an exact limit of 2.10818... MW
    r = SiteSimulator(B, 9.5).step(cmd(2.1082), 0.0, 0.0)
    assert r.setpoint_applied_mw == pytest.approx(B.max_charge_mw(9.5))
    assert not r.clipped and r.warnings == []
    assert r.stored_after_mwh == pytest.approx(B.e_max)


def test_curtailment_only_removes_what_would_be_exported():
    # negative price, battery almost full (as the guard sends it): charge the limit, curtail the rest
    r = SiteSimulator(B, 9.5).step(cmd(2.1082, curtail=4.1918), demand_mw=1.2, production_mw=7.5)
    assert r.curtailed_mw == pytest.approx(4.1918, abs=1e-4)
    assert r.grid_export_mwh == pytest.approx(0.0, abs=1e-4)
    assert r.warnings == []
    # actual production came in lower than forecast: curtail less, never import because of it
    r = SiteSimulator(B, 9.5).step(cmd(2.1082, curtail=4.1918), demand_mw=1.2, production_mw=5.0)
    assert r.curtailed_mw == pytest.approx(5.0 - 1.2 - B.max_charge_mw(9.5))
    assert r.grid_import_mwh == pytest.approx(0.0, abs=1e-9)
    assert any("less surplus than forecast" in w for w in r.warnings)


def test_broken_command_means_idle():
    r = SiteSimulator(B, 5.0).step({"battery_setpoint_mw": float("nan")}, 2.0, 1.0)
    assert r.setpoint_applied_mw == 0.0 and r.curtailed_mw == 0.0
    assert r.grid_import_mwh == pytest.approx(1.0 * DT)
    assert len(r.warnings) == 2  # setpoint and curtail


@pytest.mark.parametrize("demand, production", [(-1, 2), (2, None), (float("inf"), 1), ("3", 1), (True, 1)])
def test_bad_measurements_raise(demand, production):
    with pytest.raises(SimulatorError):
        SiteSimulator(B, 5.0).step(cmd(0), demand, production)


@pytest.mark.parametrize("stored", [-0.1, 10.5, float("nan"), "5"])
def test_bad_starting_state_raises(stored):
    with pytest.raises(SimulatorError):
        SiteSimulator(B, stored)


def test_sensors_history_and_reset():
    sim = SiteSimulator(B)               # starts at the minimum
    assert sim.sensors() == {"stored_mwh": B.e_min_mwh}
    sim.step(cmd(1.0), 2.0, 3.0, time="00:00")
    assert sim.sensors() == {"stored_mwh": sim.stored_mwh, "demand_mw": 2.0, "production_mw": 3.0}
    assert [r.time for r in sim.history] == ["00:00"]
    assert sim.history[0].stored_after_mwh == sim.stored_mwh               # full precision is kept
    assert sim.history[0].to_dict()["stored_after_mwh"] == round(sim.stored_mwh, 6)
    sim.reset(4.0)
    assert sim.stored_mwh == 4.0 and sim.history == []


def test_accepts_the_guard_result_object():
    r = run_cycle(RESERVE["features"], RESERVE["goal_config"],
                  llm_call=lambda prompt: json.dumps(RESERVE["decision"]), battery=B)
    sim = SiteSimulator(B, RESERVE["features"]["stored_mwh"])
    out = sim.step(r.setpoint, 4.6, 5.0)
    assert out.setpoint_applied_mw == r.setpoint.battery_setpoint_mw == pytest.approx(0.87)
    # the guard's prediction and the simulator agree when nothing is clipped
    assert out.stored_after_mwh == pytest.approx(r.setpoint.expected_soc_after_mwh, abs=0.005)


def test_full_cycles_feed_the_new_state_back():
    """Pipeline -> guard -> simulator for one hour, each cycle starting from the simulator's state."""
    goals = parse_goals("Store 6 MWh by 18:00").config
    features = dict(RESERVE["features"])
    sim = SiteSimulator(B, features["stored_mwh"])
    for t in ("15:00", "15:15", "15:30", "15:45"):
        features.update(time=t, stored_mwh=sim.stored_mwh,
                        max_charge_mw=B.max_charge_mw(sim.stored_mwh),
                        max_discharge_mw=B.max_discharge_mw(sim.stored_mwh))
        cycle = run_cycle(features, goals, llm_call=lambda prompt: json.dumps(RESERVE["decision"]), battery=B)
        sim.step(cycle.setpoint, features["demand_mw"], features["production_mw"], time=t)
    assert [r.time for r in sim.history] == ["15:00", "15:15", "15:30", "15:45"]
    assert sim.stored_mwh > 3.5 + 4 * 0.8 * DT                    # charged every interval
    for prev, cur in zip(sim.history, sim.history[1:]):
        assert cur.stored_before_mwh == prev.stored_after_mwh   # state carried over
    assert all(math.isfinite(r.grid_import_mwh) for r in sim.history)


def test_never_curtails_more_than_was_produced():
    # discharging at full power while asked to curtail more than the panels make
    r = SiteSimulator(B, 8.0).step(cmd(-3.0, curtail=2.0), demand_mw=0.0, production_mw=1.0)
    assert r.curtailed_mw == pytest.approx(1.0)
    assert r.grid_export_mwh == pytest.approx(3.0 * DT)        # the battery's energy is sold, not thrown away
    assert any("more than was produced" in w for w in r.warnings)


def test_negative_curtail_is_reported():
    r = SiteSimulator(B, 5.0).step(cmd(0.0, curtail=-5.0), 1.0, 3.0)
    assert r.curtailed_mw == 0.0
    assert any("negative" in w for w in r.warnings)


def test_state_can_only_change_through_step_or_reset():
    sim = SiteSimulator(B, 5.0)
    with pytest.raises(AttributeError):
        sim.stored_mwh = 25.0
    assert sim.stored_mwh == 5.0


def test_failed_step_changes_nothing():
    sim = SiteSimulator(B, 5.0)
    with pytest.raises(SimulatorError):
        sim.step(cmd(1.0), -1.0, 1.0)
    assert sim.stored_mwh == 5.0 and sim.history == []


def test_bad_time_is_ignored_with_a_warning():
    r = SiteSimulator(B, 5.0).step(cmd(0.0), 1.0, 1.0, time=1500)
    assert r.time is None
    assert len(r.warnings) == 1
    assert SiteSimulator(B, 5.0).step(cmd(0.0), 1.0, 1.0, time="24:00").time is None


def test_to_dict_has_clean_numbers():
    d = SiteSimulator(B, 3.5).step(cmd(0.87), 4.6, 5.0).to_dict()
    assert d["grid_import_mwh"] == 0.1175
    assert d["stored_after_mwh"] == 3.706339
    assert all(len(repr(v).split(".")[-1]) <= 6 for v in d.values() if isinstance(v, float))
