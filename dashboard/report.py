"""The day report: what the battery did, why, and what it was worth (GET /api/report).

Built from the dashboard's own Interval payloads (with their explanations), the
ledger's totals, the goals, and the operator's audit log, so every number in it
can be traced to an interval. Deterministic; the optional AI-written paragraph
is :func:`ai_summary`, which may only use numbers that appear in the report.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Callable, Mapping, Optional, Sequence

from .explain import EPS_MW, eur, kind_of, price

KEY_DECISIONS = 5
SAFETY_LISTED = 6
MIN_PHASE_MWH = 0.25     # a phase smaller than this is noise, not part of the story
FALLBACK_WORDS = {
    "timeout": "the AI took too long", "llm_error": "the AI was not reachable",
    "feature_error": "site data was missing", "prompt_error": "site data was missing",
    "approval_timeout": "no operator answer in time",
}
QUIET_AUDIT = {"sim_speed"}  # pressing play is not an operator decision


def _end(time: str, minutes: int) -> str:
    h, m = (int(x) for x in time.split(":"))
    t = h * 60 + m + minutes
    return "24:00" if t >= 24 * 60 else f"{t // 60:02d}:{t % 60:02d}"


def _sp(iv: Mapping[str, Any]) -> float:
    return float(iv["guard"]["battery_setpoint_mw"])


def _worth(iv: Mapping[str, Any], interval_h: float) -> float:
    """What the battery's energy in this interval was worth: + delivered, − taken in."""
    return -_sp(iv) * interval_h * iv["price_eur_mwh"]


def phases(intervals: Sequence[Mapping[str, Any]], interval_h: float) -> list[dict[str, Any]]:
    """Charging and discharging stretches, with the energy, its average price and its value.
    One idle interval inside a stretch does not end it."""
    runs: list[dict[str, Any]] = []
    for i, iv in enumerate(intervals):
        k = kind_of(_sp(iv))
        if k == "hold":
            continue
        last = runs[-1] if runs else None
        if last and last["kind"] == k and i - last["last"] <= 2:
            last["items"].append(iv)
            last["last"] = i
        else:
            runs.append({"kind": k, "items": [iv], "last": i})
    out = []
    step = round(interval_h * 60)
    for r in runs:
        items = r["items"]
        energy = sum(abs(_sp(iv)) * interval_h for iv in items)
        if energy < MIN_PHASE_MWH:
            continue
        avg = sum(abs(_sp(iv)) * interval_h * iv["price_eur_mwh"] for iv in items) / energy
        value = sum(_worth(iv, interval_h) for iv in items)
        own = 0.0
        for iv in items:
            net = iv["production_mw"] - iv["demand_mw"]
            sp = abs(_sp(iv))
            own += min(sp, max(0.0, net if r["kind"] == "charge" else -net)) * interval_h
        start, end = items[0]["time"], _end(items[-1]["time"], step)
        if r["kind"] == "charge":
            where = "mostly the site's own surplus" if own >= energy / 2 else "mostly bought from the grid"
            text = f"{start}–{end}: stored {energy:.1f} MWh at {price(avg)} on average ({where})"
        else:
            where = "mostly the site's own demand" if own >= energy / 2 else "mostly sold to the grid"
            text = f"{start}–{end}: used {energy:.1f} MWh when power cost {price(avg)} on average ({where})"
        out.append({"kind": r["kind"], "start": start, "end": end, "energy_mwh": round(energy, 2),
                    "avg_price_eur_mwh": round(avg, 2), "value_eur": round(value, 2), "text": text})
    return out


def _weighted_price(intervals: Sequence[Mapping[str, Any]], kind: str, interval_h: float) -> Optional[float]:
    items = [iv for iv in intervals if kind_of(_sp(iv)) == kind]
    energy = sum(abs(_sp(iv)) * interval_h for iv in items)
    if energy < 1e-6:
        return None
    return sum(abs(_sp(iv)) * interval_h * iv["price_eur_mwh"] for iv in items) / energy


def day_report(
    *,
    date: str,
    intervals: Sequence[Mapping[str, Any]],
    total_intervals: int,
    metrics: Mapping[str, Any],
    goals: Sequence[Mapping[str, Any]],
    audit: Sequence[Mapping[str, Any]] = (),
    flags: Sequence[Mapping[str, Any]] = (),
    interval_h: float = 0.25,
) -> dict[str, Any]:
    """The report for the intervals decided so far (``complete`` once all have run)."""
    n = len(intervals)
    profit = float(metrics.get("profit_today_eur") or 0.0)
    trades = sum(_worth(iv, interval_h) for iv in intervals)

    sources = Counter(iv["decision"]["source"] for iv in intervals)
    backup = Counter(FALLBACK_WORDS.get(str(iv["decision"].get("fallback_reason")), "the AI answer was unusable")
                     for iv in intervals if iv["decision"]["source"] == "fallback")
    adjusted = [iv for iv in intervals if (iv.get("explanation") or {}).get("safety") or iv["guard"]["clipped"]]
    dropped = sum(1 for iv in intervals if (iv.get("explanation") or {}).get("ai_note_dropped"))

    best = sorted((iv for iv in intervals if _worth(iv, interval_h) > 0.5),
                  key=lambda iv: -_worth(iv, interval_h))[:KEY_DECISIONS]

    def decision(iv: Mapping[str, Any]) -> dict[str, Any]:
        e = iv.get("explanation") or {}
        return {"time": iv["time"], "setpoint_mw": round(_sp(iv), 2), "price_eur_mwh": iv["price_eur_mwh"],
                "value_eur": round(_worth(iv, interval_h), 2), "source": iv["decision"]["source"],
                "summary": e.get("summary") or iv["decision"].get("explanation_for_operator", ""),
                "facts": list(e.get("facts") or []), "safety": e.get("safety"), "ai_note": e.get("ai_note")}

    ph = phases(intervals, interval_h)
    buy, sell = _weighted_price(intervals, "charge", interval_h), _weighted_price(intervals, "discharge", interval_h)
    stored = sum(p["energy_mwh"] for p in ph if p["kind"] == "charge")
    used = sum(p["energy_mwh"] for p in ph if p["kind"] == "discharge")

    goal_rows = [{"id": g.get("id"), "text": g.get("source_text", ""), "status": g.get("status", "on_track"),
                  "hard": g.get("hard", True), "message": g.get("message", "")}
                 for g in goals if g.get("type") != "max_profit"]
    met = sum(1 for g in goal_rows if g["status"] == "met")
    broken = [g for g in goal_rows if g["status"] in ("missed", "violated")]

    people = [{"sim_time": a.get("sim_time"), "action": a.get("action"), "summary": a.get("summary", "")}
              for a in audit if a.get("action") not in QUIET_AUDIT]

    # -- the paragraph a manager reads --------------------------------------------
    parts = []
    if n == 0:
        parts.append("No decisions yet today.")
    else:
        parts.append(f"The site's result was {eur(profit)}.")
        if buy is not None and sell is not None and stored and used:
            parts.append(f"The battery stored {stored:.1f} MWh at {price(buy)} on average and delivered "
                         f"{used:.1f} MWh when power cost {price(sell)}"
                         + (f": the energy it delivered was worth {eur(trades)} more than the energy it took in."
                            if trades >= 0 else
                            f": the energy it took in cost {eur(-trades)} more than the energy it delivered was worth."))
        who = [f"the AI made {sources.get('llm', 0)} of the {n} decisions"]
        if sources.get("fallback"):
            who.append(f"the backup rules {sources['fallback']}")
        if sources.get("operator"):
            who.append(f"the operator {sources['operator']}")
        parts.append(who[0][0].upper() + ", ".join(who)[1:]
                     + f"; the safety layer adjusted {len(adjusted)}.")
        if goal_rows:
            parts.append(f"{met} of {len(goal_rows)} goals met"
                         + (f"; not kept: {', '.join(repr(g['text']) for g in broken)}." if broken else "."))
    if n < total_intervals:
        parts.append(f"The day is still running ({n} of {total_intervals} intervals so far).")

    return {
        "date": date,
        "complete": n >= total_intervals > 0,
        "intervals": n,
        "total_intervals": total_intervals,
        "through": _end(intervals[-1]["time"], round(interval_h * 60)) if intervals else None,
        "summary": " ".join(parts),
        "money": {"profit_eur": round(profit, 2), "battery_trades_eur": round(trades, 2),
                  "imbalance_eur": round(sum(iv["settlement"]["imbalance_eur"] for iv in intervals), 2)},
        "energy": {"stored_mwh": round(stored, 2), "used_mwh": round(used, 2),
                   "avg_charge_price": None if buy is None else round(buy, 2),
                   "avg_discharge_price": None if sell is None else round(sell, 2),
                   "sold_mwh": round(sum(iv["settlement"]["grid_export_mwh"] for iv in intervals), 2),
                   "bought_mwh": round(sum(iv["settlement"]["grid_import_mwh"] for iv in intervals), 2)},
        "phases": ph,
        "key_decisions": [decision(iv) for iv in best],
        "goals": goal_rows,
        "decisions": {"total": n, "ai": sources.get("llm", 0), "backup": sources.get("fallback", 0),
                      "operator": sources.get("operator", 0), "safety_adjusted": len(adjusted),
                      "backup_reasons": dict(backup), "ai_notes_dropped": dropped},
        "safety": [{"time": iv["time"], "text": (iv.get("explanation") or {}).get("safety")
                    or "The safety layer adjusted the decision.", "rules": list(iv["guard"]["applied_rules"])}
                   for iv in adjusted[:SAFETY_LISTED]],
        "people": people,
        "flags": [{"time": f.get("time"), "reason": f.get("reason", "")} for f in flags],
        "forecast": {"demand_mae_mw": metrics.get("demand_mae_mw"),
                     "production_mae_mw": metrics.get("production_mae_mw")},
    }


# ---------------------------------------------------------------- AI paragraph

SUMMARY_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}

SUMMARY_PROMPT = """You write the closing paragraph of a battery operator's daily report.
Use ONLY the facts below. Every number you write must appear in the facts, written the same way.
Write 3 to 5 plain sentences for a site manager: what the battery achieved, how (when it stored and
used energy), and anything that needs attention (goals not kept, backup rules, safety corrections).
No lists, no headings. Answer as JSON: {"summary": "..."}"""

_NUM = re.compile(r"\d+(?:[.,]\d+)*")


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUM.findall(text)}


def report_facts(report: Mapping[str, Any]) -> str:
    """The report as short fact lines: the only material the AI may use."""
    lines = [report["summary"]]
    lines += [p["text"] for p in report["phases"]]
    lines += [f"{d['time']}: {d['summary']} ({eur(d['value_eur'])})" for d in report["key_decisions"][:3]]
    lines += [f"Goal '{g['text']}': {g['status'].replace('_', ' ')}" for g in report["goals"]]
    lines += [f"Backup rules decided {v} intervals because {k}" for k, v in report["decisions"]["backup_reasons"].items()]
    lines += [f"Operator: {p['summary']}" for p in report["people"][:5]]
    return "\n".join(f"- {line}" for line in lines)


def ai_summary(report: Mapping[str, Any], chat_json: Callable[..., Any]) -> dict[str, Any]:
    """An AI-written paragraph from the report's facts. Falls back to the report's own
    summary when the model fails or writes a number that is not in the facts."""
    facts = report_facts(report)
    try:
        res = chat_json([{"role": "system", "content": SUMMARY_PROMPT},
                         {"role": "user", "content": f"Facts:\n{facts}\n\nJSON:"}],
                        SUMMARY_SCHEMA, tag="day_report")
        text = str(json.loads(res.content).get("summary") or "").strip()
    except Exception as exc:
        return {"summary": report["summary"], "source": "report", "note": f"the AI could not write it ({exc.__class__.__name__})"}
    if not text:
        return {"summary": report["summary"], "source": "report", "note": "the AI returned nothing"}
    invented = _numbers(text) - _numbers(facts)
    if invented:
        return {"summary": report["summary"], "source": "report",
                "note": f"the AI's paragraph used numbers not in the report ({', '.join(sorted(invented)[:3])})"}
    return {"summary": text, "source": "ai", "note": None}
