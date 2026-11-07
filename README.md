# GitRekt
Smarthack 2026

## Pipeline

```
user goals ─► goal_parser (GoalStore) ─► GoalConfig        (only when the goals change)

every 15 min:
forecasts + prices + SoC + GoalConfig ─► feature_builder ─► features
features + GoalConfig ─► prompt_assembly ─► llm.DecisionAgent ─► validator (+ fallback) ─► guard ─► setpoints
                                                          site runs it ─► settlement.DayLedger.record(cycle, features, actual)
```

`pipeline.run_cycle(features, goal_config)` runs one cycle end to end and never raises.

| Module | Role |
|---|---|
| `predictions/` | ML 1 + ML 2: demand and production forecasts (solar, wind, total) with 10–90 % ranges for any 15-min timestamp up to 7 days ahead; `DemandForecaster`, `ProductionForecaster` and an HTTP API (see `predictions/README.md`) |
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
