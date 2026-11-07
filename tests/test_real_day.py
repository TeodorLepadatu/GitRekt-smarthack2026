"""`loop.real_day`: the ML models and the ENTSO-E price file as a day the loop can run.

Most tests here drive the code with stub models and an injected price table, so
they are fast and need no files; the ones at the end load the real models and
the real CSVs and are skipped when those are not there.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from loop import DayRunner, RealDayError, SiteScale, real_day
from loop.real_day import (
    LOCAL_TZ,
    N,
    NATIONAL_P99_MW,
    PRICES_CSV,
    STEP_S,
    day_timestamps,
    gate_closure,
    load_prices,
    site_actuals,
    site_forecasts,
)

pd = pytest.importorskip("pandas")


def no_llm(prompt: str) -> str:
    """``run_cycle`` defaults ``llm_call`` to a real ``DecisionAgent``; these tests
    check the day's data, so they take the rule-based path and call no model."""
    raise RuntimeError("no LLM in these tests")


# --------------------------------------------------------------------------- #
# stubs


class StubModel:
    """Returns a fixed value per column, so scaling is easy to check."""

    def __init__(self, columns: dict[str, float], last_known_ts: int = 2 ** 31):
        self.columns = columns
        self.last_known_ts = last_known_ts
        self.calls: list[dict] = []

    def predict(self, timestamps, issued_at=None, allow_fetch=True):
        self.calls.append({"n": len(timestamps), "issued_at": issued_at, "allow_fetch": allow_fetch})
        return pd.DataFrame({name: [value] * len(timestamps) for name, value in self.columns.items()})


def stub_models(demand=5000.0, solar=1000.0, wind=500.0, spread=0.1):
    """Models whose output is flat, with a +-`spread` fraction band."""
    return {
        "demand": StubModel({"demand_mw": demand, "demand_low_mw": demand * (1 - spread),
                             "demand_high_mw": demand * (1 + spread)}),
        "production": StubModel({
            "solar_mw": solar, "solar_low_mw": solar * (1 - spread), "solar_high_mw": solar * (1 + spread),
            "wind_mw": wind, "wind_low_mw": wind * (1 - spread), "wind_high_mw": wind * (1 + spread),
        }),
    }


def flat_prices(date: str, value: float = 90.0) -> dict[int, float]:
    return {t: value for t in day_timestamps(date)}


def national_rows(date: str, demand=5000.0, solar=1000.0, wind=500.0):
    stamps = day_timestamps(date)
    return pd.DataFrame({"timestamp": stamps, "actual_total_load": [demand] * len(stamps),
                         "solar": [solar] * len(stamps), "wind_onshore": [wind] * len(stamps)})


# --------------------------------------------------------------------------- #
# the day's clock


def test_day_timestamps_is_a_local_day():
    stamps = day_timestamps("2026-06-15")
    assert len(stamps) == N
    assert all(b - a == STEP_S for a, b in zip(stamps, stamps[1:]))
    first = datetime.fromtimestamp(stamps[0], LOCAL_TZ)
    assert (first.hour, first.minute) == (0, 0)
    assert first.date().isoformat() == "2026-06-15"


@pytest.mark.parametrize("date", ["2026-01-15", "2026-03-29", "2026-06-15", "2026-10-25"])
def test_day_is_always_96_intervals(date):
    """Including the two DST days, which a local calendar would make 92 or 100."""
    assert len(day_timestamps(date)) == N


@pytest.mark.parametrize("bad", ["15-06-2026", "2026-6-15x", "", None, "tomorrow"])
def test_bad_dates_are_rejected(bad):
    with pytest.raises(RealDayError):
        day_timestamps(bad)


def test_gate_closure_is_noon_the_day_before():
    gate = datetime.fromtimestamp(gate_closure("2026-06-15"), LOCAL_TZ)
    assert gate.date().isoformat() == "2026-06-14"
    assert (gate.hour, gate.minute) == (12, 0)
    assert gate_closure("2026-06-15") < day_timestamps("2026-06-15")[0]


# --------------------------------------------------------------------------- #
# scaling


def test_factors_put_the_national_peak_at_the_site_peak():
    scale = SiteScale(demand_peak_mw=4.5, solar_peak_mw=6.2, wind_peak_mw=1.5)
    f = scale.factors
    assert NATIONAL_P99_MW["demand"] * f["demand"] == pytest.approx(4.5)
    assert NATIONAL_P99_MW["solar"] * f["solar"] == pytest.approx(6.2)
    assert NATIONAL_P99_MW["wind"] * f["wind"] == pytest.approx(1.5)


def test_default_scale_is_the_sites_size():
    """The defaults must match the site demo.py describes, or runs stop comparing."""
    f = SiteScale().factors
    assert NATIONAL_P99_MW["demand"] * f["demand"] == pytest.approx(4.5)
    assert 5.0 < NATIONAL_P99_MW["solar"] * f["solar"] < 7.0


@pytest.mark.parametrize("kwargs", [{"demand_peak_mw": -1}, {"solar_peak_mw": float("nan")},
                                    {"wind_peak_mw": float("inf")}, {"demand_peak_mw": "big"}])
def test_bad_scales_are_rejected(kwargs):
    with pytest.raises(RealDayError):
        SiteScale(**kwargs)


def test_forecasts_are_scaled_to_the_site():
    scale = SiteScale()
    f = scale.factors
    out = site_forecasts(day_timestamps("2026-06-15")[:4], issued_at=0, scale=scale,
                         models=stub_models(demand=5000.0, solar=1000.0, wind=500.0))
    assert len(out) == 4
    one = out[0]
    assert one.demand_mw == pytest.approx(5000.0 * f["demand"], abs=1e-3)
    assert one.solar_mw == pytest.approx(1000.0 * f["solar"], abs=1e-3)
    assert one.wind_mw == pytest.approx(500.0 * f["wind"], abs=1e-3)
    assert one.production_mw == pytest.approx(one.solar_mw + one.wind_mw, abs=1e-3)


def test_forecast_band_brackets_the_forecast():
    out = site_forecasts(day_timestamps("2026-06-15")[:2], issued_at=0, scale=SiteScale(),
                         models=stub_models(spread=0.2))
    for one in out:
        assert 0 <= one.prod_low_mw < one.production_mw < one.prod_high_mw
        # independent errors: the combined band is narrower than the two added up
        separate = 0.2 * one.solar_mw + 0.2 * one.wind_mw
        assert one.prod_high_mw - one.production_mw < separate + 1e-9


def test_forecasts_never_go_negative():
    models = stub_models(demand=-100.0, solar=-50.0, wind=-10.0, spread=2.0)
    for one in site_forecasts(day_timestamps("2026-06-15")[:2], issued_at=0,
                              scale=SiteScale(), models=models):
        assert one.demand_mw >= 0 and one.production_mw >= 0
        assert one.solar_mw >= 0 and one.wind_mw >= 0 and one.prod_low_mw >= 0


def test_issued_at_is_clamped_to_the_end_of_the_data():
    """A day past the data must still forecast, from the last moment the models know."""
    models = stub_models()
    last = 1_700_000_000
    models["demand"].last_known_ts = last
    models["production"].last_known_ts = last
    site_forecasts([last + 10 * STEP_S], issued_at=last + 99 * STEP_S,
                   scale=SiteScale(), models=models)
    assert models["demand"].calls[0]["issued_at"] == last + STEP_S


def test_models_must_come_as_a_pair():
    with pytest.raises(RealDayError, match="both"):
        site_forecasts([0], issued_at=0, scale=SiteScale(), models={"demand": StubModel({})})


def test_a_model_error_says_what_to_do():
    class Broken(StubModel):
        def predict(self, *a, **k):
            raise ValueError("timestamps must lie between ...")

    with pytest.raises(RealDayError, match="7 days"):
        site_forecasts([0], issued_at=0, scale=SiteScale(),
                       models={"demand": Broken({}), "production": Broken({})})


# --------------------------------------------------------------------------- #
# actuals


def test_actuals_are_scaled_and_merged():
    date = "2026-06-15"
    f = SiteScale().factors
    out = site_actuals(day_timestamps(date)[:3], scale=SiteScale(),
                       data=national_rows(date, demand=6000.0, solar=800.0, wind=400.0))
    assert len(out) == 3
    assert out[0]["demand_mw"] == pytest.approx(6000.0 * f["demand"], abs=1e-3)
    assert out[0]["production_mw"] == pytest.approx(800.0 * f["solar"] + 400.0 * f["wind"], abs=1e-3)


def test_missing_and_invalid_actuals_become_none():
    date = "2026-06-15"
    rows = national_rows(date)
    rows.loc[1, "solar"] = float("nan")  # one of the dataset's invalid records
    out = site_actuals(day_timestamps(date)[:3] + [1], scale=SiteScale(), data=rows)
    assert out[0] is not None
    assert out[1] is None, "an invalid record must not be settled against"
    assert out[3] is None, "a timestamp outside the data must not be settled against"


# --------------------------------------------------------------------------- #
# prices


def test_load_prices_snaps_and_fills_hourly_rows(tmp_path):
    p = tmp_path / "prices.csv"
    p.write_text("timestamp,Day-ahead Price (EUR/MWh)\n3600,10\n7200,20\n", encoding="utf-8")
    table = load_prices(p)
    assert table[3600] == 10.0
    assert [table[3600 + k * STEP_S] for k in range(4)] == [10.0] * 4
    assert table[7200] == 20.0


def test_load_prices_ignores_unparsable_rows(tmp_path):
    p = tmp_path / "prices.csv"
    p.write_text("timestamp,Day-ahead Price (EUR/MWh)\n3600,n/a\n7200,20\n", encoding="utf-8")
    assert 3600 not in load_prices(p)


def test_missing_price_file_says_how_to_get_one(tmp_path):
    with pytest.raises(RealDayError, match="energy_price.py"):
        load_prices(tmp_path / "nope.csv")


def test_a_day_without_prices_is_refused():
    with pytest.raises(RealDayError, match="no day-ahead price"):
        real_day("2026-06-15", prices={}, models=stub_models(),
                 data=national_rows("2026-06-15"))


def test_given_prices_must_cover_the_day():
    with pytest.raises(RealDayError, match="for a 96-interval day"):
        real_day("2026-06-15", prices=[50.0] * 10, models=stub_models(),
                 data=national_rows("2026-06-15"))


# --------------------------------------------------------------------------- #
# the whole day


def test_real_day_builds_a_runnable_day():
    date = "2026-06-15"
    day = real_day(date, prices=flat_prices(date, 90.0), models=stub_models(),
                   data=national_rows(date))
    assert day.date == date and day.n == N
    assert day.prices == [90.0] * N
    assert len(day.forecasts) == N and len(day.actuals) == N
    DayRunner(day, None, start_soc_mwh=4.0, llm_call=no_llm).run()


def test_real_day_defaults_to_the_gate_closure():
    date = "2026-06-15"
    models = stub_models()
    real_day(date, prices=flat_prices(date), models=models, data=national_rows(date))
    assert models["demand"].calls[0]["issued_at"] == gate_closure(date)


def test_plan_mode_leaves_out_the_actuals():
    date = "2026-06-15"
    day = real_day(date, prices=flat_prices(date), models=stub_models(),
                   data=national_rows(date), with_actuals=False)
    assert day.actuals is None


def test_a_real_day_settles_on_the_actuals_not_the_forecast():
    """The books must move when the measurement differs from the forecast."""
    date = "2026-06-15"
    common = dict(prices=flat_prices(date, 90.0), models=stub_models(solar=1000.0))
    on_forecast = real_day(date, **common, data=national_rows(date, solar=1000.0))
    on_actual = real_day(date, **common, data=national_rows(date, solar=1500.0))
    assert on_actual.actuals[0]["production_mw"] > on_forecast.actuals[0]["production_mw"]

    def profit(day):
        r = DayRunner(day, None, start_soc_mwh=4.0, llm_call=no_llm)
        r.run()
        return r.summary()["metrics"]["profit_today_eur"]

    assert profit(on_actual) != profit(on_forecast)


# --------------------------------------------------------------------------- #
# with the real models and the real files


real_models = pytest.mark.skipif(
    not (Path("predictions/models/demand_model.pkl").exists()
         and Path("predictions/models/production_model.pkl").exists()),
    reason="trained models not found - run predictions/train_and_evaluate.ipynb",
)
real_prices = pytest.mark.skipif(not PRICES_CSV.exists(), reason=f"{PRICES_CSV} not found")

DATE = "2026-06-15"  # a summer day inside both the price file and the dataset


@pytest.fixture(scope="module")
def real(request):
    return real_day(DATE, allow_fetch=False)


@real_models
@real_prices
def test_real_day_from_the_real_feeds(real):
    assert real.n == N and real.date == DATE
    assert all(f.demand_mw > 0 for f in real.forecasts), "the site always draws something"
    assert any(f.solar_mw > 1.0 for f in real.forecasts), "a June day must have sun"
    assert max(f.solar_mw for f in real.forecasts) < 15.0, "site scale, not national"
    assert max(f.demand_mw for f in real.forecasts) < 10.0
    assert sum(1 for a in real.actuals if a is not None) == N, "the whole day is in the dataset"


@real_models
@real_prices
def test_real_prices_are_a_real_curve(real):
    assert len(set(real.prices)) > 20, "a real day's prices move"
    assert all(-500 < p < 4000 for p in real.prices)


@real_models
@real_prices
def test_the_forecast_is_close_to_what_happened(real):
    """A loose sanity bound: the models are good to a few percent of site scale."""
    errors = [abs(f.demand_mw - a["demand_mw"]) for f, a in zip(real.forecasts, real.actuals)]
    assert sum(errors) / len(errors) < 1.0

    inside = [a["production_mw"] for f, a in zip(real.forecasts, real.actuals)
              if f.prod_low_mw <= a["production_mw"] <= f.prod_high_mw]
    assert len(inside) / N > 0.5, "the 10-90 % band should hold most of the time"


@real_models
@real_prices
def test_a_real_day_runs_through_the_whole_cycle(real):
    goals = pytest.importorskip("goal_parser").parse_goals(
        "Store 6 MWh by 18:00", battery_capacity_mwh=10).config
    runner = DayRunner(real, goals, start_soc_mwh=4.0, llm_call=no_llm)
    runner.run()
    s = runner.summary()
    assert s["intervals_run"] == N
    assert s["final_goal_status"] == {"g1": "met"}
    m = s["metrics"]
    assert m["decisions_total"] == N
    assert m["demand_mae_mw"] is not None, "actuals were used, so errors are measurable"
    assert 0.0 <= m["production_range_coverage"] <= 1.0
