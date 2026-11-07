"""The operator's desk: what a human needs to step in, and the record of when they did.

    AlertBook    which conditions need the operator's attention right now
    Overrides    actions the operator set for upcoming intervals (the loop asks for them)
    FlagBook     decisions the operator disagreed with, and why (kept for prompt examples)
    AuditLog     every operator action, with a timestamp

Alerts are recomputed from the latest snapshot after every interval. An alert
stays up while its condition holds and goes away by itself when it clears;
"ignore" hides it until the condition clears and comes back. Nothing here talks
to the loop directly: the backend calls these with its own lock held.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import os
import re
import statistics
import threading
from typing import Any, Mapping, Optional

log = logging.getLogger(__name__)

FALLBACK_STREAK = 3          # this many backup-rule decisions in a row raise an alert
SPIKE_LOOKAHEAD = 8          # intervals (2 h) scanned for a price spike
SPIKE_FACTOR = 1.5           # a spike is >= 1.5 x the day's median price ...
SPIKE_MIN_JUMP_EUR = 50.0    # ... and at least 50 EUR/MWh above it
DEMAND_ERROR_MIN_MW = 0.5    # demand off by more than this ...
DEMAND_ERROR_SHARE = 0.25    # ... and by more than 25 % of the forecast

ACTIONS = ("override", "relax_goal", "ignore")
MAX_AUDIT = 500


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


# --------------------------------------------------------------------------- #
# alerts


def alert_conditions(
    records: list[Mapping[str, Any]],
    goal_summary: list[Mapping[str, Any]],
    goal_defs: list[Mapping[str, Any]],
    prices: list[float],
    times: list[str],
) -> dict[str, dict[str, Any]]:
    """Every condition that holds after the latest interval, keyed by what it is about.

    The key is stable while the condition lasts, so the same problem is one
    alert, not one per interval.
    """
    out: dict[str, dict[str, Any]] = {}
    defs = {str(g.get("id")): g for g in goal_defs if isinstance(g, Mapping)}

    # goals behind or unreachable (only goals that are still active)
    for st in goal_summary:
        gid, status = str(st.get("id")), st.get("status")
        if gid not in defs or status not in ("behind", "unreachable"):
            continue
        g = defs[gid]
        name = g.get("source_text") or f"goal {gid}"
        unreachable = status == "unreachable"
        out[f"goal:{gid}:{status}"] = {
            "kind": f"goal_{status}",
            "severity": "critical" if unreachable else "warning",
            "title": f"'{name}' can no longer be met" if unreachable else f"'{name}' is behind",
            "message": st.get("message") or ("Not enough time left to store the target."
                                             if unreachable else "Charging is behind schedule."),
            "goal_id": gid,
            "actions": ["override", "relax_goal", "ignore"] if g.get("type") == "reserve" else ["override", "ignore"],
        }

    # the AI keeps failing
    streak = 0
    for r in reversed(records):
        if (r.get("decision") or {}).get("source") != "fallback":
            break
        streak += 1
    if streak >= FALLBACK_STREAK:
        reason = (records[-1].get("decision") or {}).get("fallback_reason") or "unknown"
        out["fallback_streak"] = {
            "kind": "fallback_streak", "severity": "warning",
            "title": f"The AI failed {streak} times in a row",
            "message": f"The backup rules have decided since {records[-streak].get('time')} "
                       f"(last reason: {reason}). Check the model, or set the next intervals yourself.",
            "goal_id": None, "actions": ["override", "ignore"],
        }

    # a price spike in the next two hours
    n = len(records)
    if prices and n < len(prices):
        median = statistics.median(prices)
        threshold = max(median * SPIKE_FACTOR, median + SPIKE_MIN_JUMP_EUR)
        ahead = [(times[i], prices[i]) for i in range(n, min(len(prices), n + SPIKE_LOOKAHEAD))]
        spikes = [(t, p) for t, p in ahead if p >= threshold]
        if spikes:
            t_peak, p_peak = max(spikes, key=lambda x: x[1])
            # one key for the whole episode: the alert stays one alert while the window slides through the peak
            out["price_spike"] = {
                "kind": "price_spike", "severity": "info",
                "title": f"Price spike from {spikes[0][0]}",
                "message": f"Up to €{p_peak:.0f}/MWh at {t_peak}, against a day median of €{median:.0f}. "
                           "Make sure there is energy stored to sell.",
                "goal_id": None, "actions": ["override", "ignore"],
            }

    # the latest interval came in far from its forecast
    if records:
        last = records[-1]
        fc = last.get("forecast") or {}
        feats = last.get("features") or {}
        parts = []
        if fc.get("production_in_range") is False:
            low, high = _num(feats.get("prod_low_mw")), _num(feats.get("prod_high_mw"))
            band = f" (band {low:.2f}–{high:.2f} MW)" if low is not None and high is not None else ""
            parts.append(f"production {_num(last.get('production_mw')) or 0:.2f} MW, "
                         f"forecast {_num(feats.get('production_mw')) or 0:.2f} MW{band}")
        dem_err, f_dem = _num(fc.get("demand_error_mw")), _num(feats.get("demand_mw"))
        if dem_err is not None and abs(dem_err) > max(DEMAND_ERROR_MIN_MW, DEMAND_ERROR_SHARE * (f_dem or 0)):
            parts.append(f"demand {_num(last.get('demand_mw')) or 0:.2f} MW, forecast {f_dem or 0:.2f} MW")
        if parts:
            out["forecast_error"] = {
                "kind": "forecast_error", "severity": "warning",
                "title": f"Forecast off at {last.get('time')}",
                "message": "; ".join(parts).capitalize() + ". Later decisions rely on the same forecast.",
                "goal_id": None, "actions": ["override", "ignore"],
            }
    return out


class AlertBook:
    def __init__(self) -> None:
        self._active: dict[str, dict[str, Any]] = {}   # key -> alert
        self._ignored: set[str] = set()                # keys hidden until their condition clears
        self._next = 1

    def update(self, conditions: Mapping[str, Mapping[str, Any]], sim_time: Optional[str]) -> list[dict]:
        """Apply this interval's conditions. Returns the alerts raised just now."""
        raised = []
        for key in list(self._active):
            if key not in conditions:
                del self._active[key]                   # resolved by itself
        self._ignored &= set(conditions)                # cleared: may come back next time
        for key, cond in conditions.items():
            if key in self._ignored:
                continue
            if key in self._active:                     # still on: keep id and time, refresh the text
                self._active[key].update(title=cond["title"], message=cond["message"])
                continue
            alert = {"id": f"a{self._next}", "key": key, "time": sim_time, "raised_at": now_iso(), **cond}
            self._next += 1
            self._active[key] = alert
            raised.append(public_alert(alert))
        return raised

    def list(self) -> list[dict]:
        order = {"critical": 0, "warning": 1, "info": 2}
        return [public_alert(a) for a in sorted(self._active.values(), key=lambda a: (order[a["severity"]], a["id"]))]

    def get(self, alert_id: str) -> Optional[dict]:
        return next((a for a in self._active.values() if a["id"] == alert_id), None)

    def ignore(self, alert_id: str) -> Optional[dict]:
        alert = self.get(alert_id)
        if alert is not None:
            self._ignored.add(alert["key"])
            del self._active[alert["key"]]
        return alert

    def clear(self) -> None:
        self._active.clear()
        self._ignored.clear()


def public_alert(a: Mapping[str, Any]) -> dict:
    return {k: a[k] for k in ("id", "kind", "severity", "title", "message", "time", "raised_at",
                              "goal_id", "actions")}


# --------------------------------------------------------------------------- #
# overrides


class Overrides:
    """time -> the operator's action for that interval. Read by the loop's worker thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_time: dict[str, dict[str, Any]] = {}

    def get(self, time: str) -> Optional[dict]:
        with self._lock:
            o = self._by_time.get(time)
            return None if o is None else dict(o)

    def set(self, time: str, fraction: float, note: str) -> dict:
        o = {"time": time, "battery_fraction": round(max(-1.0, min(1.0, fraction)), 4),
             "note": note, "set_at": now_iso()}
        with self._lock:
            self._by_time[time] = o
        return dict(o)

    def remove(self, time: str) -> Optional[dict]:
        with self._lock:
            return self._by_time.pop(time, None)

    def all(self) -> dict[str, dict]:
        with self._lock:
            return {t: dict(o) for t, o in self._by_time.items()}

    def clear(self) -> None:
        with self._lock:
            self._by_time.clear()


# --------------------------------------------------------------------------- #
# append-only JSONL files


class _Jsonl:
    def __init__(self, path: Optional[str]) -> None:
        self.path = path

    def append(self, entry: Mapping[str, Any]) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:  # a log that cannot be written must not stop the site
            log.warning("could not write %s: %s", self.path, exc)

    def read(self, last: int) -> list[dict]:
        if not self.path or not os.path.exists(self.path):
            return []
        out = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        return out[-last:]


class FlagBook:
    """Decisions the operator flagged as wrong. The file keeps the whole decision, so it can
    later be turned into a counter-example for the prompt."""

    def __init__(self, path: Optional[str] = None) -> None:
        self._file = _Jsonl(path)
        self._by_time: dict[str, dict[str, Any]] = {}

    def flag(self, date: str, record: Mapping[str, Any], interval: Mapping[str, Any], reason: str) -> dict:
        time = str(interval.get("time"))
        flag = {"time": time, "reason": reason, "flagged_at": now_iso()}
        self._by_time[time] = flag
        feats = record.get("features") or {}
        self._file.append({
            "kind": "flag", "date": date, **flag,
            "decision": interval.get("decision"), "guard": interval.get("guard"),
            "situation": {k: feats.get(k) for k in ("price_now", "break_even", "stored_mwh", "demand_mw",
                                                     "production_mw", "net_mw") if k in feats},
            "profit_eur": (interval.get("settlement") or {}).get("profit_eur"),
        })
        return dict(flag)

    def unflag(self, date: str, time: str) -> Optional[dict]:
        flag = self._by_time.pop(time, None)
        if flag is not None:
            self._file.append({"kind": "unflag", "date": date, "time": time, "at": now_iso()})
        return flag

    def list(self) -> list[dict]:
        return [dict(f) for _, f in sorted(self._by_time.items())]

    def clear(self) -> None:
        self._by_time.clear()


class AuditLog:
    """Every operator action. Kept across day resets; the file keeps everything."""

    def __init__(self, path: Optional[str] = None) -> None:
        self._file = _Jsonl(path)
        self._entries: list[dict] = self._file.read(MAX_AUDIT)
        ids = [int(e["id"][1:]) for e in self._entries if re.fullmatch(r"e\d+", str(e.get("id", "")))]
        self._next = max(ids, default=0) + 1

    def add(self, action: str, summary: str, *, sim_time: Optional[str], date: str,
            detail: Optional[Mapping[str, Any]] = None) -> dict:
        entry = {"id": f"e{self._next}", "at": now_iso(), "date": date, "sim_time": sim_time,
                 "action": action, "summary": summary, "detail": dict(detail or {})}
        self._next += 1
        self._entries.append(entry)
        del self._entries[:-MAX_AUDIT]
        self._file.append(entry)
        return dict(entry)

    def list(self, limit: int = 200) -> list[dict]:
        return [dict(e) for e in reversed(self._entries[-limit:])]


def relaxed_sentence(sentence: str, new_mwh: float) -> Optional[str]:
    """The goal sentence with its first MWh amount replaced, or None if it has none."""
    amount = f"{new_mwh:g}"
    new, n = re.subn(r"\d+(?:[.,]\d+)?(?=\s*MWh)", amount, sentence, count=1, flags=re.I)
    return new if n else None
