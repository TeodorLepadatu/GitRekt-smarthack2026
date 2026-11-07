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
"""
from __future__ import annotations

from typing import Any, Mapping

from .fmt import PromptAssemblyError, eur, frac, mw, num, qty

HEADER = "USER GOALS (hard goals come before profit; within them, follow the objective)"

OBJECTIVE_TEXT = {
    "max_profit": "maximise profit",
    "self_consumption": (
        "use own energy first: stay close to the matching fraction and avoid buying from "
        "or selling to the grid"
    ),
}

_INDENT = "         "


def render_user_goals(goal_config: Any, features: Mapping[str, Any]) -> str:
    config = _as_dict(goal_config)
    objective = config.get("objective") or "max_profit"
    if objective not in OBJECTIVE_TEXT:
        raise PromptAssemblyError(f"unknown objective {objective!r}")

    progress = {}
    for p in features.get("goals") or []:
        if isinstance(p, Mapping) and p.get("id") is not None:
            progress[p["id"]] = p

    lines = [HEADER, f"- Objective: {OBJECTIVE_TEXT[objective]}"]
    goals = [g for g in config.get("goals") or [] if isinstance(g, Mapping)]
    for g in goals:
        tag = "[HARD]" if g.get("hard", True) is not False else "[SOFT]"
        gtype = g.get("type")
        if gtype == "reserve":
            lines.append(f"- {tag} {_reserve(g, progress.get(g.get('id')), features)}")
        elif gtype == "min_sell_price":
            lines.append(f"- {tag} {_min_sell_price(g, features)}")
        else:
            raise PromptAssemblyError(f"unknown goal type {gtype!r} in goal {g.get('id')!r}")
    if not goals:
        lines.append("- No hard goals: follow the objective and the break-even rule.")
    return "\n".join(lines)


def _as_dict(goal_config: Any) -> Mapping[str, Any]:
    if goal_config is None:
        return {}
    if hasattr(goal_config, "to_dict"):
        goal_config = goal_config.to_dict()
    if not isinstance(goal_config, Mapping):
        raise PromptAssemblyError(f"goal_config must be a mapping, got {type(goal_config).__name__}")
    return goal_config


def _reserve(g: Mapping[str, Any], p: Mapping[str, Any] | None, features: Mapping[str, Any]) -> str:
    target = qty(num(g.get("mwh"), f"goal {g.get('id')} mwh"))
    by, until = g.get("by"), g.get("until")
    if until and until != by:
        title = f"Keep at least {target} MWh from {by} until {until}"
    else:
        title = f"Store {target} MWh by {by}"

    if p is None:
        return f"{title} -> status: NOT COMPUTED. The guard still enforces it."

    status = str(p.get("status") or "unknown").lower()
    have = mw(num(features["stored_mwh"], "stored_mwh"))
    if status == "met":
        keep_until = until or by
        return (f"{title} -> status: MET. Have {have} MWh.\n"
                f"{_INDENT}Do not discharge below {target} MWh until {keep_until}.")
    if status in ("on_track", "behind"):
        gid = g.get("id")
        need = mw(num(p.get("need_mwh"), f"goal {gid} need_mwh"))
        hours = f"{num(p.get('hours_left'), f'goal {gid} hours_left'):.1f}"
        pace = frac(num(p.get("even_pace_fraction"), f"goal {gid} even_pace_fraction"))
        line = (f"{title} -> status: {status.upper().replace('_', ' ')}. "
                f"Have {have} MWh, need +{need} MWh in {hours} h.\n"
                f"{_INDENT}Even pace: battery_fraction >= {pace}.")
        if status == "behind":
            line += " Charge at least at this pace now."
        if p.get("force_from"):
            line += f" Guard forces full charging from {p['force_from']} if still open."
        return line
    return f"{title} -> status: {status.upper()}."


def _min_sell_price(g: Mapping[str, Any], features: Mapping[str, Any]) -> str:
    limit = num(g.get("eur_per_mwh"), f"goal {g.get('id')} eur_per_mwh")
    price = num(features["price_now"], "price_now")
    title = f"Never sell below EUR {eur(limit)}/MWh"
    if price < limit:
        return (f"{title} -> current price EUR {eur(price)}: BLOCKED.\n"
                f"{_INDENT}Do not sell to the grid now; store the surplus, curtail what the battery cannot take.")
    return f"{title} -> current price EUR {eur(price)}: allowed."
