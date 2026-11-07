# GitRekt
Smarthack 2026

## Pipeline

```
user goals ─► goal_parser (GoalStore) ─► GoalConfig        (only when the goals change)

predictions (ML 1 + ML 2) + data/energy_price.csv ─► loop.real_day ─► DayInput

every 15 min:
forecasts + prices + SoC + GoalConfig ─► feature_builder ─► features
features + GoalConfig ─► prompt_assembly ─► llm.DecisionAgent ─► validator (+ fallback) ─► guard ─► setpoints
                                                          site runs it ─► settlement.DayLedger.record(cycle, features, actual)
                                                                      ─► dashboard.interval_payload ─► the frontend
```

`pipeline.run_cycle(features, goal_config)` runs one cycle end to end and never raises.

## Run a day

```powershell
python -m loop                        # a made-up demo day, rule-based fallback only (instant)
python -m loop --date 2026-06-15      # a real day: ML forecasts + ENTSO-E day-ahead prices
python -m loop --date 2026-06-15 --llm --model qwen3.5:latest --goals "Store 6 MWh by 18:00"
```

## Run the dashboard

From the repository root, run:

```powershell
.\run_app.ps1
```

This opens the backend and frontend in separate PowerShell windows. The dashboard is available at
`http://localhost:5173`. The launcher uses rule-based decisions by default and does not require Ollama.
To use Ollama, change `$UseLLM = $false` to `$UseLLM = $true` in `run_app.ps1`.

### Run with Docker

With Docker Desktop running, start the complete app from the repository root:

```powershell
docker compose up --build
```

This starts the API with LLM mode enabled, starts Ollama, and downloads the default
`qwen3.5:4b` model automatically. The first run may take several minutes and requires
enough disk space for the model. To use another Ollama model:

```powershell
$env:ENERGY_AGENT_MODEL = "qwen3:4b"
docker compose up --build
```

Open `http://localhost:5173`. Stop it with `Ctrl+C`, or run:

```powershell
docker compose down
```

### macOS and Linux

Without Docker, from the repository root (uses `.venv/bin/python` if it exists, otherwise
`python3`; install the packages once with `python3 -m pip install -r requirements.txt`):

```bash
./run_app.sh                          # rule-based decisions, no Ollama needed
./run_app.sh --llm --model qwen3:4b   # decisions by the LLM (Ollama running on the machine)
./run_app.sh --date 2026-06-15        # a real day
```

It starts the backend and the dashboard in one terminal (`http://localhost:5173`); `Ctrl+C` stops
both. Without Node.js installed, the dashboard dev server runs in Docker instead.

With Docker, the same commands as above work on macOS and Linux; set the model in the shell:

```bash
ENERGY_AGENT_MODEL=qwen3:4b ENERGY_AGENT_GOAL_MODEL=qwen3:4b docker compose up --build
```

On a Mac, Ollama inside Docker runs on the CPU only (no Apple GPU), so decisions are slow and
often fall back to the rules after the 60 s timeout. To use the Ollama installed on the Mac
instead (GPU, and the models you already pulled), add `docker-compose.host-ollama.yml`:

```bash
ollama pull qwen3:4b    # once; Ollama must be running on the Mac
ENERGY_AGENT_MODEL=qwen3:4b ENERGY_AGENT_GOAL_MODEL=qwen3:4b \
  docker compose -f docker-compose.yml -f docker-compose.host-ollama.yml up --build
```

Stop it with `Ctrl+C`, or `docker compose -f docker-compose.yml -f docker-compose.host-ollama.yml down`.

### Linux

Needs Docker Engine with the Compose plugin, version 2.24 or newer (`docker compose version`;
the old `docker-compose` v1 does not read these files). Run Docker without `sudo` by adding
yourself to the `docker` group once, then log out and back in:

```bash
sudo usermod -aG docker $USER
```

Then, from the repository root, the full app with Ollama in Docker:

```bash
ENERGY_AGENT_MODEL=qwen3:4b ENERGY_AGENT_GOAL_MODEL=qwen3:4b docker compose up --build
```

On Linux Ollama in Docker can use an **NVIDIA GPU** (install the NVIDIA Container Toolkit first;
`docker run --rm --gpus all ubuntu nvidia-smi` checks it). Without a GPU, decisions run on the CPU
and are slow:

```bash
ENERGY_AGENT_MODEL=qwen3:4b ENERGY_AGENT_GOAL_MODEL=qwen3:4b \
  docker compose -f docker-compose.yml -f docker-compose.gpu.yml up --build
```

To use an Ollama already installed on the machine instead, `docker-compose.host-ollama.yml` works on
Linux too, but Ollama must listen on all interfaces, not only on 127.0.0.1:

```bash
sudo systemctl edit ollama.service   # add: [Service] / Environment="OLLAMA_HOST=0.0.0.0:11434"
sudo systemctl restart ollama
ENERGY_AGENT_MODEL=qwen3:4b ENERGY_AGENT_GOAL_MODEL=qwen3:4b \
  docker compose -f docker-compose.yml -f docker-compose.host-ollama.yml up --build
```

Without Docker, `./run_app.sh` works on Linux as on macOS (it needs Python 3 and, for the
dashboard, Node.js or Docker). Open `http://localhost:5173` in every case.

`--date` replays a real day end to end. The forecasts are issued at the day-ahead gate
(12:00 local the day before), so the models only see what was known then, and the books
are settled against what was actually measured — the printed forecast error is honest.
`--plan` settles on the forecasts instead, for a day that has not happened yet.

| Module | Role |
|---|---|
| `predictions/` | ML 1 + ML 2: demand and production forecasts (solar, wind, total) with 10–90 % ranges for any 15-min timestamp up to 7 days ahead; `DemandForecaster`, `ProductionForecaster` and an HTTP API (see `predictions/README.md`) |
| `loop/` | The clock: one day, interval by interval. `demo_day()` makes a day up; `real_day()` builds one from the ML models and the real price file, scaled from national to site size by `SiteScale` |
| `dashboard/` | The loop's objects as the shapes `frontend/src/api/types.ts` declares, so the API layer has nothing left to translate |
| `goal_parser/` | Plain-language goals → `GoalConfig`; `GoalStore` keeps the active config |
| `feature_builder/` | All the arithmetic: section 8 of the prompt and goal progress |
| `prompt_assembly/` | Rules, solved examples, current situation and USER GOALS → one prompt |
| `llm/` | The shared Ollama client and the decision agent |
| `validator/` | Parses and checks the LLM output; rule-based fallback on any failure |
| `guard/` | Physical limits and hard goals → safe battery setpoint and curtailment |
| `settlement/` | The cashier: grid import/export, energy cost, wear, profit and new SoC per interval (on actual values); goal status (met / missed / violated, "6 MWh reached at 16:00"); day metrics, JSONL log, goal cost vs a profit-only run |
| `battery.py` | `BatteryConfig`, the one battery definition |
| `config.py` | `Settings`: Ollama host, models, timeouts, battery (from environment variables) |
| `common.py` | Small shared helpers |

Run the tests with `pytest -q` (see `tests/README.md`).

## Data visualization

Create and activate the virtual environment, then install the dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Generate the parsed dataset and plots:

```powershell
python visualize_data.py
```
