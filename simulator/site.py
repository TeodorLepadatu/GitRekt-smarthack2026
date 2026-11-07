"""Site simulator: a pretend battery, inverter and grid meter (plan section 2, "Site").

Every 15 minutes it takes the guard's command and the interval's *actual*
demand and production, runs the command, and reports what physically happened:

    guard command ─► SiteSimulator.step() ─► new stored energy, grid import/export,
                     (+ actual demand,        curtailed energy, battery throughput
                        actual production)    and losses ─► back to "sensors" for the
                                              next cycle, and to settlement

Physics (all from battery.BatteryConfig, the same formulas the guard uses):
- The battery runs the setpoint for one interval. Charging stores eta_c x energy in,
  discharging delivers energy out and removes energy / eta_d from storage.
- The battery can never go past its limits: above e_max, below e_min, or over
  p_max. A setpoint that would is cut back (``clipped``), like a real battery's
  management system would.
- The grid trade is automatic (plan section 2):
      grid = production - curtailed - demand - battery_setpoint
  positive = export (sold), negative = import (bought).
- Curtailment only removes production, and only what would otherwise be
  exported. If the actual surplus turns out smaller than forecast, less is
  curtailed, so curtailing never causes an import or throws away battery energy.

No money here: prices, profit and wear cost belong to settlement, which reads
the StepResult.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional

from battery import BatteryConfig
from common import finite_float

_EPS = 1e-9
# The guard rounds setpoints to 4 decimals, so a setpoint "at the limit" can be a
# hair over it. Overshoots this small are rounding: the battery still applies the
# exact limit, but it is not reported as clipping.
ROUNDING_TOLERANCE_MW = 1e-3
_HHMM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class SimulatorError(ValueError):
    """Bad simulator input: actual measurements or the starting state are invalid."""


@dataclass(frozen=True)
class StepResult:
    """What physically happened in one 15-minute interval."""

    time: Optional[str]
    stored_before_mwh: float
    stored_after_mwh: float
    setpoint_requested_mw: float   # what the guard asked for (+ charge, - discharge)
    setpoint_applied_mw: float     # what the battery actually ran
    clipped: bool                  # True if the battery had to cut the setpoint back
    demand_mw: float               # actual
    production_mw: float           # actual, before curtailment
    curtailed_mw: float
    grid_import_mwh: float
    grid_export_mwh: float
    battery_throughput_mwh: float  # energy through the battery terminals (for wear)
    losses_mwh: float              # energy lost to charge/discharge efficiency
    demand_error_mw: Optional[float] = None      # actual - forecast (data for ML retraining)
    production_error_mw: Optional[float] = None
    warnings: list[str] = field(default_factory=list)

    @property
    def grid_net_mwh(self) -> float:
        """Export minus import: positive = the site sold energy overall."""
        return self.grid_export_mwh - self.grid_import_mwh

    def to_dict(self) -> dict[str, Any]:
        """As a dict, with floats rounded to 6 decimals (no 0.11749999999 in JSON)."""
        return {k: round(v, 6) if isinstance(v, float) else v for k, v in asdict(self).items()}


class SiteSimulator:
    """A pretend site with one battery. Keeps the stored energy between steps.

        sim = SiteSimulator(BatteryConfig(), stored_mwh=3.5)
        result = sim.step(guard_result, demand_mw=4.6, production_mw=5.0, time="15:00")
        next_features["stored_mwh"] = sim.stored_mwh
    """

    def __init__(self, battery: BatteryConfig | None = None, stored_mwh: float | None = None):
        self.battery = battery or BatteryConfig()
        start = self.battery.e_min_mwh if stored_mwh is None else stored_mwh
        self._stored = self._check_stored(start)
        self.history: list[StepResult] = []

    @property
    def stored_mwh(self) -> float:
        """Energy in the battery now. Read-only: it changes only through step() or reset()."""
        return self._stored

    # ------------------------------------------------------------------ #

    def step(
        self,
        command: Any,
        demand_mw: float,
        production_mw: float,
        *,
        time: Optional[str] = None,
        forecast: Optional[Mapping[str, Any]] = None,
    ) -> StepResult:
        """Run one interval.

        ``command``: the guard's output (``GuardResult`` or its dict) with
        ``battery_setpoint_mw`` and ``curtail_mw``.
        ``demand_mw`` / ``production_mw``: the interval's actual values.
        ``forecast``: optional features used for the decision; if it has
        ``demand_mw`` / ``production_mw`` the forecast errors are recorded.
        """
        b, dt = self.battery, self.battery.interval_h
        demand = _measurement(demand_mw, "demand_mw")
        production = _measurement(production_mw, "production_mw")
        warnings: list[str] = []
        if time is not None and not (isinstance(time, str) and _HHMM_RE.match(time)):
            warnings.append(f"time must be 'HH:MM', got {time!r}; not recorded")
            time = None
        if forecast is not None and not isinstance(forecast, Mapping):
            warnings.append(f"forecast must be a mapping, got {type(forecast).__name__}; ignored")
            forecast = None

        requested = _command_value(command, "battery_setpoint_mw", warnings)
        curtail_requested = _command_value(command, "curtail_mw", warnings)
        if curtail_requested < 0:
            warnings.append(f"curtail_mw {curtail_requested} is negative; using 0")
            curtail_requested = 0.0

        # Battery: never past e_min / e_max / p_max.
        before = self.stored_mwh
        applied = requested
        if requested > 0:
            applied = min(requested, b.max_charge_mw(before))
        elif requested < 0:
            applied = -min(-requested, b.max_discharge_mw(before))
        clipped = abs(applied - requested) > ROUNDING_TOLERANCE_MW
        if clipped:
            warnings.append(f"setpoint {requested:+.3f} MW clipped to {applied:+.3f} MW by the battery limits")
        after = min(b.e_max, max(min(b.e_min_mwh, before), b.energy_after(before, applied)))

        # Curtail only production (never battery energy), and only what would otherwise be exported.
        exportable = max(0.0, production - demand - applied)
        curtailed = min(curtail_requested, exportable, production)
        if curtail_requested - curtailed > ROUNDING_TOLERANCE_MW:
            warnings.append(f"curtailed {curtailed:.3f} MW instead of {curtail_requested:.3f} MW: "
                            "less surplus than forecast, or more than was produced")

        grid_mw = production - curtailed - demand - applied
        throughput = abs(applied) * dt

        result = StepResult(
            time=time,
            stored_before_mwh=before,
            stored_after_mwh=after,
            setpoint_requested_mw=requested,
            setpoint_applied_mw=applied,
            clipped=clipped,
            demand_mw=demand,
            production_mw=production,
            curtailed_mw=curtailed,
            grid_import_mwh=max(0.0, -grid_mw) * dt,
            grid_export_mwh=max(0.0, grid_mw) * dt,
            battery_throughput_mwh=throughput,
            losses_mwh=abs(throughput - abs(after - before)),
            demand_error_mw=_error(demand, forecast, "demand_mw"),
            production_error_mw=_error(production, forecast, "production_mw"),
            warnings=warnings,
        )
        self._stored = after
        self.history.append(result)
        return result

    def sensors(self) -> dict[str, float]:
        """What the site reports back for the next cycle (plan: "new SoC back to Sensors")."""
        last = self.history[-1] if self.history else None
        out = {"stored_mwh": self.stored_mwh}
        if last is not None:
            out.update(demand_mw=last.demand_mw, production_mw=last.production_mw)
        return out

    def reset(self, stored_mwh: float) -> None:
        """Start again (e.g. a new simulated day) from ``stored_mwh``."""
        self._stored = self._check_stored(stored_mwh)
        self.history = []

    # ------------------------------------------------------------------ #

    def _check_stored(self, value: Any) -> float:
        v = finite_float(value)
        if v is None or not 0.0 <= v <= self.battery.e_max:
            raise SimulatorError(f"stored_mwh must be between 0 and {self.battery.e_max} MWh, got {value!r}")
        return v


def _measurement(value: Any, name: str) -> float:
    v = finite_float(value)
    if v is None or v < 0:
        raise SimulatorError(f"{name} must be a finite number >= 0, got {value!r}")
    return v


def _command_value(command: Any, key: str, warnings: list[str]) -> float:
    """A setpoint from the guard. A missing or broken one means "do nothing", like
    a real inverter ignoring an invalid command."""
    raw = command.get(key) if isinstance(command, Mapping) else getattr(command, key, None)
    v = finite_float(raw)
    if v is None:
        warnings.append(f"{key} missing or invalid ({raw!r}); using 0")
        return 0.0
    return v


def _error(actual: float, forecast: Optional[Mapping[str, Any]], key: str) -> Optional[float]:
    if not forecast:
        return None
    f = finite_float(forecast.get(key))
    return None if f is None else actual - f
