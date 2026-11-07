"""Prompt and output schema for the goal-parser LLM.

The LLM only *extracts*: numbers and units are copied as written, and the
code does every conversion (kWh -> MWh, ct/kWh -> EUR/MWh) and decides which
goals are hard.
"""
from __future__ import annotations

import json

_EXAMPLES = [
    (
        "I want to maximise profit. Store 6 MWh by 18:00",
        {
            "objective": "max_profit",
            "goals": [
                {"type": "reserve", "amount": 6, "unit": "MWh", "by": "18:00", "until": None,
                 "source_text": "Store 6 MWh by 18:00"},
            ],
            "clarifications": [],
        },
    ),
    (
        "Use my own energy first and keep 2 MWh backup overnight. Never sell below 5 ct/kWh.",
        {
            "objective": "self_consumption",
            "goals": [
                {"type": "reserve", "amount": 2, "unit": "MWh", "by": "22:00", "until": "06:00",
                 "source_text": "keep 2 MWh backup overnight"},
                {"type": "min_sell_price", "amount": 5, "unit": "ct/kWh",
                 "source_text": "Never sell below 5 ct/kWh"},
            ],
            "clarifications": [],
        },
    ),
    (
        "Have 1500 kWh ready from 7 pm until 23:00, and don't sell for less than €50/MWh",
        {
            "objective": None,
            "goals": [
                {"type": "reserve", "amount": 1500, "unit": "kWh", "by": "19:00", "until": "23:00",
                 "source_text": "Have 1500 kWh ready from 7 pm until 23:00"},
                {"type": "min_sell_price", "amount": 50, "unit": "EUR/MWh",
                 "source_text": "don't sell for less than €50/MWh"},
            ],
            "clarifications": [],
        },
    ),
    (
        "Keep some energy for the evening and make sure the neighbours are happy",
        {
            "objective": None,
            "goals": [],
            "clarifications": [
                {"source_text": "Keep some energy for the evening",
                 "question": "How much energy (in MWh) should I keep, and by what time (HH:MM)?"},
                {"source_text": "make sure the neighbours are happy",
                 "question": "I can't turn this into a battery goal. What should the battery do differently?"},
            ],
        },
    ),
]

SYSTEM_PROMPT = """You turn a battery site operator's plain-language goals into JSON.
You only extract what the user said. You never calculate and never convert units.

Return one JSON object with exactly these keys:
- "objective": "max_profit" if the user wants profit / money, "self_consumption" if they want to use
  their own energy first or avoid the grid, null if they did not say.
- "goals": a list. Each goal is one of
  {"type": "reserve", "amount": <number>, "unit": "MWh" | "kWh", "by": "HH:MM" | null, "until": "HH:MM" | null, "source_text": "<user's words>"}
  {"type": "min_sell_price", "amount": <number>, "unit": "EUR/MWh" | "EUR/kWh" | "ct/kWh", "source_text": "<user's words>"}
- "clarifications": a list of {"source_text": "<user's words>", "question": "<short question back to the user>"}.

Rules:
1. Copy every number exactly as the user wrote it, with the unit they used. Do not convert kWh to MWh or cents to euros.
2. Times are 24-hour "HH:MM" (6 pm -> "18:00").
3. A time after "by" / "before" goes in "by". A time after "until" / "till" goes in "until".
   "from A to B" means by A, until B. "overnight" means by "22:00", until "06:00".
4. If an amount, unit or time is missing or vague ("some", "a bit", "this evening", "later"), do NOT guess:
   leave that goal out and add a clarification question instead.
5. If an hour could be morning or evening ("by 6", "until 7"), copy it exactly as written ("6"); do not pick one.
6. If the user changes their mind ("actually", "instead"), use what they said last.
7. Anything that is not an objective, a reserve or a minimum selling price goes to "clarifications".
8. Never invent goals the user did not state.

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
                    "type": {"type": "string", "enum": ["reserve", "min_sell_price"]},
                    "amount": {"type": "number"},
                    "unit": {"type": "string"},
                    "by": _NULLABLE_STR,
                    "until": _NULLABLE_STR,
                    "source_text": {"type": "string"},
                },
                "required": ["type", "amount", "unit", "source_text"],
            },
        },
        "clarifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"source_text": {"type": "string"}, "question": {"type": "string"}},
                "required": ["question"],
            },
        },
    },
    "required": ["objective", "goals", "clarifications"],
}


def render_user_message(text: str) -> str:
    return f"User: {text.strip()}\nJSON:"
