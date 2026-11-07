"""HTTP API for the dashboard (frontend/), the routes in frontend/README.md "API contract".

    GET    /api/state           StateSnapshot
    GET    /api/day             {date, now, intervals}: 96 slots, future ones forecast-only
    GET    /api/goals           Goal[]
    POST   /api/goals {text}    {goals, question?}
    DELETE /api/goals/{id}
    GET    /api/metrics         Metrics
    GET    /api/stream          SSE, event "cycle" = one Interval, event "alert" = one Alert just raised
    POST   /api/sim/run {speed} speed 0 pauses
    POST   /api/sim/reset       back to 00:00
    GET    /api/health

The operator's desk (backend/desk.py):

    GET    /api/alerts                    Alert[], what needs attention now
    POST   /api/alerts/{id}/ignore        hide it until its condition clears
    POST   /api/goals/{id}/relax          lower a reserve goal to what is still reachable
    GET    /api/upcoming?n=8              {now, slots}: the next intervals' planned action
    PUT    /api/overrides/{time} {battery_fraction, note}   set an upcoming interval (0 = hold)
    DELETE /api/overrides/{time}          give it back to the AI
    GET    /api/flags                     DecisionFlag[]
    POST   /api/decisions/{time}/flag {reason}
    DELETE /api/decisions/{time}/flag
    GET    /api/audit?limit=200           AuditEntry[], latest first
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .service import Backend, Conflict, GoalNotFound, NotFound

MAX_GOAL_TEXT = 2000
MAX_NOTE = 500


class GoalIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_GOAL_TEXT)


class RunIn(BaseModel):
    speed: float = Field(default=60, ge=0, le=100_000)


class OverrideIn(BaseModel):
    battery_fraction: float = Field(ge=-1, le=1)
    note: str = Field(default="", max_length=MAX_NOTE)


class FlagIn(BaseModel):
    reason: str = Field(default="", max_length=MAX_NOTE)


def _desk(fn, *args):
    """Run a desk command, turning its errors into HTTP ones."""
    try:
        return fn(*args)
    except (NotFound, GoalNotFound) as exc:
        raise HTTPException(404, f"{exc.args[0]!r} not found" if exc.args else "not found")
    except Conflict as exc:
        raise HTTPException(409, str(exc))


def create_app(backend: Backend, *, heartbeat_s: float = 15.0) -> FastAPI:
    app = FastAPI(title="Energy agent backend", version="1.0")
    app.state.backend = backend
    # The Vite dev server proxies /api; CORS also lets the dashboard call the API directly.
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/state")
    def state() -> dict:
        return backend.state()

    @app.get("/api/day")
    def day() -> dict:
        return backend.day()

    @app.get("/api/goals")
    def goals() -> list:
        return backend.goal_list()

    @app.post("/api/goals")
    def add_goal(body: GoalIn) -> dict:
        text = body.text.strip()
        if not text:
            raise HTTPException(422, "goal text is empty")
        return backend.add_goal(text)

    @app.delete("/api/goals/{goal_id}")
    def delete_goal(goal_id: str) -> dict:
        try:
            backend.delete_goal(goal_id)
        except GoalNotFound:
            raise HTTPException(404, f"goal {goal_id!r} not found")
        return {"ok": True}

    @app.get("/api/metrics")
    def metrics() -> dict:
        return backend.metrics()

    @app.post("/api/sim/run")
    def sim_run(body: RunIn) -> dict:
        return backend.run(body.speed)

    @app.post("/api/sim/reset")
    def sim_reset() -> dict:
        return backend.reset()

    # ------------------------------------------------------------ operator desk

    @app.get("/api/alerts")
    def alerts() -> list:
        return backend.alert_list()

    @app.post("/api/alerts/{alert_id}/ignore")
    def ignore_alert(alert_id: str) -> dict:
        _desk(backend.ignore_alert, alert_id)
        return {"ok": True}

    @app.post("/api/goals/{goal_id}/relax")
    def relax_goal(goal_id: str) -> dict:
        return _desk(backend.relax_goal, goal_id)

    @app.get("/api/upcoming")
    def upcoming(n: int = Query(default=8, ge=1, le=96)) -> dict:
        return backend.upcoming(n)

    @app.put("/api/overrides/{time}")
    def set_override(time: str, body: OverrideIn) -> dict:
        return _desk(backend.set_override, time, body.battery_fraction, body.note)

    @app.delete("/api/overrides/{time}")
    def clear_override(time: str) -> dict:
        _desk(backend.clear_override, time)
        return {"ok": True}

    @app.get("/api/flags")
    def flags() -> list:
        return backend.flag_list()

    @app.post("/api/decisions/{time}/flag")
    def flag(time: str, body: FlagIn) -> dict:
        return _desk(backend.flag_decision, time, body.reason.strip())

    @app.delete("/api/decisions/{time}/flag")
    def unflag(time: str) -> dict:
        _desk(backend.unflag_decision, time)
        return {"ok": True}

    @app.get("/api/audit")
    def audit(limit: int = Query(default=200, ge=1, le=500)) -> list:
        return backend.audit_list(limit)

    # ------------------------------------------------------------------- stream

    @app.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)

        def listener(event: str):
            def on_event(payload: dict) -> None:          # called from the worker thread
                def put() -> None:
                    if not queue.full():                  # a slow client must not hold memory
                        queue.put_nowait((event, payload))
                loop.call_soon_threadsafe(put)
            return on_event

        on_interval, on_alert = listener("cycle"), listener("alert")

        async def events() -> AsyncIterator[str]:
            backend.add_listener(on_interval)
            backend.add_alert_listener(on_alert)
            try:
                yield "retry: 3000\n\n"                   # browser reconnect delay
                while not await request.is_disconnected():
                    try:
                        event, payload = await asyncio.wait_for(queue.get(), timeout=heartbeat_s)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"                # keeps proxies from closing the connection
                        continue
                    yield f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            finally:
                backend.remove_listener(on_interval)
                backend.remove_alert_listener(on_alert)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app
