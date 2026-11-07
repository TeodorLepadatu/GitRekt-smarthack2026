# Prompt assembly

Builds the complete prompt the **decision LLM** reads every 15 minutes (plan section 2, "Prompt
assembly"). It joins:

```
sections 1–6   fixed rules                         (sections.py)
section 7      five solved examples                (examples.py)
section 8      current situation                   ← feature builder's values   (situation.py)
USER GOALS     objective + status of each goal     ← goal parser + feature builder (user_goals.py)
               "Respond ONLY with the JSON decision …"
```

into one string (about 3.7k tokens) for `DecisionAgent.decide(prompt)` in `llm/decision.py`.

Prompt assembly does **no arithmetic**: every number in the prompt comes from the feature builder.
It only formats values, adds a few yes/no **labels** (comparisons of numbers it was given) and checks
that the inputs are complete.

---

## Usage

```python
from prompt_assembly import render_prompt

prompt = render_prompt(features, goal_config)   # str
result = agent.decide(prompt)                   # llm.DecisionAgent
```

- `features`: the feature builder's values for this interval (dict, see below).
- `goal_config`: the goal parser's output, as a dict or anything with `to_dict()` (e.g.
  `goal_parser.GoalConfig`). `None` means objective "maximise profit" and no goals.
- Raises `PromptAssemblyError` if a required value is missing or not a number, or if the goal
  config has an unknown objective or goal type. The prompt is never sent half-filled.
- The result depends only on the inputs: the same inputs always give the same prompt.

Smaller pieces are available too: `render_situation(features)`, `render_user_goals(goal_config,
features)`, `render_examples()`, `estimate_tokens(text)`.

---

## Input: `features` (from the feature builder)

Names follow the plan's section 5 contract "Feature builder → prompt assembly".

**Required.** Missing any of these raises `PromptAssemblyError`:

| Field | Example | Meaning |
|---|---|---|
| `time` | `"15:00"` | start of the interval |
| `demand_mw`, `production_mw`, `net_mw` | `4.6`, `5.0`, `0.4` | ML forecasts; net = production − demand |
| `price_now` | `95` | EUR/MWh |
| `break_even` | `148.0` | EUR/MWh, value of a stored MWh |
| `stored_mwh` | `3.5` | battery energy now |
| `max_charge_mw`, `max_discharge_mw` | `3.0`, `3.0` | limits for **this 15-minute interval** |
| `matching_fraction` | `0.13` | battery_fraction that exactly absorbs the net |

**Optional.** Each one adds a line or detail to the prompt when present:

| Field | Shown as |
|---|---|
| `net_label` | `(SURPLUS)` / `(DEFICIT)` |
| `prod_low_mw`, `prod_high_mw` | forecast range |
| `uncertainty` | `LOW` / `MEDIUM` / `HIGH` |
| `price_rank`, `price_avg` | rank 0–100 today, day average |
| `future_max_price`, `future_max_time`, `future_min_price`, `future_min_time` | "Later today" line |
| `price_vs_breakeven` | e.g. `"EUR 53 BELOW"` |
| `break_even_before_refill`, `next_refill_time` | plan section 6, fix 3 |
| `min_soc_mwh`, `capacity_mwh` | battery bounds |
| `max_charge_fraction`, `max_discharge_fraction` | the limits as battery_fraction (limit ÷ rated power). **Strongly recommended**: the model cannot divide, and without these it only sees MW |
| `goals` | goal progress, see below |
| `price_zone`, `price_cheap`, `surplus_left` | labels; computed here if not sent |

**Goal progress** (`features["goals"]`), one entry per reserve goal, matched to the goal config by `id`:

```json
{"id": "g1", "status": "on_track", "need_mwh": 2.5, "hours_left": 3.0,
 "even_pace_fraction": 0.29, "force_from": "16:30"}
```

`status` is `on_track`, `behind` or `met`. A reserve goal with no progress entry is still listed
(as "NOT COMPUTED"; the guard still enforces it). A minimum sell price needs no progress: prompt
assembly compares it with `price_now` and shows "allowed" or "BLOCKED".

---

## What the prompt looks like

The current situation and USER GOALS for the plan's 15:00 example:

```
8. CURRENT SITUATION (interval starting 15:00)
- Site: demand 4.6 MW, production 5.0 MW -> net +0.4 MW (SURPLUS)
- Price now: EUR 95/MWh (rank 70/100 today, day average EUR 80)
- Later today: highest EUR 170 at 20:30
- Break-even: EUR 148/MWh -> price now is EUR 53 BELOW break-even
- Battery: 3.5 MWh stored
- This interval: charge up to 3.0 MW; discharge up to 3.0 MW
- Matching fraction: +0.13
- Labels: price zone BELOW; price negative NO; price cheap NO; surplus left after charge limit NO

USER GOALS (hard goals come before profit; within them, follow the objective)
- Objective: maximise profit
- [HARD] Store 6 MWh by 18:00 -> status: ON TRACK. Have 3.5 MWh, need +2.5 MWh in 3.0 h.
         Even pace: battery_fraction >= 0.29. Guard forces full charging from 16:30 if still open.
- [HARD] Never sell below EUR 50/MWh -> current price EUR 95: allowed.

Respond ONLY with the JSON decision, with these keys in this order: …
```

To see a whole prompt:

```bash
python -c "from prompt_assembly import render_prompt, SOLVED_EXAMPLES as E; print(render_prompt(E[4]['features'], E[4]['goal_config']))"
```

---

## How the rules work

The plan's original break-even prompt was not available, so sections 1–7 were written from the
plan. They were then tuned against the real model (qwen3:4b): prose rules such as "discharge more
only if the price is well above break-even" were followed unreliably, while a **lookup table over
labels plus numbered steps** was followed reliably.

### Labels (`situation.labels()`)

Added to every situation, computed by comparing values the feature builder sent:

| Label | Rule |
|---|---|
| price zone | `FAR ABOVE` if price > break-even + 20, `ABOVE` if > +5, `NEAR` if within ±5, else `BELOW`. Uses `break_even_before_refill` when given |
| price cheap | price < 0, or `price_rank` ≤ 30 |
| price negative | price < 0 |
| surplus left after charge limit | `net_mw` > `max_charge_mw` |

If the feature builder sends `price_zone`, `price_cheap` or `surplus_left` itself, its values are used.
The thresholds are `NEAR_EUR`, `FAR_ABOVE_EUR` and `CHEAP_RANK` in `situation.py`.

### The six steps (prompt section 4)

| Step | Rule |
|---|---|
| 1 | Start from the table: FAR ABOVE → sell at the discharge limit; ABOVE → surplus 0.00 / deficit matching; NEAR → matching; BELOW → surplus matching / deficit 0.00 |
| 2 | Price cheap and zone BELOW or NEAR → charge limit (buy cheap energy) |
| 3 | Objective "use own energy first" → matching fraction (unless step 2) |
| 4 | Hard goals: at least the reserve goal's even pace; a BLOCKED minimum price → don't export |
| 5 | Uncertainty HIGH → keep within ±0.50 (unless step 4 needs more) |
| 6 | Never past the charge / discharge limits |

Then **curtail** = surplus left after the charge limit AND (price negative OR a minimum sell price is BLOCKED).

### The five solved examples

Each example's reasoning walks through the steps ("Step 1: … Step 2: … Curtail: …"). The model
copies that pattern, which is what made it reliable. Together they cover steps 1–6, the FAR ABOVE
sign (sell even with a surplus), negative prices with curtailment, HIGH uncertainty and a reserve
goal (the plan's 15:00 example). Every example also shows its USER GOALS section, so the format
always matches the current situation.

All example numbers follow the plan's formulas, including the section 6 fixes:
- break-even = 0.9 × highest later price − 5
- max charge = min(3, free / (η × 0.25 h)), max discharge = min(3, (stored − min) × η / 0.25 h)
- matching = net / 3

The tests recompute them, and a reference version of the six steps checks that every example decision
follows the written rules. The plan asks to regenerate the examples with the feature builder's code
once it exists.

### Plan section 6 fixes

| Fix | Where |
|---|---|
| 1. Limits on a 15-minute basis | example numbers; "This interval: … up to" lines |
| 2. Break-even from the k-th highest later price | the feature builder computes `break_even`; the prompt only uses it |
| 3. Break-even before the next refill | optional `break_even_before_refill`; the price zone uses it |
| 4. Energy balance includes curtailment | section 2: `production − demand = battery_charging + grid_export + curtailed` |

### Safety choices

- **User text never enters the prompt.** Goals are described from their structured fields
  ("Store 6 MWh by 18:00"), never from the user's `source_text`, so a goal sentence cannot add
  instructions to the decision prompt.
- **The model is told not to compute.** The operator sentence may only reuse numbers from the
  situation (no MW values worked out from fractions).
- **Reasoning is kept short.** One sentence per step, never going back. Without this the model
  sometimes looped ("Wait, let's re-read…") until the timeout.

---

## Files

| File | Contents |
|---|---|
| `assemble.py` | `render_prompt()`, `render_examples()`, `estimate_tokens()`, `TOKEN_BUDGET` |
| `sections.py` | fixed text: sections 1–6, examples header, closing instruction |
| `examples.py` | the five solved examples (feature-builder-shaped situations + decisions) |
| `situation.py` | section 8 renderer, required/optional fields, labels |
| `user_goals.py` | USER GOALS renderer |
| `fmt.py` | number formatting shared by all sections, `PromptAssemblyError` |

No extra packages, Python 3.10+.

---

## Tests and results

```bash
python -m pytest tests/test_prompt_assembly.py       # fast, no model needed
python -m pytest tests/test_prompt_assembly_llm.py   # real model; skipped if it is not installed
```

`test_prompt_assembly_llm.py` sends 9 situations that are not among the examples (selling at a
peak, negative price, goal behind schedule, near break-even, own energy first, blocked minimum
price, HIGH uncertainty, covering a deficit, staying idle) through the decision contract of
`llm/decision.py`. With **qwen3:4b** all 9 are decided correctly, at 5–10 s each.
`PROMPT_ASSEMBLY_TEST_MODEL=qwen3.5:4b` runs them against the decision step's default model.

## Notes for the team

- **Feature builder:** please send `max_charge_fraction` / `max_discharge_fraction` and the goal
  progress fields above. The labels can move into the feature builder at any time (same keys).
- **Decision step (`llm/decision.py`):** consider capping the answer length (Ollama option
  `num_predict`, e.g. 600). A small model that starts looping otherwise only stops at the 60 s
  timeout.
- **Prompt size:** about 3.7k tokens, more than the plan's 2.5k estimate, because of the step-by-step
  examples. That fits the 8192-token `num_ctx`; `TOKEN_BUDGET` (6000) is checked in the tests.
