"""Guard layer of the LLM energy management agent.

    from guard import guard, BatteryConfig
    result = guard(decision, state, goal_config)   # decision = validator output
    site_input = result.to_dict()                  # battery_setpoint_mw, curtail_mw, applied_rules, ...
"""

from .config import BatteryConfig
from .guard import GuardResult, guard

__all__ = ["guard", "GuardResult", "BatteryConfig"]
