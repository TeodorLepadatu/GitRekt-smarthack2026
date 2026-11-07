"""Small helpers shared by the pipeline stages."""

from __future__ import annotations

import math
from typing import Any, Mapping


def finite_float(value: Any) -> float | None:
    """``value`` as a float if it is a finite int or float, else None.

    Booleans are rejected (``True`` is an ``int`` in Python), and so are strings:
    every stage receives numbers from code, never from text.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def goal_config_dict(goal_config: Any) -> Mapping[str, Any]:
    """The goal config as a mapping.

    Accepts the goal parser's ``GoalConfig`` (anything with ``to_dict()``), a
    plain dict, or None (no goals). Raises TypeError for anything else.
    """
    if goal_config is None:
        return {}
    if hasattr(goal_config, "to_dict"):
        goal_config = goal_config.to_dict()
    if not isinstance(goal_config, Mapping):
        raise TypeError(f"goal_config must be a mapping or GoalConfig, got {type(goal_config).__name__}")
    return goal_config
