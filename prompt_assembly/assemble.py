"""Prompt assembly: rules (1–6) + solved examples (7) + current situation (8)
+ USER GOALS + the closing instruction, as one prompt string for the decision LLM.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Mapping

from .examples import SOLVED_EXAMPLES
from .fmt import frac
from .sections import EXAMPLES_HEADER, RESPOND, RULES
from .situation import labels, render_situation
from .user_goals import render_user_goals

# The decision LLM runs with num_ctx 8192. The prompt is about 3.8k tokens
# (measured with qwen3:4b); keeping it under 6000 leaves room for the answer.
TOKEN_BUDGET = 6000


@dataclass(frozen=True)
class AssembledPrompt:
    """The prompt plus what is worth logging next to the decision."""

    text: str
    labels: dict[str, Any]          # price zone, cheap, negative, surplus left: what the rules saw
    warnings: list[str] = field(default_factory=list)  # optional inputs that were dropped or degraded
    estimated_tokens: int = 0


def assemble_prompt(features: Mapping[str, Any], goal_config: Any = None) -> AssembledPrompt:
    """Build the complete prompt for one 15-minute decision.

    ``features``: the feature builder's values for this interval (see
    ``situation.REQUIRED_FIELDS``); goal progress goes in ``features["goals"]``.
    ``goal_config``: the goal parser's output (dict or object with ``to_dict()``);
    ``None`` means objective max_profit and no goals.

    Raises :class:`PromptAssemblyError` when a required value is missing or
    broken. Problems in optional values and goals are reported in ``warnings``
    instead, and the prompt is still built.
    """
    warnings: list[str] = []
    situation = render_situation(features, title="8. CURRENT SITUATION", warnings=warnings)
    goals = render_user_goals(goal_config, features, warnings=warnings)
    text = "\n\n".join([RULES, render_examples(), situation, goals, RESPOND]) + "\n"
    tokens = estimate_tokens(text)
    if tokens > TOKEN_BUDGET:
        warnings.append(f"prompt is about {tokens} tokens, over the budget of {TOKEN_BUDGET}")
    return AssembledPrompt(text, labels(features), warnings, tokens)


def render_prompt(features: Mapping[str, Any], goal_config: Any = None) -> str:
    """Same as :func:`assemble_prompt`, returning only the prompt text."""
    return assemble_prompt(features, goal_config).text


@lru_cache(maxsize=1)
def render_examples() -> str:
    """Section 7. Fixed text, so it is rendered once."""
    blocks = [EXAMPLES_HEADER]
    for i, ex in enumerate(SOLVED_EXAMPLES, start=1):
        blocks.append("\n".join([
            render_situation(ex["features"], title=f"Example {i}: SITUATION"),
            render_user_goals(ex.get("goal_config"), ex["features"]),
            "Decision: " + render_decision(ex["decision"]),
        ]))
    return "\n\n".join(blocks)


def render_decision(decision: Mapping[str, Any]) -> str:
    """JSON in the decision contract's key order, battery_fraction always with 2 decimals."""
    d = decision
    return (
        "{"
        f'"reasoning": {json.dumps(d["reasoning"], ensure_ascii=False)}, '
        f'"battery_fraction": {frac(d["battery_fraction"])}, '
        f'"curtail": {json.dumps(d["curtail"])}, '
        f'"confidence": {json.dumps(d["confidence"])}, '
        f'"explanation_for_operator": {json.dumps(d["explanation_for_operator"], ensure_ascii=False)}'
        "}"
    )


def estimate_tokens(text: str) -> int:
    """Rough token count, on the high side. This prompt is number-heavy: about
    3.2 characters per token with Qwen3 (12.3k characters = 3.8k tokens)."""
    return int(len(text) / 3.1) + 1
