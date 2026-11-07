# GitRekt
Smarthack 2026

## Pipeline

What feeds the agent, and where its decisions end up.

```mermaid
flowchart TB
    subgraph prep["Prepared once"]
        DATASET["predictions/dataset.csv<br/>ENTSO-E energy + Open-Meteo weather<br/>140k rows, 15 min"]
        DAPRICE["data/energy_price.csv<br/>ENTSO-E day-ahead price"]
        IMBCSV["synthetic_out/prices.csv<br/>synth_market.py<br/>imbalance prices"]
        MODELS["predictions/models/*.pkl<br/>ML 1 demand, ML 2 solar + wind<br/>each with a 10-90% band"]
        DATASET --> MODELS
    end

    subgraph build["loop/real_day.py - builds one day"]
        SCALE["SiteScale<br/>national GW to site MW"]
        DAYIN["DayInput<br/>96 x price, forecast, actual, imbalance"]
    end

    MODELS --> SCALE
    DATASET -->|"what really happened"| SCALE
    SCALE --> DAYIN
    DAPRICE --> DAYIN
    IMBCSV --> DAYIN

    GOALTXT["operator writes a goal<br/>'Store 6 MWh by 18:00'"] --> GP["goal_parser<br/>LLM, rule parser on failure<br/>a vague goal becomes a question"]
    GP --> GC["GoalConfig<br/>objective + hard goals"]

    DAYIN --> CYCLE["one interval, every 15 min"]
    GC --> CYCLE
    CYCLE --> LEDGER["settlement.DayLedger<br/>money, goal status, JSONL log"]
    LEDGER -->|"stored energy carries over"| CYCLE
    LEDGER --> CONTRACT["dashboard/contract.py<br/>the shapes types.ts declares"]
    CONTRACT --> BACKEND["backend/app.py<br/>REST + SSE"]
    BACKEND --> UI["frontend<br/>Now, Day, Goals, Decisions"]
```

### One interval

`pipeline.run_cycle(features, goal_config)` runs the middle of this end to end and never
raises; `loop.DayRunner.step()` adds the physics, the money and the balancing around it.

```mermaid
flowchart TB
    F["feature_builder<br/>break-even, price zone, net balance,<br/>this interval's limits, uncertainty,<br/>imbalance risk, goal progress"]
    P["prompt_assembly<br/>rules + solved examples<br/>+ situation + USER GOALS"]
    L["llm.DecisionAgent<br/>Ollama"]
    V{"validator<br/>JSON? fields? ranges?"}
    FB["fallback_decision<br/>deterministic rules"]
    D["Decision<br/>battery_fraction, curtail,<br/>confidence, reasoning"]
    G["guard<br/>battery limits + hard goals"]
    SP["setpoint<br/>this is the promise to the grid"]
    B{"is deviating<br/>charged for?"}
    BAL["adjust by the net forecast error,<br/>then through the guard again"]
    SIM["simulator.SiteSimulator<br/>the only place the physics lives"]
    SET["settlement<br/>energy + wear + imbalance"]

    F --> P --> L --> V
    V -->|"valid"| D
    V -->|"timeout, llm_error, bad_json,<br/>missing_field, out_of_range, ..."| FB
    FB --> D
    F -.->|"feature_builder failed:<br/>the LLM is never called"| FB
    D --> G --> SP --> B
    B -->|"no"| SIM
    B -->|"yes"| BAL --> SIM
    SIM --> SET
```

The LLM only ever advises. Every path ends at the guard, and the guard is the last word.

## Decision making

The prompt walks the model through six steps in order; each may change the value, and the
last change wins. All the comparisons are precomputed, so the model only compares labels.

```mermaid
flowchart TB
    S1["<b>Step 1 - where the price sits vs break-even</b><br/>FAR ABOVE, more than EUR 20 above: the discharge limit, sell now<br/>ABOVE: never charge. DEFICIT the matching fraction, SURPLUS 0.00<br/>NEAR, within EUR 5: the matching fraction<br/>BELOW: never discharge. SURPLUS the matching fraction, DEFICIT 0.00"]
    S2["<b>Step 2 - the objective</b><br/>'use my own energy first': the matching fraction"]
    S3["<b>Step 3 - cheap energy</b><br/>price cheap AND zone BELOW or NEAR: the charge limit, buy it"]
    S4["<b>Step 4 - keep room to correct a forecast error</b><br/>uncertainty HIGH: clamp to -0.50 .. 0.50<br/>uncertainty MEDIUM and the band worth EUR 5 or more: clamp to -0.75 .. 0.75<br/>at the full limit nothing is left to correct with, and the deviation is charged for"]
    S5["<b>Step 5 - hard goals</b><br/>a reserve needing fraction >= X: X<br/>'never sell below' BLOCKED: no discharging past the deficit"]
    S6["<b>Step 6 - the battery</b><br/>clamp to this interval's charge and discharge limit"]
    OUT["battery_fraction, -1.00 .. 1.00"]
    CURT["<b>curtail</b><br/>true only when surplus is left after the charge limit<br/>AND the price is negative or 'never sell below' is BLOCKED"]

    S1 --> S2 --> S3 --> S4 --> S5 --> S6 --> OUT
    S6 -.-> CURT --> OUT
```

Then the guard checks the answer against the physics and the hard goals. It may clip
anything, and whatever it applies is named in `applied_rules` so the dashboard can say why.

```mermaid
flowchart LR
    IN["battery_fraction<br/>curtail"] --> G{"guard"}
    G --> R1["soc_min_limit, soc_max_limit<br/>stored energy stays inside the battery"]
    G --> R2["goal_X_forced_charge<br/>goal_X_blocked_discharge<br/>a reserve must still be reachable"]
    G --> R3["goal_X_blocked_export<br/>nothing sold below the minimum price"]
    G --> R4["curtail_applied, curtail_forced_min_sell<br/>switching output off is a last resort"]
    G --> R5["no_soc_hold, guard_error_hold<br/>unknown state: hold"]
    R1 --> OUT["battery_setpoint_mw<br/>curtail_mw<br/>clipped"]
    R2 --> OUT
    R3 --> OUT
    R4 --> OUT
    R5 --> OUT
```

## Run a day

```powershell
python -m loop                        # a made-up demo day, rule-based fallback only (instant)
python -m loop --date 2026-06-15      # a real day: ML forecasts + ENTSO-E day-ahead prices
python -m loop --date 2026-06-15 --llm --model qwen3.5:latest --goals "Store 6 MWh by 18:00"
```

`--date` replays a real day end to end. The forecasts are issued at the day-ahead gate
(12:00 local the day before), so the models only see what was known then, and the books
are settled against what was actually measured — the printed forecast error is honest.
`--plan` settles on the forecasts instead, for a day that has not happened yet.

## Imbalance: paying for being wrong

A setpoint is chosen on the forecast and the grid balances whatever really happens, so a
forecast error leaves the site short or long of what it promised. `--imbalance` prices
that gap the way a market does — energy promised and not delivered is bought back above
the day-ahead price, energy delivered unasked is sold below it — and lets the battery
defend the promise, within whatever the guard and its own limits allow:

```powershell
python -m loop --date 2026-06-15 --imbalance
```

Without it the uncertainty the models work out is only a label: being wrong costs the
same as being right, so there is nothing to hedge. With it the 10–90 % band has a price,
and what the battery is really for becomes visible. The same hour of 2026-06-15, with the
battery starting empty:

| | profit | without a battery | the battery added |
|---|---|---|---|
| deviation not priced | −9.84 | +19.72 | **−29.55** |
| deviation priced | −3.20 | −90.31 | **+87.11** |

The battery's measured value changes sign: without one, nothing absorbs the error and the
penalty lands in full. Prices come from `synthetic_out/prices.csv` (`synth_market.py`), so
they are synthetic even on a real day; the day-ahead price stays the real ENTSO-E one and
the pair is clamped so deviating can never pay better than delivering the plan.

| Module | Role |
|---|---|
| `predictions/` | ML 1 + ML 2: demand and production forecasts (solar, wind, total) with 10–90 % ranges for any 15-min timestamp up to 7 days ahead; `DemandForecaster`, `ProductionForecaster` and an HTTP API (see `predictions/README.md`) |
| `loop/` | The clock: one day, interval by interval. `demo_day()` makes a day up; `real_day()` builds one from the ML models and the real price file, scaled from national to site size by `SiteScale` |
| `dashboard/` | The loop's objects as the shapes `frontend/src/api/types.ts` declares, so the API layer has nothing left to translate |
| `backend/` | FastAPI: the dashboard's routes and the SSE stream, running the day loop on a worker thread |
| `simulator/` | The site's physics: what the battery, the export limiter and the grid actually did in one interval |
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
