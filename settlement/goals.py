"""Goal status after each settled interval (plan section 2, "Settlement + logging").

The guard enforces hard goals before the battery moves; this module checks,
with the ACTUAL state of charge, whether they were in fact met, and when.

Reserve goal {mwh, by, until}  (same time rules as guard/goals.py)
    before `by`  : met       stored >= mwh (records "reached at HH:MM")
                   on_track  full power + safety buffer still fits before `by`
                   behind    it no longer fits with the buffer (the guard forces charging)
                   unreachable  even full power cannot make it any more
    at `by`      : met if stored >= mwh, else missed (final for the day)
    [by, until)  : violated if the battery discharged below mwh (final)

min_sell_price goal {eur_per_mwh}
    violated if anything was exported while price < eur_per_mwh (final);
    on_track during the day, met at the end of the day (finalize()).

Statuses: on_track | behind | met | unreachable | missed | violated.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional

from battery import BatteryConfig
from common import finite_float, goal_config_dict
from guard.goals import DAY_MIN, in_window, to_minutes

from .settle import IntervalSettlement

TOL_MWH = 0.01  # rounding tolerance on stored energy
FINAL = ("missed", "violated")


def goal_definition(g: Mapping[str, Any]) -> tuple:
    """What a goal asks for, independent of its id."""
    return (g.get("type"), finite_float(g.get("mwh")), g.get("by"), g.get("until"), finite_float(g.get("eur_per_mwh")))


def hhmm(minutes: float) -> str:
    m = int(round(minutes)) % DAY_MIN
    return f"{m // 60:02d}:{m % 60:02d}"


@dataclass
class GoalState:
    id: str
    type: str
    hard: bool
    source_text: str
    status: str = "on_track"
    reached_at: Optional[str] = None   # reserve: first time the target was stored
    violated_at: Optional[str] = None
    violations: int = 0                # intervals that broke the goal
    blocked_intervals: int = 0         # min_sell: intervals where selling was not allowed
    need_mwh: Optional[float] = None
    hours_left: Optional[float] = None
    message: str = ""
    events: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class GoalTracker:
    """Keeps one GoalState per goal for the day. Never raises on a bad goal."""

    def __init__(self, goal_config: Any = None, cfg: BatteryConfig = BatteryConfig()):
        self.cfg = cfg
        self.goals: list[Mapping[str, Any]] = []
        self.states: dict[str, GoalState] = {}
        self.set_goals(goal_config)

    def set_goals(self, goal_config: Any) -> None:
        """New goal config (the user changed goals). Goals that stay keep their history."""
        goals = goal_config_dict(goal_config).get("goals") or []
        old_goals = self.goals
        # Match history by what a goal says, not by its id: the goal parser renumbers
        # g1, g2, ... whenever a goal is removed, so an id can come back as a different goal.
        old = {goal_definition(g): self.states[str(g["id"])] for g in old_goals if str(g["id"]) in self.states}
        self.goals = [g for g in goals if isinstance(g, Mapping) and g.get("id") is not None]
        self.states = {}
        for g in self.goals:
            gid = str(g["id"])
            kept = old.get(goal_definition(g))
            if kept is not None:
                kept.id = gid
            self.states[gid] = kept or GoalState(
                id=gid, type=str(g.get("type")), hard=g.get("hard", True) is not False,
                source_text=str(g.get("source_text") or ""),
            )

    # ------------------------------------------------------------------ update
    def update(self, s: IntervalSettlement) -> dict[str, str]:
        """Judge every goal on one settled interval. Returns {goal_id: status}."""
        start = to_minutes(s.time)
        for g in self.goals:
            st = self.states[str(g["id"])]
            if start is None:
                st.message = "time missing: not checked"
                continue
            if st.type == "reserve":
                self._reserve(g, st, start, s)
            elif st.type == "min_sell_price":
                self._min_sell(g, st, s)
        return {gid: st.status for gid, st in self.states.items()}

    def _reserve(self, g: Mapping[str, Any], st: GoalState, start: int, s: IntervalSettlement) -> None:
        cfg = self.cfg
        target = finite_float(g.get("mwh"))
        by = to_minutes(g.get("by"))
        if target is None or by is None:
            st.message = "goal incomplete: not checked"
            return
        until = to_minutes(g.get("until"))
        if until is None or until == by:
            until = DAY_MIN  # same assumption as the guard: protect until midnight
        step = cfg.interval_h * 60
        end = start + step
        soc = s.soc_after_mwh

        if in_window(start, by, until):
            # Protection phase: only a discharge below the target breaks it.
            if s.battery_mw < 0 and soc < target - TOL_MWH:
                if st.status != "violated":
                    st.violated_at = s.time
                    st.events.append(f"{s.time}: discharged to {soc:.2f} MWh, below the {target:g} MWh reserve")
                st.status = "violated"
                st.violations += 1
            if st.status not in FINAL:
                st.status = "met" if soc >= target - TOL_MWH else st.status
            st.need_mwh = round(max(0.0, target - soc), 4)
            st.hours_left = 0.0
            st.message = self._reserve_message(st, target, g)
            return

        # Charging phase (before the deadline; start == by is inside the window above).
        to_deadline = (by - start) % DAY_MIN
        deadline_now = to_deadline <= step        # this interval ends at (or past) `by`
        minutes_left = max(0.0, to_deadline - step)
        need = max(0.0, target - soc)
        st.need_mwh = round(need, 4)
        st.hours_left = round(minutes_left / 60, 2)

        if st.status in FINAL:
            st.message = self._reserve_message(st, target, g)
            return
        if need <= TOL_MWH:
            if st.reached_at is None:
                st.reached_at = hhmm(end)
                st.events.append(f"{target:g} MWh reached at {st.reached_at}")
            st.status = "met"
        else:
            st.reached_at = None  # dropped back below before the deadline
            if deadline_now:
                st.status = "missed"
                st.events.append(f"missed: {soc:.2f} of {target:g} MWh stored at {g.get('by')}")
            else:
                full_power_min = need / (cfg.eta_c * cfg.p_max_mw) * 60
                if target > cfg.e_max + TOL_MWH or full_power_min > minutes_left + 1e-6:
                    st.status = "unreachable"
                elif full_power_min + cfg.goal_buffer_min > minutes_left:
                    st.status = "behind"
                else:
                    st.status = "on_track"
        st.message = self._reserve_message(st, target, g)

    @staticmethod
    def _reserve_message(st: GoalState, target: float, g: Mapping[str, Any]) -> str:
        if st.status == "violated":
            return f"{target:g} MWh reserve broken at {st.violated_at}"
        if st.status == "missed":
            return f"{target:g} MWh not reached by {g.get('by')}"
        if st.status == "met":
            return f"{target:g} MWh reached at {st.reached_at}" if st.reached_at else f"holding {target:g} MWh"
        if st.status == "unreachable":
            return f"{target:g} MWh by {g.get('by')} can no longer be reached (need +{st.need_mwh:.2f} MWh)"
        return f"need +{st.need_mwh:.2f} MWh in {st.hours_left:.2f} h ({st.status.replace('_', ' ')})"

    def _min_sell(self, g: Mapping[str, Any], st: GoalState, s: IntervalSettlement) -> None:
        limit = finite_float(g.get("eur_per_mwh"))
        if limit is None:
            limit = finite_float(g.get("eur"))
        if limit is None:
            st.message = "goal incomplete: not checked"
            return
        if s.price_eur_mwh < limit:
            if s.grid_export_mwh > 1e-6:
                if st.status != "violated":
                    st.violated_at = s.time
                st.status = "violated"
                st.violations += 1
                st.events.append(
                    f"{s.time}: sold {s.grid_export_mwh:.4f} MWh at EUR {s.price_eur_mwh:g} < EUR {limit:g}")
            else:
                st.blocked_intervals += 1
        if st.status == "violated":
            st.message = f"sold below EUR {limit:g}/MWh {st.violations}x (first at {st.violated_at})"
        else:
            st.message = f"never sold below EUR {limit:g}/MWh ({st.blocked_intervals} intervals held back)"

    # ---------------------------------------------------------------- end of day
    def finalize(self) -> dict[str, str]:
        """End of day: an unbroken min-sell goal counts as met. Reserves were judged at their deadline."""
        for st in self.states.values():
            if st.type == "min_sell_price" and st.status == "on_track":
                st.status = "met"
        return {gid: st.status for gid, st in self.states.items()}

    def summary(self) -> list[dict]:
        return [st.to_dict() for st in self.states.values()]
