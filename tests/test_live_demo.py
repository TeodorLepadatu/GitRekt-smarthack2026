"""The live-data demo (backend/live_demo.py) and Backend.replace_day. No network: the scrape and
the training are faked; the forecasts use the real models in predictions/models/."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    not (ROOT / "predictions" / "dataset.csv").exists()
    or not (ROOT / "predictions" / "models" / "demand_model.pkl").exists(),
    reason="needs predictions/dataset.csv and the trained models",
)
DATE = "2026-11-06"          # fully measured in dataset.csv, and priced


def _fake_tick(now, *, directory, train=None):
    """What the live loop's tick does, with the original data standing in for the scrape."""
    from predictions import live
    from predictions.core import DATASET_CSV
    base = live.read_dataset(Path(directory) / "dataset.csv")
    full = live.read_dataset(DATASET_CSV)
    cut = int(now.timestamp())
    new = full[(full.timestamp > base.timestamp.max()) & (full.timestamp < cut)]
    live.write_dataset(__import__("pandas").concat([base, new]), Path(directory) / "dataset.csv")
    return {"energy_source": "transelectrica", "added": len(new), "error": None}


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    """Built once (each build forecasts a day with the real models); the tests below run in order."""
    from backend.live_demo import LiveDemo
    tmp_path = tmp_path_factory.mktemp("live")
    days = []
    calls = []
    d = LiveDemo(DATE, days.append, start="08:00", directory=tmp_path / "demo", tick=_fake_tick,
                 train=lambda data, out: calls.append((data, out)) or {"seconds": 0.1, "trained_until": "x"})
    d.start()
    d.wait()
    return d, days, calls


def test_demo_starts_cut_off_and_each_press_adds_an_hour(demo):
    d, days, calls = demo
    s = d.status()
    assert (s["phase"], s["data_until"], s["measured_intervals"], s["steps"]) == ("idle", "08:00", 32, 0)
    first = days[-1]
    assert sum(a is not None for a in first.actuals) == 32

    d.fetch(1)
    d.wait()
    s = d.status()
    assert s["error"] is None and (s["data_until"], s["measured_intervals"], s["steps"]) == ("09:00", 36, 1)
    assert s["last"]["added"] == 4 and s["last"]["source"] == "transelectrica" and len(calls) == 1
    second = days[-1]
    assert list(second.forecasts[:36]) == list(first.forecasts[:36])   # the past keeps the forecasts it was planned with
    assert list(second.forecasts[36:]) != list(first.forecasts[36:])   # the rest is forecast again from 09:00
    assert sum(a is not None for a in second.actuals) == 36


def test_demo_reports_a_failed_scrape(demo, monkeypatch):
    d, _, _ = demo
    monkeypatch.setattr(d, "_tick", lambda now, **kw: {"error": "TranselectricaError: unreachable"})
    d.fetch(1)
    d.wait()
    s = d.status()
    assert "unreachable" in s["error"] and s["phase"] == "idle" and s["data_until"] == "09:00"


def test_demo_refuses_while_busy_and_at_the_end(demo, monkeypatch):
    from backend.live_demo import DemoBusy, DemoDone
    d, _, _ = demo
    import threading
    gate = threading.Event()
    monkeypatch.setattr(d, "_fetch_work", lambda hours: gate.wait(5))
    d.fetch(1)
    with pytest.raises(DemoBusy):
        d.fetch(1)
    gate.set()
    d.wait()
    d.cut = d.stamps[-1] + 900
    with pytest.raises(DemoDone):
        d.fetch(1)
    assert d.status()["data_until"] == "24:00" and d.status()["can_fetch"] is False


def test_routes(demo):
    from backend import Backend, create_app
    from goal_parser import GoalParser, GoalStore
    d, _, _ = demo
    store = GoalStore(GoalParser(None))
    store.set(["Maximise profit"])
    b = Backend(store, llm_call=lambda p: (_ for _ in ()).throw(RuntimeError("off")))
    try:
        assert TestClient(create_app(b)).get("/api/live").status_code == 404
        c = TestClient(create_app(b, live_demo=d))
        assert c.get("/api/live").json() == d.status()
        d.cut = d.stamps[-1] + 900
        assert c.post("/api/live/fetch", json={"hours": 1}).status_code == 409
        assert c.post("/api/live/fetch", json={"hours": 0}).status_code == 422
    finally:
        b.close()


def test_replace_day_keeps_what_has_run():
    from dataclasses import replace

    from backend import Backend
    from goal_parser import GoalParser, GoalStore
    from loop import demo_day
    store = GoalStore(GoalParser(None))
    store.set(["Maximise profit"])
    b = Backend(store, llm_call=lambda p: (_ for _ in ()).throw(RuntimeError("off")))
    try:
        b.advance(8)
        before = b.day()["intervals"]
        day = demo_day()
        bumped = [dict(f.__dict__, demand_mw=f.demand_mw + 1) for f in day.forecasts]
        b.replace_day(replace(day, forecasts=bumped))
        b.wait_idle()
        after = b.day()["intervals"]
        assert after[:8] == before[:8]                              # decided intervals unchanged
        assert after[20]["demand_mw"] == pytest.approx(before[20]["demand_mw"] + 1, abs=1e-3)
        assert b.advance(1)[0]["time"] == "02:00"                   # carries on where it was
    finally:
        b.close()
