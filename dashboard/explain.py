"""Plain-language explanation of one decided interval, built from its numbers.

The decision LLM writes ``explanation_for_operator`` itself, but a small model
is unreliable at prose: the sentences rarely hold numbers, sometimes contradict
the action ("Battery idle to cover the deficit"), and they describe what the AI
proposed, not what happened after the safety layer. So the operator-facing
explanation is built here from the settled record instead:

    summary   one sentence: what the battery did and the main reason, with numbers
    facts     short supporting facts (grid, money, the next price peak)
    value_eur what the battery's energy was worth in this interval at its price:
              + when it delivered energy, − when it took energy in
    safety    what the safety layer changed, if anything
    ai_note   the AI's own sentence, kept as a note unless it contradicts the action

Everything here is deterministic and never raises on an odd record.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from battery import BatteryConfig

EPS_MW = 0.005
NEAR_EUR = 5.0          # "close to" break-even, same band as the prompt's NEAR zone


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v and abs(v) != float("inf") else default


def _m(x: Any) -> Mapping[str, Any]:
    return x if isinstance(x, Mapping) else {}


def eur(v: float) -> str:
    """€1,234 / −€12 (whole euros: explanations are not invoices)."""
    r = round(v)
    return f"{'−' if r < 0 else ''}€{abs(r):,}"


def price(v: float) -> str:
    return f"{'−' if v < -0.005 else ''}€{abs(v):,.2f}/MWh" if abs(v) < 100 else f"€{v:,.0f}/MWh"


def mw(v: float) -> str:
    return f"{abs(v):.2f} MW"


def kind_of(setpoint_mw: float) -> str:
    if setpoint_mw > EPS_MW:
        return "charge"
    if setpoint_mw < -EPS_MW:
        return "discharge"
    return "hold"


def phrase(setpoint_mw: float) -> str:
    k = kind_of(setpoint_mw)
    return "hold" if k == "hold" else f"{k} {mw(setpoint_mw)}"


def _goal_texts(rules: list[str], goals: list[Mapping[str, Any]], what: str) -> list[str]:
    names = {str(g.get("id")): str(g.get("source_text") or g.get("id")) for g in goals}
    out = []
    for r in rules:
        m = re.match(rf"^goal_(.+)_{what}$", r)
        if m:
            out.append(names.get(m.group(1), f"goal {m.group(1)}"))
    return out


# ------------------------------------------------------------------ AI note check

_SAYS = {
    "discharge": re.compile(r"\b(discharg\w*|sell\w*|selling|export\w*)\b", re.I),
    "charge": re.compile(r"\b(?<!dis)charg(e|es|ed|ing)\b|\bstor(e|es|ed|ing)\b|\bbuy\w*\b", re.I),
    "hold": re.compile(r"\b(idle|hold\w*|keep\w* the battery|no action)\b", re.I),
}


def note_contradicts(note: str, proposed_kind: str, net_label: str) -> bool:
    """True when the AI's sentence says the opposite of what it proposed, or calls a
    deficit a surplus (and the other way round). Leading words count most: "Charging
    ... instead of selling" is about charging."""
    text = note.strip()
    if not text:
        return False
    first = text.split()[0].lower()
    lead = next((k for k, rx in _SAYS.items() if rx.match(first)), None)
    if lead is not None and lead != proposed_kind:
        return True
    if lead is None:
        said = {k for k, rx in _SAYS.items() if rx.search(text)}
        if said and proposed_kind not in said:
            return True
    low = text.lower()
    if net_label == "DEFICIT" and "surplus" in low and "deficit" not in low and "shortfall" not in low:
        return True
    if net_label == "SURPLUS" and ("deficit" in low or "shortfall" in low) and "surplus" not in low:
        return True
    return False


# ------------------------------------------------------------------ the explanation

def explain(record: Mapping[str, Any], *, p_max_mw: Optional[float] = None,
            goals: Optional[list[Mapping[str, Any]]] = None) -> dict[str, Any]:
    """The explanation of one decided interval (see the module docstring)."""
    rec = _m(record)
    f, d, g, site = _m(rec.get("features")), _m(rec.get("decision")), _m(rec.get("guard")), _m(rec.get("site"))
    goals = list(goals or [])
    p_max = BatteryConfig().p_max_mw if p_max_mw is None else _f(p_max_mw, BatteryConfig().p_max_mw)
    rules = [str(r) for r in (g.get("applied_rules") or [])]

    sp = _f(site.get("setpoint_applied_mw", g.get("battery_setpoint_mw")))
    k = kind_of(sp)
    p = _f(rec.get("price_eur_mwh", f.get("price_now")))
    be = _f(f.get("break_even"))
    demand, production = _f(rec.get("demand_mw")), _f(rec.get("production_mw"))
    net = production - demand                                   # > 0 surplus, < 0 shortfall
    curtail = max(0.0, _f(site.get("curtailed_mw", g.get("curtail_mw"))))
    imported, exported = _f(rec.get("grid_import_mwh")), _f(rec.get("grid_export_mwh"))
    interval_h = _f(f.get("interval_h"), 0.25) or 0.25
    energy = abs(sp) * interval_h                               # MWh into or out of the battery
    value = -sp * interval_h * p                                # what that energy was worth
    stored = _f(rec.get("soc_after_mwh"))
    cap = _f(f.get("capacity_mwh"), 0.0)
    fmax, tmax = f.get("future_max_price"), f.get("future_max_time")
    source = str(d.get("source") or "fallback")

    forced = _goal_texts(rules, goals, "forced_charge")
    kept = _goal_texts(rules, goals, "blocked_discharge")
    below_min = any(r.endswith("_blocked_export") for r in rules)

    facts: list[str] = []
    gap = p - be

    # -- what it did and why ------------------------------------------------------
    if k == "charge":
        head = f"Charging {mw(sp)}"
        if forced:
            why = f"to reach '{forced[0]}' in time"
        elif p < -0.005:
            why = f"the price is negative ({price(p)}): the site is paid to take energy"
        elif net > EPS_MW and net >= sp - 0.05:
            why = (f"storing the site's own {mw(net)} surplus instead of selling it at {price(p)}"
                   + (f"; stored energy is worth about {price(be)} later" if be > p + NEAR_EUR else ""))
        elif be > p + NEAR_EUR:
            why = f"buying at {price(p)}, {eur(be - p)}/MWh below what stored energy is worth later ({price(be)})"
        else:
            why = f"buying at {price(p)} to have energy for the hours ahead"
        if fmax is not None and _f(fmax) > p + NEAR_EUR and tmax:
            facts.append(f"Highest price still to come today: {price(_f(fmax))} at {tmax}")
    elif k == "discharge":
        head = f"Discharging {mw(sp)}"
        covered = min(-sp, max(0.0, -net))
        extra = -sp - max(0.0, -net)
        if covered > EPS_MW and covered >= -net - 0.01 and extra > 0.05:
            why = f"covering the site's whole {mw(-net)} shortfall and selling {mw(extra)} more at {price(p)}"
        elif covered > EPS_MW and covered >= -net - 0.01:
            why = f"covering the site's whole {mw(-net)} shortfall instead of buying it at {price(p)}"
        elif covered > EPS_MW:
            why = f"covering {mw(covered)} of the site's {mw(-net)} shortfall instead of buying it at {price(p)}"
        else:
            why = f"selling stored energy at {price(p)}"
        if gap > NEAR_EUR:
            why += f", {eur(gap)}/MWh above what the stored energy is worth ({price(be)})"
    else:
        head = "Holding"
        if kept:
            why = f"keeping the stored energy for '{kept[0]}'"
        elif source == "operator" or str(_m(rec.get("control")).get("action")) in ("hold", "rejected", "timeout"):
            why = "the battery was set to hold"
        elif cap and stored >= cap - 0.01 and net > EPS_MW:
            why = "the battery is full"
        elif stored <= _f(f.get("min_soc_mwh"), 0.0) + 0.01 and net < -EPS_MW:
            why = "the battery is at its lowest allowed level"
        elif abs(gap) <= NEAR_EUR:
            why = f"the price ({price(p)}) is close to what stored energy is worth ({price(be)}): not worth the wear"
        elif net < -EPS_MW and p < be:
            why = (f"buying the shortfall at {price(p)} is cheaper than using stored energy, "
                   f"which is worth {price(be)} later")
        elif net > EPS_MW and p > be:
            why = f"selling the surplus at {price(p)} pays more than storing it (worth {price(be)})"
        else:
            why = "no move pays off in this interval"

    summary = f"{head}: {why}."

    # -- grid, curtailment, money ----------------------------------------------
    if k == "discharge" and imported < 0.001 and net < -EPS_MW:
        facts.append("Nothing bought from the grid in this interval")
    if k != "discharge" and exported > 0.001:
        facts.append(f"{exported:.2f} MWh of surplus sold to the grid at {price(p)}")
    if curtail > EPS_MW:
        reason = ("the price is below zero" if p < 0 else
                  "selling is below your minimum price" if below_min else "nowhere to put it")
        facts.append(f"{mw(curtail)} of solar/wind output switched off: {reason}")
    elif below_min and exported < 0.001:
        facts.append("Not selling: the price is below your minimum")
    if k == "discharge":
        facts.append(f"{energy:.2f} MWh delivered, worth {eur(value)} at this price")
    elif k == "charge":
        facts.append(f"{energy:.2f} MWh stored, " + (f"paid {eur(value)} to take it" if value > 0
                                                    else f"costing {eur(-value)} at this price"))

    # -- the safety layer ---------------------------------------------------------
    safety = None
    if source != "operator":
        proposed = _f(d.get("battery_fraction")) * p_max
        if abs(proposed - sp) > 0.05:
            safety = f"The {'AI' if source == 'llm' else 'backup rules'} proposed to {phrase(proposed)}; the safety layer set {phrase(sp)}."

    # -- the AI's own words ---------------------------------------------------------
    ai_note, dropped = None, False
    if source == "llm":
        note = str(d.get("explanation_for_operator") or "").strip()
        proposed_kind = kind_of(_f(d.get("battery_fraction")) * p_max)
        if note:
            if note_contradicts(note, proposed_kind, str(f.get("net_label") or "")):
                dropped = True
            else:
                ai_note = note

    return {"summary": summary, "facts": facts, "value_eur": round(value, 2), "safety": safety,
            "ai_note": ai_note, "ai_note_dropped": dropped}
