"""HTTP API for the dashboard (frontend/), the routes in frontend/README.md "API contract".

    GET    /api/state           StateSnapshot
    GET    /api/day             {date, now, intervals}: 96 slots, future ones forecast-only
    GET    /api/goals           Goal[]
    POST   /api/goals {text}    {goals, question?}
    DELETE /api/goals/{id}
    GET    /api/metrics         Metrics
    GET    /api/stream          SSE: event "cycle" = one Interval, "control" = ControlState, "alert" = one Alert just raised
    GET    /api/control         ControlState: override, emergency hold, autonomy, pending approval
    POST   /api/override {setpoint_mw, until}   operator setpoint until HH:MM (still guarded)
    DELETE /api/override        hand control back to the AI
    POST   /api/hold            emergency hold: battery idle, no export, until released
    DELETE /api/hold            release it
    PUT    /api/autonomy {mode?, max_auto_mw?, ask_on_low_confidence?, timeout_s?}
    POST   /api/approvals/{id} {approve}         answer the AI's waiting proposal
    POST   /api/sim/run {speed} speed 0 pauses
    POST   /api/sim/reset       back to 00:00
    POST   /api/sim/next-day {speed}   the next date, from the energy stored now (409 if it cannot be built)
    GET    /api/health

The operator's desk (backend/desk.py):

    GET    /api/alerts                    Alert[], what needs attention now
    POST   /api/alerts/{id}/ignore        hide it until its condition clears
    POST   /api/goals/{id}/relax          lower a reserve goal to what is still reachable
    GET    /api/upcoming?n=8              {now, slots}: the next intervals' planned action
    PUT    /api/plan/{time} {battery_fraction, note}   set one upcoming interval (0 = hold)
    DELETE /api/plan/{time}               give it back to the AI
    GET    /api/flags                     DecisionFlag[]
    POST   /api/decisions/{time}/flag {reason}
    DELETE /api/decisions/{time}/flag
    GET    /api/audit?limit=200           AuditEntry[], latest first
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from control import ControlError, NoPendingApproval

from .service import Backend, Conflict, GoalNotFound, NotFound

MAX_GOAL_TEXT = 2000
MAX_NOTE = 500


class GoalIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_GOAL_TEXT)


class RunIn(BaseModel):
    speed: float = Field(default=60, ge=0, le=100_000)


class NextDayIn(BaseModel):
    speed: float = Field(default=0, ge=0, le=100_000)


class OverrideIn(BaseModel):
    setpoint_mw: float              # > 0 charge, < 0 discharge; the range is checked against the battery
    until: str = Field(pattern=r"^\d{2}:\d{2}$")


class AutonomyIn(BaseModel):
    """Only the fields sent change; the others stay as they are."""
    mode: Optional[Literal["autonomous", "supervised", "advisory"]] = None
    max_auto_mw: Optional[float] = Field(default=None, ge=0, le=10_000)
    ask_on_low_confidence: Optional[bool] = None
    timeout_s: Optional[float] = Field(default=None, ge=5, le=900)


class AnswerIn(BaseModel):
    approve: bool


class PlanIn(BaseModel):
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

    @app.get("/api/control")
    def control() -> dict:
        return backend.control_state()

    @app.post("/api/override")
    def set_override(body: OverrideIn) -> dict:
        try:
            return backend.set_override(body.setpoint_mw, body.until)
        except ControlError as exc:
            raise HTTPException(422, str(exc))

    @app.delete("/api/override")
    def clear_override() -> dict:
        return backend.clear_override()

    @app.post("/api/hold")
    def hold() -> dict:
        return backend.set_hold(True)

    @app.delete("/api/hold")
    def release_hold() -> dict:
        return backend.set_hold(False)

    @app.put("/api/autonomy")
    def set_autonomy(body: AutonomyIn) -> dict:
        try:
            return backend.set_autonomy(**body.model_dump())
        except ControlError as exc:
            raise HTTPException(422, str(exc))

    @app.post("/api/approvals/{proposal_id}")
    def answer_approval(proposal_id: int, body: AnswerIn) -> dict:
        try:
            return backend.answer_approval(proposal_id, body.approve)
        except NoPendingApproval:
            raise HTTPException(409, "that proposal is no longer waiting for an answer")

    @app.post("/api/sim/run")
    def sim_run(body: RunIn) -> dict:
        return backend.run(body.speed)

    @app.post("/api/sim/reset")
    def sim_reset() -> dict:
        return backend.reset()

    @app.post("/api/sim/next-day")
    def sim_next_day(body: NextDayIn) -> dict:
        return _desk(backend.next_day, body.speed)

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

    @app.put("/api/plan/{time}")
    def set_plan(time: str, body: PlanIn) -> dict:
        return _desk(backend.set_plan, time, body.battery_fraction, body.note)

    @app.delete("/api/plan/{time}")
    def clear_plan(time: str) -> dict:
        _desk(backend.clear_plan, time)
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

        def on_event(event: str, data: dict) -> None:     # called from the worker or an HTTP thread
            def put() -> None:
                if not queue.full():                      # a slow client must not hold memory
                    queue.put_nowait((event, data))
            loop.call_soon_threadsafe(put)

        async def events() -> AsyncIterator[str]:
            backend.add_listener(on_event)
            try:
                yield "retry: 3000\n\n"                   # browser reconnect delay
                while not await request.is_disconnected():
                    try:
                        event, data = await asyncio.wait_for(queue.get(), timeout=heartbeat_s)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"                # keeps proxies from closing the connection
                        continue
                    yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            finally:
                backend.remove_listener(on_event)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app
