"""Goal parser: turns plain-language user goals into a structured goal config."""
from .parser import GoalParser, parse_goals
from .schema import (
    DEFAULT_OBJECTIVE,
    OBJECTIVES,
    Clarification,
    Goal,
    GoalConfig,
    ParseResult,
    normalize_time,
)

__all__ = [
    "DEFAULT_OBJECTIVE",
    "OBJECTIVES",
    "Clarification",
    "Goal",
    "GoalConfig",
    "GoalParser",
    "ParseResult",
    "normalize_time",
    "parse_goals",
]
