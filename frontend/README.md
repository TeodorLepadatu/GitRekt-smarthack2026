# Dashboard (frontend)

The operator dashboard for the energy agent. It shows what the battery is doing and why,
whether the goals are on track, today's profit, and who made each decision
(AI, backup rules, or adjusted by the safety layer). Operators can also type goals in plain language.

React + TypeScript + Vite, Tailwind CSS (no component kit, no icon set), Motion for scroll reveals and hover, Recharts, TanStack Query,
Server-Sent Events for live updates.

## Run with mock data (no backend needed)

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. `.env.development` sets `VITE_USE_MOCKS=true`, so the whole API
runs in the browser (`src/mocks/`). It simulates one realistic day: 96 intervals with
negative prices at noon, an evening peak, a few AI failures that the backup rules handle, and
real safety-layer rules. The demo day starts at 11:15. Use the **Simulation** strip along the bottom
(run the rest of the day at 1×, 60× or 600×, or restart at 00:00) for the live demo.

## Run against the real API

```bash
npm run dev:api                          # VITE_USE_MOCKS=false, proxies /api to http://localhost:8000
API_URL=http://10.0.0.5:8000 npm run dev:api   # another API host
```

## Docker

```bash
docker build -t energy-dashboard ./frontend
docker run -p 8080:80 --network <compose-network> energy-dashboard
```

The image builds the app with `node:20` and serves it with `nginx:alpine`.
[`nginx.conf`](nginx.conf) serves the app and proxies `/api/*` to `http://api:8000`, with
buffering turned off for the `/api/stream` SSE endpoint. The API service must be called `api`
on the same Docker network. For a standalone demo image that uses the mock data:

```bash
docker build --build-arg VITE_USE_MOCKS=true -t energy-dashboard:demo ./frontend
docker run -p 8080:80 energy-dashboard:demo
```

Example `docker-compose.yml` service:

```yaml
  web:
    build: ./frontend
    ports: ["8080:80"]
    depends_on: [api]
```

## API contract

All types live in [`src/api/types.ts`](src/api/types.ts). The mock layer uses exactly these shapes.

| Route | Returns |
|---|---|
| `GET /api/state` | `StateSnapshot`: `capacity_mwh`, `max_power_mw`, `profit_today_eur`, `current` (latest interval or `null`), `sim: {running, speed}` |
| `GET /api/day` | `{date, now, intervals}`: 96 slots. Slots not decided yet have only the forecast fields (no `decision`/`guard`/`settlement`/`soc_mwh`) |
| `GET /api/goals` | `Goal[]` with `status` |
| `POST /api/goals {text}` | `{goals, question?}`. `question` is set when the goal was too vague to parse |
| `DELETE /api/goals/:id` | anything (2xx) |
| `GET /api/metrics` | `Metrics` (profit, MWh sold/bought/curtailed, decision counts by source, goals met) |
| `GET /api/stream` | SSE, event `cycle`, data = one interval (same shape as a `/api/day` slot); event `alert`, data = one `Alert` just raised |
| `POST /api/sim/run {speed}` | starts the simulation; **`speed: 0` pauses** |
| `POST /api/sim/reset` | restarts the day at 00:00 |

### Operator desk

What a person needs to step in (`backend/desk.py`, mocked in `src/mocks/desk.ts`):

| Route | Returns |
|---|---|
| `GET /api/alerts` | `Alert[]`: goal behind/unreachable, backup rules 3× in a row, price spike in the next 2 h, forecast off. One alert per condition; it goes away by itself when the condition clears |
| `POST /api/alerts/:id/ignore` | hides it until its condition clears and comes back |
| `POST /api/goals/:id/relax` | `{goals, message}`: lowers a reserve goal to what can still be stored by its deadline (409 if nothing useful is left) |
| `GET /api/upcoming?n=8` | `{now, slots: UpcomingSlot[]}`: the next intervals' planned action (backup rules + guard on the forecast) and any override |
| `PUT /api/overrides/:time {battery_fraction, note}` | `Override`. `0` = hold ("Skip"). The AI is not asked for that interval; the guard still checks it. 409 if already decided |
| `DELETE /api/overrides/:time` | gives the interval back to the AI |
| `GET /api/flags` | `DecisionFlag[]` |
| `POST /api/decisions/:time/flag {reason}` / `DELETE` | flag or unflag a decision. The full decision and its situation are kept in `logs/decision_flags.jsonl` for later prompt examples |
| `GET /api/audit?limit=200` | `AuditEntry[]`, latest first: goal changes, overrides, flags, ignored alerts, simulation settings. Kept in `logs/audit.jsonl` |

An overridden interval comes back with `decision.source = "operator"`.

Settled with the backend. `dashboard/contract.py` builds exactly these shapes from the
loop's objects, and `tests/test_dashboard_contract.py` parses this `types.ts` and checks
every payload against it, so the two cannot drift apart:

| This file | Built by |
|---|---|
| `Interval` (and the SSE `cycle` event) | `dashboard.interval_payload(record)` |
| `ForecastInterval` | `dashboard.forecast_payload(...)` |
| `DayResponse` | `dashboard.day_payload(runner)` |
| `StateSnapshot` | `dashboard.state_payload(runner, running=, speed=)` |
| `Metrics` | `dashboard.metrics_payload(runner)` |
| `Goal[]` | `dashboard.goals_payload(runner)` |

- `soc_mwh` is the energy stored at the **end** of the interval (the loop's `soc_after_mwh`).
- `/api/day` future slots carry the forecast only, and their `break_even` is recomputed
  from the price curve and the latest state of charge.
- `demand_mw` and `production_mw` are measured; `solar_mw` and `wind_mw` are only
  forecast apart, so the measured production is split between them in the forecast's
  ratio. They always add up to `production_mw`, which is what `EnergyFlow` balances on.
- `Metrics` also carries the backend's own evaluation numbers (forecast MAE, band
  coverage, clip and fallback rates) as optional fields.
- `speed: 0` on `/api/sim/run` as the pause command.
- Goal `type` values: `reserve`, `min_sell_price`, `max_profit`.

Still missing: the HTTP layer itself. The payloads are ready, but nothing serves them
yet, so `VITE_USE_MOCKS=true` is still the only working mode (see `src/mocks/`).

## Code layout

| Path | Contents |
|---|---|
| `src/api/` | types, fetch client + SSE subscription, TanStack Query hooks |
| `src/lib/labels.ts` | every plain-language label: actions, sources, goal statuses, guard rules, fallback reasons |
| `src/mocks/` | forecast, a TypeScript copy of the decide → guard loop, goal parser, mock routes |
| `src/components/layout/` | masthead and the four sections (Now · Day · Goals · Decisions, kept in the URL hash) |
| `src/components/now/` | current action and reason, power-flow schematic, decision trace (forecast → AI → check → safety → battery), the day-so-far ledger |
| `src/components/chart/` | today chart: price, battery level, production vs demand (synced hover) |
| `src/components/goals/` | goal input with examples, active goals, parser questions |
| `src/components/timeline/` | decision history with filters and expandable AI reasoning |
| `src/components/metrics/` | ruled figure rows for the Day and Decisions pages |
| `src/components/demo/` | simulation strip: run / speed / restart |
| `src/components/common/marks.tsx` | status words, who-decided marks, progress lines, sections (instead of badges and cards) |

The price and battery level are on separate charts that share the time axis, so there is no dual
y-axis. The decision history works as the table view of the chart data.
