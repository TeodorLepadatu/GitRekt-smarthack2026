"""USER GOALS section (plan sections 2 and 4).

Inputs:
- the goal config from the goal parser:
    {"objective": "max_profit", "goals": [{"id": "g1", "type": "reserve", "mwh": 6, "by": "18:00", ...}]}
- the goal progress from the feature builder, in ``features["goals"]``:
    [{"id": "g1", "status": "on_track", "need_mwh": 2.5, "hours_left": 3.0,
      "even_pace_fraction": 0.29, "force_from": "16:30"}]

Goals are described from their structured fields, never from the user's raw
``source_text``: free text typed by a user must not be able to add
instructions to the decision prompt.

A broken goal or progress entry never stops the prompt: the goal is still
listed (the guard enforces hard goals regardless) and the problem is reported
in ``warnings``.
"""
from __future__ import annotations

from typing import Any, Mapping

from common import goal_config_dict

from .fmt import PromptAssemblyError, eur, frac, is_hhmm, mw, num, qty

HEADER = "USER GOALS (hard goals come before profit; within them, follow the objective)"

OBJECTIVE_TEXT = {
    "max_profit": "maximise profit",
    "self_consumption": "use own energy first (step 2: use the matching fraction)",
}
DEFAULT_OBJECTIVE = "max_profit"

_INDENT = "         "
_GUARD = "The guard still enforces it."


def render_user_goals(goal_config: Any, features: Mapping[str, Any], *, warnings: list[str] | None = None) -> str:
    w = warnings if warnings is not None else []
    config = _as_dict(goal_config)

    objective = config.get("objective") or DEFAULT_OBJECTIVE
    if objective not in OBJECTIVE_TEXT:
        w.append(f"unknown objective {objective!r}; using {DEFAULT_OBJECTIVE}")
        objective = DEFAULT_OBJECTIVE

    progress: dict[Any, Mapping[str, Any]] = {}
    for p in features.get("goals") or []:
        if isinstance(p, Mapping) and p.get("id") is not None:
            progress[p["id"]] = p
        else:
            w.append(f"ignored goal progress entry without an id: {p!r}")

    lines = [HEADER, f"- Objective: {OBJECTIVE_TEXT[objective]}"]
    goals = config.get("goals") or []
    if not isinstance(goals, list):
        w.append(f"ignored goals: expected a list, got {type(goals).__name__}")
        goals = []
    shown = 0
    for g in goals:
        if not isinstance(g, Mapping):
            w.append(f"ignored goal that is not an object: {g!r}")
            continue
        tag = "[SOFT]" if g.get("hard", True) is False else "[HARD]"
        gtype, gid = g.get("type"), g.get("id")
        if gtype == "reserve":
            text = _reserve(g, progress.pop(gid, None), features, w)
        elif gtype == "min_sell_price":
            text = _min_sell_price(g, features, w)
        else:
            w.append(f"goal {gid!r} has unknown type {gtype!r}")
            text = f"Goal {gid} (type not understood). {_GUARD}"
        lines.append(f"- {tag} {text}")
        shown += 1
    for gid in progress:
        if not any(isinstance(g, Mapping) and g.get("id") == gid for g in goals):
            w.append(f"goal progress for {gid!r} has no matching goal in the goal config")
    if not shown:
        lines.append("- No hard goals: follow the objective and the break-even rule.")
    return "\n".join(lines)


def _as_dict(goal_config: Any) -> Mapping[str, Any]:
    try:
        return goal_config_dict(goal_config)
    except TypeError as exc:
        raise PromptAssemblyError(str(exc)) from exc


def _number(d: Mapping[str, Any], key: str, what: str, w: list[str]) -> float | None:
    try:
        return num(d.get(key), key)
    except PromptAssemblyError:
        w.append(f"{what}: {key} is missing or not a number ({d.get(key)!r})")
        return None


def _reserve(g: Mapping[str, Any], p: Mapping[str, Any] | None, features: Mapping[str, Any], w: list[str]) -> str:
    gid = g.get("id")
    mwh = _number(g, "mwh", f"reserve goal {gid}", w)
    by, until = g.get("by"), g.get("until")
    if mwh is None or mwh <= 0 or not is_hhmm(by) or (until is not None and not is_hhmm(until)):
        if mwh is not None and mwh <= 0:
            w.append(f"reserve goal {gid}: mwh must be positive ({mwh})")
        if not is_hhmm(by) or (until is not None and not is_hhmm(until)):
            w.append(f"reserve goal {gid}: by/until must be 'HH:MM' ({by!r}, {until!r})")
        return f"Reserve goal {gid} (incomplete). {_GUARD}"

    target = qty(mwh)
    title = (f"Keep at least {target} MWh from {by} until {until}" if until and until != by
             else f"Store {target} MWh by {by}")
    if p is None:
        return f"{title} -> status: NOT COMPUTED. {_GUARD}"

    status = str(p.get("status") or "").strip().lower()
    have = mw(num(features["stored_mwh"], "stored_mwh"))
    if status == "met":
        return (f"{title} -> status: MET. Have {have} MWh.\n"
                f"{_INDENT}Do not discharge below {target} MWh until {until or by}.")
    if status not in ("on_track", "behind"):
        w.append(f"reserve goal {gid}: unknown status {p.get('status')!r}")
        return f"{title} -> status: UNKNOWN. {_GUARD}"

    line = f"{title} -> status: {status.upper().replace('_', ' ')}. Have {have} MWh"
    need = _number(p, "need_mwh", f"goal {gid} progress", w)
    hours = _number(p, "hours_left", f"goal {gid} progress", w)
    if need is not None and hours is not None:
        line += f", need +{mw(need)} MWh in {hours:.1f} h"
    line += "."
    pace = _number(p, "even_pace_fraction", f"goal {gid} progress", w)
    rest = []
    if pace is not None:
        rest.append(f"Even pace: battery_fraction >= {frac(pace)}.")
        if status == "behind":
            rest.append("Charge at least at this pace now.")
    force = p.get("force_from")
    if force is not None:
        if is_hhmm(force):
            rest.append(f"Guard forces full charging from {force} if still open.")
        else:
            w.append(f"goal {gid} progress: force_from must be 'HH:MM' ({force!r})")
    if rest:
        line += f"\n{_INDENT}" + " ".join(rest)
    return line


def _min_sell_price(g: Mapping[str, Any], features: Mapping[str, Any], w: list[str]) -> str:
    gid = g.get("id")
    limit = _number(g, "eur_per_mwh", f"min_sell_price goal {gid}", w)
    if limit is None:
        return f"Minimum sell price goal {gid} (incomplete). {_GUARD}"
    price = num(features["price_now"], "price_now")
    title = f"Never sell below EUR {eur(limit)}/MWh"
    if price < limit:
        return (f"{title} -> current price EUR {eur(price)}: BLOCKED.\n"
                f"{_INDENT}Do not sell to the grid now: if surplus left after charge limit = YES, set curtail = true.")
    return f"{title} -> current price EUR {eur(price)}: allowed."
