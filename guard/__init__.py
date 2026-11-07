"""Guard layer of the LLM energy management agent.

    from guard import guard, BatteryConfig
    result = guard(decision, state, goal_config)   # decision = validator output
    result = guard(decision, state, goal_config, max_import_mw=2.5)  # + peak shaving
    site_input = result.to_dict()                  # battery_setpoint_mw, curtail_mw, applied_rules, ...
"""

from battery import BatteryConfig
from .guard import GuardResult, guard, import_mw

__all__ = ["guard", "GuardResult", "BatteryConfig", "import_mw"]
