"""Tests for the forecasting API (predictions/api.py) and the two forecasters.

Uses the trained models in predictions/models/ (skipped if they are missing; run
predictions/train_and_evaluate.ipynb first). No network: every request sets
allow_fetch=False, so weather comes from the archive or climatology.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

MODELS = Path(__file__).resolve().parents[1] / "predictions" / "models"
pytestmark = pytest.mark.skipif(
    not (MODELS / "demand_model.pkl").exists() or not (MODELS / "production_model.pkl").exists(),
    reason="trained models not found; run predictions/train_and_evaluate.ipynb",
)

STEP = 900
DAY = 86_400


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from predictions.api import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def window(client):
    h = client.get("/health").json()
    issued = h["models"]["demand"]["default_issued_at"]
    return issued, issued + 7 * DAY - STEP          # first and last allowed timestamp


def post(client, path, **body):
    body.setdefault("allow_fetch", False)
    return client.post(path, json=body)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #
def test_health(client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["errors"] == {}
    for key in ("demand", "production"):
        m = h["models"][key]
        assert m["default_issued_at"] % STEP == 0
        assert m["forecast_window_utc"][0] == iso(m["default_issued_at"])


# --------------------------------------------------------------------------- #
# correctness: API == direct class call
# --------------------------------------------------------------------------- #
def test_demand_matches_class(client, window):
    from predictions import DemandForecaster
    first, _ = window
    ts = [first, first + 6 * 3600, first + 3 * DAY]
    api = post(client, "/forecast/demand", timestamps=ts).json()["predictions"]
    direct = DemandForecaster.load().predict(ts, allow_fetch=False)
    for row, (_, d) in zip(api, direct.iterrows()):
        assert row["timestamp"] == d.timestamp
        assert row["demand_mw"] == pytest.approx(d.demand_mw)
        assert row["demand_low_mw"] == pytest.approx(d.demand_low_mw)
        assert row["demand_high_mw"] == pytest.approx(d.demand_high_mw)


def test_combined_equals_separate(client, window):
    first, _ = window
    ts = [first + 3600, first + 2 * DAY]
    d = post(client, "/forecast/demand", timestamps=ts).json()["predictions"]
    p = post(client, "/forecast/production", timestamps=ts).json()["predictions"]
    both = post(client, "/forecast", timestamps=ts).json()["predictions"]
    for b, dd, pp in zip(both, d, p):
        assert b["demand_mw"] == dd["demand_mw"]
        assert b["total_mw"] == pp["total_mw"] and b["solar_mw"] == pp["solar_mw"]


# --------------------------------------------------------------------------- #
# timestamp formats
# --------------------------------------------------------------------------- #
def test_iso_unix_ms_and_offsets_agree(client, window):
    first, _ = window
    t = first + DAY
    local = datetime.fromtimestamp(t, timezone(timedelta(hours=2))).isoformat()   # "+02:00"
    naive = datetime.fromtimestamp(t, timezone.utc).replace(tzinfo=None).isoformat()
    rows = post(client, "/forecast/demand", timestamps=[t, iso(t), local, naive, t * 1000]).json()["predictions"]
    assert {r["timestamp"] for r in rows} == {t}
    assert len({r["demand_mw"] for r in rows}) == 1


def test_snaps_down_to_15_min(client, window):
    first, _ = window
    rows = post(client, "/forecast/demand", timestamps=[first + DAY + 7 * 60]).json()["predictions"]
    assert rows[0]["timestamp"] == first + DAY


def test_order_and_duplicates_kept(client, window):
    first, _ = window
    ts = [first + 2 * DAY, first, first + 2 * DAY]
    for path in ("/forecast/demand", "/forecast/production", "/forecast"):
        rows = post(client, path, timestamps=ts).json()["predictions"]
        assert [r["timestamp"] for r in rows] == ts, path


def test_range(client, window):
    first, _ = window
    r = post(client, "/forecast", start=iso(first + 60), end=iso(first + DAY)).json()
    ts = [p["timestamp"] for p in r["predictions"]]
    assert ts[0] == first + STEP                     # start rounded up
    assert ts == list(range(first + STEP, first + DAY, STEP))
    assert r["count"] == 95


# --------------------------------------------------------------------------- #
# forecast window and issued_at
# --------------------------------------------------------------------------- #
def test_window_edges(client, window):
    first, last = window
    assert post(client, "/forecast/demand", timestamps=[first, last]).status_code == 200
    assert post(client, "/forecast/demand", timestamps=[last + STEP]).status_code == 422
    assert post(client, "/forecast/demand", timestamps=[first - STEP]).status_code == 422


def test_backtest_issued_at(client):
    issued = 1772323200                              # 2026-03-01 00:00 UTC
    r = post(client, "/forecast", timestamps=[issued + DAY], issued_at=iso(issued)).json()
    assert r["issued_at"] == issued
    row = r["predictions"][0]
    assert row["lead_hours"] == pytest.approx(24.2, abs=0.1)
    assert row["weather_source"] == "archive"


def test_issued_at_outside_data_rejected(client, window):
    first, _ = window
    late = post(client, "/forecast/demand", timestamps=[first + 2 * DAY], issued_at=first + DAY)
    assert late.status_code == 422 and "issued_at" in late.json()["detail"]
    early = post(client, "/forecast/demand", timestamps=[1667779200 + DAY], issued_at=1667779200)
    assert early.status_code == 422


# --------------------------------------------------------------------------- #
# bad requests
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("body", [
    {},                                              # nothing
    {"timestamps": []},                              # empty
    {"start": "2026-11-08T00:00:00Z"},               # range without end
    {"start": "2026-11-09T00:00:00Z", "end": "2026-11-08T00:00:00Z"},   # end before start
    {"timestamps": ["not a date"]},
    {"start": 0, "end": 10**10},                     # huge range: rejected before allocating
])
def test_bad_requests(client, body):
    assert post(client, "/forecast/demand", **body).status_code == 422


def test_too_many_points(client, window):
    first, _ = window
    assert post(client, "/forecast/demand", timestamps=[first] * 5001).status_code == 422


# --------------------------------------------------------------------------- #
# CSV: forecasts as CSV, and the /csv file routes
# --------------------------------------------------------------------------- #
def test_forecast_as_csv_matches_json(client, window):
    import io
    import pandas as pd
    first, _ = window
    ts = [first, first + DAY, first + 3 * DAY]
    r = post(client, "/forecast", timestamps=ts, format="csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    csv = pd.read_csv(io.StringIO(r.text))
    js = pd.DataFrame(post(client, "/forecast", timestamps=ts).json()["predictions"])
    assert list(csv.columns) == list(js.columns)
    assert csv.timestamp.tolist() == ts
    assert np.allclose(csv.demand_mw, js.demand_mw) and np.allclose(csv.total_mw, js.total_mw)


def test_csv_list(client):
    files = {f["name"]: f for f in client.get("/csv").json()["files"]}
    assert "dataset.csv" in files and "reports/eval_2026.csv" in files
    assert files["dataset.csv"]["filterable"] and "actual_total_load" in files["dataset.csv"]["columns"]
    assert not files["reports/eval_2026.csv"]["filterable"]
    assert all(f["url"] == f"/csv/{n}" for n, f in files.items())


def test_csv_download_full_and_filtered(client):
    import io
    import pandas as pd
    full = client.get("/csv/reports/eval_2026.csv")
    assert full.status_code == 200 and full.headers["content-type"].startswith("text/csv")
    assert full.text.splitlines()[0].startswith("series,lead_h,mae")

    start, end = 1772323200, 1772323200 + DAY            # 2026-03-01, one day
    for s, e in [(start, end), (iso(start), iso(end))]:
        part = client.get("/csv/dataset.csv", params={"start": s, "end": e})
        assert part.status_code == 200
        df = pd.read_csv(io.StringIO(part.text))
        assert len(df) == 96 and df.timestamp.min() == start and df.timestamp.max() == end - STEP


@pytest.mark.parametrize("path,params,code", [
    ("/csv/nope.csv", {}, 404),
    ("/csv/../core.py", {}, 404),                         # no path traversal
    ("/csv/core.py", {}, 404),                            # only CSV files
    ("/csv/reports/eval_2026.csv", {"start": 0}, 422),    # no timestamp column to filter
    ("/csv/dataset.csv", {"start": "yesterday"}, 422),
])
def test_csv_errors(client, path, params, code):
    assert client.get(path, params=params).status_code == code


# --------------------------------------------------------------------------- #
# value sanity over a full week
# --------------------------------------------------------------------------- #
def test_week_values_sane(client, window):
    first, last = window
    rows = post(client, "/forecast", start=first, end=last + STEP).json()["predictions"]
    assert len(rows) == 7 * 96
    get = lambda k: np.array([r[k] for r in rows], dtype=float)
    for k in ("demand", "solar", "wind", "total"):
        lo, mid, hi = get(f"{k}_low_mw"), get(f"{k}_mw"), get(f"{k}_high_mw")
        assert np.isfinite(mid).all(), k
        assert (lo >= 0).all() and (lo <= mid + 0.11).all() and (mid <= hi + 0.11).all(), k
    assert 3000 < get("demand_mw").min() and get("demand_mw").max() < 11000
    assert (get("total_mw") >= get("solar_mw") + get("wind_mw") - 0.2).all()
    night = np.array([datetime.fromtimestamp(r["timestamp"], timezone.utc).hour in (0, 1, 2) for r in rows])
    assert get("solar_mw")[night].max() < 50        # no sun at 02:00-05:00 local
