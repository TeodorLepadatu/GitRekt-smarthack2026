# Dashboard (frontend)

The operator dashboard for the energy agent. It shows what the battery is doing and why,
whether the goals are on track, today's profit, and who made each decision
(AI, backup rules, or adjusted by the safety layer). Operators can also type goals in plain language.

React + TypeScript + Vite, Tailwind CSS + shadcn/ui components, Recharts, TanStack Query,
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
real safety-layer rules. The demo day starts at 11:15. Use the **Demo** dock in the bottom-right
corner (Run full day at 1×, 60× or 600×, and Reset) for the live demo.

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
| `GET /api/stream` | SSE, event `cycle`, data = one interval (same shape as a `/api/day` slot) |
| `POST /api/sim/run {speed}` | starts the simulation; **`speed: 0` pauses** |
| `POST /api/sim/reset` | restarts the day at 00:00 |

Points to confirm with the backend team:

- `soc_mwh` is the energy stored at the **end** of the interval.
- `/api/day` future slots carry the forecast only (see above).
- `/api/state` and `/api/metrics` field names are proposals (the plan only fixed the interval and goal shapes).
- `speed: 0` on `/api/sim/run` as the pause command.
- Goal `type` values: `reserve`, `min_sell_price`, `max_profit`.

## Code layout

| Path | Contents |
|---|---|
| `src/api/` | types, fetch client + SSE subscription, TanStack Query hooks |
| `src/lib/labels.ts` | every plain-language label: actions, sources, goal statuses, guard rules, fallback reasons |
| `src/mocks/` | forecast, a TypeScript copy of the decide → guard loop, goal parser, mock routes |
| `src/components/layout/` | top bar and the four tabs (Now · Today · Goals · History, kept in the URL hash) |
| `src/components/now/` | live energy-flow diagram, current action and reason, the "on track / money / trust" cards |
| `src/components/chart/` | today chart: price, battery level, production vs demand (synced hover) |
| `src/components/goals/` | goal input with examples, active goals, parser questions |
| `src/components/timeline/` | decision history with filters and expandable AI reasoning |
| `src/components/metrics/` | profit, energy, AI reliability, goals met |
| `src/components/demo/` | run / speed / reset controls |
| `src/components/ui/` | shadcn/ui-style primitives |

The price and battery level are on separate charts that share the time axis, so there is no dual
y-axis. The decision history works as the table view of the chart data.
