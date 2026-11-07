"""Settlement of one 15-minute interval: the cashier (plan sections 2 and 5).

The physics (what the battery did, what was curtailed, what went to the grid)
comes from the site simulator (simulator/), the one place it is computed.
Settlement adds the money, always on the ACTUAL demand and production, so
forecast errors show up in the money instead of hiding:

    energy    = export_mwh x price - import_mwh x price
    wear      = - wear_eur_per_mwh x |battery| x dt
    imbalance = deviation_mwh x (penalty price - day-ahead price)     <= 0
    profit    = energy + wear + imbalance

Plan section 5 check (15:00, price 95, surplus 0.4 MW, battery +0.87 MW):
    import 0.47 MW x 0.25 h = 0.1175 MWh -> energy -11.16, wear -0.54, profit -11.71,
    SoC 3.5 -> 3.71 MWh.

Imbalance
---------
A setpoint is a commitment: it is chosen before the interval, on the forecast,
and the grid balances whatever really happens. Markets charge for that gap -
energy you promised and did not deliver is bought back above the day-ahead
price, energy you delivered unasked is sold below it - so being wrong costs
more than being right:

    scheduled = (forecast production - curtailed - forecast demand - setpoint asked) x dt
    deviation = actual grid exchange - scheduled
    imbalance = deviation x (penalty price - day-ahead price)

The curtailment and the setpoint cancel between the two lines, so a deviation
is exactly the forecast error plus any part of the setpoint the battery could
not deliver. Deviating never pays better than the plan (see
:meth:`ImbalancePrices.penalty_for`), so ``imbalance_eur`` is always <= 0.

``no_battery_profit_eur`` bears its own imbalance: without a battery the same
forecast error still happens and nothing can absorb it. ``value_added_eur``
therefore counts what the battery earned by covering the gap, which is how a
real battery earns most of its money.

Grid import (peak shaving)
--------------------------
``grid_import_mw`` is the interval's average import power, the simulator's
``grid_import_mwh / dt`` (= max(0, demand + battery + curtailed - production)).
``no_battery_import_mw`` is the same without battery or curtailment,
max(0, demand - production), the counterfactual ``no_battery_profit_eur`` uses.

Without ``imbalance`` prices nothing changes: the deviation is not priced,
``imbalance_eur`` is 0 and ``profit`` is ``energy + wear`` as before.

Two ways in:
    settle(step, price)          the site simulator already ran the interval (the normal loop)
    settle_interval(...)         run the simulator for one interval here, then settle

Sign convention: battery > 0 = CHARGE, < 0 = DISCHARGE (same as the guard).
Costs are negative numbers, income positive.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from battery import BatteryConfig
from simulator import SiteSimulator, StepResult

EPS = 1e-9


@dataclass(frozen=True)
class ImbalancePrices:
    """What one interval's deviation from the plan settles at, in EUR/MWh.

    ``short``: the site delivered less than it promised, so the missing energy
    is bought back - at or above the day-ahead price. ``long``: it delivered
    more than promised, sold at or below it.
    """

    short_eur_mwh: float
    long_eur_mwh: float

    def penalty_for(self, deviation_mwh: float, day_ahead_eur_mwh: float) -> float:
        """The price this deviation settles at.

        Clamped to the day-ahead price, so deviating can never pay better than
        delivering the plan. Synthetic or stale imbalance prices sometimes cross
        the day-ahead price; without this they would turn a penalty into a bonus.
        """
        if deviation_mwh > 0:
            return min(self.long_eur_mwh, day_ahead_eur_mwh)
        return max(self.short_eur_mwh, day_ahead_eur_mwh)


def imbalance_cost(
    deviation_mwh: float,
    day_ahead_eur_mwh: float,
    prices: Optional[ImbalancePrices],
) -> float:
    """What deviating cost beyond settling the same energy at the day-ahead price (<= 0)."""
    if prices is None or abs(deviation_mwh) < EPS:
        return 0.0
    return deviation_mwh * (prices.penalty_for(deviation_mwh, day_ahead_eur_mwh) - day_ahead_eur_mwh)


def scheduled_position_mwh(
    *,
    forecast_demand_mw: float,
    forecast_production_mw: float,
    battery_mw: float,
    curtail_mw: float = 0.0,
    cfg: BatteryConfig = BatteryConfig(),
) -> float:
    """The grid exchange the site promised for one interval (> 0 export, < 0 import)."""
    net_mw = forecast_production_mw - curtail_mw - forecast_demand_mw - battery_mw
    return net_mw * cfg.interval_h


@dataclass
class IntervalSettlement:
    time: str
    price_eur_mwh: float
    soc_before_mwh: float
    soc_after_mwh: float
    battery_mw: float            # what the battery actually ran
    curtail_mw: float            # what was actually switched off
    demand_mw: float             # actual
    production_mw: float         # actual (before curtailment)
    grid_import_mwh: float
    grid_export_mwh: float
    energy_cost_eur: float       # export x price - import x price
    wear_eur: float              # <= 0
    profit_eur: float            # energy_cost + wear + imbalance
    no_battery_profit_eur: float  # same interval without battery or curtailment, own imbalance
    value_added_eur: float       # profit - no_battery_profit: what the battery earned
    scheduled_grid_mwh: float = 0.0   # the exchange promised on the forecast
    deviation_mwh: float = 0.0        # actual exchange - scheduled
    imbalance_eur: float = 0.0        # <= 0; 0 when the deviation is not priced
    grid_import_mw: float = 0.0       # average import power over the interval
    no_battery_import_mw: float = 0.0  # the same without battery or curtailment
    site_clipped: bool = False   # the setpoint did not fit the actual SoC and was cut
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def settle(
    step: StepResult,
    price_eur_mwh: float,
    *,
    time: Optional[str] = None,
    cfg: BatteryConfig = BatteryConfig(),
    imbalance: Optional[ImbalancePrices] = None,
    scheduled_mwh: Optional[float] = None,
    forecast_demand_mw: Optional[float] = None,
    forecast_production_mw: Optional[float] = None,
) -> IntervalSettlement:
    """The money for one interval the site simulator has already run.

    Give ``imbalance`` and ``scheduled_mwh`` (from :func:`scheduled_position_mwh`,
    on the setpoint that was *promised*, before any balancing) to price the
    deviation from the plan. The two forecasts add the no-battery counterfactual's
    own imbalance. With ``imbalance`` or ``scheduled_mwh`` missing the deviation
    is not priced and the result is the same as before.
    """
    energy = (step.grid_export_mwh - step.grid_import_mwh) * price_eur_mwh
    wear = -cfg.wear_eur_per_mwh * step.battery_throughput_mwh
    no_battery = (step.production_mw - step.demand_mw) * cfg.interval_h * price_eur_mwh

    scheduled = deviation = imb = 0.0
    if imbalance is not None and scheduled_mwh is not None:
        scheduled = scheduled_mwh
        deviation = step.grid_net_mwh - scheduled
        imb = imbalance_cost(deviation, price_eur_mwh, imbalance)
        if forecast_demand_mw is not None and forecast_production_mw is not None:
            # Without a battery the same forecast error happens and nothing absorbs it.
            nb_scheduled = scheduled_position_mwh(
                forecast_demand_mw=forecast_demand_mw, forecast_production_mw=forecast_production_mw,
                battery_mw=0.0, curtail_mw=0.0, cfg=cfg,
            )
            nb_actual = (step.production_mw - step.demand_mw) * cfg.interval_h
            no_battery += imbalance_cost(nb_actual - nb_scheduled, price_eur_mwh, imbalance)

    profit = energy + wear + imb

    return IntervalSettlement(
        time=time or step.time or "??:??",
        price_eur_mwh=price_eur_mwh,
        soc_before_mwh=round(step.stored_before_mwh, 4),
        soc_after_mwh=round(step.stored_after_mwh, 4),
        battery_mw=round(step.setpoint_applied_mw, 4),
        curtail_mw=round(step.curtailed_mw, 4),
        demand_mw=step.demand_mw,
        production_mw=step.production_mw,
        grid_import_mwh=round(step.grid_import_mwh, 4),
        grid_export_mwh=round(step.grid_export_mwh, 4),
        energy_cost_eur=round(energy, 2),
        wear_eur=round(wear, 2),
        profit_eur=round(profit, 2),
        no_battery_profit_eur=round(no_battery, 2),
        value_added_eur=round(profit - no_battery, 2),
        scheduled_grid_mwh=round(scheduled, 4),
        deviation_mwh=round(deviation, 4),
        imbalance_eur=round(imb, 2),
        grid_import_mw=round(step.grid_import_mwh / cfg.interval_h, 4),
        no_battery_import_mw=round(max(0.0, step.demand_mw - step.production_mw), 4),
        site_clipped=step.clipped,
        warnings=list(step.warnings),
    )


def settle_interval(
    *,
    time: str,
    price_eur_mwh: float,
    soc_before_mwh: float,
    battery_setpoint_mw: float,
    curtail_mw: float,
    demand_mw: float,
    production_mw: float,
    cfg: BatteryConfig = BatteryConfig(),
    imbalance: Optional[ImbalancePrices] = None,
    scheduled_mwh: Optional[float] = None,
    forecast_demand_mw: Optional[float] = None,
    forecast_production_mw: Optional[float] = None,
) -> IntervalSettlement:
    """Run the setpoint for one interval on the site simulator, then settle it.

    Raises simulator.SimulatorError for impossible inputs (negative demand or
    production, a state of charge outside the battery).

    When ``imbalance`` is given without ``scheduled_mwh``, the promise is taken
    to be this setpoint on the forecast, which is what a one-shot call means.
    """
    step = SiteSimulator(cfg, soc_before_mwh).step(
        {"battery_setpoint_mw": battery_setpoint_mw, "curtail_mw": curtail_mw},
        demand_mw, production_mw,
    )
    if (imbalance is not None and scheduled_mwh is None
            and forecast_demand_mw is not None and forecast_production_mw is not None):
        scheduled_mwh = scheduled_position_mwh(
            forecast_demand_mw=forecast_demand_mw, forecast_production_mw=forecast_production_mw,
            battery_mw=battery_setpoint_mw, curtail_mw=curtail_mw, cfg=cfg,
        )
    return settle(step, price_eur_mwh, time=time, cfg=cfg, imbalance=imbalance,
                  scheduled_mwh=scheduled_mwh, forecast_demand_mw=forecast_demand_mw,
                  forecast_production_mw=forecast_production_mw)


def forecast_errors(forecast: dict, demand_mw: float, production_mw: float) -> dict[str, Optional[float]]:
    """Actual minus forecast, and whether actual production fell inside ML 2's range.

    Logged for ML retraining (plan section 2, "Settlement + logging").
    """
    def num(key: str) -> Optional[float]:
        v = forecast.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    f_dem, f_prod = num("demand_mw"), num("production_mw")
    low, high = num("prod_low_mw"), num("prod_high_mw")
    return {
        "demand_error_mw": None if f_dem is None else round(demand_mw - f_dem, 4),
        "production_error_mw": None if f_prod is None else round(production_mw - f_prod, 4),
        "production_in_range": None if low is None or high is None else bool(low - EPS <= production_mw <= high + EPS),
    }
