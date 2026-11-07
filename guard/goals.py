"""Hard-goal rules for the guard (plan section 4).

Goal config comes from the goal parser (plan section 5):
    {"objective": "max_profit",
     "goals": [{"id": "g1", "type": "reserve", "mwh": 6.0, "by": "18:00", "until": "18:00", "hard": true},
               {"id": "g2", "type": "min_sell_price", "eur_per_mwh": 50, "hard": true}]}

Only hard goals are enforced here; soft goals only shape the prompt.

Reserve goal {mwh, by, until}:
  * Protection window [by, until): discharge may not take the battery below mwh.
    The window may wrap midnight ("by 22:00 until 06:00").
    ASSUMPTION: if `until` is missing or equal to `by`, the window lasts until
    midnight (plan: "after 18:00 blocks discharge below 6 MWh").
  * Before the window (charging phase): latest-start rule. After this interval,
    the time left must still cover full-power charging of the missing energy
    plus a safety buffer; otherwise charging is forced at full power. This also
    blocks discharges that would make the goal unreachable.
    Plan example: 3.5 of 6 MWh, 3 MW, eta_c 0.949 -> 53 min + 30 min buffer
    -> forced from the 16:30 interval.

min_sell_price goal {eur_per_mwh}: when price_now < eur_per_mwh nothing may be
sold to the grid: battery discharge is limited to covering the site's own
deficit, a surplus must be stored, and what the battery cannot take is curtailed.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from battery import BatteryConfig
from common import finite_float, goal_config_dict

DAY_MIN = 24 * 60


def to_minutes(value: Any) -> Optional[int]:
    """'HH:MM', datetime.time or datetime -> minutes since midnight. None if unparseable."""
    if isinstance(value, _dt.datetime):
        return value.hour * 60 + value.minute
    if isinstance(value, _dt.time):
        return value.hour * 60 + value.minute
    if isinstance(value, str):
        try:
            hh, mm = value.strip().split(":")[:2]
            h, m = int(hh), int(mm)
        except (ValueError, AttributeError):
            return None
        if h == 24 and m == 0:
            return DAY_MIN
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
    return None


def in_window(now: int, start: int, end: int) -> bool:
    """now in [start, end), wrapping midnight when end <= start."""
    if start < end:
        return start <= now < end
    return now >= start or now < end


def _num(goal: Mapping[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        value = finite_float(goal.get(key))
        if value is not None:
            return value
    return None


@dataclass
class Bounds:
    """Allowed setpoint range with the rule that set each side."""
    lo: float
    hi: float
    lo_rule: str
    hi_rule: str
    force_curtail_export: bool = False
    notes: list[str] = field(default_factory=list)

    def raise_lo(self, value: float, rule: str) -> None:
        if value > self.lo:
            self.lo, self.lo_rule = value, rule

    def lower_hi(self, value: float, rule: str) -> None:
        if value < self.hi:
            self.hi, self.hi_rule = value, rule


def hard_goals(goal_config: Any) -> list[Mapping[str, Any]]:
    """Hard goals from a GoalConfig, its dict, or None."""
    goals = goal_config_dict(goal_config).get("goals") or []
    if not isinstance(goals, list):
        return []
    return [g for g in goals if isinstance(g, Mapping) and g.get("hard", True) is not False]


def apply_reserve(
    goal: Mapping[str, Any],
    bounds: Bounds,
    *,
    now: int,
    stored_mwh: float,
    proposed_mw: float,
    cfg: BatteryConfig,
) -> None:
    gid = goal.get("id", "reserve")
    target = _num(goal, "mwh")
    by = to_minutes(goal.get("by"))
    if target is None or by is None:
        bounds.notes.append(f"goal_{gid}_invalid")
        return
    until = to_minutes(goal.get("until"))
    if until is None or until == by:
        until = DAY_MIN  # assumption: protect until midnight

    if in_window(now, by, until):
        # Protection phase: no discharge below target.
        cap = cfg.max_discharge_mw(stored_mwh, floor_mwh=target)
        before = bounds.lo
        bounds.raise_lo(-cap, f"goal_{gid}_blocked_discharge")
        if bounds.lo == before:
            bounds.notes.append(f"goal_{gid}_ok")
        return

    # Charging phase: latest-start rule, judged on the energy after this interval.
    minutes_left_after = (by - now) % DAY_MIN - cfg.interval_h * 60
    e_after = cfg.energy_after(stored_mwh, proposed_mw)
    need_after = max(0.0, target - e_after)
    need_min = need_after / (cfg.eta_c * cfg.p_max_mw) * 60
    if need_after > 0 and minutes_left_after < need_min + cfg.goal_buffer_min:
        bounds.raise_lo(bounds.hi, f"goal_{gid}_forced_charge")
        # Flag goals that cannot be met even at full power (settlement reports it).
        need_now = max(0.0, target - stored_mwh)
        minutes_left = (by - now) % DAY_MIN
        if target > cfg.e_max or need_now / (cfg.eta_c * cfg.p_max_mw) * 60 > minutes_left:
            bounds.notes.append(f"goal_{gid}_unreachable")
    else:
        bounds.notes.append(f"goal_{gid}_ok")


def apply_min_sell(
    goal: Mapping[str, Any],
    bounds: Bounds,
    *,
    price_now: Optional[float],
    net_mw: Optional[float],
) -> None:
    gid = goal.get("id", "min_sell")
    limit = _num(goal, "eur_per_mwh", "eur")
    if limit is None or price_now is None:
        bounds.notes.append(f"goal_{gid}_invalid")
        return
    if price_now >= limit:
        bounds.notes.append(f"goal_{gid}_ok")
        return
    net = net_mw or 0.0
    rule = f"goal_{gid}_blocked_export"
    if net >= 0:
        # Surplus: no discharge, store as much surplus as possible.
        bounds.raise_lo(min(net, bounds.hi), rule)
    else:
        # Deficit: discharge only to cover the site's own demand.
        bounds.raise_lo(net, rule)
    bounds.force_curtail_export = True
