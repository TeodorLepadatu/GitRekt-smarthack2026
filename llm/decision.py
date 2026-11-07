"""Small LLM decision step.

Input: the complete prompt from prompt assembly (rules + solved examples + current situation + USER GOALS).
Output: {reasoning, battery_fraction, curtail, confidence, explanation_for_operator}.

This module never calculates and never touches hardware. It returns what the model said, plus whether it
parsed against the contract. Range checks and the rule-based fallback belong to the validator box.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from config import Settings
from llm.ollama_client import ChatClient, LLMError, OllamaClient


class LLMDecision(BaseModel):
    """Contract: Small LLM -> validator. Field order matters: reasoning is generated before the numbers."""

    reasoning: str = Field(description="Short comparison of price vs break-even, timing, battery level, uncertainty and goals.")
    battery_fraction: float = Field(
        ge=-1.0,
        le=1.0,
        description="Fraction of max battery power. Positive = charge, negative = discharge, 0 = idle.",
    )
    curtail: bool = Field(description="True only if surplus production should be curtailed.")
    confidence: Literal["low", "medium", "high"]
    explanation_for_operator: str = Field(description="One plain sentence for the operator dashboard.")


DECISION_SCHEMA: dict[str, Any] = LLMDecision.model_json_schema()

DEFAULT_SYSTEM_PROMPT = (
    "You are the battery dispatch agent for an energy site. You choose ONE action for the next 15-minute "
    "interval. All numbers in the prompt are already computed: compare them, do not recalculate them. "
    "Hard user goals come before profit. Respond ONLY with a JSON object with the keys "
    "reasoning, battery_fraction, curtail, confidence, explanation_for_operator."
)


@dataclass
class DecisionResult:
    decision: LLMDecision | None  # set only when the output matched the contract
    raw: str | None  # exact model text, for logging and debugging
    data: dict[str, Any] | None  # parsed JSON even if it failed the contract (validator may inspect it)
    error: str | None
    latency_s: float
    model: str
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.decision is not None


class DecisionAgent:
    def __init__(
        self,
        client: ChatClient | None = None,
        settings: Settings | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    ) -> None:
        settings = settings or Settings()
        self.client = client or OllamaClient(settings.decision_model, settings)
        self.system_prompt = system_prompt

    def decide(self, prompt: str) -> DecisionResult:
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": prompt},
        ]
        try:
            res = self.client.chat_json(messages, DECISION_SCHEMA, tag="decision")
        except LLMError as exc:
            return DecisionResult(None, None, None, str(exc), 0.0, self.client.model)

        return parse_decision(res.content, res.latency_s, res.model)


def parse_decision(raw: str, latency_s: float = 0.0, model: str = "") -> DecisionResult:
    try:
        data = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as exc:
        return DecisionResult(None, raw, None, f"invalid JSON: {exc}", latency_s, model)
    if not isinstance(data, dict):
        return DecisionResult(None, raw, None, "JSON is not an object", latency_s, model)
    try:
        decision = LLMDecision.model_validate(data)
    except ValidationError as exc:
        errs = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
        return DecisionResult(None, raw, data, "output does not match contract", latency_s, model, errs)
    return DecisionResult(decision, raw, data, None, latency_s, model)


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()
