"""Prompt assembly: rules (1–6) + solved examples (7) + current situation (8)
+ USER GOALS + the closing instruction, as one prompt string for the decision LLM.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from .examples import SOLVED_EXAMPLES
from .fmt import frac
from .sections import EXAMPLES_HEADER, RESPOND, RULES
from .situation import render_situation
from .user_goals import render_user_goals

# The decision LLM runs with num_ctx 8192 and the plan budgets ~2.5k tokens for
# the prompt; this leaves room for the answer.
TOKEN_BUDGET = 4000


def render_examples() -> str:
    blocks = [EXAMPLES_HEADER]
    for i, ex in enumerate(SOLVED_EXAMPLES, start=1):
        parts = [render_situation(ex["features"], title=f"Example {i}: SITUATION")]
        if ex.get("goal_config"):
            parts.append(render_user_goals(ex["goal_config"], ex["features"]))
        parts.append("Decision: " + render_decision(ex["decision"]))
        blocks.append("\n".join(parts))
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


_EXAMPLES_TEXT: str | None = None


def render_prompt(features: Mapping[str, Any], goal_config: Any = None) -> str:
    """Build the complete prompt for one 15-minute decision.

    ``features``: the feature builder's values for this interval (see
    ``situation.REQUIRED_FIELDS``); goal progress goes in ``features["goals"]``.
    ``goal_config``: the goal parser's output (dict or object with ``to_dict()``);
    ``None`` means objective max_profit and no goals.

    Raises :class:`PromptAssemblyError` when a required value is missing or not a number.
    """
    global _EXAMPLES_TEXT
    if _EXAMPLES_TEXT is None:  # fixed text: render once
        _EXAMPLES_TEXT = render_examples()
    situation = render_situation(features, title="8. CURRENT SITUATION")
    goals = render_user_goals(goal_config, features)
    return "\n\n".join([RULES, _EXAMPLES_TEXT, situation, goals, RESPOND]) + "\n"


def estimate_tokens(text: str) -> int:
    """Rough token count (about 4 characters per token for English text and numbers)."""
    return (len(text) + 3) // 4
