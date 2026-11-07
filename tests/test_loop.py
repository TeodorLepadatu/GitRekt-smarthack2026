"""The clock: a whole day through feature builder -> pipeline -> simulator -> settlement."""

import json

import pytest

from battery import BatteryConfig
from feature_builder import build_features
from goal_parser import parse_goals
from loop import REASON_FEATURE_ERROR, DayInput, DayRunner, LoopError, demo_day
from loop.__main__ import main

B = BatteryConfig()
DAY = demo_day()
GOALS = parse_goals("Store 6 MWh by 18:00. Never sell below 50 EUR/MWh").config
REPLY = json.dumps({"reasoning": "r", "battery_fraction": 0.2, "curtail": False,
                    "confidence": "low", "explanation_for_operator": "e"})


def llm_off(prompt):
    raise RuntimeError("off")


def runner(**kw):
    kw.setdefault("llm_call", llm_off)
    kw.setdefault("start_soc_mwh", 4.0)
    return DayRunner(kw.pop("day", DAY), kw.pop("goals", GOALS), **kw)


def test_a_whole_day_runs_and_carries_the_battery_over():
    r = runner()
    records = r.run()
    assert r.done and r.now is None and len(records) == 96
    assert [rec["time"] for rec in records[:3]] == ["00:00", "00:15", "00:30"] and records[-1]["time"] == "23:45"
    for prev, cur in zip(records, records[1:]):
        assert cur["soc_before_mwh"] == prev["soc_after_mwh"]          # one battery for the whole day
        assert cur["features"]["stored_mwh"] == pytest.approx(prev["site"]["stored_after_mwh"], abs=1e-4)  # builder rounds to 4 decimals
    assert all(B.e_min_mwh - 1e-6 <= rec["soc_after_mwh"] <= B.e_max + 1e-6 for rec in records)
    assert all(rec["loop_warnings"] == [] for rec in records)


def test_books_match_the_simulator():
    r = runner()
    r.run()
    m = r.summary()["metrics"]
    assert m["decisions_total"] == 96
    assert m["energy_bought_mwh"] == pytest.approx(sum(s.grid_import_mwh for s in r.sim.history), abs=1e-3)
    assert m["energy_sold_mwh"] == pytest.approx(sum(s.grid_export_mwh for s in r.sim.history), abs=1e-3)
    assert m["soc_mwh"] == pytest.approx(r.sim.stored_mwh, abs=1e-4)


def test_actuals_differ_from_the_forecast_and_are_settled():
    r = runner()
    rec = r.step()
    assert rec["demand_mw"] == DAY.actuals[0]["demand_mw"]               # settled on what really happened
    assert rec["features"]["demand_mw"] == DAY.forecasts[0].demand_mw     # decided on the forecast
    assert rec["forecast"]["demand_error_mw"] == pytest.approx(DAY.actuals[0]["demand_mw"] - DAY.forecasts[0].demand_mw)


def test_reserve_goal_is_met_by_its_deadline():
    r = runner(goals=parse_goals("Store 6 MWh by 18:00").config, start_soc_mwh=1.0)
    r.run()
    assert r.summary()["final_goal_status"] == {"g1": "met"}


def test_with_an_llm_the_decisions_come_from_it():
    r = runner(llm_call=lambda p: REPLY)
    r.run(until="02:00")
    assert len(r.records) == 8
    assert {rec["decision"]["source"] for rec in r.records} == {"llm"}


def test_feature_builder_failure_falls_back_and_the_day_goes_on():
    def flaky(**kw):
        if kw["time"] == "00:30":
            raise ValueError("price feed down")
        return build_features(**kw)

    r = runner(feature_builder=flaky, llm_call=lambda p: REPLY)
    r.run(until="01:00")
    bad = r.records[2]
    assert bad["decision"]["source"] == "fallback"
    assert bad["decision"]["fallback_reason"] == REASON_FEATURE_ERROR
    assert "price feed down" in bad["loop_warnings"][0]
    assert r.records[3]["decision"]["source"] == "llm"                    # next interval is back to normal
    assert len(r.records) == 4


def test_feature_builder_failure_still_enforces_a_reserve_goal():
    # 17:00, 3 MWh short of the 18:00 target: even without features the guard must force charging
    r = runner(goals=parse_goals("Store 6 MWh by 18:00").config, start_soc_mwh=3.0, start_time="17:00",
               feature_builder=lambda **kw: (_ for _ in ()).throw(ValueError("down")))
    rec = r.step()
    assert rec["guard"]["battery_setpoint_mw"] > 0


def test_missing_or_broken_actuals_use_the_forecast():
    actuals = list(DAY.actuals)
    actuals[0], actuals[1] = None, {"demand_mw": -3, "production_mw": "x"}
    day = DayInput(DAY.prices, DAY.forecasts, actuals)
    r = runner(day=day)
    a, b = r.step(), r.step()
    assert a["demand_mw"] == DAY.forecasts[0].demand_mw and a["loop_warnings"] == []   # None = "use forecast"
    assert b["demand_mw"] == DAY.forecasts[1].demand_mw and len(b["loop_warnings"]) == 2


def test_start_mid_day_and_stop_early():
    r = runner(start_time="11:15")
    r.run(until="12:00")
    assert [rec["time"] for rec in r.records] == ["11:15", "11:30", "11:45"]
    assert r.now == "12:00" and not r.done and r.summary()["final_goal_status"] is None
    r.run(until="24:00")
    assert r.done and r.records[-1]["time"] == "23:45"


def test_goals_can_change_during_the_day():
    r = runner(goals=None)
    r.run(until="12:00")
    r.set_goals(parse_goals("Keep 3 MWh backup overnight").config)
    rec = r.step()
    assert "g1" in rec["goals"] and rec["features"]["goals"]


def test_on_interval_callback_sees_every_record():
    seen = []
    runner(on_interval=seen.append).run(until="03:00")
    assert [rec["time"] for rec in seen][-1] == "02:45" and len(seen) == 12


class FakeTime:
    """A clock that only moves when an interval runs (step_s) or the loop sleeps (+ overshoot)."""

    def __init__(self, step_s, overshoot_s=0.0):
        self.now, self.step_s, self.overshoot_s, self.sleeps = 0.0, step_s, overshoot_s, []

    def clock(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(round(s, 6))
        self.now += s + self.overshoot_s

    def on_interval(self, rec):
        self.now += self.step_s


def test_realtime_waits_for_each_slot():
    t = FakeTime(step_s=0.5)
    r = runner(on_interval=t.on_interval)
    r.run_realtime(speed=60, until="01:00", sleep=t.sleep, clock=t.clock)    # 15 s slots
    assert len(r.records) == 4 and t.sleeps == [14.5, 14.5, 14.5]            # no wait after the last


def test_realtime_does_not_drift():
    t = FakeTime(step_s=0.5, overshoot_s=0.2)                                 # every sleep runs 0.2 s long
    r = runner(on_interval=t.on_interval)
    r.run_realtime(speed=60, until="05:00", sleep=t.sleep, clock=t.clock)
    # 20 intervals end 19 slots after the start (+ the last interval and one overshoot), not 19 x 0.2 s late
    assert t.now == pytest.approx(19 * 15 + 0.5 + 0.2)


def test_realtime_records_an_overrun_and_does_not_rush_to_catch_up():
    t = FakeTime(step_s=20.0)                                                 # 20 s per interval, slot 1.5 s at 600x
    r = runner(on_interval=t.on_interval)
    r.run_realtime(speed=600, until="00:30", sleep=t.sleep, clock=t.clock)
    assert t.sleeps == [] and "longer than its 1.5 s slot" in r.records[0]["loop_warnings"][0]


def test_a_failing_callback_never_stops_the_day():
    def boom(rec):
        raise ConnectionError("dashboard down")

    r = runner(on_interval=boom)
    r.run(until="01:00")
    assert len(r.records) == 4
    assert "dashboard down" in r.records[0]["loop_warnings"][0]


def test_curtailment_keeps_the_min_sell_goal_despite_forecast_errors():
    r = runner()
    r.run()
    sold_below = [rec for rec in r.records if rec["features"]["price_now"] < 50 and rec["grid_export_mwh"] > 0]
    assert sold_below == []
    assert r.summary()["final_goal_status"]["g2"] == "met"


def test_step_after_the_day_is_over_raises():
    r = runner(start_time="23:45")
    r.step()
    with pytest.raises(LoopError):
        r.step()


@pytest.mark.parametrize("kw", [
    {"day": DayInput([50.0] * 95, [DAY.forecasts[0]] * 95)},     # not a whole day
    {"start_time": "10:07"},                                      # not an interval start
    {"start_time": "noon"},
])
def test_bad_day_setup_raises(kw):
    with pytest.raises(LoopError):
        runner(**kw)


def test_day_input_lengths_must_match():
    with pytest.raises(LoopError):
        DayInput([50.0] * 96, [DAY.forecasts[0]] * 95)
    with pytest.raises(LoopError):
        DayInput([50.0] * 96, [DAY.forecasts[0]] * 96, actuals=[None] * 10)


def test_demo_day_is_reproducible():
    assert demo_day(seed=3) == demo_day(seed=3)
    assert demo_day(seed=3).actuals != demo_day(seed=4).actuals
    assert min(DAY.prices) < 0 < max(DAY.prices)                 # negative midday prices and a peak


def test_command_line_model_implies_llm(monkeypatch, capsys):
    import llm

    class FakeAgent:                      # no Ollama needed
        def __init__(self, settings=None):
            assert settings.decision_model == "qwen3:4b"

        def __call__(self, prompt):
            return REPLY

    monkeypatch.setattr(llm, "DecisionAgent", FakeAgent)
    assert main(["--model", "qwen3:4b", "--quiet", "--until", "00:30"]) == 0
    assert "decisions: 2 LLM, 0 fallback" in capsys.readouterr().out


def test_command_line_runs_and_writes_the_log(tmp_path, capsys):
    out = tmp_path / "day.jsonl"
    assert main(["--goals", "Store 6 MWh by 18:00", "--start", "17:00", "--until", "18:00",
                 "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "17:45 |" in printed and "=== summary ===" in printed and "intervals 4/96" in printed
    assert len(out.read_text(encoding="utf-8").splitlines()) == 4
