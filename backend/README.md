# Backend

FastAPI server for the dashboard (`frontend/`). It runs the day loop in the background and serves
exactly the routes in `frontend/README.md` ("API contract"). The shapes come from
`dashboard/contract.py`, the one place the loop's objects are translated into
`frontend/src/api/types.ts`; the backend only adds the HTTP layer, the objective entry and the
goal commands.

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
python -m backend --date 2026-06-15             # a real day: ML forecasts + ENTSO-E prices
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
| `--date` | demo day | a real day (`loop.real_day`): ML forecasts scaled to the site + ENTSO-E prices; `--plan` settles on the forecasts |
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
| `GET /api/stream` | SSE: event `cycle` with each new `Interval`, event `control` with the `ControlState` whenever it changes; `: ping` every 15 s |
| `GET /api/control` | emergency `hold`, `override`, `autonomy` and the `pending` approval |
| `POST /api/override {setpoint_mw, until}` | the operator's setpoint until `until` (guarded); 422 if out of range or in the past |
| `DELETE /api/override` | back to the AI |
| `POST /api/hold`, `DELETE /api/hold` | emergency hold on / off |
| `PUT /api/autonomy {mode?, max_auto_mw?, ask_on_low_confidence?, timeout_s?}` | `autonomous`, `supervised` or `advisory`; only the fields sent change |
| `POST /api/approvals/{id} {approve}` | answer the waiting proposal; 409 if it is no longer waiting |
| `POST /api/sim/run {speed}` | 1 = real time, 600 = 1.5 s per interval, 0 = pause; after the day ends, starts a new one |
| `POST /api/sim/reset` | pause and start again at 00:00 |
| `GET /api/health` | `{"status": "ok"}` |

In an `Interval`, `guard.battery_setpoint_mw` and `guard.curtail_mw` are what the battery and the
export limiter **actually** did (the simulator's step, in `dashboard.interval_payload`), `demand_mw`/`production_mw` are actual values,
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
- **Operator control is not queued.** `control.OperatorControl` outlives the days and is asked by the
  loop at the start of every interval. When the autonomy mode makes a decision wait, the worker blocks
  in it until the operator answers, the timeout runs out (the battery then holds), or an override or
  hold replaces the proposal. A reset clears the override and lets a waiting proposal go; an
  emergency hold stays until released.
- **The stream** is async and notices a closed browser tab, so dead connections are released and the
  server shuts down cleanly.

The day is `loop.demo_day()` by default, or a real day with `--date` (`loop/real_day.py` scales the
national ML forecasts to the site).

## Tests

```bash
python -m pytest tests/test_backend.py
```

Every route and its shape, run / pause / reset / day end, goals (vague, add, delete with
renumbering), the SSE stream on a real uvicorn server (including a client disconnecting), and an
LLM-driven run. `test_every_response_matches_types_ts` checks every live response against
`frontend/src/api/types.ts` in both directions, with the reader from `tests/test_dashboard_contract.py`.
