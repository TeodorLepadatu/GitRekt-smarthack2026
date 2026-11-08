"""dashboard/explain.py and dashboard/report.py: explanations built from the numbers, and the day report."""

import json

import pytest

from backend.service import Backend
from dashboard.explain import explain, note_contradicts
from dashboard.report import ai_summary
from goal_parser import GoalParser, GoalStore
from llm import ChatResult
from loop import demo_day


def _record(**over):
    """A settled interval: 3 MW discharge at EUR 300 with a 2 MW shortfall."""
    rec = {
        "time": "19:00", "price_eur_mwh": 300.0, "demand_mw": 3.0, "production_mw": 1.0,
        "soc_after_mwh": 5.0, "grid_import_mwh": 0.0, "grid_export_mwh": 0.25, "value_added_eur": 150.0,
        "features": {"break_even": 200.0, "net_label": "DEFICIT", "capacity_mwh": 10.0, "min_soc_mwh": 1.0,
                     "future_max_price": 320.0, "future_max_time": "19:45"},
        "decision": {"source": "llm", "battery_fraction": -1.0,
                     "explanation_for_operator": "Discharging into the evening peak."},
        "guard": {"battery_setpoint_mw": -3.0, "curtail_mw": 0.0, "clipped": False, "applied_rules": []},
        "site": {"setpoint_applied_mw": -3.0, "curtailed_mw": 0.0},
    }
    for k, v in over.items():
        rec[k] = {**rec[k], **v} if isinstance(v, dict) else v
    return rec


def test_discharge_explained_with_numbers_and_money():
    e = explain(_record(), p_max_mw=3.0)
    assert e["summary"].startswith("Discharging 3.00 MW: covering the site's whole 2.00 MW shortfall and selling 1.00 MW more")
    assert "€100/MWh above what the stored energy is worth (€200/MWh)" in e["summary"]
    assert "Nothing bought from the grid in this interval" in e["facts"]
    assert "€150 better off than without the battery" in e["facts"]
    assert e["ai_note"] == "Discharging into the evening peak." and e["safety"] is None


def test_safety_change_is_said_and_goal_named():
    rec = _record(guard={"battery_setpoint_mw": 0.0, "clipped": True, "applied_rules": ["goal_g1_blocked_discharge"]},
                  site={"setpoint_applied_mw": 0.0}, value_added_eur=0.0)
    e = explain(rec, p_max_mw=3.0, goals=[{"id": "g1", "source_text": "Keep 6 MWh until 22:00"}])
    assert e["summary"] == "Holding: keeping the stored energy for 'Keep 6 MWh until 22:00'."
    assert e["safety"] == "The AI proposed to discharge 3.00 MW; the safety layer set hold."


def test_charging_on_own_surplus():
    rec = _record(price_eur_mwh=9.0, demand_mw=1.0, production_mw=5.0, value_added_eur=-2.0,
                  decision={"battery_fraction": 1.0}, guard={"battery_setpoint_mw": 3.0},
                  site={"setpoint_applied_mw": 3.0}, features={"net_label": "SURPLUS"})
    e = explain(rec, p_max_mw=3.0)
    assert e["summary"].startswith("Charging 3.00 MW: storing the site's own 4.00 MW surplus instead of selling it at €9.00/MWh")
    assert "Highest price still to come today: €320/MWh at 19:45" in e["facts"]


@pytest.mark.parametrize("note, kind, label, bad", [
    ("Battery idle to cover the small deficit.", "discharge", "DEFICIT", True),
    ("Discharging at half power to sell surplus during the peak.", "discharge", "DEFICIT", True),
    ("Charging while energy is cheap instead of selling it.", "charge", "SURPLUS", False),
    ("Discharging to cover the site's deficit.", "discharge", "DEFICIT", False),
    ("Selling stored energy at the evening peak.", "charge", "DEFICIT", True),
])
def test_contradicting_ai_notes_are_dropped(note, kind, label, bad):
    assert note_contradicts(note, kind, label) is bad


def test_backup_and_operator_have_no_ai_note():
    assert explain(_record(decision={"source": "fallback"}), p_max_mw=3.0)["ai_note"] is None
    e = explain(_record(decision={"source": "operator", "battery_fraction": 0.0}), p_max_mw=3.0)
    assert e["ai_note"] is None and e["safety"] is None


def test_explain_never_raises_on_a_broken_record():
    e = explain({"features": None, "guard": "x"})
    assert e["summary"].startswith("Holding")


# ------------------------------------------------------------------ the report

@pytest.fixture
def backend():
    store = GoalStore(GoalParser(None))
    store.set(["Maximise profit", "Store 6 MWh by 18:00"])
    b = Backend(store, llm_call=lambda p: (_ for _ in ()).throw(RuntimeError("off")),
                day_factory=lambda: demo_day(seed=7))
    yield b
    b.close()


def test_report_for_a_whole_day(backend):
    backend.advance(96)
    r = backend.report()
    assert r["complete"] and r["intervals"] == 96 and r["through"] == "24:00"
    m = r["money"]
    assert m["saved_eur"] == pytest.approx(m["profit_eur"] - m["no_battery_eur"], abs=0.02)
    assert r["decisions"]["backup"] == 96 and r["decisions"]["backup_reasons"] == {"the AI was not reachable": 96}
    assert r["phases"] and all(p["kind"] in ("charge", "discharge") and p["energy_mwh"] >= 0.25 for p in r["phases"])
    values = [d["value_eur"] for d in r["key_decisions"]]
    assert values == sorted(values, reverse=True) and all(v > 0 for v in values)
    assert r["goals"] and r["goals"][0]["text"] == "Store 6 MWh by 18:00"
    assert "backup rules 96" in r["summary"]


def test_report_part_way_and_people(backend):
    backend.advance(10)
    backend.set_plan("03:00", 0.0, "testing")
    r = backend.report()
    assert not r["complete"] and r["through"] == "02:30" and "still running (10 of 96" in r["summary"]
    assert [p["action"] for p in r["people"]] == ["override_set"]


class FakeChat:
    def __init__(self, text):
        self.text = text

    def chat_json(self, messages, schema, *, tag=""):
        return ChatResult(json.dumps({"summary": self.text}), 0.0, "fake")


def test_ai_summary_must_use_the_reports_numbers(backend):
    backend.advance(96)
    r = backend.report()
    saved = f"€{round(r['money']['saved_eur']):,}"
    ok = ai_summary(r, FakeChat(f"The battery saved {saved} today.").chat_json)
    assert ok["source"] == "ai" and saved in ok["summary"]
    bad = ai_summary(r, FakeChat("The battery saved €123,456 today.").chat_json)
    assert bad["source"] == "report" and "123456" in bad["note"] and bad["summary"] == r["summary"]
    assert backend.report_summary()["note"] == "the AI is switched off"
