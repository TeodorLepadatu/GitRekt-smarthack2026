"""Prompt and output schema for the goal-parser LLM.

The LLM only *extracts*: numbers and units are copied as written, and the
code does every conversion (kWh -> MWh, ct/kWh -> EUR/MWh) and decides which
goals are hard.

Every goal has the same fields, all required (``by``/``until`` are null for a
price goal). With Ollama's structured output an optional field is often just
left out, so nothing here is optional. ``source_text`` comes first so the
model quotes the user's words before it extracts values from them.
"""
from __future__ import annotations

import json


def _reserve(source, amount, unit, by, until):
    return {"source_text": source, "type": "reserve", "amount": amount, "unit": unit, "by": by, "until": until}


def _min_price(source, amount, unit):
    return {"source_text": source, "type": "min_sell_price", "amount": amount, "unit": unit, "by": None, "until": None}


_EXAMPLES = [
    (
        "I'd like to be as independent from the grid as possible",
        {"objective": "self_consumption", "goals": [], "clarifications": []},
    ),
    (
        "I want to maximise profit. Store 6 MWh by 18:00",
        {"objective": "max_profit",
         "goals": [_reserve("Store 6 MWh by 18:00", 6, "MWh", "18:00", None)],
         "clarifications": []},
    ),
    (
        "Keep 3 MWh backup overnight",
        {"objective": None,
         "goals": [_reserve("Keep 3 MWh backup overnight", 3, "MWh", "22:00", "06:00")],
         "clarifications": []},
    ),
    (
        "Use my own energy first and never sell below 5 ct/kWh.",
        {"objective": "self_consumption",
         "goals": [_min_price("never sell below 5 ct/kWh", 5, "ct/kWh")],
         "clarifications": []},
    ),
    (
        "Have 1500 kWh ready from 7 pm until 23:00, and don't sell for less than €50/MWh",
        {"objective": None,
         "goals": [_reserve("Have 1500 kWh ready from 7 pm until 23:00", 1500, "kWh", "19:00", "23:00"),
                   _min_price("don't sell for less than €50/MWh", 50, "EUR/MWh")],
         "clarifications": []},
    ),
    (
        "Keep some energy for the evening and make sure the neighbours are happy",
        {"objective": None,
         "goals": [],
         "clarifications": [
             {"source_text": "Keep some energy for the evening",
              "question": "How much energy (in MWh) should I keep, and by what time (HH:MM)?"},
             {"source_text": "make sure the neighbours are happy",
              "question": "I can't turn this into a battery goal. What should the battery do differently?"},
         ]},
    ),
]

SYSTEM_PROMPT = """You turn a battery site operator's plain-language goals into JSON.
You only extract what the user said. You never calculate and never convert units.

Return one JSON object with exactly these keys:
- "objective": "max_profit" only if the user asks for profit / money / earnings,
  "self_consumption" only if the user asks to use their own energy first or avoid the grid,
  otherwise null. A reserve or a price limit on its own is NOT an objective.
- "goals": a list. Every goal has all six fields:
  {"source_text": "<the user's words for this goal>", "type": "reserve" | "min_sell_price",
   "amount": <number>, "unit": "MWh" | "kWh" | "EUR/MWh" | "EUR/kWh" | "ct/kWh",
   "by": "HH:MM" | null, "until": "HH:MM" | null}
  type "reserve": keep or store energy in the battery (unit MWh or kWh).
  type "min_sell_price": never sell / export below a price (unit EUR/MWh, EUR/kWh or ct/kWh; by and until are null).
- "clarifications": a list of {"source_text": "<user's words>", "question": "<short question back to the user>"}.

Rules:
1. Copy every number exactly as the user wrote it, with the unit they used. Do not convert kWh to MWh or cents to euros.
2. Times are 24-hour "HH:MM" (6 pm -> "18:00").
3. A time after "by" / "before" goes in "by". A time after "until" / "till" goes in "until".
   "from A to B" or "from A until B" means by A, until B. "overnight" means by "22:00", until "06:00".
4. One sentence can hold several goals. Make one goal for each: every amount with a unit belongs to a goal or a clarification.
5. If an amount, unit or time is missing or vague ("some", "a bit", "this evening", "later"), do NOT guess:
   leave that goal out and add a clarification question instead. A goal that is complete gets no clarification.
6. If an hour could be morning or evening ("by 6", "until 7"), copy it exactly as written ("6"); do not pick one.
7. If the user changes their mind ("actually", "instead"), use what they said last.
8. Anything that is not an objective, a reserve or a minimum selling price goes to "clarifications".
9. Never invent goals the user did not state.

Examples:
""" + "\n".join(
    f"User: {text}\nJSON: {json.dumps(out, ensure_ascii=False)}" for text, out in _EXAMPLES
)

_NULLABLE_STR = {"type": ["string", "null"]}

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "objective": {"type": ["string", "null"], "enum": ["max_profit", "self_consumption", None]},
        "goals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_text": {"type": "string"},
                    "type": {"type": "string", "enum": ["reserve", "min_sell_price"]},
                    "amount": {"type": "number"},
                    "unit": {"type": "string", "enum": ["MWh", "kWh", "EUR/MWh", "EUR/kWh", "ct/kWh"]},
                    "by": _NULLABLE_STR,
                    "until": _NULLABLE_STR,
                },
                "required": ["source_text", "type", "amount", "unit", "by", "until"],
            },
        },
        "clarifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"source_text": {"type": "string"}, "question": {"type": "string"}},
                "required": ["source_text", "question"],
            },
        },
    },
    "required": ["objective", "goals", "clarifications"],
}


def render_user_message(text: str) -> str:
    return f"User: {text.strip()}\nJSON:"
