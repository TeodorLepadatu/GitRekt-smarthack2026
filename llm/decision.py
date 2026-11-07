"""Small LLM decision step.

Input: the complete prompt from prompt assembly (rules + solved examples + current situation + USER GOALS).
Output: the model's raw text, constrained by Ollama to the decision contract
{reasoning, battery_fraction, curtail, confidence, explanation_for_operator}.

This module never calculates, never validates and never touches hardware. Parsing, range checks and
the rule-based fallback all belong to the validator (validator.decide), which calls this agent.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from config import Settings
from llm.ollama_client import ChatClient, OllamaClient


class LLMDecision(BaseModel):
    """Contract: Small LLM -> validator. Used only to build the JSON schema that constrains decoding.

    Field order matters: reasoning is generated before the numbers.
    """

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


class DecisionAgent:
    """Sends the assembled prompt to the decision model and returns its raw text.

    The assembled prompt already holds the role, rules and output format, so by default no
    separate system message is sent. An agent is callable, so it can be passed straight to
    ``validator.decide(prompt, features, llm_call=agent)``.
    """

    def __init__(
        self,
        client: ChatClient | None = None,
        settings: Settings | None = None,
        system_prompt: str | None = None,
    ) -> None:
        settings = settings or Settings()
        self.client = client or OllamaClient(settings.decision_model, settings)
        self.system_prompt = system_prompt

    def complete(self, prompt: str) -> str:
        """Raw model output for one prompt. Raises LLMError if Ollama fails."""
        messages = [{"role": "system", "content": self.system_prompt}] if self.system_prompt else []
        messages.append({"role": "user", "content": prompt})
        return self.client.chat_json(messages, DECISION_SCHEMA, tag="decision").content

    __call__ = complete
