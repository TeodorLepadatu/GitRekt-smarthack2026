# feature_builder

The accountant of the cycle. It does **all** the arithmetic, so nothing after it
has to calculate anything: the small LLM only compares numbers it is handed, and
the guard only clamps them.

```python
from feature_builder import SiteForecast, build_features
from pipeline import run_cycle

features = build_features(
    time="15:00",                       # interval start
    forecast=SiteForecast(              # ML 1 + ML 2
        demand_mw=4.6, production_mw=5.0,
        prod_low_mw=4.4, prod_high_mw=5.6,
    ),
    prices=day_ahead_prices,            # today's 96 EUR/MWh values
    stored_mwh=3.5,                     # sensors
    goal_config=store.config,           # goal parser, or None
)
result = run_cycle(features, store.config)
```

## What it sends

One flat dict holding every field of prompt section 8 plus the goal progress.
Section 5 of the plan, at 15:00:

```json
{"time": "15:00", "demand_mw": 4.6, "production_mw": 5.0, "net_mw": 0.4, "net_label": "SURPLUS",
 "price_now": 95, "price_rank": 71, "price_avg": 79.7, "future_max_price": 170, "future_max_time": "20:30",
 "break_even": 148.0, "price_vs_breakeven": "EUR 53 BELOW",
 "stored_mwh": 3.5, "max_charge_mw": 3.0, "max_discharge_mw": 3.0, "matching_fraction": 0.13,
 "goals": [{"id": "g1", "status": "on_track", "need_mwh": 2.5, "hours_left": 3.0,
            "even_pace_fraction": 0.29, "force_from": "16:30"}]}
```

Consumers and what each one reads:

| Consumer | Reads |
|---|---|
| `prompt_assembly.situation` | the section 8 fields and the three labels |
| `prompt_assembly.user_goals` | `goals`, the progress list |
| `validator.fallback` | `price_now`, `break_even`, `net_mw`, `matching_fraction`, `max_charge_mw`, `goals` |
| `guard` | `time`, `stored_mwh`, `net_mw`, `price_now`, `production_mw` |
| settlement | `interval_h`, `wear_eur_per_mwh`, and everything above |

Extra keys such as `break_even_k`, `break_even_price`, `solar_mw` and `wind_mw`
are for the dashboard and the logs. Prompt assembly ignores keys it does not know.

## Files

| File | Role |
|---|---|
| `prices.py` | `PriceCurve`: rank, day average, later extremes, k-th highest, next cheap refill |
| `goals.py` | reserve-goal progress: status, energy still needed, even pace, latest start |
| `builder.py` | `build_features()`: puts section 8 together |

## The plan's section 6 fixes

**Fix 1, limits on a 15-minute basis.** `max_charge_mw` and `max_discharge_mw`
come straight from `battery.BatteryConfig`, which uses
`min(P_max, free / (eta_c x 0.25 h))`. A battery at 9.5 of 10 MWh can only take
2.11 MW, not 3.

**Fix 2, break-even from the k-th highest later price.** One interval at 3 MW
sells 0.75 MWh, so a battery holding several intervals' worth cannot get the
single best price for all of it. `k` is how many full-power intervals it takes
to empty the usable energy, and the k-th highest later price is used instead of
the maximum. At 15:00 with 3.5 MWh stored, k is 4. With 8 MWh stored, k is 10 and
break-even drops, which is the point: a fuller battery should be readier to sell.

**Fix 3, break-even before the next refill.** Holding energy past a cheap period
earns nothing extra, because the battery can refill there. When a cheap interval
comes before the peak, a second break-even is computed over the intervals up to
it, and reported as `break_even_before_refill`. That value, not the headline one,
decides the price zone. In the plan's case of morning 120, midday -8, evening 165,
the agent now sells in the morning instead of holding.

**Fix 4, curtailment in the energy balance.** That is prompt text, not a number,
and it is already in `prompt_assembly.sections`.

## Decisions worth knowing

**Goal progress carries two numbers, not one.** `even_pace_fraction` is the
steady pace that reaches the target without hurrying, so the LLM is free to wait
for a cheaper interval. `force_from` is the last interval from which full power
still makes the deadline, including a 30-minute buffer, so the prompt can warn
before the guard takes over. The guard's own rule is the authoritative one;
`force_from` is computed assuming no charging happens this interval, which is
what gives the plan's 16:30.

**Window semantics are imported from `guard.goals`.** `to_minutes`, `in_window`
and the "no `until` means protect to midnight" assumption all come from the
guard, because the prompt and the guard must describe the same window.

**Labels are computed here, and a test checks they match.**
`prompt_assembly.situation.labels` derives `price_zone`, `price_cheap` and
`surplus_left` for itself when a feature set arrives without them. Both copies
must agree, so `tests/test_feature_builder.py` builds features for 24 time and
state-of-charge combinations and asserts the two agree on every one.

**Uncertainty is measured against rated battery power.** The ML 2 band divided
by `P_max`: at or below 0.25 is LOW, at or below 0.50 is MEDIUM, above that is
HIGH. What matters is whether the forecast error is large enough to change the
action, not how large it is next to production. Five of the six hand-written
solved examples in `prompt_assembly.examples` agree with this rule. The 15:00
one is labelled LOW by hand and MEDIUM by the rule; the plan asks for the
examples to be regenerated from this code, which will settle it.

**Price rank uses the midpoint of a tied block.** Intervals at the same price
always get the same rank. A flat plateau at the cheapest price therefore reads
slightly above 0, and a unique cheapest interval reads exactly 0.

**The wear rate comes from `battery.BatteryConfig`.** Break-even charges twice
that rate, because holding a MWh for later costs one charge and one discharge.
Settlement charges the same rate per MWh of throughput, so the number the agent
decides on and the number it is billed for can never drift apart.

**It raises rather than guesses.** A missing state of charge, an unreadable time
or a demand figure that is not a number raises `FeatureBuilderError`. The
pipeline catches it and the rule-based fallback takes the interval, which is
safer than inventing a number the LLM would then trust. Goals the parser left
incomplete are skipped instead, because the guard still refuses to break them.

## Tests

```
python -m pytest tests/test_feature_builder.py -q
```

52 tests. They reproduce the plan's 15:00 worked example end to end, including
the 0.87 MW setpoint, check each section 6 fix, and run all 96 intervals of a
day through the full pipeline while carrying the state of charge forward.
