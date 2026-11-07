# goals — user goals

Turns what the site owner types ("store 6 MWh by 18:00") into a structured
`GoalConfig`. The feature builder, the guard layer and settlement all read that config.

```
user sentences ──► GoalStore.set() ──(only if changed)──► GoalParser.parse()
                                                            │
                                    LLM extracts fields ◄───┘
                                    code assigns ids, defaults, checks
                                                            │
                     ParseResult(config, questions, error) ◄┘
```

The parser runs **only when the goals change**, not every 15-minute cycle.

## Files

| File | Contents |
|---|---|
| `schema.py` | The contract: `GoalConfig`, `ReserveGoal`, `MinSellPriceGoal` |
| `parser.py` | `GoalParser`: LLM extraction + deterministic post-processing (`build_config`) |
| `store.py` | `GoalStore`: holds the active config, re-parses on change, persists to JSON |

## Supported goals

| User says | Parsed as | Hard/soft |
|---|---|---|
| "I want to maximise profit" | `objective: "max_profit"` (default) | soft |
| "Use my own energy first" | `objective: "self_consumption"` | soft |
| "Store 6 MWh by 18:00" | `reserve, mwh=6, by="18:00", until="18:00"` | hard |
| "Keep 2 MWh backup overnight" | `reserve, mwh=2, by="22:00", until="06:00"` | hard |
| "Never sell below €50/MWh" | `min_sell_price, eur_per_mwh=50` | hard |

How a `reserve` goal uses its fields:
- The battery must hold at least `mwh` by `by`, and keep it until `until`.
- `until == by` means a plain deadline.
- `until` earlier than `by` means the window runs past midnight.
- Times are always `HH:MM` in 24-hour format.

The parser only produces this config; it does not enforce it. The guard layer enforces hard goals. Soft goals
(the objective) reach the decision LLM through the USER GOALS prompt section.

## Contract (output)

```json
{
  "objective": "max_profit",
  "goals": [
    {"id": "g1", "type": "reserve", "mwh": 6.0, "by": "18:00", "until": "18:00",
     "hard": true, "source_text": "Store 6 MWh by 18:00"},
    {"id": "g2", "type": "min_sell_price", "eur_per_mwh": 50.0,
     "hard": true, "source_text": "Never sell below 50 EUR/MWh"}
  ]
}
```

`goals` is a discriminated union on `type`, so consumers can branch with
`isinstance(goal, ReserveGoal)` or `goal.type == "reserve"`.

## Who does what

The LLM **only extracts** what the user said: type, amount, times, price. It follows the
few-shot examples in `SYSTEM_PROMPT`, and its output is limited to a JSON schema.

Then `build_config()` does the rest in plain Python:
- assigns ids `g1, g2, …` in order
- sets `until = by` when only a deadline was given
- defaults the objective to `max_profit` when none was stated
- rejects a reserve larger than `battery_capacity_mwh`
- validates `HH:MM` times

Each problem becomes a **question** for the user instead of a guess.

## Usage

```python
from goals import GoalParser, GoalStore

store = GoalStore(GoalParser(), path="data/goals.json")

result = store.set(["I want to maximise profit", "Store 6 MWh by 18:00"])
if result.ok:
    print(store.config)        # now active
elif result.questions:
    print(result.questions)    # ask the user, then call set()/add() again
else:
    print(result.error)        # LLM unreachable / unusable output

store.add("Never sell below €50/MWh")
store.remove(0)                # by index into store.sentences
```

### `ParseResult`

| Field | Meaning |
|---|---|
| `config` | The parsed `GoalConfig` (may be partial if there are questions) |
| `questions` | Clarifications needed; non-empty → not activated |
| `error` | LLM/transport failure or output that didn't match the schema |
| `ok` | `True` only if there is a config, no questions and no error |

### Store behaviour

- **Same sentences:** no LLM call; the cached config comes back.
- **Questions or an error:** the new sentences are **not** saved and the **previous config stays
  in force**. A vague edit can never wipe out working goals.
- **Persistence:** `{sentences, config}` is written to `path`, default `data/goals.json`.
  It is reloaded when the store is created.

## Configuration

Set through environment variables (see `config.py`):

| Variable | Default | Used for |
|---|---|---|
| `ENERGY_AGENT_GOAL_MODEL` | falls back to `ENERGY_AGENT_MODEL` | model for parsing |
| `ENERGY_AGENT_MODEL` | `qwen3.5:4b` | |
| `ENERGY_AGENT_BATTERY_MWH` | `10` | capacity check on reserve goals |
| `ENERGY_AGENT_GOALS_PATH` | `data/goals.json` | where the store persists |

## Adding a goal type

1. Add a model with a `type: Literal["..."]` field in `schema.py`. Then add it to the `Goal` union.
2. Add the type to `_RawGoal.type` and to its fields in `parser.py`.
3. Describe it, with one example, in `SYSTEM_PROMPT`.
4. Handle it in `build_config()`: check required fields and add questions when they are missing.
5. Enforce it in the guard layer, and add a test in `tests/test_goals.py`.

## Known limitations

- **No live test yet.** The parser has only been tested against a fake client. In live runs Mistral
  dropped the clear goals, and Gemma4 was killed before it answered.
  Pull and test the target model (`ollama pull qwen3.5:4b`) before relying on it.
- **Vague goals block the whole set.** If one sentence is vague, the clear goals parsed alongside it
  are not activated either.
- **No conversation memory.** Clarification works by editing the sentences: the user rewrites or adds
  the vague goal, then calls `set()` or `add()` again.
- **Fixed 24-hour times.** Times are clock times that repeat every day; there is no date or timezone handling.
