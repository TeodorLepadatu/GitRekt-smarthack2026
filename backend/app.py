"""HTTP API for the dashboard (frontend/), the routes in frontend/README.md "API contract".

    GET    /api/state           StateSnapshot
    GET    /api/day             {date, now, intervals}: 96 slots, future ones forecast-only
    GET    /api/goals           Goal[]
    POST   /api/goals {text}    {goals, question?}
    DELETE /api/goals/{id}
    GET    /api/metrics         Metrics
    GET    /api/stream          SSE, event "cycle" = one Interval
    POST   /api/sim/run {speed} speed 0 pauses
    POST   /api/sim/reset       back to 00:00
    GET    /api/health
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .service import Backend, GoalNotFound

MAX_GOAL_TEXT = 2000


class GoalIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_GOAL_TEXT)


class RunIn(BaseModel):
    speed: float = Field(default=60, ge=0, le=100_000)


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

    @app.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)

        def on_interval(interval: dict) -> None:          # called from the worker thread
            def put() -> None:
                if not queue.full():                      # a slow client must not hold memory
                    queue.put_nowait(interval)
            loop.call_soon_threadsafe(put)

        async def events() -> AsyncIterator[str]:
            backend.add_listener(on_interval)
            try:
                yield "retry: 3000\n\n"                   # browser reconnect delay
                while not await request.is_disconnected():
                    try:
                        interval = await asyncio.wait_for(queue.get(), timeout=heartbeat_s)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"                # keeps proxies from closing the connection
                        continue
                    yield f"event: cycle\ndata: {json.dumps(interval, ensure_ascii=False)}\n\n"
            finally:
                backend.remove_listener(on_interval)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app
