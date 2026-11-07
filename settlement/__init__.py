"""Settlement + logging: the cashier at the end of each cycle (plan section 2).

    from settlement import DayLedger
    ledger = DayLedger(goal_config, start_soc_mwh=4.0)
    step = sim.step(cycle.setpoint, demand, production)   # site simulator: the physics
    rec = ledger.record(cycle, features, step)            # settlement: the money
    rec["profit_eur"], rec["goal_g1"], rec["dashboard_line"]; ledger.metrics()
"""

from .goals import GoalState, GoalTracker
from .ledger import DayLedger, goal_costs
from .settle import (
    ImbalancePrices,
    IntervalSettlement,
    forecast_errors,
    imbalance_cost,
    scheduled_position_mwh,
    settle,
    settle_interval,
)

__all__ = [
    "DayLedger",
    "GoalState",
    "GoalTracker",
    "ImbalancePrices",
    "IntervalSettlement",
    "forecast_errors",
    "imbalance_cost",
    "scheduled_position_mwh",
    "goal_costs",
    "settle",
    "settle_interval",
]
