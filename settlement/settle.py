"""Settlement of one 15-minute interval: the cashier (plan sections 2 and 5).

The physics (what the battery did, what was curtailed, what went to the grid)
comes from the site simulator (simulator/), the one place it is computed.
Settlement adds the money, always on the ACTUAL demand and production, so
forecast errors show up in the money instead of hiding:

    energy    = export_mwh x price - import_mwh x price
    wear      = - wear_eur_per_mwh x |battery| x dt
    profit    = energy + wear

Plan section 5 check (15:00, price 95, surplus 0.4 MW, battery +0.87 MW):
    import 0.47 MW x 0.25 h = 0.1175 MWh -> energy -11.16, wear -0.54, profit -11.71,
    SoC 3.5 -> 3.71 MWh.

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
    profit_eur: float            # energy_cost + wear
    no_battery_profit_eur: float  # same interval without battery or curtailment
    value_added_eur: float       # profit - no_battery_profit: what the battery earned
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
) -> IntervalSettlement:
    """The money for one interval the site simulator has already run."""
    energy = (step.grid_export_mwh - step.grid_import_mwh) * price_eur_mwh
    wear = -cfg.wear_eur_per_mwh * step.battery_throughput_mwh
    profit = energy + wear
    no_battery = (step.production_mw - step.demand_mw) * cfg.interval_h * price_eur_mwh

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
) -> IntervalSettlement:
    """Run the setpoint for one interval on the site simulator, then settle it.

    Raises simulator.SimulatorError for impossible inputs (negative demand or
    production, a state of charge outside the battery).
    """
    step = SiteSimulator(cfg, soc_before_mwh).step(
        {"battery_setpoint_mw": battery_setpoint_mw, "curtail_mw": curtail_mw},
        demand_mw, production_mw,
    )
    return settle(step, price_eur_mwh, time=time, cfg=cfg)


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
