"""Dashboard backend: every route of frontend/README.md's API contract, on the real loop."""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from backend import Backend, create_app
from goal_parser import GoalParser, GoalStore
from dashboard import interval_payload
from settlement import GoalTracker
from tests.test_dashboard_contract import _check_interface, contract  # noqa: F401  (types.ts reader, fixture)

GOALS = ["Maximise profit", "Store 6 MWh by 18:00", "Never sell below 50 EUR/MWh"]
REPLY = json.dumps({"reasoning": "r", "battery_fraction": 0.2, "curtail": False,
                    "confidence": "low", "explanation_for_operator": "e"})

INTERVAL_KEYS = {"time", "price_eur_mwh", "break_even", "demand_mw", "production_mw", "solar_mw", "wind_mw",
                 "soc_mwh", "decision", "guard", "settlement", "control"}
FORECAST_KEYS = {"time", "price_eur_mwh", "break_even", "demand_mw", "production_mw", "solar_mw", "wind_mw"}


def llm_off(prompt):
    raise RuntimeError("off")


@pytest.fixture
def setup():
    store = GoalStore(GoalParser(None))
    store.set(GOALS)
    backend = Backend(store, llm_call=llm_off)
    client = TestClient(create_app(backend, heartbeat_s=0.2))
    yield backend, client
    backend.close()


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_state_before_the_first_interval(setup):
    _, c = setup
    s = c.get("/api/state").json()
    assert s == {"capacity_mwh": 10.0, "max_power_mw": 3.0, "profit_today_eur": 0,
                 "current": None, "sim": {"running": False, "speed": 0.0}}


def test_day_has_96_slots_decided_ones_first(setup):
    b, c = setup
    b.advance(10)
    d = c.get("/api/day").json()
    assert d["now"] == "02:15" and len(d["intervals"]) == 96
    decided, future = d["intervals"][:10], d["intervals"][10:]
    assert all(set(s) == INTERVAL_KEYS for s in decided)
    assert all(set(s) == FORECAST_KEYS for s in future)           # forecast only, no decision yet
    assert [s["time"] for s in d["intervals"][9:11]] == ["02:15", "02:30"]


def test_interval_shape_matches_the_contract(setup):
    b, c = setup
    b.advance(1)
    iv = c.get("/api/state").json()["current"]
    assert set(iv["decision"]) == {"battery_fraction", "curtail", "source", "confidence", "reasoning",
                                   "explanation_for_operator", "fallback_reason"}
    assert set(iv["guard"]) == {"battery_setpoint_mw", "curtail_mw", "clipped", "applied_rules"}
    assert set(iv["settlement"]) == {"grid_import_mwh", "grid_export_mwh", "energy_cost_eur", "wear_eur",
                                     "profit_eur", "no_battery_profit_eur", "value_added_eur",
                                     "deviation_mwh", "imbalance_eur"}
    assert iv["decision"]["source"] == "fallback" and iv["decision"]["fallback_reason"]


def test_soc_is_carried_over_and_metrics_add_up(setup):
    b, c = setup
    ivs = b.advance(20)
    m = c.get("/api/metrics").json()
    assert m["decisions_total"] == 20 and m["decisions_fallback"] == 20
    assert m["profit_today_eur"] == pytest.approx(sum(iv["settlement"]["profit_eur"] for iv in ivs), abs=0.05)
    assert c.get("/api/state").json()["profit_today_eur"] == m["profit_today_eur"]
    assert ivs[-1]["soc_mwh"] == pytest.approx(b._runner.sim.stored_mwh, abs=1e-4)


def test_goals_list_with_status(setup):
    b, c = setup
    b.advance(46)                                   # to 11:15, past the noon charging
    goals = c.get("/api/goals").json()
    assert [(g["id"], g["type"]) for g in goals] == [("objective", "max_profit"), ("g1", "reserve"),
                                                     ("g2", "min_sell_price")]
    g1 = goals[1]
    assert (g1["mwh"], g1["by"], g1["status"]) == (6.0, "18:00", "met") and g1["reached_at"]
    assert goals[2]["eur_per_mwh"] == 50


def test_vague_goal_asks_back_and_changes_nothing(setup):
    _, c = setup
    r = c.post("/api/goals", json={"text": "keep some energy for later"}).json()
    assert "How much energy" in r["question"]
    assert [g["id"] for g in r["goals"]] == ["objective", "g1", "g2"]


def test_add_goal_applies_from_the_next_interval(setup):
    b, c = setup
    r = c.post("/api/goals", json={"text": "Keep 2 MWh backup overnight"}).json()
    assert "question" not in r and r["goals"][-1]["by"] == "22:00"
    b.wait_idle()
    iv = b.advance(1)[0]
    assert b._runner.goal_config.to_dict()["goals"][-1]["until"] == "06:00"
    assert iv["time"] == "00:00"


@pytest.mark.parametrize("body", [{}, {"text": ""}, {"text": "x" * 3000}])
def test_bad_goal_requests(setup, body):
    _, c = setup
    assert c.post("/api/goals", json=body).status_code == 422


def test_delete_goal_and_history_follows_the_goal_not_the_id(setup):
    b, c = setup
    c.post("/api/goals", json={"text": "Keep 2 MWh backup overnight"})
    b.wait_idle()
    b.advance(4)
    assert c.delete("/api/goals/g2").status_code == 200             # the min-price goal
    b.wait_idle()
    goals = c.get("/api/goals").json()
    assert [(g["id"], g["type"]) for g in goals] == [("objective", "max_profit"), ("g1", "reserve"), ("g2", "reserve")]
    assert goals[2]["by"] == "22:00"                                 # the overnight reserve, renumbered
    assert c.delete("/api/goals/nope").status_code == 404


def test_goal_tracker_keeps_history_when_ids_are_renumbered():
    g1 = {"id": "g1", "type": "reserve", "mwh": 6.0, "by": "18:00", "until": "18:00"}
    g2 = {"id": "g2", "type": "min_sell_price", "eur_per_mwh": 50}
    g3 = {"id": "g3", "type": "reserve", "mwh": 2.0, "by": "22:00", "until": "06:00"}
    t = GoalTracker({"goals": [g1, g2, g3]})
    t.states["g2"].status, t.states["g3"].status = "violated", "met"
    t.set_goals({"goals": [g1, {**g3, "id": "g2"}]})                 # g2 deleted, g3 renumbered to g2
    assert (t.states["g2"].type, t.states["g2"].status) == ("reserve", "met")


def test_run_pause_and_day_end(setup):
    b, c = setup
    assert c.post("/api/sim/run", json={"speed": 90000}).json() == {"running": True, "speed": 90000.0}
    assert wait_for(lambda: c.get("/api/day").json()["now"] == "23:45")      # 10 ms per interval
    assert c.get("/api/state").json()["sim"] == {"running": False, "speed": 0.0}   # stops at the end of the day
    goals = {g["id"]: g["status"] for g in c.get("/api/goals").json()}
    assert goals["g2"] in ("met", "violated")                                     # finalised at the end


def test_pause_stops_the_clock(setup):
    b, c = setup
    c.post("/api/sim/run", json={"speed": 9000})            # 100 ms per interval
    assert wait_for(lambda: len(b._snap.records) >= 2)
    c.post("/api/sim/run", json={"speed": 0})
    n = len(b._snap.records)
    time.sleep(0.4)
    assert len(b._snap.records) <= n + 1                  # at most the interval already running


def test_reset_starts_the_day_again(setup):
    b, c = setup
    b.advance(8)
    assert c.post("/api/sim/reset").json() == {"running": False, "speed": 0.0}
    assert c.get("/api/day").json()["now"] is None and c.get("/api/state").json()["current"] is None
    assert c.get("/api/metrics").json()["decisions_total"] == 0


def test_running_again_after_the_day_starts_a_new_one(setup):
    b, c = setup
    b.advance(96)
    c.post("/api/sim/run", json={"speed": 90000})
    assert wait_for(lambda: (c.get("/api/day").json()["now"] or "") not in ("", "23:45")
                    or c.get("/api/metrics").json()["decisions_total"] < 96)


def test_advance_refuses_while_running(setup):
    b, c = setup
    c.post("/api/sim/run", json={"speed": 1})
    with pytest.raises(RuntimeError):
        b.advance(1)


@pytest.mark.parametrize("speed", [-1, "fast"])
def test_bad_speed(setup, speed):
    _, c = setup
    assert c.post("/api/sim/run", json={"speed": speed}).status_code == 422


def test_stream_sends_each_new_interval(setup):
    b, _ = setup
    received = []
    events = b.subscribe(heartbeat_s=0.2)
    t = threading.Thread(target=lambda: received.append(next(i for i in events if i is not None)))
    t.start()
    time.sleep(0.1)
    b.advance(1)
    t.join(timeout=3)
    assert received and received[0]["time"] == "00:00"


def test_stream_route_speaks_sse_and_lets_go_of_closed_clients(setup):
    """On a real uvicorn server: the TestClient never reports a disconnect, a browser does."""
    import httpx
    import uvicorn

    b, _ = setup
    server = uvicorn.Server(uvicorn.Config(create_app(b, heartbeat_s=0.2), host="127.0.0.1", port=0,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    assert wait_for(lambda: server.started)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        threading.Timer(0.5, lambda: b.advance(1)).start()
        lines = []
        with httpx.stream("GET", f"http://127.0.0.1:{port}/api/stream", timeout=5) as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            assert wait_for(lambda: len(b._listeners) == 1)
            for line in r.iter_lines():
                lines.append(line)
                if line.startswith("data:"):
                    break
        assert "event: cycle" in lines
        assert json.loads(lines[-1][len("data: "):])["time"] == "00:00"
        assert wait_for(lambda: len(b._listeners) == 0)       # the closed client was let go
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    assert not thread.is_alive()                              # shuts down with no client hanging


def test_with_an_llm_decisions_come_from_it():
    store = GoalStore(GoalParser(None))
    store.set(GOALS)
    b = Backend(store, llm_call=lambda p: REPLY)
    try:
        assert {iv["decision"]["source"] for iv in b.advance(4)} == {"llm"}
    finally:
        b.close()


def test_health(setup):
    _, c = setup
    assert c.get("/api/health").json() == {"status": "ok"}


def test_objective_is_listed_only_when_set_and_can_be_deleted(setup):
    b, c = setup
    assert c.get("/api/goals").json()[0]["id"] == "objective"
    assert c.delete("/api/goals/objective").status_code == 200
    b.wait_idle()
    goals = c.get("/api/goals").json()
    assert [g["id"] for g in goals] == ["g1", "g2"]                      # gone, like in the frontend mock
    assert b.goals.sentences == GOALS[1:]
    assert c.delete("/api/goals/objective").status_code == 404


def test_objective_inside_a_goal_sentence_cannot_be_deleted_alone():
    store = GoalStore(GoalParser(None))
    store.set(["Maximise profit and store 6 MWh by 18:00"])
    b = Backend(store, llm_call=llm_off)
    try:
        c = TestClient(create_app(b))
        assert c.delete("/api/goals/objective").status_code == 404      # would also delete the 6 MWh goal
        assert len(c.get("/api/goals").json()) == 2
    finally:
        b.close()


def test_energy_flow_adds_up(setup):
    """Solar + wind shown on the dashboard equal the actual production."""
    b, c = setup
    ivs = b.advance(96)
    for iv in ivs:
        assert iv["solar_mw"] + iv["wind_mw"] == pytest.approx(iv["production_mw"], abs=1e-3)
        assert iv["solar_mw"] >= 0 and iv["wind_mw"] >= 0


def test_every_response_matches_types_ts(setup, contract):
    """The live API, checked against frontend/src/api/types.ts in both directions."""
    b, c = setup
    _check_interface(c.get("/api/state").json(), "StateSnapshot", contract)       # before the first interval
    c.post("/api/goals", json={"text": "Keep 2 MWh backup overnight"})
    b.wait_idle()
    events = []
    b.add_listener(lambda event, data: event == "cycle" and events.append(data))
    b.advance(50)
    _check_interface(c.get("/api/state").json(), "StateSnapshot", contract)
    _check_interface(c.get("/api/metrics").json(), "Metrics", contract)
    day = c.get("/api/day").json()
    _check_interface(day, "DayResponse", contract)
    for slot in day["intervals"][:50]:
        _check_interface(slot, "Interval", contract)
    for slot in day["intervals"][50:]:
        _check_interface(slot, "ForecastInterval", contract)
    for goal in c.get("/api/goals").json():
        _check_interface(goal, "Goal", contract)
    _check_interface(c.post("/api/goals", json={"text": "keep some"}).json(), "AddGoalResponse", contract)
    _check_interface(events[0], "Interval", contract)                              # the SSE "cycle" event


def test_interval_shows_what_the_site_actually_did():
    """The export limiter can switch off more than the guard asked for; the dashboard shows what happened."""
    record = {"guard": {"battery_setpoint_mw": 2.1082, "curtail_mw": 4.19, "clipped": False, "applied_rules": []},
              "site": {"setpoint_applied_mw": 2.1082, "curtailed_mw": 5.58, "clipped": False}}
    assert interval_payload(record)["guard"]["curtail_mw"] == 5.58
    assert interval_payload({"guard": record["guard"]})["guard"]["curtail_mw"] == 4.19     # no site: the guard's
