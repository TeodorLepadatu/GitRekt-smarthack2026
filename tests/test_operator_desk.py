"""The operator's desk: alerts, upcoming decisions with overrides, flags and the audit log."""

import json

import pytest
from fastapi.testclient import TestClient

from backend import Backend, create_app
from backend.desk import AlertBook, alert_conditions, relaxed_sentence
from goal_parser import GoalParser, GoalStore
from tests.test_dashboard_contract import contract  # noqa: F401  (types.ts reader, fixture)


def llm_off(prompt):
    raise RuntimeError("off")


@pytest.fixture
def desk(tmp_path):
    store = GoalStore(GoalParser(None))
    store.set(["Maximise profit", "Store 9 MWh by 10:00", "Never sell below 50 EUR/MWh"])
    backend = Backend(store, llm_call=llm_off, operator_log_dir=str(tmp_path))
    yield backend, TestClient(create_app(backend)), tmp_path
    backend.close()


def test_backup_rules_in_a_row_raise_one_alert(desk):
    b, c, _ = desk
    b.advance(2)
    assert c.get("/api/alerts").json() == []
    b.advance(3)
    alerts = [a for a in c.get("/api/alerts").json() if a["kind"] == "fallback_streak"]
    assert len(alerts) == 1 and alerts[0]["time"] == "00:30"     # raised once, kept while it lasts
    assert "ignore" in alerts[0]["actions"]


def test_ignored_alert_stays_hidden_while_its_condition_lasts(desk):
    b, c, tmp = desk
    b.advance(3)
    a = c.get("/api/alerts").json()[0]
    assert c.post(f"/api/alerts/{a['id']}/ignore").json() == {"ok": True}
    b.advance(1)
    assert all(x["kind"] != a["kind"] for x in c.get("/api/alerts").json())
    assert c.post(f"/api/alerts/{a['id']}/ignore").status_code == 404
    assert c.get("/api/audit").json()[0]["action"] == "alert_ignored"


def test_alert_book_resolves_and_re_raises():
    book = AlertBook()
    cond = {"x": {"kind": "price_spike", "severity": "info", "title": "t", "message": "m",
                  "goal_id": None, "actions": ["ignore"]}}
    assert len(book.update(cond, "10:00")) == 1
    assert book.update(cond, "10:15") == []          # same condition: no new alert
    book.update({}, "10:30")                         # cleared
    assert book.list() == []
    assert len(book.update(cond, "10:45")) == 1      # back again: a new alert


def test_goal_behind_and_price_spike_conditions():
    out = alert_conditions(
        records=[{"time": "00:00", "decision": {"source": "llm"}, "forecast": {}, "features": {}}],
        goal_summary=[{"id": "g1", "status": "behind", "message": "2 MWh short"}],
        goal_defs=[{"id": "g1", "type": "reserve", "source_text": "Store 6 MWh by 18:00"}],
        prices=[50.0] * 3 + [300.0] + [50.0] * 92,
        times=[f"{i // 4:02d}:{i % 4 * 15:02d}" for i in range(96)],
    )
    assert out["goal:g1:behind"]["actions"] == ["override", "relax_goal", "ignore"]
    assert out["price_spike"]["title"] == "Price spike from 00:45"


def test_a_price_spike_is_one_alert_while_the_window_slides_through_it():
    prices = [50.0] * 40 + [300.0] * 12 + [50.0] * 44
    times = [f"{i // 4:02d}:{i % 4 * 15:02d}" for i in range(96)]
    book = AlertBook()
    raised = []
    for n in range(30, 60):
        recs = [{"time": times[i], "decision": {"source": "llm"}, "forecast": {}, "features": {}} for i in range(n)]
        raised += book.update(alert_conditions(recs, [], [], prices, times), times[n - 1])
    assert [a["kind"] for a in raised] == ["price_spike"]


def test_upcoming_override_runs_as_operator_and_is_audited(desk):
    b, c, _ = desk
    b.advance(3)
    up = c.get("/api/upcoming?n=4").json()
    assert up["now"] == "00:30" and [s["time"] for s in up["slots"]] == ["00:45", "01:00", "01:15", "01:30"]
    assert all(s["override"] is None and "setpoint_mw" in s["planned"] for s in up["slots"])

    assert c.put("/api/plan/01:00", json={"battery_fraction": 0, "note": "skip"}).status_code == 200
    assert c.get("/api/upcoming?n=2").json()["slots"][1]["override"]["battery_fraction"] == 0
    assert c.put("/api/plan/00:15", json={"battery_fraction": 0}).status_code == 409   # already decided
    assert c.put("/api/plan/01:07", json={"battery_fraction": 0}).status_code == 404   # not an interval
    assert c.put("/api/plan/01:00", json={"battery_fraction": 2}).status_code == 422

    b.advance(2)
    iv = next(x for x in c.get("/api/day").json()["intervals"] if x["time"] == "01:00")
    assert iv["decision"]["source"] == "operator"
    assert iv["guard"]["battery_setpoint_mw"] == 0
    assert [e["action"] for e in c.get("/api/audit").json()] == ["override_set"]


def test_the_interval_being_decided_cannot_be_overridden(tmp_path):
    import threading
    started, release = threading.Event(), threading.Event()

    def slow_llm(prompt):
        started.set()
        release.wait(5)
        raise RuntimeError("off")

    store = GoalStore(GoalParser(None))
    store.set(["Maximise profit"])
    b = Backend(store, llm_call=slow_llm)
    c = TestClient(create_app(b))
    try:
        worker = threading.Thread(target=b.advance, args=(1,))
        worker.start()
        assert started.wait(5)
        assert c.put("/api/plan/00:00", json={"battery_fraction": 0}).status_code == 409
        assert c.get("/api/upcoming?n=2").json()["slots"][0]["time"] == "00:15"
        assert c.put("/api/plan/00:15", json={"battery_fraction": 0}).status_code == 200
        release.set()
        worker.join(5)
        assert c.get("/api/upcoming?n=1").json()["slots"][0]["time"] == "00:15"
    finally:
        release.set()
        b.close()


def test_override_still_obeys_the_guard(desk):
    b, c, _ = desk
    c.put("/api/plan/00:00", json={"battery_fraction": -1})      # discharge into an empty-ish battery
    b.advance(1)
    iv = c.get("/api/day").json()["intervals"][0]
    assert iv["decision"]["source"] == "operator"
    assert iv["guard"]["battery_setpoint_mw"] >= -3.0                  # within the battery's limits


def test_clear_override_gives_the_interval_back(desk):
    b, c, _ = desk
    c.put("/api/plan/00:15", json={"battery_fraction": 1})
    assert c.delete("/api/plan/00:15").json() == {"ok": True}
    assert c.delete("/api/plan/00:15").status_code == 404
    b.advance(2)
    assert c.get("/api/day").json()["intervals"][1]["decision"]["source"] == "fallback"


def test_flag_a_decision_is_kept_with_its_situation(desk):
    b, c, tmp = desk
    b.advance(2)
    assert c.post("/api/decisions/00:15/flag", json={"reason": "should have held"}).json()["reason"] == "should have held"
    assert c.post("/api/decisions/05:00/flag", json={"reason": "x"}).status_code == 404
    assert [f["time"] for f in c.get("/api/flags").json()] == ["00:15"]
    line = json.loads((tmp / "decision_flags.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert line["decision"]["source"] == "fallback" and "price_now" in line["situation"]
    assert c.delete("/api/decisions/00:15/flag").json() == {"ok": True}
    assert c.get("/api/flags").json() == []


def test_relax_goal_lowers_the_reserve(desk):
    b, c, _ = desk
    b.advance(4)
    gid = next(g["id"] for g in c.get("/api/goals").json() if g["type"] == "reserve")
    r = c.post(f"/api/goals/{gid}/relax").json()
    assert r["message"].startswith("Now: Store ")
    reserve = next(g for g in r["goals"] if g["type"] == "reserve")
    assert reserve["mwh"] < 9
    assert c.post("/api/goals/nope/relax").status_code == 404


def test_audit_survives_a_restart(desk, tmp_path):
    b, c, tmp = desk
    c.post("/api/sim/run", json={"speed": 0.001})
    c.post("/api/sim/run", json={"speed": 0})
    assert [e["action"] for e in c.get("/api/audit").json()] == ["sim_speed", "sim_speed"]
    store = GoalStore(GoalParser(None))
    store.set(["Maximise profit"])
    again = Backend(store, llm_call=llm_off, operator_log_dir=str(tmp))
    try:
        assert len(again.audit_list()) == 2
        again.reset()
        assert again.audit_list()[0]["action"] == "sim_reset"
        assert again.audit_list()[0]["id"] == "e3"
    finally:
        again.close()


def test_sse_carries_alert_events(desk):
    b, c, _ = desk
    seen = []
    b.add_listener(lambda event, data: seen.append(data["kind"]) if event == "alert" else None)
    b.advance(3)
    assert seen == ["fallback_streak"]


def test_a_standing_override_wins_over_a_plan_and_both_are_audited(desk):
    b, c, _ = desk
    c.put("/api/plan/00:00", json={"battery_fraction": 1})
    assert c.post("/api/override", json={"setpoint_mw": -1.0, "until": "00:15"}).status_code == 200
    b.advance(1)
    iv = c.get("/api/day").json()["intervals"][0]
    assert iv["control"]["action"] == "override" and iv["guard"]["battery_setpoint_mw"] <= 0   # his, not the plan
    c.post("/api/hold")
    c.delete("/api/hold")
    actions = [e["action"] for e in c.get("/api/audit").json()]
    assert actions[:3] == ["emergency_hold", "emergency_hold", "control_override"] and "override_set" in actions


def test_desk_payloads_match_types_ts(desk, contract):
    from tests.test_dashboard_contract import _check_interface
    b, c, _ = desk
    b.advance(4)
    c.put("/api/plan/01:30", json={"battery_fraction": 0.5, "note": "n"})
    c.post("/api/decisions/00:15/flag", json={"reason": "r"})
    for a in c.get("/api/alerts").json():
        _check_interface(a, "Alert", contract, "Alert")
    up = c.get("/api/upcoming?n=4").json()
    _check_interface(up, "UpcomingResponse", contract, "UpcomingResponse")
    for f in c.get("/api/flags").json():
        _check_interface(f, "DecisionFlag", contract, "DecisionFlag")
    for e in c.get("/api/audit").json():
        _check_interface(e, "AuditEntry", contract, "AuditEntry")
    b.advance(3)
    iv = next(x for x in c.get("/api/day").json()["intervals"] if x["time"] == "01:30")
    _check_interface(iv, "Interval", contract, "Interval@01:30")


def test_relaxed_sentence():
    assert relaxed_sentence("Store 6 MWh by 18:00", 4.5) == "Store 4.5 MWh by 18:00"
    assert relaxed_sentence("keep 2.5mwh overnight", 1) == "keep 1mwh overnight"
    assert relaxed_sentence("Never sell below 50 EUR/MWh", 1) is None
