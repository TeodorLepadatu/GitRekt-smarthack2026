"""Validator + fallback stage of the LLM energy management agent.

    from validator import decide
    decision = decide(prompt, features)          # uses local Ollama
    guard_input = decision.to_dict()             # battery_fraction, curtail, source, ...
"""

from .decide import decide, ollama_generate
from .fallback import fallback_decision
from .schema import Decision, ValidationError
from .validator import extract_json, validate

__all__ = [
    "decide",
    "ollama_generate",
    "fallback_decision",
    "validate",
    "extract_json",
    "Decision",
    "ValidationError",
]
