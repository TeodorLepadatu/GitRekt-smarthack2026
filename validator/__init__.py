"""Validator + fallback stage of the LLM energy management agent.

    from validator import decide
    decision = decide(prompt, features, llm_call=llm_fn)   # llm_fn from the Small LLM part
    guard_input = decision.to_dict()                        # battery_fraction, curtail, source, ...
"""

from .decide import decide
from .fallback import fallback_decision
from .schema import Decision, ValidationError
from .validator import extract_json, validate

__all__ = [
    "decide",
    "fallback_decision",
    "validate",
    "extract_json",
    "Decision",
    "ValidationError",
]
