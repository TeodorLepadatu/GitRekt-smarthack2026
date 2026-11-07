# GitRekt-
Smarthack 2026

## Pipeline

```
user goals ─► goal_parser (GoalStore) ─► GoalConfig        (only when the goals change)

every 15 min:
forecasts + prices + SoC + GoalConfig ─► feature_builder ─► features
features + GoalConfig ─► prompt_assembly ─► llm.DecisionAgent ─► validator (+ fallback) ─► guard ─► setpoints
```

`pipeline.run_cycle(features, goal_config)` runs one cycle end to end and never raises.

| Module | Role |
|---|---|
| `goal_parser/` | Plain-language goals → `GoalConfig`; `GoalStore` keeps the active config |
| `feature_builder/` | All the arithmetic: section 8 of the prompt and goal progress |
| `prompt_assembly/` | Rules, solved examples, current situation and USER GOALS → one prompt |
| `llm/` | The shared Ollama client and the decision agent |
| `validator/` | Parses and checks the LLM output; rule-based fallback on any failure |
| `guard/` | Physical limits and hard goals → safe battery setpoint and curtailment |
| `battery.py` | `BatteryConfig`, the one battery definition |
| `config.py` | `Settings`: Ollama host, models, timeouts, battery (from environment variables) |
| `common.py` | Small shared helpers |

Run the tests with `pytest -q` (see `tests/README.md`).
