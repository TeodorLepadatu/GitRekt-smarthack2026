"""Mock energy price API: day-ahead and imbalance prices per 15-minute interval.

    python -m price_api                      # http://127.0.0.1:8100, docs at /docs

Endpoints
    GET /health                     status and the real data's coverage
    GET /prices/day/{date}          the 96 intervals of a local day (Europe/Bucharest), YYYY-MM-DD
    GET /prices?start=..&end=..     any range, start inclusive, end exclusive, at most 31 days;
                                    unix seconds or ISO-8601 (naive = UTC)

Both price endpoints take ``synthetic`` (default true). True fills every interval
the real files do not cover with a generated price, so any date answers; false
answers 404 instead. Every interval says where each of its prices came from.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

from loop.real_day import LOCAL_TZ, STEP_S, RealDayError, day_timestamps

from .source import SYNTHETIC, IntervalPrice, PriceSource

MAX_RANGE_S = 31 * 86_400


def create_app(source: Optional[PriceSource] = None) -> FastAPI:
    src = source or PriceSource()
    app = FastAPI(title="Mock energy price API",
                  description="Day-ahead (ENTSO-E) and imbalance prices for Romania, 15-min, EUR/MWh.")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "timezone": str(LOCAL_TZ), "interval_minutes": STEP_S // 60,
                "real_coverage": src.coverage()}

    @app.get("/prices/day/{date}")
    def prices_day(date: str, synthetic: bool = True) -> dict:
        try:
            stamps = day_timestamps(date)
        except RealDayError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"date": date, **_payload(src, stamps, synthetic)}

    @app.get("/prices")
    def prices_range(start: str = Query(..., description="unix seconds or ISO-8601"),
                     end: str = Query(..., description="unix seconds or ISO-8601, exclusive"),
                     synthetic: bool = True) -> dict:
        a, b = _stamp(start, "start"), _stamp(end, "end")
        a = (a // STEP_S) * STEP_S
        if b <= a:
            raise HTTPException(422, "end must be after start")
        if b - a > MAX_RANGE_S:
            raise HTTPException(422, f"range is longer than {MAX_RANGE_S // 86_400} days")
        return _payload(src, list(range(a, b, STEP_S)), synthetic)

    return app


def _payload(src: PriceSource, stamps: list[int], synthetic: bool) -> dict:
    rows: list[IntervalPrice] = []
    for t in stamps:
        p = src.at(t, synthetic=synthetic)
        if p is None:
            raise HTTPException(404, {
                "error": f"no real price for {_iso(t)}; pass synthetic=true to generate one",
                "real_coverage": src.coverage(),
            })
        rows.append(p)
    return {
        "timezone": str(LOCAL_TZ),
        "interval_minutes": STEP_S // 60,
        "currency": "EUR",
        "unit": "MWh",
        "source": {"day_ahead": _summary(r.day_ahead_source for r in rows),
                   "imbalance": _summary(r.imbalance_source for r in rows)},
        "intervals": [{
            "timestamp": r.timestamp,
            "time": _iso(r.timestamp),
            "day_ahead_eur_mwh": r.day_ahead_eur_mwh,
            "imbalance_short_eur_mwh": r.imbalance_short_eur_mwh,
            "imbalance_long_eur_mwh": r.imbalance_long_eur_mwh,
            "source": {"day_ahead": r.day_ahead_source, "imbalance": r.imbalance_source},
        } for r in rows],
    }


def _summary(sources) -> str:
    kinds = set(sources)
    if len(kinds) == 1:
        return kinds.pop()
    return "mixed" if SYNTHETIC in kinds else "real"


def _stamp(raw: str, name: str) -> int:
    text = raw.strip()
    try:
        value = int(text)
        return value // 1000 if value > 10**11 else value  # milliseconds, as predictions/api.py
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(422, f"{name} must be unix seconds or ISO-8601, got {raw!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), LOCAL_TZ).isoformat(timespec="minutes")


app = create_app()  # the CSVs are read on the first request, not on import
