"""Operator control: override, emergency hold, autonomy and approvals (control/, loop/day.py, backend/)."""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from backend import Backend, create_app
from battery import BatteryConfig
from control import (APPROVED, REJECTED, SUPERSEDED, TIMEOUT, Autonomy, ControlError,
                     NoPendingApproval, OperatorControl)
from goal_parser import GoalParser, GoalStore
from loop import DayRunner, demo_day
from tests.test_dashboard_contract import _check_interface, contract  # noqa: F401  (types.ts reader, fixture)

B = BatteryConfig()
DAY = demo_day()


def reply(fraction, confidence="high"):
    """A decision LLM that always proposes ``fraction`` of max power."""
    text = json.dumps({"reasoning": "r", "battery_fraction": fraction, "curtail": False,
                       "confidence": confidence, "explanation_for_operator": "e"})
    return lambda prompt: text


def llm_off(prompt):
    raise RuntimeError("off")


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


def answer_when_asked(ctl, act):
    """In the background: as soon as a proposal waits, call ``act(ctl, pending)``."""
    def run():
        assert wait_for(lambda: ctl.snapshot()["pending"] is not None)
        act(ctl, ctl.snapshot()["pending"])
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def make_runner(ctl, *, llm=llm_off, **kw):
    kw.setdefault("start_soc_mwh", 4.0)
    return DayRunner(DAY, None, battery=B, llm_call=llm, control=ctl, **kw)


# --------------------------------------------------------------------------- #
# the controller on its own


def test_override_is_checked_before_it_is_accepted():
    ctl = OperatorControl(max_power_mw=3)
    for sp, until, now in [(3.5, "12:00", "10:00"),      # more than the battery can do
                           (1.0, "10:00", "10:00"),      # ends before it starts
                           (1.0, "09:00", "10:00"),      # already passed
                           (1.0, "12:10", "10:00"),      # not an interval boundary
                           (1.0, "25:00", "10:00"),
                           (float("nan"), "12:00", "10:00"),
                           (1.0, "12:00", None)]:        # the day is over
        with pytest.raises(ControlError):
            ctl.set_override(sp, until, now=now)
    assert ctl.snapshot()["override"] is None
    assert ctl.set_override(-3, "24:00", now="23:45")["override"] == {"setpoint_mw": -3.0, "until": "24:00"}


def test_override_ends_by_itself_and_hold_comes_first():
    changes = []
    ctl = OperatorControl(max_power_mw=3, on_change=changes.append)
    ctl.set_override(2.0, "11:00", now="10:00")
    assert ctl.command_for("10:45").kind == "override"
    ctl.set_hold(True)
    assert ctl.command_for("10:45").kind == "hold"         # the emergency button beats everything
    ctl.set_hold(False)
    assert ctl.command_for("11:00") is None                # ran its course: back to the AI
    assert ctl.snapshot()["override"] is None
    assert changes[-1]["override"] is None                 # and the dashboard was told


@pytest.mark.parametrize("mode, mw, confidence, asks", [
    ("autonomous", 3.0, "low", False),
    ("supervised", 1.0, "high", False),
    ("supervised", 2.0, "high", True),       # a big move
    ("supervised", 1.0, "low", True),        # an unsure one
    ("supervised", 0.0, "low", False),       # holding never needs approval
    ("advisory", 0.3, "high", True),
    ("advisory", 0.0, "high", False),
])
def test_which_moves_wait_for_the_operator(mode, mw, confidence, asks):
    policy = Autonomy(mode=mode, max_auto_mw=1.5)
    assert policy.needs_review(mw, confidence) is asks
    assert policy.needs_review(-mw, confidence) is asks


def test_autonomy_is_validated():
    for bad in [dict(mode="yolo"), dict(max_auto_mw=-1), dict(timeout_s=0)]:
        with pytest.raises(ControlError):
            Autonomy(**bad)


def review(ctl, mw=2.0, confidence="high"):
    return ctl.review(time="10:00", setpoint_mw=mw, source="llm", confidence=confidence)


def test_review_waits_for_the_answer():
    ctl = OperatorControl(max_power_mw=3, autonomy=Autonomy(mode="advisory"))
    answer_when_asked(ctl, lambda c, p: c.answer(p["id"], approve=True))
    assert review(ctl) == APPROVED
    answer_when_asked(ctl, lambda c, p: c.answer(p["id"], approve=False))
    assert review(ctl) == REJECTED
    assert ctl.snapshot()["pending"] is None


def test_review_times_out():
    ctl = OperatorControl(max_power_mw=3, autonomy=Autonomy(mode="advisory", timeout_s=0.05))
    assert review(ctl) == TIMEOUT
    assert ctl.snapshot()["pending"] is None


def test_a_stale_answer_is_refused():
    ctl = OperatorControl(max_power_mw=3, autonomy=Autonomy(mode="advisory"))
    with pytest.raises(NoPendingApproval):
        ctl.answer(1, approve=True)                       # nothing waits

    def wrong_then_right(c, p):
        with pytest.raises(NoPendingApproval):
            c.answer(p["id"] + 1, approve=True)
        c.answer(p["id"], approve=False)
    answer_when_asked(ctl, wrong_then_right)
    assert review(ctl) == REJECTED


def test_a_command_or_a_trusting_policy_ends_the_wait():
    ctl = OperatorControl(max_power_mw=3, autonomy=Autonomy(mode="advisory"))
    answer_when_asked(ctl, lambda c, p: c.set_hold(True))
    assert review(ctl) == SUPERSEDED
    assert review(ctl) == SUPERSEDED                      # while held, nothing is asked at all
    ctl.set_hold(False)

    answer_when_asked(ctl, lambda c, p: c.set_autonomy(Autonomy(mode="autonomous")))
    assert review(ctl) == APPROVED                        # trusting the AI again approves what waits


def test_the_pending_proposal_is_shown_with_its_time_left():
    ctl = OperatorControl(max_power_mw=3, autonomy=Autonomy(mode="advisory", timeout_s=30))
    seen = []
    answer_when_asked(ctl, lambda c, p: (seen.append(p), c.answer(p["id"], approve=True)))
    ctl.review(time="10:00", setpoint_mw=-2.5, source="llm", confidence="medium",
               explanation_for_operator="Selling")
    p = seen[0]
    assert (p["time"], p["setpoint_mw"], p["confidence"], p["explanation_for_operator"]) == \
        ("10:00", -2.5, "medium", "Selling")
    assert 0 < p["expires_in_s"] <= 30


# --------------------------------------------------------------------------- #
# in the loop


def test_override_runs_through_the_guard_until_its_end():
    ctl = OperatorControl(max_power_mw=B.p_max_mw)
    r = make_runner(ctl)
    ctl.set_override(2.0, "01:00", now=r.now)
    recs = r.run(until="01:15")
    for rec in recs[:4]:
        assert rec["decision"]["source"] == "operator"
        assert rec["control"] == {"action": "override", "mode": "autonomous", "proposed_mw": None}
        assert rec["site"]["setpoint_applied_mw"] == pytest.approx(2.0)
    assert recs[4]["decision"]["source"] == "fallback" and recs[4]["control"]["action"] == "auto"
    assert r.ledger.metrics()["decisions_operator"] == 4


def test_override_cannot_drain_the_battery():
    ctl = OperatorControl(max_power_mw=B.p_max_mw)
    r = make_runner(ctl, start_soc_mwh=B.e_min_mwh)
    ctl.set_override(-3.0, "02:00", now=r.now)
    rec = r.step()
    assert "soc_min_limit" in rec["guard"]["applied_rules"]
    assert rec["site"]["setpoint_applied_mw"] == pytest.approx(0.0, abs=1e-6)
    assert rec["soc_after_mwh"] >= B.e_min_mwh - 1e-9


def test_emergency_hold_idles_the_battery_and_exports_nothing():
    ctl = OperatorControl(max_power_mw=B.p_max_mw)
    r = make_runner(ctl, start_time="10:00", llm=reply(-1.0))
    ctl.set_hold(True)
    recs = r.run(until="12:00")                          # midday: a large solar surplus
    for rec in recs:
        assert rec["control"]["action"] == "hold" and rec["decision"]["source"] == "operator"
        assert rec["guard"]["applied_rules"] == ["emergency_hold"]
        assert rec["site"]["setpoint_applied_mw"] == 0.0
        assert rec["grid_export_mwh"] == pytest.approx(0.0, abs=1e-9)
    assert sum(rec["site"]["curtailed_mw"] for rec in recs) > 0
    assert r.sim.stored_mwh == pytest.approx(4.0)
    ctl.set_hold(False)
    assert r.step()["decision"]["source"] == "llm"       # released: the AI is back


def test_supervised_big_move_runs_once_approved():
    ctl = OperatorControl(max_power_mw=B.p_max_mw, autonomy=Autonomy(mode="supervised", max_auto_mw=1.5))
    r = make_runner(ctl, llm=reply(1.0))
    answer_when_asked(ctl, lambda c, p: c.answer(p["id"], approve=True))
    rec = r.step()
    assert rec["control"] == {"action": "approved", "mode": "supervised", "proposed_mw": 3.0}
    assert rec["decision"]["source"] == "llm"
    assert rec["site"]["setpoint_applied_mw"] == pytest.approx(3.0)


def test_small_confident_move_runs_without_asking():
    ctl = OperatorControl(max_power_mw=B.p_max_mw, autonomy=Autonomy(mode="supervised", max_auto_mw=1.5))
    rec = make_runner(ctl, llm=reply(0.2)).step()       # 0.6 MW, high confidence
    assert rec["control"]["action"] == "auto" and rec["control"]["mode"] == "supervised"


def test_rejected_proposal_holds_through_the_guard():
    ctl = OperatorControl(max_power_mw=B.p_max_mw, autonomy=Autonomy(mode="advisory"))
    r = make_runner(ctl, llm=reply(1.0))
    answer_when_asked(ctl, lambda c, p: c.answer(p["id"], approve=False))
    rec = r.step()
    assert rec["control"] == {"action": "rejected", "mode": "advisory", "proposed_mw": 3.0}
    assert rec["decision"]["source"] == "operator"
    assert rec["site"]["setpoint_applied_mw"] == 0.0
    assert "soc_limits_ok" in rec["guard"]["applied_rules"]          # the guard checked the hold


def test_unanswered_proposal_holds_and_says_why():
    ctl = OperatorControl(max_power_mw=B.p_max_mw, autonomy=Autonomy(mode="advisory", timeout_s=0.05))
    rec = make_runner(ctl, llm=reply(-1.0)).step()
    assert rec["control"]["action"] == "timeout"
    assert (rec["decision"]["source"], rec["decision"]["fallback_reason"]) == ("fallback", "approval_timeout")
    assert rec["site"]["setpoint_applied_mw"] == 0.0


def test_override_given_while_a_proposal_waits_takes_its_place():
    ctl = OperatorControl(max_power_mw=B.p_max_mw, autonomy=Autonomy(mode="advisory"))
    r = make_runner(ctl, llm=reply(1.0))
    answer_when_asked(ctl, lambda c, p: c.set_override(-1.0, "01:00", now="00:00"))
    rec = r.step()
    assert rec["control"] == {"action": "override", "mode": "advisory", "proposed_mw": 3.0}
    assert rec["site"]["setpoint_applied_mw"] == pytest.approx(-1.0)


# --------------------------------------------------------------------------- #
# over the API


@pytest.fixture
def api():
    store = GoalStore(GoalParser(None))
    backend = Backend(store, llm_call=reply(1.0))
    yield backend, TestClient(create_app(backend))
    backend.close()


def test_control_state_and_commands_match_types_ts(api, contract):
    b, c = api
    s = c.get("/api/control").json()
    _check_interface(s, "ControlState", contract)
    assert s == {"hold": False, "override": None, "pending": None,
                 "autonomy": {"mode": "autonomous", "max_auto_mw": 1.5, "ask_on_low_confidence": True,
                              "timeout_s": 60.0}}
    _check_interface(c.post("/api/override", json={"setpoint_mw": 1, "until": "06:00"}).json(),
                     "ControlState", contract)
    _check_interface(c.post("/api/hold").json(), "ControlState", contract)
    ivs = b.advance(1)
    _check_interface(ivs[0], "Interval", contract)
    _check_interface(c.get("/api/metrics").json(), "Metrics", contract)


@pytest.mark.parametrize("body", [{"setpoint_mw": 9, "until": "06:00"}, {"setpoint_mw": 1, "until": "00:00"},
                                  {"setpoint_mw": 1, "until": "6pm"}, {"setpoint_mw": 1, "until": "06:05"},
                                  {"until": "06:00"}])
def test_bad_override_is_refused(api, body):
    _, c = api
    assert c.post("/api/override", json=body).status_code == 422
    assert c.get("/api/control").json()["override"] is None


def test_override_and_hold_over_the_api(api):
    b, c = api
    assert c.post("/api/override", json={"setpoint_mw": -1.5, "until": "01:00"}).status_code == 200
    iv = b.advance(1)[0]
    assert (iv["decision"]["source"], iv["control"]["action"]) == ("operator", "override")
    assert iv["guard"]["battery_setpoint_mw"] == pytest.approx(-1.5)
    assert c.delete("/api/override").json()["override"] is None

    assert c.post("/api/hold").json()["hold"] is True
    assert b.advance(1)[0]["control"]["action"] == "hold"
    assert c.delete("/api/hold").json()["hold"] is False
    assert b.advance(1)[0]["decision"]["source"] == "llm"
    assert c.get("/api/metrics").json()["decisions_operator"] == 2


def test_autonomy_changes_only_what_is_sent(api):
    _, c = api
    s = c.put("/api/autonomy", json={"mode": "supervised", "max_auto_mw": 2}).json()["autonomy"]
    assert s == {"mode": "supervised", "max_auto_mw": 2.0, "ask_on_low_confidence": True, "timeout_s": 60.0}
    for bad in [{"mode": "yolo"}, {"timeout_s": 1}, {"max_auto_mw": -1}]:
        assert c.put("/api/autonomy", json=bad).status_code == 422
    assert c.get("/api/control").json()["autonomy"]["mode"] == "supervised"


def test_approving_over_the_api(api, contract):
    b, c = api
    c.put("/api/autonomy", json={"mode": "supervised"})
    out = []
    t = threading.Thread(target=lambda: out.extend(b.advance(1)), daemon=True)
    t.start()
    assert wait_for(lambda: c.get("/api/control").json()["pending"] is not None)
    pending = c.get("/api/control").json()
    _check_interface(pending, "ControlState", contract)
    pid = pending["pending"]["id"]
    assert c.post(f"/api/approvals/{pid + 1}", json={"approve": True}).status_code == 409
    assert c.post(f"/api/approvals/{pid}", json={"approve": True}).json()["pending"] is None
    t.join(timeout=5)
    assert out[0]["control"] == {"action": "approved", "mode": "supervised", "proposed_mw": 3.0}
    assert c.post(f"/api/approvals/{pid}", json={"approve": True}).status_code == 409   # already answered


def test_control_changes_are_pushed_to_listeners(api):
    b, c = api
    events = []
    b.add_listener(lambda event, data: events.append((event, data)))
    c.post("/api/hold")
    b.advance(1)
    assert [e for e, _ in events] == ["control", "cycle"]
    assert events[0][1]["hold"] is True


def test_reset_drops_the_override_but_not_the_hold(api):
    b, c = api
    c.post("/api/override", json={"setpoint_mw": 1, "until": "12:00"})
    c.post("/api/hold")
    b.advance(2)
    c.post("/api/sim/reset")
    s = c.get("/api/control").json()
    assert s["override"] is None and s["hold"] is True


def test_reset_lets_go_of_a_waiting_proposal(api):
    b, c = api
    c.put("/api/autonomy", json={"mode": "advisory"})
    t = threading.Thread(target=lambda: b.advance(1), daemon=True)
    t.start()
    assert wait_for(lambda: c.get("/api/control").json()["pending"] is not None)
    c.post("/api/sim/reset")
    t.join(timeout=5)
    assert not t.is_alive()
    assert c.get("/api/control").json()["pending"] is None
    assert c.get("/api/state").json()["current"] is None          # the interval that waited was dropped
