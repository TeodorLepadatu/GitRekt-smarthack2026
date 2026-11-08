"""HTTP API for the demand and production forecasters.

Run from the repo root:
    uvicorn predictions.api:app --reload --port 8000
Interactive docs: http://localhost:8000/docs

Endpoints
    GET  /health                 model status, latest data, allowed forecast window
    POST /forecast/demand        demand forecast
    POST /forecast/production    solar / wind / total production forecast
    POST /forecast               both, merged per timestamp
    GET  /csv                    list the CSV files under predictions/ (dataset, reports, weather)
    GET  /csv/{name}             download one, optionally only rows with start <= timestamp < end

Request body (either `timestamps`, or `start` + `end`):
    {"timestamps": [1794050100, "2026-11-08T18:00:00Z"], "issued_at": null}
    {"start": "2026-11-08T00:00:00Z", "end": "2026-11-09T00:00:00Z", "format": "csv"}
Timestamps are unix seconds (or milliseconds, auto-detected) or ISO-8601 strings
(naive = UTC), snapped down to the 15-min grid.
`issued_at` (the forecast's "now") defaults to just after the latest known data.
`format`: "json" (default) or "csv" (the predictions table as a CSV download).

With PREDICTIONS_LIVE=1 the API serves the live loop's data and retrained models
(predictions/live.py) and reloads them whenever the loop writes new ones.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, TypeAdapter, ValidationError, model_validator

from predictions.core import MAX_LEAD_STEPS, STEP, active_dataset_path, active_model_path, load_data
from predictions.demand_model import DemandForecaster
from predictions.production_model import ProductionForecaster

MAX_POINTS = 5000
MS_THRESHOLD = 10**11          # integers above this are milliseconds (10**11 s is the year 5138)
Stamp = int | datetime
CSV_ROOT = Path(__file__).resolve().parent     # CSV files served by /csv live under predictions/

MODELS: dict[str, DemandForecaster | ProductionForecaster] = {}
LOAD_ERRORS: dict[str, str] = {}
_LOADED: dict[str, tuple] = {}          # what MODELS was loaded from: path -> mtime
_RELOAD_LOCK = threading.Lock()


def _sources() -> dict[str, tuple]:
    models_dir = os.environ.get("PREDICTIONS_MODELS_DIR")   # optional override of predictions/models
    paths = {"data": active_dataset_path()}                 # live files only with PREDICTIONS_LIVE=1
    for key, cls in (("demand", DemandForecaster), ("production", ProductionForecaster)):
        paths[key] = Path(models_dir) / f"{cls.NAME}.pkl" if models_dir else active_model_path(cls.NAME)
    return {k: (str(p), p.stat().st_mtime_ns if p.exists() else 0) for k, p in paths.items()}


def _load_models() -> None:
    sources = _sources()
    data = load_data(Path(sources["data"][0]))
    models, errors = {}, {}
    for key, cls in (("demand", DemandForecaster), ("production", ProductionForecaster)):
        path = Path(sources[key][0])
        try:
            models[key] = cls.load(path, data=data)
        except FileNotFoundError:
            errors[key] = (f"{path} not found - run "
                           "predictions/train_and_evaluate.ipynb to train and save it")
    MODELS.clear(); MODELS.update(models)          # noqa: E702  (swap in one step)
    LOAD_ERRORS.clear(); LOAD_ERRORS.update(errors)  # noqa: E702
    _LOADED.clear(); _LOADED.update(sources)        # noqa: E702


def _reload_if_changed() -> None:
    """Pick up the live loop's new data and models (checked per request; a stat call or two)."""
    if _LOADED and _sources() != _LOADED:
        with _RELOAD_LOCK:
            if _sources() != _LOADED:
                _load_models()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_models()
    yield


app = FastAPI(title="Energy forecast API", version="1.0", lifespan=lifespan,
              description="Demand and production forecasts for any 15-min timestamps up to 7 days ahead.")


# --------------------------------------------------------------------------- #
class ForecastRequest(BaseModel):
    timestamps: list[Stamp] | None = Field(None, description="unix seconds or ISO-8601 (naive = UTC)")
    start: Stamp | None = Field(None, description="range start (inclusive), alternative to timestamps")
    end: Stamp | None = Field(None, description="range end (exclusive)")
    issued_at: Stamp | None = Field(None, description="forecast 'now'; default = just after latest data")
    allow_fetch: bool = Field(True, description="fetch live weather forecasts if needed")
    format: Literal["json", "csv"] = Field("json", description="response format")

    @model_validator(mode="after")
    def _check(self):
        if (self.timestamps is None) == (self.start is None or self.end is None):
            raise ValueError("give either `timestamps` or both `start` and `end`")
        return self


def _unix(x: Stamp) -> int:
    if isinstance(x, datetime):
        if x.tzinfo is None:
            x = x.replace(tzinfo=timezone.utc)
        return int(x.timestamp())
    x = int(x)
    return x // 1000 if abs(x) > MS_THRESHOLD else x   # milliseconds (e.g. JS Date.now())


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _timestamps(req: ForecastRequest) -> np.ndarray:
    if req.timestamps is not None:
        n = len(req.timestamps)
    else:
        start = -(-_unix(req.start) // STEP) * STEP          # round up to the 15-min grid
        end = _unix(req.end)
        n = max(0, -(-(end - start) // STEP))
    if n == 0:
        raise HTTPException(422, "no timestamps in request (empty list, or end <= start)")
    if n > MAX_POINTS:
        raise HTTPException(422, f"at most {MAX_POINTS} timestamps per request (got {n})")
    if req.timestamps is not None:
        return np.array([_unix(t) for t in req.timestamps], dtype="int64")
    return np.arange(start, end, STEP, dtype="int64")


def _model(key: str):
    _reload_if_changed()
    if key not in MODELS:
        raise HTTPException(503, LOAD_ERRORS.get(key, f"{key} model not loaded"))
    return MODELS[key]


def _run(key: str, req: ForecastRequest) -> tuple[pd.DataFrame, int]:
    model = _model(key)
    issued = model.last_known_ts + STEP if req.issued_at is None else _unix(req.issued_at)
    try:
        df = model.predict(_timestamps(req), issued_at=issued, allow_fetch=req.allow_fetch)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return df, (issued // STEP) * STEP


def _csv_response(text: str, filename: str) -> Response:
    return Response(text, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _response(key: str, df: pd.DataFrame, issued: int, fmt: str = "json"):
    df = df.assign(time_utc=[_iso(int(t)) for t in df.timestamp])
    if fmt == "csv":
        name = f"forecast_{key.replace('+', '_')}_{_iso(issued).replace(':', '')}.csv"
        return _csv_response(df.to_csv(index=False), name)
    return {
        "model": key,
        "issued_at": issued,
        "issued_at_utc": _iso(issued),
        "count": len(df),
        "predictions": json.loads(df.to_json(orient="records")),
    }


# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict:
    _reload_if_changed()
    out = {"status": "ok" if len(MODELS) == 2 else "degraded", "errors": LOAD_ERRORS, "models": {},
           "sources": {k: v[0] for k, v in _LOADED.items()}}
    now = int(datetime.now(timezone.utc).timestamp())
    for key, m in MODELS.items():
        issued = m.last_known_ts + STEP
        out["models"][key] = {
            "latest_data_utc": _iso(m.last_known_ts),
            "data_age_hours": round((now - issued) / 3600, 1),
            "default_issued_at": issued,
            "forecast_window_utc": [_iso(issued), _iso(issued + (MAX_LEAD_STEPS - 1) * STEP)],
        }
    return out


@app.post("/forecast/demand")
def forecast_demand(req: ForecastRequest):
    return _response("demand", *_run("demand", req), req.format)


@app.post("/forecast/production")
def forecast_production(req: ForecastRequest):
    return _response("production", *_run("production", req), req.format)


@app.post("/forecast")
def forecast_both(req: ForecastRequest):
    d, issued = _run("demand", req)
    p, _ = _run("production", req)
    # same timestamps in the same order -> join by position (safe with duplicate timestamps)
    extra = p.drop(columns=["timestamp", "time_utc", "lead_hours", "weather_source"])
    merged = pd.concat([d.reset_index(drop=True), extra.reset_index(drop=True)], axis=1)
    return _response("demand+production", merged, issued, req.format)


# --------------------------------------------------------------------------- #
# CSV files
# --------------------------------------------------------------------------- #
def _csv_files() -> dict[str, Path]:
    """name (path relative to predictions/, '/'-separated) -> file. Only these are served."""
    return {p.relative_to(CSV_ROOT).as_posix(): p for p in sorted(CSV_ROOT.rglob("*.csv"))}


def _query_stamp(value: str | None, field: str) -> int | None:
    if value is None:
        return None
    try:
        return _unix(TypeAdapter(Stamp).validate_python(int(value) if value.lstrip("-").isdigit() else value))
    except (ValidationError, ValueError) as exc:
        raise HTTPException(422, f"`{field}` must be unix seconds or ISO-8601, got {value!r}") from exc


@app.get("/csv")
def list_csv() -> dict:
    files = []
    for name, path in _csv_files().items():
        with open(path, encoding="utf-8") as f:
            columns = f.readline().strip().split(",")
        files.append({"name": name, "url": f"/csv/{name}", "size_bytes": path.stat().st_size,
                      "columns": columns, "filterable": "timestamp" in columns})
    return {"count": len(files), "files": files}


@app.get("/csv/{name:path}")
def get_csv(name: str,
            start: str | None = Query(None, description="keep rows with timestamp >= start (unix s or ISO)"),
            end: str | None = Query(None, description="keep rows with timestamp < end (unix s or ISO)")):
    path = _csv_files().get(name)
    if path is None:
        raise HTTPException(404, f"unknown CSV {name!r}; GET /csv lists the available files")
    if start is None and end is None:
        return FileResponse(path, media_type="text/csv", filename=path.name)

    df = pd.read_csv(path)
    if "timestamp" not in df.columns:
        raise HTTPException(422, f"{name} has no `timestamp` column, so start/end can't filter it")
    lo, hi = _query_stamp(start, "start"), _query_stamp(end, "end")
    mask = pd.Series(True, index=df.index)
    if lo is not None:
        mask &= df["timestamp"] >= lo
    if hi is not None:
        mask &= df["timestamp"] < hi
    return _csv_response(df[mask].to_csv(index=False), path.name)
