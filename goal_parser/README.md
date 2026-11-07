# Goal parser

Turns the user's goals, written in plain language, into the structured **goal config** that the
feature builder, guard layer and settlement use (sections 4–5 of the project plan).

```
"Maximise profit. Store 6 MWh by 18:00. Never sell below €50/MWh"
                              │
                              ▼
{
  "objective": "max_profit",
  "goals": [
    {"id": "g1", "type": "reserve", "mwh": 6, "by": "18:00", "until": "18:00",
     "hard": true, "source_text": "Store 6 MWh by 18:00"},
    {"id": "g2", "type": "min_sell_price", "eur_per_mwh": 50,
     "hard": true, "source_text": "Never sell below €50/MWh"}
  ]
}
```

It runs **only when the goals change**, not every 15-minute cycle. If a goal is vague, it asks the
user a question instead of guessing.

---

## Usage

### One-off parse

```python
from goal_parser import parse_goals
from goal_parser.llm import OllamaClient

result = parse_goals(
    "Maximise profit. Store 6 MWh by 18:00. Never sell below €50/MWh",
    llm=OllamaClient(),          # leave out to use only the rule-based parser
    battery_capacity_mwh=10,     # optional: rejects reserves larger than the battery
)

result.config.to_dict()   # the goal config JSON shown above
result.clarifications     # questions to ask the user (empty when everything is clear)
result.source             # "llm" or "rules": which parser produced the result
```

`text` can be one string or a list of strings (e.g. one goal per line from the dashboard).

### In a chat or dashboard: `GoalParser`

`GoalParser` keeps the current goal text, re-parses only when the text actually changes (and
ignores changes in case or spacing), and takes the user's answers to its questions:

```python
from goal_parser import GoalParser
from goal_parser.llm import OllamaClient

parser = GoalParser(OllamaClient(), battery_capacity_mwh=10)

result = parser.parse("Maximise profit. Keep some energy for the evening")
# result.clarifications[0].question
#   -> 'How much energy (in MWh) should I keep for "Keep some energy for the evening", and by what time?'

result = parser.answer(result.clarifications[0], "3 MWh by 19:00")
# result.config.goals -> reserve of 3 MWh by 19:00, no more questions

parser.parse("Maximise profit. Keep some energy for the evening (3 MWh by 19:00)")  # same text: cached, no LLM call
```

Goals that were understood are always in `result.config`, even while other goals are waiting for
an answer, so the rest of the pipeline can keep running.

### What the parser returns

`ParseResult`

| Field / method | Meaning |
|---|---|
| `config` | `GoalConfig` with everything that was understood |
| `clarifications` | list of `Clarification(source_text, question)` to ask the user |
| `needs_clarification` | `True` if there is at least one question |
| `source` | `"llm"` or `"rules"` |
| `to_dict()` | `{"status": "ok" \| "needs_clarification", "source", "config", "clarifications"}` |

`GoalConfig.to_dict()` is the data contract for the other components:

| Field | Values |
|---|---|
| `objective` | `"max_profit"` (default) or `"self_consumption"` (soft: shapes the LLM's choice through the prompt) |
| `goals` | list of goals, ids `g1`, `g2`, … in order |

Goal types (all of them are **hard**, i.e. the guard enforces them):

| User says | Goal | Fields |
|---|---|---|
| "Store 6 MWh by 18:00" | `reserve` | `mwh`, `by` (reach it by), `until` (keep it until; equals `by` if not given) |
| "Keep 2 MWh backup overnight" | `reserve` | overnight = `by: "22:00"`, `until: "06:00"` |
| "Never sell below €50/MWh" | `min_sell_price` | `eur_per_mwh` |

Units are always converted to **MWh** and **EUR/MWh**, and times to 24-hour **`"HH:MM"`**.

---

## How it works

```
user text
   │
   ▼
┌────────────────────┐  raw goals:   ┌───────────────────────────┐   ok   ┌──────────────────────┐
│ LLM (Qwen3 4B via  │ numbers +     │ Checks against the user's │ ─────► │ Validation +         │──► ParseResult
│ Ollama) extracts   │ units as      │ text (parser.py)          │        │ conversion           │    (source: "llm")
│ (prompt.py, llm.py)│ written ────► │                           │        │ (schema.py)          │
└────────────────────┘               └───────────────────────────┘        └──────────────────────┘
         │ error / timeout / not running        │ rejected
         ▼                                      ▼
┌──────────────────────────────────────────────────┐        ┌──────────────────────┐
│ Rule-based parser (rules.py)                     │ ─────► │ same validation      │──► ParseResult
│ same raw format, no AI                           │        │ (schema.py)          │    (source: "rules")
└──────────────────────────────────────────────────┘        └──────────────────────┘
```

The design follows the plan's rule: **the LLM proposes, the code disposes.**

### 1. The LLM extracts (`prompt.py`, `llm.py`)

The model only *reads*: it copies each number and unit exactly as the user wrote them
(`"amount": 1500, "unit": "kWh"`) and never converts or calculates. Settings:

- **temperature 0**: the same input always gives the same output
- **fixed JSON format**: Ollama forces the reply into the exact fields we expect; every field is
  required, because Ollama tends to leave out optional ones
- **thinking off**: Qwen3 answers with the JSON directly
- the prompt has rules plus six worked examples (small models learn mostly from examples)

### 2. The code checks the LLM's answer (`parser.py`)

The answer is rejected, and the rule-based parser used instead, if:

- it is not valid JSON in the expected shape, or has an unknown objective
- it contains a **number** the user never wrote (e.g. the model converted 2 MWh to 2000 kWh, or invented a 0 MWh goal)
- it contains a **time** the user never wrote; a bare hour like "by 6" counts as unconfirmed, so a
  model that picks 06:00 or 18:00 by itself is rejected
- it **skipped** an amount the user wrote (e.g. dropped "never sell under 5 ct/kWh" from a long
  sentence) without asking about it

### 3. Validation and conversion (`schema.py`)

Both the LLM's and the rule parser's raw goals go through the same code, which:

- converts units: kWh → MWh, EUR/kWh and ct/kWh → EUR/MWh
- normalises times: `"6 pm"`, `"18h"`, `"noon"` → `"18:00"`, `"18:00"`, `"12:00"`
- sets `hard: true` on every goal and assigns ids
- turns problems into **questions** instead of guesses:

| Problem | Example | Question |
|---|---|---|
| missing amount | "Keep some energy for later" | how much (MWh), and by what time? |
| missing time | "Store 4 MWh" | by what time? |
| am or pm unclear | "Store 6 MWh by 6" | 06:00 or 18:00? |
| larger than the battery | "Store 15 MWh" (10 MWh battery) | the battery holds at most 10 MWh |
| price outside the market range (−500 to 4000 EUR/MWh) | "Never sell below 50 €/kWh" | did you mean a different unit? |
| not a goal | "Make the neighbours happy" | lists what kinds of goals are possible |

If the same goal is stated twice, **the later statement wins**: "Never sell below €40/MWh. Actually,
never sell below €55/MWh" gives 55. (The LLM also understands shorter corrections such as "Actually
make that 55 €/MWh"; the rule-based parser needs the full goal repeated.) This applies to the objective, the minimum price, and reserves with the
same time window. Reserves for different windows are all kept.

### 4. Rule-based fallback (`rules.py`)

Pattern matching with no AI, used when Ollama is not running, times out, or the LLM's answer is
rejected. It recognises the common phrasings, for example:

- objectives: "maximise profit", "make money"; "use my own energy / my solar first", "self-sufficient", "avoid the grid"
- reserves: "store / keep / hold X MWh (or kWh, megawatt hours) by / before / from … to / until … / overnight"
- times: "18:00", "6 pm", "6 in the evening", "7 in the morning", "10 at night", "18 o'clock", "noon"
- prices: "never / don't sell / export below / under X €/MWh, EUR/kWh, ct/kWh", "sell only above X", "minimum sell price is X"
- filler such as "Hi", "Thanks", "Actually," is ignored

Anything it does not recognise becomes a question, never a guess.

---

## Files

| File | Contents |
|---|---|
| `parser.py` | `parse_goals()`, `GoalParser` (parse, cache, answer), checks on the LLM's answer |
| `schema.py` | output data classes, unit and time conversion, validation and questions |
| `rules.py` | rule-based fallback parser and text helpers |
| `prompt.py` | instructions, examples and JSON format sent to the LLM |
| `llm.py` | minimal Ollama client (standard library only, no extra packages) |

---

## Setup

The parser needs only Python 3.10+ and no extra packages. The LLM part needs
[Ollama](https://ollama.com) with the model downloaded:

```bash
ollama pull qwen3:4b
```

| Environment variable | Default | Meaning |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | where Ollama runs |
| `GOAL_PARSER_MODEL` | `qwen3:4b` | which model to use |

`OllamaClient(model=..., host=..., timeout=60.0, num_ctx=8192)` can also be configured in code.
Without Ollama the parser still works and uses only the rule-based parser (`source: "rules"`).
Input longer than 2000 characters is not parsed; the user is asked to shorten it.

## Tests

```bash
python -m pytest tests/test_goal_parser.py       # fast, no LLM needed (uses a fake LLM)
python -m pytest tests/test_goal_parser_llm.py   # against the real model; skipped if it is not installed
```

With `qwen3:4b` the parser gets 18 of 18 test sentences right (plan examples, unit conversions,
several goals in one sentence, corrections, vague goals) in about 1–4 seconds each.
