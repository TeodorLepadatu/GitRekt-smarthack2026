"""Battery parameters and physical power limits: the one battery definition
shared by the guard, the prompt's solved examples and the settings.

Defaults are the plan's example battery: 10 MWh, 3 MW max, minimum stored
energy 1 MWh, round-trip efficiency 0.90 (so eta_charge = eta_discharge =
sqrt(0.90) ~= 0.949), 15-minute intervals.

Sign convention: setpoint > 0 = CHARGE, < 0 = DISCHARGE.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class BatteryConfig:
    capacity_mwh: float = 10.0
    p_max_mw: float = 3.0
    round_trip_eff: float = 0.90
    e_min_mwh: float = 1.0          # lowest allowed stored energy
    e_max_mwh: float | None = None  # highest allowed; defaults to capacity
    interval_h: float = 0.25        # 15 minutes
    goal_buffer_min: float = 30.0   # safety buffer for reserve deadlines (plan section 4)

    @property
    def eta_c(self) -> float:
        return math.sqrt(self.round_trip_eff)

    @property
    def eta_d(self) -> float:
        return math.sqrt(self.round_trip_eff)

    @property
    def e_max(self) -> float:
        return self.capacity_mwh if self.e_max_mwh is None else self.e_max_mwh

    # --- plan section 6 formulas, defined in code ---------------------------
    def max_charge_mw(self, stored_mwh: float) -> float:
        """min(P_max, free / (eta_c * dt))"""
        free = max(0.0, self.e_max - stored_mwh)
        return min(self.p_max_mw, free / (self.eta_c * self.interval_h))

    def max_discharge_mw(self, stored_mwh: float, floor_mwh: float | None = None) -> float:
        """min(P_max, (E - E_min) * eta_d / dt). floor_mwh overrides E_min (reserve goals)."""
        floor = self.e_min_mwh if floor_mwh is None else max(self.e_min_mwh, floor_mwh)
        usable = max(0.0, stored_mwh - floor)
        return min(self.p_max_mw, usable * self.eta_d / self.interval_h)

    def energy_after(self, stored_mwh: float, setpoint_mw: float) -> float:
        """Stored energy after running setpoint_mw for one interval."""
        if setpoint_mw >= 0:
            return stored_mwh + self.eta_c * setpoint_mw * self.interval_h
        return stored_mwh + setpoint_mw * self.interval_h / self.eta_d
