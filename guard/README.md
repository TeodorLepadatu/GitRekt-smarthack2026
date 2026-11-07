# Guard layer

The last check before the battery. It turns the validated decision into MW
setpoints that are **physically possible** and **keep the user's hard goals**,
whatever the LLM said.

```
validator decision ──▶ [ guard ] ──▶ battery_setpoint_mw, curtail_mw, applied_rules
```

Pure Python standard library, no dependencies. Works with plain dicts, so it
does not need the `validator` package.

## Usage

```python
from guard import guard, BatteryConfig

result = guard(
    decision={"battery_fraction": 0.29, "curtail": False, "source": "llm"},
    state={"time": "15:00", "stored_mwh": 3.5, "net_mw": 0.4,
           "price_now": 95, "production_mw": 5.0},
    goal_config={"objective": "max_profit", "goals": [
        {"id": "g1", "type": "reserve", "mwh": 6.0, "by": "18:00", "hard": True},
        {"id": "g2", "type": "min_sell_price", "eur_per_mwh": 50, "hard": True}]},
    cfg=BatteryConfig(),          # 10 MWh, 3 MW, round-trip 0.90, 15 min
)
result.to_dict()
# {"battery_setpoint_mw": 0.87, "curtail_mw": 0.0, "clipped": False,
#  "applied_rules": ["soc_limits_ok", "goal_g1_ok", "goal_g2_ok"],
#  "source": "llm", "expected_soc_after_mwh": 3.7064, ...}
```

`guard()` never raises. On bad input it holds the battery (`0.0`).

## Steps

1. **Fraction to MW:** `battery_fraction × P_max`.
2. **Physical limits** (plan section 6):
   `max_charge = min(P_max, free / (ηc × 0.25 h))`,
   `max_discharge = min(P_max, (E − E_min) × ηd / 0.25 h)`.
3. **Hard goals** (`hard: false` goals are ignored here):
   - **reserve** `{mwh, by, until}`:
     - Before `by`: latest-start rule. If, after this interval, full-power charging
       plus a 30 min buffer no longer fits, charging is forced at full power.
       This also blocks discharges that would make the goal unreachable.
       Plan example: forced from the 16:30 interval.
     - In `[by, until)`: no discharge below `mwh`. Windows that cross midnight work.
   - **min_sell_price** `{eur_per_mwh}`: if the price is below the limit, nothing
     is exported. Discharge only covers the site's own deficit, surplus is
     stored, and the rest is curtailed.
4. **Physics wins over goals.** If a goal can't be met even at full power, the
   rule `goal_<id>_unreachable` is added.
5. **Curtailment** is only allowed when the price is below 0 and surplus remains
   (or when min-sell forces it), and is capped at `production_mw`. The guard
   doesn't discharge while curtailing.

## `applied_rules` values

`soc_limits_ok`, `soc_max_limit`, `soc_min_limit`, `fraction_clamped`,
`invalid_fraction_hold`, `no_soc_hold`, `guard_error_hold`,
`goal_<id>_ok`, `goal_<id>_forced_charge`, `goal_<id>_blocked_discharge`,
`goal_<id>_blocked_export`, `goal_<id>_unreachable`, `goal_<id>_invalid`,
`goal_<id>_unknown_type`, `curtail_applied`, `curtail_forced_min_sell`,
`curtail_blocked_price_not_negative`, `curtail_blocked_no_surplus`,
`no_discharge_while_curtailing`.

## Assumptions to confirm with the team

- Sign convention: setpoint **+ = charge, − = discharge**.
- A reserve goal with `until` missing or equal to `by` is protected **until midnight**.
- "Never sell below €X" blocks **all** export, including solar surplus.
- `state` keys: `time`, `stored_mwh`, `net_mw`, `price_now`, `production_mw` (optional).
- The guard uses the **forecast** `net_mw`. If the actual surplus is higher, some
  energy can still be exported below the min-sell price. The site/simulator needs
  a real-time check for that.

## Files

| File | Contents |
|---|---|
| `config.py` | `BatteryConfig`, power limit formulas, SoC after one interval |
| `goals.py` | reserve and min-sell rules, time windows |
| `guard.py` | `guard()`, `GuardResult` |

## Tests

```bash
python -m unittest discover -s guard/tests -t .
```

Not tested yet: the plan's target of **hard goals never violated in simulation**
(needs the simulator).
