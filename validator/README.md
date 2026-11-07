# Validator + fallback

Makes sure the guard layer **always** gets a usable decision every 15 minutes,
whatever the LLM did: broken JSON, missing fields, out-of-range numbers,
errors or timeouts.

```
Small LLM ──raw output──▶ [ validator ] ──ok──▶ decision (source = "llm")
                               │
                            invalid / timeout
                               ▼
                         [ fallback rules ] ──▶ decision (source = "fallback")
```

Pure Python standard library, no dependencies.

## Usage

```python
from validator import decide

decision = decide(prompt, features)        # calls local Ollama (qwen3:4b)
decision.to_dict()
# {"battery_fraction": 0.29, "curtail": False, "source": "llm",
#  "confidence": "medium", "explanation_for_operator": "...",
#  "fallback_reason": None, "warnings": [], "latency_s": 2.1, ...}
```

Without Ollama, pass any function `prompt -> raw text`:

```python
decide(prompt, features, llm_call=my_llm, timeout_s=60, logger=print)
```

`decide()` never raises. If the LLM is unreachable, the fallback decides.

## What gets checked

| Field | Rule | If wrong |
|---|---|---|
| `battery_fraction` | number in [-1, 1] (+ charge, − discharge) | fallback |
| | up to ±1.05 is clamped to ±1 | warning |
| `curtail` | bool (`"true"`/`"false"` strings accepted) | fallback |
| `confidence` | `low` / `medium` / `high` | default `low` + warning |
| `reasoning`, `explanation_for_operator` | strings | empty + warning |

It also handles ```` ```json ```` fences, Qwen3 `<think>` blocks and text around the JSON.

## Fallback rules

1. `price_now < 0`: charge at full power; curtail if the surplus is bigger than `max_charge_mw`.
2. `price_now >= break_even`: discharge at full power.
3. Otherwise: `battery_fraction = matching_fraction` (clamped to [-1, 1]).
4. Open hard goal: at least `even_pace_fraction`.
5. Features missing: hold (`0.0`).

`fallback_reason` is one of `timeout`, `llm_error`, `empty_output`, `bad_json`,
`not_a_json_object`, `missing_field`, `bad_type`, `out_of_range`. Use it for the
plan's *valid JSON rate* and *fallback rate* metrics.

## Inputs from other parts

`features` (from the feature builder) needs: `price_now`, `break_even`, `net_mw`,
`matching_fraction`, `max_charge_mw`, and `goals: [{status, even_pace_fraction}]`.

**Assumptions to confirm with the team** (all kept in [`schema.py`](schema.py)):
the sign convention, the field names above, and who owns the Ollama call.

## Files

| File | Contents |
|---|---|
| `schema.py` | contracts, ranges, `Decision`, fallback reasons |
| `validator.py` | `extract_json()`, `validate()` |
| `fallback.py` | `fallback_decision()` |
| `decide.py` | `decide()`, timeout, `ollama_generate()` |

## Tests

```bash
python -m unittest discover -s validator/tests -t .
```

Not tested yet: the plan's target of **≥ 99% valid JSON over a simulated week**
with the real model.
