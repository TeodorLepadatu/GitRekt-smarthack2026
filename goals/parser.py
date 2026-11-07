"""Goal parser: plain-language goal sentences -> GoalConfig, or questions back to the user if vague.

The LLM only extracts fields. Ids, defaults, hard/soft flags and sanity checks are done here in code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ValidationError

from config import Settings
from goals.schema import Goal, GoalConfig, MinSellPriceGoal, Objective, ReserveGoal
from llm.ollama_client import ChatClient, LLMError, OllamaClient


class _RawGoal(BaseModel):
    type: Literal["reserve", "min_sell_price"]
    source_text: str
    mwh: float | None = None
    by: str | None = None
    until: str | None = None
    eur_per_mwh: float | None = None


class _RawParse(BaseModel):
    objective: Objective | None = None
    goals: list[_RawGoal] = []
    questions: list[str] = []


SYSTEM_PROMPT = """You turn a battery site owner's goals into JSON. Extract only what the user said; never invent numbers or times.

Supported:
- objective "max_profit": maximise trading profit.
- objective "self_consumption": use own energy first, avoid grid trading.
- goal "reserve": have at least `mwh` stored by time `by` (HH:MM, 24h) and keep it until `until` (HH:MM). If the user gives only a deadline, set until = by. "Overnight" means by "22:00", until "06:00".
- goal "min_sell_price": never sell below `eur_per_mwh` (EUR/MWh).

If a sentence is vague (missing amount, time or price) or asks for something unsupported, do not guess: add a short question to `questions` and leave that goal out.
Set `objective` to null if the user did not state one.

Examples:
User: "I want to maximise profit"
{"objective": "max_profit", "goals": [], "questions": []}

User: "Store 6 MWh by 18:00"
{"objective": null, "goals": [{"type": "reserve", "source_text": "Store 6 MWh by 18:00", "mwh": 6, "by": "18:00", "until": "18:00"}], "questions": []}

User: "Keep 2 MWh backup overnight"
{"objective": null, "goals": [{"type": "reserve", "source_text": "Keep 2 MWh backup overnight", "mwh": 2, "by": "22:00", "until": "06:00"}], "questions": []}

User: "Never sell below €50/MWh"
{"objective": null, "goals": [{"type": "min_sell_price", "source_text": "Never sell below €50/MWh", "eur_per_mwh": 50}], "questions": []}

User: "Keep some energy for the evening"
{"objective": null, "goals": [], "questions": ["How many MWh should be stored, and by what time?"]}"""


@dataclass
class ParseResult:
    config: GoalConfig | None
    questions: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.config is not None and not self.questions and self.error is None


class GoalParser:
    def __init__(self, client: ChatClient | None = None, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.client = client or OllamaClient(self.settings.goal_model, self.settings)

    def parse(self, sentences: list[str]) -> ParseResult:
        if not sentences:
            return ParseResult(GoalConfig())
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(f'"{s}"' for s in sentences)},
        ]
        try:
            res = self.client.chat_json(messages, _RawParse.model_json_schema(), tag="goal_parser")
            raw = _RawParse.model_validate(json.loads(res.content))
        except LLMError as exc:
            return ParseResult(None, error=str(exc))
        except (json.JSONDecodeError, ValidationError) as exc:
            return ParseResult(None, error=f"goal parser returned invalid output: {exc}")
        return build_config(raw, self.settings.battery_capacity_mwh)


def build_config(raw: _RawParse, capacity_mwh: float) -> ParseResult:
    """Deterministic step: assign ids, fill defaults, check limits, collect questions."""
    questions = list(raw.questions)
    goals: list[Goal] = []

    for g in raw.goals:
        gid = f"g{len(goals) + 1}"
        try:
            if g.type == "reserve":
                if g.mwh is None or g.by is None:
                    questions.append(f'"{g.source_text}": how many MWh, and by what time?')
                    continue
                if g.mwh > capacity_mwh:
                    questions.append(
                        f'"{g.source_text}": {g.mwh:g} MWh exceeds the battery capacity of {capacity_mwh:g} MWh.'
                    )
                    continue
                goals.append(ReserveGoal(id=gid, mwh=g.mwh, by=g.by, until=g.until or g.by, source_text=g.source_text))
            else:
                if g.eur_per_mwh is None:
                    questions.append(f'"{g.source_text}": what minimum price in EUR/MWh?')
                    continue
                goals.append(MinSellPriceGoal(id=gid, eur_per_mwh=g.eur_per_mwh, source_text=g.source_text))
        except ValidationError:
            questions.append(f'"{g.source_text}": please give times as HH:MM (24h).')

    config = GoalConfig(objective=raw.objective or "max_profit", goals=goals)
    return ParseResult(config, questions)
