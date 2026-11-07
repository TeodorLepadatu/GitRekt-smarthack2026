# Prompt assembly

Builds the prompt the decision LLM reads every 15 minutes (plan section 2):

```
1–6  rules            sections.py
7    6 solved examples examples.py
8    current situation situation.py   ← feature builder
     USER GOALS        user_goals.py  ← goal parser + feature builder
     "Respond ONLY with the JSON decision"
```

About 3.8k tokens. It does **no arithmetic**: every number comes from the feature builder.

## Usage

```python
from prompt_assembly import assemble_prompt

a = assemble_prompt(features, goal_config)  # goal_config: GoalConfig, its dict, or None
a.text              # the prompt
a.labels            # price_zone, price_negative, price_cheap, surplus_left
a.warnings          # inputs that were dropped (empty for clean input)
a.estimated_tokens
```

`render_prompt(features, goal_config)` returns just the text. The output depends only on the inputs.

## Inputs

**Required** (missing or invalid → `PromptAssemblyError`):
`time` (`"HH:MM"`), `demand_mw`, `production_mw`, `net_mw`, `price_now`, `break_even`,
`stored_mwh`, `max_charge_mw`, `max_discharge_mw`, `matching_fraction`.

**Optional:** `net_label`, `prod_low_mw`, `prod_high_mw`, `uncertainty`, `price_rank`, `price_avg`,
`future_max_price`/`_time`, `future_min_price`/`_time`, `price_vs_breakeven` (`"EUR 53 BELOW"`),
`break_even_before_refill`, `next_refill_time`, `min_soc_mwh`, `capacity_mwh`,
`max_charge_fraction`, `max_discharge_fraction` (recommended: the model can't divide), and the
labels `price_zone`, `price_cheap`, `surplus_left` (computed if not sent).

**Goal progress**, in `features["goals"]`, one entry per reserve goal:

```json
{"id": "g1", "status": "on_track", "need_mwh": 2.5, "hours_left": 3.0,
 "even_pace_fraction": 0.29, "force_from": "16:30"}
```

`status` is `on_track`, `behind` or `met`. Minimum sell prices need no progress entry.

### Bad input

| Problem | Result |
|---|---|
| Required field missing or broken | `PromptAssemblyError`; the pipeline falls back to the rule-based policy |
| Optional field broken | left out, with a warning |
| Other spelling (`"high"`, `"far_above"`) | normalised |
| Broken goal or goal progress | shown as incomplete or `UNKNOWN`, with a warning; the guard still enforces it |

## How the rules work

Tuned against qwen3:4b. Three things made the small model reliable:

1. **Labels instead of comparisons.** Code computes them; the model just reads them.

   | Label | Rule |
   |---|---|
   | price zone | `FAR ABOVE` > break-even + 20, `ABOVE` > +5, `NEAR` ±5, else `BELOW` (uses `break_even_before_refill` if given) |
   | price cheap | price < 0 or `price_rank` ≤ 30 |
   | surplus left | `net_mw` > `max_charge_mw` |

2. **Six ordered steps where the last change wins**, so no step needs an "unless":
   1. start from the zone (FAR ABOVE sell, ABOVE never charge, NEAR match, BELOW never discharge)
   2. "own energy first" → matching fraction
   3. cheap price → charge limit
   4. HIGH uncertainty → within ±0.50
   5. hard goals → at least the goal's pace
   6. stay within the charge/discharge limits

   Then `curtail` = surplus left AND (price negative OR a minimum sell price is blocked).

3. **Examples that walk through every step.** The model copies the examples more than it follows
   the rules. The six examples cover every zone, negative prices with curtailment, HIGH uncertainty
   and a reserve goal (the plan's 15:00 case). Their numbers follow the plan's formulas and
   `battery.BatteryConfig`.

**Safety:** the user's goal text never enters the prompt; goals are rewritten from their structured
fields, so a goal sentence can't inject instructions.

## Files

| File | Contents |
|---|---|
| `assemble.py` | `assemble_prompt`, `render_prompt`, `estimate_tokens`, `TOKEN_BUDGET` |
| `sections.py` | rules text |
| `examples.py` | the six solved examples |
| `situation.py` | current situation, input checks, labels |
| `user_goals.py` | USER GOALS section |
| `fmt.py` | number formatting, `PromptAssemblyError` |

## Tests

```bash
python -m pytest tests/test_prompt_assembly.py       # no model needed
python -m pytest tests/test_prompt_assembly_llm.py   # real model, skipped if not installed
```

The real-model test runs 19 situations that aren't in the examples. qwen3:4b gets all 19 right
(5–15 s each). Use `PROMPT_ASSEMBLY_TEST_MODEL=qwen3.5:4b` to test the project's default model.

## Notes for the team

- **Feature builder:** please send `max_charge_fraction`, `max_discharge_fraction` and the goal progress.
- **Decision step:** cap the answer length (`num_predict` ≈ 600); a looping model otherwise runs to the 60 s timeout.
- **Size:** about 3.8k tokens, above the plan's 2.5k estimate, but well within `num_ctx` 8192.
