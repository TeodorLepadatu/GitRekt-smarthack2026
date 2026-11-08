"""The mock price API (price_api/): its data source, its routes, its client, and real_day using it.

Everything runs on injected price tables and FastAPI's TestClient: no files, no server.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pd = pytest.importorskip("pandas")

from fastapi.testclient import TestClient

from loop import real_day
from loop.real_day import N, STEP_S, day_timestamps
from price_api import PriceApiError, fetch_day
from price_api.app import create_app
from price_api.source import REAL_DAY_AHEAD, REAL_IMBALANCE, SYNTHETIC, PriceSource
from test_real_day import national_rows, stub_models

REAL_DATE = "2026-06-15"      # covered by the injected tables
FUTURE_DATE = "2027-03-10"    # not covered: generated


def tables(date: str = REAL_DATE, *, da: float = 80.0):
    """A real-looking day: a daily shape for the profiles, and an imbalance pair per interval."""
    stamps = day_timestamps(date)
    day_ahead = {t: da + 40.0 * (i >= 68) - 30.0 * (40 <= i < 60) for i, t in enumerate(stamps)}
    imbalance = {t: (day_ahead[t] * 1.5, day_ahead[t] * 0.5) for t in stamps}
    return day_ahead, imbalance


@pytest.fixture
def source():
    da, imb = tables()
    return PriceSource(da, imb)


@pytest.fixture
def client(source):
    with TestClient(create_app(source)) as c:
        yield c


# --------------------------------------------------------------------------- #
# source


def test_real_prices_pass_through(source):
    t = day_timestamps(REAL_DATE)[70]
    p = source.at(t)
    assert p.day_ahead_eur_mwh == 120.0
    assert (p.imbalance_short_eur_mwh, p.imbalance_long_eur_mwh) == (180.0, 60.0)
    assert (p.day_ahead_source, p.imbalance_source) == (REAL_DAY_AHEAD, REAL_IMBALANCE)


def test_timestamps_snap_to_the_interval(source):
    t = day_timestamps(REAL_DATE)[70]
    assert source.at(t + 7 * 60).timestamp == t


def test_synthetic_day_is_repeatable_and_date_dependent():
    da, imb = tables()
    a = [PriceSource(da, imb).at(t) for t in day_timestamps(FUTURE_DATE)]
    b = [PriceSource(da, imb).at(t) for t in day_timestamps(FUTURE_DATE)]
    c = [PriceSource(da, imb).at(t) for t in day_timestamps("2027-03-11")]
    assert a == b
    assert [x.day_ahead_eur_mwh for x in a] != [x.day_ahead_eur_mwh for x in c]
    assert {x.day_ahead_source for x in a} == {SYNTHETIC}


def test_synthetic_day_follows_the_real_shape():
    """The real profile has a midday dip and an evening peak; so must a generated day."""
    da, imb = tables()
    curve = [PriceSource(da, imb).at(t).day_ahead_eur_mwh for t in day_timestamps(FUTURE_DATE)]
    midday, evening = sum(curve[40:60]) / 20, sum(curve[68:]) / 28
    assert midday < evening
    assert all(0 < v < 400 for v in curve)


def test_synthetic_imbalance_brackets_the_day_ahead_price():
    da = {t: -20.0 for t in day_timestamps(REAL_DATE)}  # negative prices too
    src = PriceSource(da, {})
    for t in day_timestamps(REAL_DATE):
        p = src.at(t)
        assert p.imbalance_source == SYNTHETIC and p.day_ahead_source == REAL_DAY_AHEAD
        assert p.imbalance_short_eur_mwh > p.day_ahead_eur_mwh > p.imbalance_long_eur_mwh


def test_no_files_at_all_still_answers(tmp_path):
    src = PriceSource(day_ahead_path=tmp_path / "none.csv", imbalance_path=tmp_path / "none2.csv")
    assert src.coverage() == {"day_ahead": None, "imbalance": None}
    assert src.at(day_timestamps(FUTURE_DATE)[0]).day_ahead_source == SYNTHETIC


def test_real_only_refuses_to_generate(source):
    assert source.at(day_timestamps(FUTURE_DATE)[0], synthetic=False) is None


# --------------------------------------------------------------------------- #
# routes


def test_health_reports_coverage(client):
    body = client.get("/health").json()
    stamps = day_timestamps(REAL_DATE)
    assert body["status"] == "ok"
    assert body["real_coverage"]["day_ahead"] == [stamps[0], stamps[-1]]


def test_day_route_returns_96_intervals(client):
    body = client.get(f"/prices/day/{REAL_DATE}").json()
    assert body["date"] == REAL_DATE and body["interval_minutes"] == 15
    assert len(body["intervals"]) == N
    assert [x["timestamp"] for x in body["intervals"]] == day_timestamps(REAL_DATE)
    assert body["source"] == {"day_ahead": REAL_DAY_AHEAD, "imbalance": REAL_IMBALANCE}
    first = body["intervals"][0]
    assert first["time"].startswith(f"{REAL_DATE}T00:00")
    assert first["day_ahead_eur_mwh"] == 80.0


def test_day_route_generates_uncovered_days(client):
    body = client.get(f"/prices/day/{FUTURE_DATE}").json()
    assert body["source"] == {"day_ahead": SYNTHETIC, "imbalance": SYNTHETIC}
    assert len(body["intervals"]) == N


def test_day_route_real_only_is_404_outside_the_data(client):
    r = client.get(f"/prices/day/{FUTURE_DATE}", params={"synthetic": "false"})
    assert r.status_code == 404
    assert "real_coverage" in r.json()["detail"]


@pytest.mark.parametrize("bad", ["2026-13-01", "tomorrow", "15-06-2026"])
def test_day_route_rejects_bad_dates(client, bad):
    assert client.get(f"/prices/day/{bad}").status_code == 422


def test_range_route_accepts_unix_and_iso(client):
    stamps = day_timestamps(REAL_DATE)
    by_unix = client.get("/prices", params={"start": stamps[0], "end": stamps[4]}).json()
    assert [x["timestamp"] for x in by_unix["intervals"]] == stamps[:4]
    iso = client.get("/prices", params={"start": "2026-06-14T21:00:00Z", "end": "2026-06-14T22:00:00"}).json()
    assert [x["timestamp"] for x in iso["intervals"]] == stamps[:4]  # 00:00 local = 21:00 UTC


def test_range_route_spanning_real_and_generated_is_mixed(client):
    stamps = day_timestamps(REAL_DATE)
    body = client.get("/prices", params={"start": stamps[-2], "end": stamps[-1] + 3 * STEP_S}).json()
    assert body["source"]["day_ahead"] == "mixed"
    assert len(body["intervals"]) == 4


@pytest.mark.parametrize("params", [
    {"start": 1800, "end": 900},
    {"start": 0, "end": 40 * 86_400},
    {"start": "yesterday", "end": 10},
])
def test_range_route_rejects_bad_ranges(client, params):
    assert client.get("/prices", params=params).status_code == 422


# --------------------------------------------------------------------------- #
# client


def test_fetch_day(client):
    day = fetch_day(REAL_DATE, client)
    assert len(day.day_ahead) == N and day.day_ahead[70] == 120.0
    assert day.imbalance[70].short_eur_mwh == 180.0
    assert not day.synthetic
    assert fetch_day(FUTURE_DATE, client).synthetic


def test_fetch_day_raises_on_http_errors(client):
    with pytest.raises(PriceApiError, match="404"):
        fetch_day(FUTURE_DATE, client, synthetic=False)


def test_fetch_day_raises_when_unreachable():
    with pytest.raises(PriceApiError, match="not reachable"):
        fetch_day(REAL_DATE, "http://127.0.0.1:9", timeout=0.5)


# --------------------------------------------------------------------------- #
# real_day on the API


def test_real_day_takes_prices_and_imbalance_from_the_api(client):
    day = real_day(REAL_DATE, price_api=client, imbalance=True,
                   models=stub_models(), data=national_rows(REAL_DATE))
    assert day.prices[70] == 120.0
    assert day.imbalance[70].short_eur_mwh == 180.0


def test_real_day_can_run_a_day_the_csv_does_not_have(client):
    day = real_day(FUTURE_DATE, price_api=client, models=stub_models(), data=national_rows(FUTURE_DATE))
    assert len(day.prices) == N


def test_real_day_falls_back_to_the_csv_when_the_api_is_down(tmp_path, caplog):
    csv = tmp_path / "prices.csv"
    pd.DataFrame({"timestamp": day_timestamps(REAL_DATE), "price": [42.0] * N}).to_csv(csv, index=False)
    day = real_day(REAL_DATE, price_api="http://127.0.0.1:9", prices_path=csv,
                   models=stub_models(), data=national_rows(REAL_DATE))
    assert set(day.prices) == {42.0}
    assert "reading the price CSVs instead" in caplog.text
