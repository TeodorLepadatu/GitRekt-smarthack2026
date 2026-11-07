"""Settlement + logging: the cashier at the end of each cycle (plan section 2).

    from settlement import DayLedger
    ledger = DayLedger(goal_config, start_soc_mwh=4.0)
    rec = ledger.record(run_cycle(features, goal_config), features, actual)
    rec["profit_eur"], rec["goal_g1"], rec["dashboard_line"]; ledger.metrics()
"""

from .goals import GoalState, GoalTracker
from .ledger import DayLedger, goal_costs
from .settle import IntervalSettlement, forecast_errors, settle_interval

__all__ = [
    "DayLedger",
    "GoalState",
    "GoalTracker",
    "IntervalSettlement",
    "forecast_errors",
    "goal_costs",
    "settle_interval",
]
