# Loop

The clock (plan section 2): runs the whole cycle for every 15-minute interval of a day.

```
for each of the 96 intervals:
  feature builder ─► pipeline (prompt → LLM → validator → guard) ─► simulator ─► settlement
  └──────────── the simulator's new stored energy feeds the next interval ◄────────────┘
```

It owns the one piece of state that crosses intervals, the battery (held by the simulator).
Everything else is recomputed every interval.

## Usage

```python
from loop import DayInput, DayRunner, demo_day
from llm import DecisionAgent

day = DayInput(prices=prices_96, forecasts=forecasts_96, actuals=actuals_96)  # or demo_day()
runner = DayRunner(day, goal_config, start_soc_mwh=4.0, llm_call=DecisionAgent())

runner.run()                          # whole day, as fast as possible
runner.run_realtime(speed=60)         # one interval every 15 s (1 = real time, 600 = 1.5 s)
runner.summary()                      # profit, goals, LLM/fallback counts, ...
```

| Input | Meaning |
|---|---|
| `prices` | 96 day-ahead prices, EUR/MWh |
| `forecasts` | 96 × ML forecast: `SiteForecast` or dict (`demand_mw`, `production_mw`, `prod_low_mw`, …) |
| `actuals` | 96 × what really happened, `{"demand_mw", "production_mw"}`; `None` = use the forecast |

Other controls: `step()` runs one interval, `run(until="12:00")` stops early, `start_time="11:15"`
starts mid-day, `set_goals(config)` applies new goals from the next interval, `on_interval=callback`
is called with every record (e.g. to push it to the dashboard), `log_path=` writes them as JSONL.

Each interval's record is the settlement record (money, goals, decision, guard, `dashboard_line`)
plus `features` (what the AI saw), `site` (what the battery did) and `loop_warnings`.

## Command line

```bash
python -m loop                                         # demo day, rule-based fallback (instant)
python -m loop --model qwen3:4b --goals "Store 6 MWh by 18:00"   # --model implies --llm
python -m loop --speed 600                             # live, 1.5 s per interval
python -m loop --start 17:00 --until 19:00 --out logs/day.jsonl
```

It prints one line per interval and a summary (profit, value added by the battery, energy bought /
sold / curtailed, LLM vs fallback decisions, goal results).

## When something fails

The day never stops part-way:

| Problem | What happens |
|---|---|
| Features can't be built (e.g. price feed down) | rule-based fallback (`fallback_reason: feature_error`), the guard still enforces hard goals |
| LLM fails or times out | the validator's fallback decides (as in the pipeline) |
| Actual measurement missing or invalid | the forecast is used, with a warning |
| An interval takes longer than its slot (`run_realtime`) | the next one starts at once, with a warning |
| The `on_interval` callback fails (e.g. dashboard down) | the day goes on, with a warning |

`run_realtime` keeps a fixed deadline per interval, so small delays never add up into drift.

Only a broken day setup (not 96 intervals, a start time that isn't an interval) raises `LoopError`.

## Demo day

`demo_day(seed)` is a made-up day at the site's scale: night prices around €80, negative prices over
midday solar, an evening peak around €160; demand 1.5–4.5 MW, solar up to ~6 MW, some wind. Actuals
are the forecast plus seeded noise. The same seed always gives the same day.

## Tests

```bash
python -m pytest tests/test_loop.py
```

A whole day with the battery carried over, books matching the simulator, a reserve goal met by its
deadline, every failure case above, mid-day start and stop, goal changes, real-time pacing, and the
command line.
