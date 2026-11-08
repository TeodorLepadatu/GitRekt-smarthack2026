"""Tests for the live loop (predictions/live.py) and the ENTSO-E API client (entsoe_api.py).

No network and no real training: ENTSO-E, Open-Meteo and the trainer are faked.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import entsoe_api
from predictions import core, live

DATASET = Path(__file__).resolve().parents[1] / "predictions" / "dataset.csv"
needs_dataset = pytest.mark.skipif(not DATASET.exists(), reason="predictions/dataset.csv not found")
UTC = timezone.utc
STEP = 900

NS = "urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0"


def _series(psr, start, end, resolution, points, curve="A01", out_domain=False):
    domain = ("<outBiddingZone_Domain.mRID codingScheme='A01'>10YRO-TEL------P</outBiddingZone_Domain.mRID>"
              if out_domain else
              "<inBiddingZone_Domain.mRID codingScheme='A01'>10YRO-TEL------P</inBiddingZone_Domain.mRID>")
    pts = "".join(f"<Point><position>{p}</position><quantity>{q}</quantity></Point>" for p, q in points)
    psr_xml = f"<MktPSRType><psrType>{psr}</psrType></MktPSRType>" if psr else ""
    return (f"<TimeSeries><mRID>1</mRID>{domain}<curveType>{curve}</curveType>{psr_xml}"
            f"<Period><timeInterval><start>{start}</start><end>{end}</end></timeInterval>"
            f"<resolution>{resolution}</resolution>{pts}</Period></TimeSeries>")


def _doc(*series):
    return f"<GL_MarketDocument xmlns='{NS}'><mRID>x</mRID>{''.join(series)}</GL_MarketDocument>".encode()


# --------------------------------------------------------------------------- #
# entsoe_api: XML parsing
# --------------------------------------------------------------------------- #
def test_parse_series_15min_hourly_and_a03():
    xml = _doc(
        _series("B16", "2026-11-07T00:00Z", "2026-11-07T01:00Z", "PT15M", [(1, 10), (2, 11), (3, 12), (4, 13)]),
        _series("B14", "2026-11-07T00:00Z", "2026-11-07T02:00Z", "PT60M", [(1, 1400), (2, 1390)]),
        _series("B19", "2026-11-07T00:00Z", "2026-11-07T01:00Z", "PT15M", [(1, 500), (3, 520)], curve="A03"),
    )
    (_, solar), (_, nuclear), (_, wind) = entsoe_api.parse_series(xml)
    t0 = datetime(2026, 11, 7, tzinfo=UTC)
    q = [t0 + timedelta(minutes=15 * k) for k in range(8)]
    assert solar == {q[0]: 10, q[1]: 11, q[2]: 12, q[3]: 13}
    assert nuclear == {**{q[k]: 1400 for k in range(4)}, **{q[k]: 1390 for k in range(4, 8)}}
    assert wind == {q[0]: 500, q[1]: 500, q[2]: 520, q[3]: 520}      # A03: a missing point repeats


def test_parse_series_no_data_and_rejection():
    ack = (b"<Acknowledgement_MarketDocument xmlns='urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:7:0'>"
           b"<Reason><code>999</code><text>No matching data found for Data item</text></Reason>"
           b"</Acknowledgement_MarketDocument>")
    assert entsoe_api.parse_series(ack) == []
    assert entsoe_api.parse_series(b"") == []
    bad = ack.replace(b"No matching data found for Data item", b"periodStart is invalid")
    with pytest.raises(entsoe_api.EntsoeApiError, match="periodStart"):
        entsoe_api.parse_series(bad)


def test_fetch_generation_maps_types_and_skips_consumption(monkeypatch):
    xml = _doc(
        _series("B16", "2026-11-07T00:00Z", "2026-11-07T00:30Z", "PT15M", [(1, 10), (2, 11)]),
        _series("B25", "2026-11-07T00:00Z", "2026-11-07T00:30Z", "PT15M", [(1, 5), (2, 6)]),
        _series("B25", "2026-11-07T00:00Z", "2026-11-07T00:30Z", "PT15M", [(1, 99), (2, 99)], out_domain=True),
        _series("B10", "2026-11-07T00:00Z", "2026-11-07T00:30Z", "PT15M", [(1, 7), (2, 7)]),  # not a dataset column
    )
    seen = {}
    monkeypatch.setattr(entsoe_api, "query", lambda params, token: seen.update(params) or xml)
    t0 = datetime(2026, 11, 7, tzinfo=UTC)
    gen = entsoe_api.fetch_generation(t0, t0 + timedelta(minutes=30), "tok")
    assert seen["documentType"] == "A75" and seen["in_Domain"] == "10YRO-TEL------P"
    assert seen["periodStart"] == "202611070000" and seen["periodEnd"] == "202611070030"
    assert gen == {t0: {"solar": 10, "energy_storage": 5},
                   t0 + timedelta(minutes=15): {"solar": 11, "energy_storage": 6}}


def test_fetch_load_clips_to_range(monkeypatch):
    xml = _doc(_series(None, "2026-11-07T00:00Z", "2026-11-07T01:00Z", "PT15M",
                       [(1, 5000), (2, 5010), (3, 5020), (4, 5030)]))
    monkeypatch.setattr(entsoe_api, "query", lambda params, token: xml)
    t0 = datetime(2026, 11, 7, tzinfo=UTC)
    load = entsoe_api.fetch_load(t0 + timedelta(minutes=15), t0 + timedelta(minutes=45), "tok")
    assert load == {t0 + timedelta(minutes=15): 5010, t0 + timedelta(minutes=30): 5020}


# --------------------------------------------------------------------------- #
# live: the website-export path reuses the scrapers' code
# --------------------------------------------------------------------------- #
LOAD_CSV = """MTU (CET/CEST),Area,Actual Total Load (MW),Day-ahead Total Load Forecast (MW)
07/11/2026 01:00 - 07/11/2026 01:15,BZN|RO,4628.69,4640.00
07/11/2026 01:15 - 07/11/2026 01:30,BZN|RO,-,4630.00
"""
GEN_CSV = """MTU (UTC),Area,Production Type,Generation (MW)
07/11/2026 00:00:00 - 07/11/2026 00:15:00,BZN|RO,Solar,0.00
07/11/2026 00:00:00 - 07/11/2026 00:15:00,BZN|RO,Wind Onshore,1101.17
07/11/2026 00:00:00 - 07/11/2026 00:15:00,BZN|RO,Hydro Pumped Storage,3.00
07/11/2026 00:15:00 - 07/11/2026 00:30:00,BZN|RO,Solar,n/e
"""


def test_export_path_parses_like_build_dataset(monkeypatch):
    import entsoe_export
    calls = []

    def fake_fetch(payload):
        calls.append(payload)
        return LOAD_CSV if payload["useCase"].startswith("load/") else GEN_CSV

    monkeypatch.setattr(entsoe_export, "fetch_window", fake_fetch)
    monkeypatch.setenv("ENTSOE_TOKEN", "abc")
    t0 = datetime(2026, 11, 7, tzinfo=UTC)
    df = live._fetch_export(t0, t0 + timedelta(hours=1))
    assert [c["parameters"]["timeZone"] for c in calls] == ["CET", "UTC"]
    first = df[df.timestamp == int(t0.timestamp())].iloc[0]          # 01:00 CET = 00:00 UTC
    assert first.actual_total_load == pytest.approx(4628.69)
    assert first.wind_onshore == pytest.approx(1101.17) and first.solar == 0
    second = df[df.timestamp == int(t0.timestamp()) + STEP].iloc[0]
    assert np.isnan(second.actual_total_load) and np.isnan(second.solar)   # "-" and "n/e"


def test_energy_source_prefers_the_official_api(monkeypatch):
    for k in ("ENTSOE_API_TOKEN", "ENTSOE_TOKEN", "ENTSOE_COOKIE"):
        monkeypatch.delenv(k, raising=False)
    assert live.energy_source() == "transelectrica"          # no login needed
    monkeypatch.setenv("ENTSOE_TOKEN", "browser")
    assert live.energy_source() == "export"
    monkeypatch.setenv("ENTSOE_API_TOKEN", "permanent")
    assert live.energy_source() == "api"


# --------------------------------------------------------------------------- #
# Transelectrica (no login)
# --------------------------------------------------------------------------- #
SEN = ("31-10-2026 00:04:46;5585;5463;4818;766;651;1487;1274;0;1230;-15;65;|"
       "31-10-2026 00:12:00;5600;5463;4900;700;650;1500;1300;0;1240;-15;65;|"
       "25-10-2026 03:30:00;5000;5000;4000;1000;600;1400;1000;0;900;-10;60;|"   # repeated DST hour
       "garbage|")


def test_transelectrica_parse_and_intervals():
    import transelectrica
    s = transelectrica.parse(SEN)
    assert len(s) == 2                                        # the ambiguous DST sample is dropped
    assert str(s.utc.iloc[0]) == "2026-10-30 22:04:46+00:00"  # EEST (UTC+2) on 31 Oct
    q = transelectrica.to_intervals(s)
    assert q.timestamp.tolist() == [int(datetime(2026, 10, 30, 22, 0, tzinfo=UTC).timestamp())]
    assert q.consumption.iloc[0] == pytest.approx((5585 + 5600) / 2)
    with pytest.raises(transelectrica.TranselectricaError):
        transelectrica.parse("<html>maintenance</html>")


def test_transelectrica_maps_to_the_dataset_columns(monkeypatch):
    import transelectrica
    monkeypatch.setattr(transelectrica, "fetch_samples", lambda start, end: transelectrica.parse(SEN))
    t0 = datetime(2026, 10, 30, 22, tzinfo=UTC)
    df = live._fetch_transelectrica(t0, t0 + timedelta(hours=1))
    row = df.iloc[0]
    assert row.actual_total_load == pytest.approx(5592.5) and row["fossil_brown_coal/Lignite"] == 650.5
    assert row.solar == 0                                     # -15 MW night self-consumption -> 0
    listed = 650.5 + 1493.5 + 1287 + 0 + 1235 + 0 + 65
    assert row.energy_storage == pytest.approx(4859 - listed)  # what PROD holds beyond the listed types
    parts = ["biomass", "energy_storage", "fossil_brown_coal/Lignite", "fossil_gas", "nuclear", "solar",
             "wind_onshore", "hydro_total"]
    assert row[parts].sum() == pytest.approx(4859)            # the columns add up to Transelectrica's total
    assert row.hydro_total == pytest.approx(1287)


@needs_dataset
def test_split_hydro_keeps_the_total(recent):
    e = pd.DataFrame({"timestamp": [1], "hydro_run_of_river_and_pondage": [np.nan],
                      "hydro_water_reservoir": [np.nan], "hydro_total": [1000.0]})
    out = live.split_hydro(e, recent)
    assert "hydro_total" not in out.columns
    assert out.hydro_run_of_river_and_pondage.iloc[0] + out.hydro_water_reservoir.iloc[0] == pytest.approx(1000)
    assert 0 < out.hydro_run_of_river_and_pondage.iloc[0] < 1000


@needs_dataset
def test_transelectrica_never_replaces_entsoe_rows(tmp_path, recent):
    d, base = _setup(tmp_path, recent)
    other = recent.copy()
    other["fossil_gas"] += 80                                 # a different source's level
    def get(start, end):
        e, _ = _energy_from(other)(start, end)
        return e, "transelectrica"
    now = datetime.fromtimestamp(int(recent.timestamp.max()) + STEP, UTC)
    s = live.tick(now, directory=d, get_energy=get, get_weather=_weather_from(recent), train=None)
    assert (s["added"], s["revised"]) == (96, 0)
    grown = live.read_dataset(d / "dataset.csv")
    old = grown[grown.timestamp.isin(base.timestamp)]
    assert np.allclose(old.fossil_gas, base.fossil_gas, equal_nan=True)      # ENTSO-E rows untouched
    assert np.allclose(grown.tail(96).fossil_gas, other.tail(96).fossil_gas)


# --------------------------------------------------------------------------- #
# live: rows, merge, tick
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def recent() -> pd.DataFrame:
    """The last 10 days of the real dataset."""
    if not DATASET.exists():
        pytest.skip("predictions/dataset.csv not found")
    df = live.read_dataset(DATASET)
    return df[df.timestamp > df.timestamp.max() - 10 * 86_400].reset_index(drop=True)


@needs_dataset
def test_build_rows_reproduces_the_training_rows(recent):
    """Energy + weather of real rows, rebuilt by the live loop, give the same rows back."""
    sample = recent.tail(200)
    weather = sample[["timestamp", *core.WEATHER]]
    rebuilt = live.build_rows(sample[["timestamp", *live.ENERGY_COLS]], weather, list(recent.columns))
    pd.testing.assert_frame_equal(rebuilt.reset_index(drop=True).round(2),
                                  sample.reset_index(drop=True).round(2), check_dtype=False)


def _shifted(rows: pd.DataFrame, days: int) -> pd.DataFrame:
    out = rows.copy()
    out["timestamp"] = out["timestamp"] + days * 86_400
    return out


@needs_dataset
def test_merge_counts_added_and_revised(recent):
    base = recent.copy()
    new = _shifted(base.tail(4), 1)                      # 4 intervals after the end
    old = base.tail(2).copy()
    old.loc[old.index[0], "solar"] += 50                 # an ENTSO-E correction
    old.loc[old.index[1], "temperature_2m"] += 0.3       # weather only: not a revision
    merged, added, revised = live.merge(base, pd.concat([old, new]))
    assert (added, revised) == (4, 1)
    assert len(merged) == len(base) + 4 and merged.timestamp.is_monotonic_increasing
    assert merged.timestamp.is_unique and list(merged.columns) == list(base.columns)
    assert merged.loc[merged.timestamp == old.timestamp.iloc[0], "solar"].iloc[0] == old.solar.iloc[0]


@needs_dataset
def test_required_columns_follow_the_last_week(recent):
    need = live.required_columns(recent)
    assert "actual_total_load" in need and "solar" in need and "wind_onshore" in need
    blank = recent.copy()
    blank.loc[blank.index[-96 * 3:], "biomass"] = np.nan   # stopped being published 3 days ago
    assert "biomass" not in live.required_columns(blank)


class FakeTrainer:
    def __init__(self):
        self.calls = []

    def __call__(self, data_path, out_dir, full):
        self.calls.append((Path(data_path), Path(out_dir), full))
        return {"kind": "fit" if full else "refit", "seconds": 0, "trained_until": "x"}


def _setup(tmp_path, recent):
    d = tmp_path / "live"
    d.mkdir()
    base = recent.iloc[:-96]                              # one day short of the real data
    live.write_dataset(base, d / "dataset.csv")
    return d, base


def _energy_from(rows):
    def get(start, end):
        t0, t1 = int(start.timestamp()), int(end.timestamp())
        e = rows[(rows.timestamp >= t0) & (rows.timestamp < t1)]
        return e[["timestamp", *live.ENERGY_COLS]].reset_index(drop=True), "fake"
    return get


def _weather_from(rows):
    return lambda ts: rows.set_index("timestamp").loc[ts, core.WEATHER].reset_index()


@needs_dataset
def test_tick_adds_published_intervals_and_retrains(tmp_path, recent):
    d, base = _setup(tmp_path, recent)
    missing = recent.iloc[-96:].copy()
    missing.loc[missing.index[-3:], "actual_total_load"] = np.nan     # not published yet
    now = datetime.fromtimestamp(int(recent.timestamp.max()) + 2 * STEP, UTC)
    trainer = FakeTrainer()
    s = live.tick(now, directory=d, get_energy=_energy_from(pd.concat([base, missing])),
                  get_weather=_weather_from(recent), train=trainer)
    assert s["error"] is None and s["energy_source"] == "fake"
    assert s["added"] == 93 and s["revised"] == 0
    grown = live.read_dataset(d / "dataset.csv")
    assert len(grown) == len(base) + 93
    added = grown.tail(93).reset_index(drop=True).round(2)
    pd.testing.assert_frame_equal(added, recent.iloc[-96:-3].reset_index(drop=True).round(2), check_dtype=False)
    assert trainer.calls == [(d / "dataset.csv", d / "models", trainer.calls[0][2])]
    assert s["training_pending"] is False and s["trained"]["kind"] in ("fit", "refit")
    assert live.read_status(d)["added"] == 93

    # Same data again: nothing new, no retrain. Then the last 3 are published.
    s = live.tick(now, directory=d, get_energy=_energy_from(pd.concat([base, missing])),
                  get_weather=_weather_from(recent), train=trainer)
    assert (s["added"], s["revised"], len(trainer.calls)) == (0, 0, 1)
    s = live.tick(now + timedelta(minutes=15), directory=d, get_energy=_energy_from(recent),
                  get_weather=_weather_from(recent), train=trainer)
    assert (s["added"], len(trainer.calls)) == (3, 2)
    assert len(live.read_dataset(d / "dataset.csv")) == len(recent)


@needs_dataset
def test_tick_survives_a_failed_fetch(tmp_path, recent):
    d, base = _setup(tmp_path, recent)
    trainer = FakeTrainer()

    def broken(start, end):
        raise RuntimeError("HTTP 401: token expired")

    now = datetime.fromtimestamp(int(recent.timestamp.max()), UTC)
    s = live.tick(now, directory=d, get_energy=broken, get_weather=_weather_from(recent), train=trainer)
    assert "token expired" in s["error"] and s["added"] == 0 and trainer.calls == []
    assert len(live.read_dataset(d / "dataset.csv")) == len(base)


@needs_dataset
def test_tick_retries_a_failed_training(tmp_path, recent):
    d, base = _setup(tmp_path, recent)
    now = datetime.fromtimestamp(int(recent.timestamp.max()) + STEP, UTC)

    def failing(*a):
        raise MemoryError("out of memory")

    s = live.tick(now, directory=d, get_energy=_energy_from(recent), get_weather=_weather_from(recent),
                  train=failing)
    assert s["added"] == 96 and "training failed" in s["error"] and s["training_pending"] is True
    trainer = FakeTrainer()
    s = live.tick(now, directory=d, get_energy=_energy_from(recent), get_weather=_weather_from(recent),
                  train=trainer)
    assert s["added"] == 0 and len(trainer.calls) == 1 and s["training_pending"] is False


@needs_dataset
def test_full_fit_once_a_day(tmp_path, recent):
    d, _ = _setup(tmp_path, recent)
    now = datetime.fromtimestamp(int(recent.timestamp.max()) + STEP, UTC)
    trainer = FakeTrainer()
    live.write_status(d, {"last_full_fit_ts": int(now.timestamp()) - 3600})
    live.tick(now, directory=d, get_energy=_energy_from(recent), get_weather=_weather_from(recent), train=trainer)
    assert trainer.calls[-1][2] is False                  # fitted an hour ago: refit
    live.write_status(d, {"last_full_fit_ts": int(now.timestamp()) - 25 * 3600, "training_pending": True})
    s = live.tick(now, directory=d, get_energy=_energy_from(recent), get_weather=_weather_from(recent),
                  train=trainer)
    assert trainer.calls[-1][2] is True and s["last_full_fit_ts"] == int(now.timestamp())


# --------------------------------------------------------------------------- #
# The app reads the live files only when asked to
# --------------------------------------------------------------------------- #
def test_active_paths_are_opt_in(tmp_path, monkeypatch):
    (tmp_path / "models").mkdir()
    (tmp_path / "dataset.csv").write_text("x")
    (tmp_path / "models" / "demand_model.pkl").write_text("x")
    monkeypatch.setenv("PREDICTIONS_LIVE_DIR", str(tmp_path))
    monkeypatch.delenv("PREDICTIONS_LIVE", raising=False)
    assert core.active_dataset_path() == core.DATASET_CSV
    assert core.active_model_path("demand_model") == core.MODELS_DIR / "demand_model.pkl"
    monkeypatch.setenv("PREDICTIONS_LIVE", "1")
    assert core.active_dataset_path() == tmp_path / "dataset.csv"
    assert core.active_model_path("demand_model") == tmp_path / "models" / "demand_model.pkl"
    # no live production model yet: the original one
    assert core.active_model_path("production_model") == core.MODELS_DIR / "production_model.pkl"


def test_live_never_targets_the_originals():
    """The loop writes only under live_dir(); the originals are only ever read."""
    src = Path(live.__file__).read_text(encoding="utf-8")
    assert "write_dataset(merged, data_path)" in src
    assert "DATASET_CSV, data_path" in src                    # copied from, never written to
    assert core.live_dir() != core.MODELS_DIR and core.live_dir() != core.DATASET_CSV.parent / "models"
