"""Goal progress: how a reserve goal is doing right now (plan section 4).

For every hard reserve goal the feature builder answers three questions the
small LLM must not have to work out for itself:

    How much energy is still missing, and how long is left?
    What charging pace keeps the goal on track without hurrying?   -> even_pace_fraction
    When does waiting stop being safe?                             -> force_from

The two numbers do different jobs. The even pace tells the LLM how urgent the
goal is, so it can still shop for a cheaper interval. The latest start is the
moment the guard takes over and charges at full power, so the prompt can warn
about it before it happens.

Window semantics (``by``, ``until``, midnight wrap) come from ``guard.goals``,
so the prompt and the guard always describe the same window.

Output entry, consumed by ``prompt_assembly.user_goals`` and
``validator.fallback``::

    {"id": "g1", "hard": True, "status": "on_track" | "behind" | "met",
     "need_mwh": 2.5, "hours_left": 3.0, "even_pace_fraction": 0.29,
     "force_from": "16:30", "reachable": True}
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from battery import BatteryConfig
from common import finite_float, goal_config_dict
from guard.goals import DAY_MIN, in_window, to_minutes

from .prices import hhmm

EPS = 1e-9


def goals_progress(
    goal_config: Any,
    *,
    now_min: int,
    stored_mwh: float,
    battery: BatteryConfig,
) -> list[dict[str, Any]]:
    """Progress entries for every reserve goal in ``goal_config``.

    Goals of other types need no progress: ``min_sell_price`` is judged from
    ``price_now``, which the prompt already has. Goals the parser left
    incomplete are skipped here; the guard still refuses to break them.
    """
    entries: list[dict[str, Any]] = []
    for goal in goal_config_dict(goal_config).get("goals") or []:
        if not isinstance(goal, Mapping) or goal.get("type") != "reserve":
            continue
        entry = reserve_progress(goal, now_min=now_min, stored_mwh=stored_mwh, battery=battery)
        if entry is not None:
            entries.append(entry)
    return entries


def reserve_progress(
    goal: Mapping[str, Any],
    *,
    now_min: int,
    stored_mwh: float,
    battery: BatteryConfig,
) -> Optional[dict[str, Any]]:
    """One reserve goal's progress, or None when the goal is unusable."""
    target = finite_float(goal.get("mwh"))
    by = to_minutes(goal.get("by"))
    if target is None or target <= 0 or by is None:
        return None
    until = to_minutes(goal.get("until"))
    if until is None or until == by:
        until = DAY_MIN  # same assumption as guard.goals: protect until midnight

    entry: dict[str, Any] = {"id": goal.get("id"), "hard": goal.get("hard", True) is not False}
    need = max(0.0, target - stored_mwh)

    if in_window(now_min, by, until):
        # Protection phase: the deadline has passed, the reserve must simply hold.
        if need <= EPS:
            return {**entry, "status": "met", "need_mwh": 0.0, "reachable": True}
        deadline = until
    else:
        # Charging phase: still working towards the deadline.
        if need <= EPS:
            return {**entry, "status": "met", "need_mwh": 0.0, "reachable": True}
        deadline = by

    minutes_left = (deadline - now_min) % DAY_MIN or DAY_MIN
    hours_left = minutes_left / 60.0
    full_power_min = _full_power_minutes(need, battery)
    reachable = full_power_min <= minutes_left + EPS

    entry.update(
        status="on_track",
        need_mwh=round(need, 3),
        hours_left=round(hours_left, 2),
        even_pace_fraction=even_pace_fraction(need, hours_left, battery),
        reachable=reachable,
    )

    if in_window(now_min, by, until):
        # Already inside the window and below the reserve: recover now.
        entry["status"] = "behind"
        return entry

    force_min = latest_start_minutes(need, by, battery)
    entry["force_from"] = hhmm(force_min)
    if not reachable or _reached(now_min, force_min, by):
        entry["status"] = "behind"
    return entry


def even_pace_fraction(need_mwh: float, hours_left: float, battery: BatteryConfig) -> float:
    """Charging fraction that reaches the goal at a steady pace (plan section 4).

    ``need / hours`` is energy that must land *in* the battery, so it is divided
    by the charging efficiency to get grid-side power, then by rated power to
    get a battery_fraction. Clamped to 1.00: nothing above full power exists.
    """
    if hours_left <= EPS:
        return 1.0
    power_mw = need_mwh / hours_left / battery.eta_c
    return round(min(1.0, max(0.0, power_mw / battery.p_max_mw)), 2)


def latest_start_minutes(need_mwh: float, by_min: int, battery: BatteryConfig) -> int:
    """Start of the last interval from which full power still makes the deadline.

    Full-power charging of ``need_mwh`` takes ``need / (eta_c x P_max)`` hours.
    A safety buffer is added, and the result is rounded back to the start of its
    interval, because the battery can only be commanded once per interval.

    Plan example: 2.5 MWh at 3 MW with eta 0.949 takes 53 min; plus a 30 min
    buffer that is 16:37 before 18:00, so the guard forces charging from 16:30.
    """
    start = by_min - _full_power_minutes(need_mwh, battery) - battery.goal_buffer_min
    grid = battery.interval_h * 60.0
    return int((start // grid) * grid) % DAY_MIN


def _full_power_minutes(need_mwh: float, battery: BatteryConfig) -> float:
    rate = battery.eta_c * battery.p_max_mw  # MWh stored per hour at full power
    return float("inf") if rate <= 0 else need_mwh / rate * 60.0


def _reached(now_min: int, force_min: int, by_min: int) -> bool:
    """True once ``now`` is at or past ``force_from`` within the run-up to ``by``.

    Compared as "minutes until the deadline" so a window that crosses midnight
    behaves the same as one that does not.
    """
    return (by_min - now_min) % DAY_MIN <= (by_min - force_min) % DAY_MIN
