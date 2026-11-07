"""Settlement of one 15-minute interval: the cashier (plan sections 2 and 5).

The site runs the guard's setpoint for one interval; the grid trade is whatever
is left over (actual net - battery - curtailment). Settlement always uses the
ACTUAL demand and production, so forecast errors show up in the money instead
of hiding.

    grid_mw   = (production - curtailed) - demand - battery     (> 0 export, < 0 import)
    energy    = export_mwh x price - import_mwh x price
    wear      = - wear_eur_per_mwh x |battery| x dt
    profit    = energy + wear

Plan section 5 check (15:00, price 95, surplus 0.4 MW, battery +0.87 MW):
    import 0.47 MW x 0.25 h = 0.1175 MWh -> energy -11.16, wear -0.54, profit -11.71,
    SoC 3.5 -> 3.71 MWh.

Sign convention: battery > 0 = CHARGE, < 0 = DISCHARGE (same as the guard).
Costs are negative numbers, income positive.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from battery import BatteryConfig

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
    """Run the setpoint for one interval and settle it with the grid."""
    dt = cfg.interval_h
    warnings: list[str] = []

    # The battery can only do what the actual SoC allows (the guard used the
    # sensor SoC too, so this only bites if the two disagree).
    hi = cfg.max_charge_mw(soc_before_mwh)
    lo = -cfg.max_discharge_mw(soc_before_mwh)
    battery = min(hi, max(lo, battery_setpoint_mw))
    site_clipped = abs(battery - battery_setpoint_mw) > 1e-6
    if site_clipped:
        warnings.append(f"setpoint {battery_setpoint_mw:.4f} MW cut to {battery:.4f} MW by the actual SoC")

    production = max(0.0, production_mw)
    curtail = min(max(0.0, curtail_mw), production)
    if curtail_mw > production + EPS:
        warnings.append(f"curtail {curtail_mw:.4f} MW cut to the actual production {production:.4f} MW")

    grid_mw = (production - curtail) - demand_mw - battery
    export_mwh = max(0.0, grid_mw) * dt
    import_mwh = max(0.0, -grid_mw) * dt

    energy = (export_mwh - import_mwh) * price_eur_mwh
    wear = -cfg.wear_eur_per_mwh * abs(battery) * dt
    profit = energy + wear
    no_battery = (production - demand_mw) * dt * price_eur_mwh

    return IntervalSettlement(
        time=time,
        price_eur_mwh=price_eur_mwh,
        soc_before_mwh=round(soc_before_mwh, 4),
        soc_after_mwh=round(cfg.energy_after(soc_before_mwh, battery), 4),
        battery_mw=round(battery, 4),
        curtail_mw=round(curtail, 4),
        demand_mw=demand_mw,
        production_mw=production,
        grid_import_mwh=round(import_mwh, 4),
        grid_export_mwh=round(export_mwh, 4),
        energy_cost_eur=round(energy, 2),
        wear_eur=round(wear, 2),
        profit_eur=round(profit, 2),
        no_battery_profit_eur=round(no_battery, 2),
        value_added_eur=round(profit - no_battery, 2),
        site_clipped=site_clipped,
        warnings=warnings,
    )


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
