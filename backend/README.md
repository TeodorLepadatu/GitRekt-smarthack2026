# Backend

FastAPI server for the dashboard (`frontend/`). It runs the day loop in the background and serves
exactly the routes in `frontend/README.md` ("API contract"), with the shapes in
`frontend/src/api/types.ts`.

```
dashboard ──HTTP/SSE──► backend/app.py ──reads snapshots / queues commands──► backend/service.py
                                                                                   │ worker thread
                                                                                   ▼
                                        loop.DayRunner: feature builder → pipeline → simulator → settlement
```

## Run it

```bash
python -m backend --no-llm --start 11:15        # rule-based decisions, no Ollama needed
python -m backend --model qwen3:4b              # decisions and goal parsing by the LLM
```

Then the dashboard against it (needs Node; `--host` only when running it in Docker):

```bash
cd frontend && npm install && npm run dev:api   # http://localhost:5173, proxies /api to :8000
```

| Option | Default | |
|---|---|---|
| `--model` | `Settings().decision_model` | Ollama model for decisions and goal parsing |
| `--no-llm` | off | rule-based fallback and rule-based goal parser only |
| `--start` | `00:00` | pre-run the day up to this time (the frontend mock starts at 11:15) |
| `--start-soc` | `4.0` | stored energy at 00:00, MWh |
| `--goals-path` | `Settings().goals_path` | where goals are saved; if empty, starts with "Maximise profit", "Store 6 MWh by 18:00", "Never sell below 50 EUR/MWh" |
| `--log` | none | every interval record as JSONL |
| `--host`, `--port` | `127.0.0.1`, `8000` | |

## Routes

| Route | Returns |
|---|---|
| `GET /api/state` | capacity, max power, profit today, latest interval (`current`), `sim: {running, speed}` |
| `GET /api/day` | `{date, now, intervals}`: 96 slots; decided ones are full `Interval`s, later ones forecast only |
| `GET /api/goals` | the objective (`type: "max_profit"`), then each goal with its live status |
| `POST /api/goals {text}` | `{goals}`, or `{goals, question}` when the goal is unclear (nothing changes then) |
| `DELETE /api/goals/{id}` | removes the sentence the goal came from; 404 if unknown |
| `GET /api/metrics` | profit, energy sold / bought / curtailed, decision counts, goals met |
| `GET /api/stream` | SSE: event `cycle` with each new `Interval`; `: ping` every 15 s |
| `POST /api/sim/run {speed}` | 1 = real time, 600 = 1.5 s per interval, 0 = pause; after the day ends, starts a new one |
| `POST /api/sim/reset` | pause and start again at 00:00 |
| `GET /api/health` | `{"status": "ok"}` |

In an `Interval`, `guard.battery_setpoint_mw` and `guard.curtail_mw` are what the battery and the
export limiter **actually** did (from the simulator), `demand_mw`/`production_mw` are actual values,
and `soc_mwh` is the stored energy at the end of the interval.

## How it works

- **One worker thread owns the day.** It runs intervals at the requested speed with fixed deadlines
  (no drift). If an LLM decision takes longer than the slot, the next interval starts at once.
- **Reads never wait for the LLM.** After every interval the worker publishes a snapshot (intervals,
  metrics, goal statuses); the routes read that.
- **Commands are applied between intervals**, never inside one: new or deleted goals take effect from
  the next interval, a reset drops an interval that was still running.
- **Goals** go through `goal_parser.GoalStore`: an unclear sentence returns a question and leaves the
  active goals unchanged. Goal history follows the goal, not its id (ids are renumbered when a goal
  is deleted).
- **The stream** is async and notices a closed browser tab, so dead connections are released and the
  server shuts down cleanly.

The day itself is `loop.demo_day()` until real price and forecast feeds are connected. The ML models
in `predictions/` forecast national demand and production, so they need scaling to the site first.

## Tests

```bash
python -m pytest tests/test_backend.py
```

Every route and its shape, run / pause / reset / day end, goals (vague, add, delete with
renumbering), the SSE stream on a real uvicorn server (including a client disconnecting), and an
LLM-driven run.
