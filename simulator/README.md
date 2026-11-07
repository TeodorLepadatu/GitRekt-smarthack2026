# Simulator

A pretend site (battery, inverter, grid meter) for the "Site" box in plan section 2. Every
15 minutes it runs the guard's command against the interval's **actual** demand and production,
and reports what physically happened.

```
guard command + actual demand/production ─► SiteSimulator.step() ─► new stored energy,
                                                                     grid import/export,
                                                                     curtailed, throughput, losses
```

## Usage

```python
from simulator import SiteSimulator
from config import Settings

sim = SiteSimulator(Settings().battery, stored_mwh=3.5)

cycle = run_cycle(features, goal_config)                  # pipeline: prompt → LLM → validator → guard
r = sim.step(cycle.setpoint, demand_mw=4.6, production_mw=5.0,
             time="15:00", forecast=features)

r.stored_after_mwh      # 3.71
r.grid_import_mwh       # 0.1175
features["stored_mwh"] = sim.stored_mwh                   # next cycle starts from here
```

`command` is the guard's `GuardResult` or its dict (`battery_setpoint_mw`, `curtail_mw`).
`sim.stored_mwh` is read-only: it changes only through `step()` or `reset(mwh)` (a new day).
`sim.sensors()` returns the state for the next cycle and `sim.history` every step.
`StepResult.to_dict()` rounds to 6 decimals; the object itself keeps full precision.

## Physics

All battery formulas come from `battery.BatteryConfig`, the same ones the guard uses.

- **Battery:** charging stores `η × energy in`; discharging removes `energy out / η`
  (η = √0.90 ≈ 0.949). It never goes above capacity, below the minimum (1 MWh) or over rated
  power. A setpoint that would is cut back and reported as `clipped`. Overshoots under 0.001 MW
  are the guard's 4-decimal rounding and are not reported.
- **Grid** (automatic): `production − curtailed − demand − battery`. Positive is export,
  negative is import.
- **Curtailment** only removes production, and only what would otherwise be exported. If actual
  production is lower than forecast, less is curtailed, so curtailing never causes an import or
  throws away battery energy.

## Output: `StepResult`

| Field | Meaning |
|---|---|
| `stored_before_mwh`, `stored_after_mwh` | battery energy |
| `setpoint_requested_mw`, `setpoint_applied_mw`, `clipped` | what the guard asked for vs. what ran |
| `demand_mw`, `production_mw` | actual values |
| `curtailed_mw` | production thrown away |
| `grid_import_mwh`, `grid_export_mwh` | bought / sold |
| `battery_throughput_mwh` | energy through the battery (settlement: wear = €2.5 × this) |
| `losses_mwh` | energy lost to efficiency |
| `demand_error_mw`, `production_error_mw` | actual − forecast, if `forecast` is given (ML retraining data) |
| `warnings` | clipping, reduced curtailment, invalid command |

## Inputs that are wrong

| Problem | Result |
|---|---|
| Missing or invalid setpoint / curtail, or a negative curtail | treated as 0, with a warning, like an inverter ignoring a bad command |
| `time` not `"HH:MM"`, or `forecast` not a dict | ignored, with a warning |
| Negative or non-numeric demand / production | `SimulatorError` |
| Starting energy outside 0 – capacity | `SimulatorError` |

A step that raises changes nothing: the stored energy and the history stay as they were.

## Not here

- **Money** (prices, profit, wear cost): settlement reads `StepResult`.
- **The 15-minute loop** over a whole day: the clock calls `step()` once per interval.

## Tests

```bash
python -m pytest tests/test_simulator.py
```

They check the plan's example (3.5 MWh + 0.87 MW → 3.71 MWh, import 0.1175 MWh), that energy
balances in 270 combinations, the battery limits, the 90 % round trip, curtailment (never more
than was produced), bad inputs, and four full cycles through `pipeline.run_cycle` with the state
carried over.
