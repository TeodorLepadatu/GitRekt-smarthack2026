"""The backend's engine: runs the day in the background and publishes what the
dashboard reads.

One worker thread owns the DayRunner and runs intervals at the requested speed.
HTTP handlers never touch the runner: they read snapshots the worker publishes
after every interval (so the dashboard stays responsive while the LLM thinks)
and queue commands (new goals, speed, reset) that the worker applies between
intervals, never in the middle of one.

The shapes come from dashboard/contract.py, the one place the loop's objects
are translated into frontend/src/api/types.ts; this module only adds the
objective entry and the goal commands the frontend's goal list needs.
"""
from __future__ import annotations

import datetime as _dt
import math
import os
import queue
import threading
import time as _time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Optional

from battery import BatteryConfig
from dashboard import day_payload, goals_payload, interval_payload, metrics_payload, state_payload
from feature_builder import build_features
from goal_parser import GoalStore
from goal_parser.rules import parse_raw
from guard import guard
from loop import DayInput, DayRunner, demo_day
from loop.day import operator_decision
from validator import fallback_decision

from .desk import AlertBook, AuditLog, FlagBook, Overrides, alert_conditions, relaxed_sentence

OBJECTIVE_ID = "objective"
OBJECTIVE_TEXT = {"max_profit": "Maximise profit", "self_consumption": "Use my own energy first"}


class GoalNotFound(KeyError):
    pass


class NotFound(KeyError):
    """An alert, interval or override that does not exist (HTTP 404)."""


class Conflict(ValueError):
    """Understood, but it cannot be done now (HTTP 409)."""


@dataclass
class _Snapshot:
    """Everything the read routes need, replaced as a whole after every interval."""
    day: Any
    curve: Any
    records: list[dict] = field(default_factory=list)       # the loop's records, decided so far
    metrics: dict = field(default_factory=dict)             # ledger.metrics()
    goal_summary: list[dict] = field(default_factory=list)  # ledger.goal_summary()
    stored_mwh: float = 0.0
    day_over: bool = False


class Backend:
    def __init__(
        self,
        goal_store: GoalStore,
        *,
        llm_call: Optional[Callable[[str], Any]] = None,
        battery: Optional[BatteryConfig] = None,
        start_soc_mwh: float = 4.0,
        day_factory: Callable[[], DayInput] = demo_day,
        clock: Callable[[], float] = _time.monotonic,
        log_path: Optional[str] = None,
        max_import_mw: Optional[float] = None,
        operator_log_dir: Optional[str] = None,
    ):
        self.goals = goal_store
        self.max_import_mw = max_import_mw  # grid import cap (peak shaving); None = none
        self.llm_call = llm_call
        self.cfg = battery or BatteryConfig()
        self.start_soc_mwh = start_soc_mwh
        self.day_factory = day_factory
        self.clock = clock
        self.log_path = log_path
        self.date = _dt.date.today().isoformat()

        self._cond = threading.Condition()
        self._goal_lock = threading.Lock()     # the goal parser may call an LLM: never under _cond
        self._speed = 0.0
        self._pending_goals = False
        self._pending_reset = False
        self._stopped = False
        self._listeners: set[Callable[[dict], None]] = set()
        self._alert_listeners: set[Callable[[dict], None]] = set()
        self._deciding: Optional[int] = None    # the interval the worker is deciding right now
        # the operator's desk (backend/desk.py); the files live in operator_log_dir
        self.alerts = AlertBook()
        self.overrides = Overrides()
        logs = operator_log_dir
        self.flags = FlagBook(os.path.join(logs, "decision_flags.jsonl") if logs else None)
        self.audit = AuditLog(os.path.join(logs, "audit.jsonl") if logs else None)
        self._new_day()
        self._thread = threading.Thread(target=self._work, name="day-loop", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ reads

    def state(self) -> dict:
        status = self.sim_status()
        return state_payload(self._view(), running=status["running"], speed=status["speed"])

    def day(self) -> dict:
        out = day_payload(self._view())
        out["date"] = self.date
        return out

    def metrics(self) -> dict:
        return metrics_payload(self._view())

    def goal_list(self) -> list[dict]:
        """The active goals with their status: the objective (if the user set one), then the goals."""
        cfg = self.goals.config.to_dict()
        out = []
        if self._objective_sentences() or cfg["objective"] != "max_profit":
            out.append({"id": OBJECTIVE_ID, "type": "max_profit", "hard": False, "status": "on_track",
                        "source_text": OBJECTIVE_TEXT.get(cfg["objective"], cfg["objective"]), "message": ""})
        return out + goals_payload(self._view())

    def alert_list(self) -> list[dict]:
        with self._cond:
            return self.alerts.list()

    def flag_list(self) -> list[dict]:
        with self._cond:
            return self.flags.list()

    def audit_list(self, limit: int = 200) -> list[dict]:
        with self._cond:
            return self.audit.list(limit)

    def upcoming(self, n: int = 8) -> dict:
        """The next ``n`` intervals' planned action, before the AI has seen them.

        The plan is a preview: the backup rules and the guard on the forecast, with
        the stored energy carried from one interval to the next. The AI decides each
        interval when it comes. An operator override replaces the plan (the guard
        still checks it, so hard goals and battery limits still win).
        """
        snap, cfg, goal_config = self._snap, self.cfg, self.goals.config
        curve, day = snap.curve, snap.day
        overrides = self.overrides.all()
        stored = snap.stored_mwh
        start = len(snap.records)
        if self._deciding is not None and self._deciding >= start:
            # being decided right now: it can no longer be changed, so it is not offered
            # (the stored energy is then one interval old: fine for a preview)
            start = self._deciding + 1
        slots = []
        for i in range(start, min(day.n, start + max(0, n))):
            t = curve.time_at(i)
            override = overrides.get(t)
            slot = {"time": t, "price_eur_mwh": round(curve.price_at(i), 2),
                    "stored_before_mwh": round(stored, 3), "override": None}
            try:
                features = build_features(time=t, forecast=day.forecasts[i], prices=curve, stored_mwh=stored,
                                          goal_config=goal_config, battery=cfg)
                planned = fallback_decision(features, "preview")
                g = guard(planned, features, goal_config, cfg, max_import_mw=self.max_import_mw)
                slot["planned"] = {"battery_fraction": planned.battery_fraction,
                                   "setpoint_mw": round(g.battery_setpoint_mw, 3),
                                   # the backup rule's own suffix says the AI was unavailable: not true here
                                   "explanation": planned.explanation_for_operator.split(" (automatic rule")[0]
                                   .rstrip(".") + "."}
                if override is not None:
                    g = guard(operator_decision(override), features, goal_config, cfg,
                              max_import_mw=self.max_import_mw)
                    slot["override"] = {**override, "setpoint_mw": round(g.battery_setpoint_mw, 3),
                                        "limited_by_safety": bool(g.clipped)}
                stored = cfg.energy_after(stored, g.battery_setpoint_mw)
            except Exception as exc:  # a preview is never worth an error
                slot["planned"] = {"battery_fraction": 0.0, "setpoint_mw": 0.0,
                                   "explanation": f"No preview ({exc.__class__.__name__})."}
                if override is not None:
                    slot["override"] = {**override, "setpoint_mw": 0.0, "limited_by_safety": False}
            slots.append(slot)
        return {"now": snap.records[-1]["time"] if snap.records else None, "slots": slots}

    def sim_status(self) -> dict:
        with self._cond:
            return {"running": self._speed > 0 and not self._snap.day_over, "speed": self._speed}

    def _view(self) -> SimpleNamespace:
        """The latest snapshot dressed as a runner, for dashboard/contract.py."""
        snap = self._snap
        return SimpleNamespace(
            cfg=self.cfg, day=snap.day, curve=snap.curve, records=snap.records,
            sim=SimpleNamespace(stored_mwh=snap.stored_mwh),
            ledger=SimpleNamespace(metrics=lambda: snap.metrics, goal_summary=lambda: snap.goal_summary,
                                   profit_eur=snap.metrics.get("profit_today_eur", 0.0)),
            goal_config=self.goals.config,   # the active goals, including ones the worker applies next
        )

    # ---------------------------------------------------------------- commands

    def add_goal(self, text: str) -> dict:
        """Parse and add a goal sentence. If it is unclear, nothing changes and a question comes back."""
        with self._goal_lock:
            result = self.goals.add(text)
        if result.needs_clarification:
            return {"goals": self.goal_list(), "question": result.clarifications[0].question}
        with self._cond:
            self._log("goal_added", f"Added goal '{text}'", {"text": text})
            self._pending_goals = True
            self._cond.notify_all()
        return {"goals": self.goal_list()}

    def delete_goal(self, goal_id: str) -> None:
        """Remove the sentence a goal came from (one sentence can hold several goals)."""
        with self._goal_lock:
            before = list(self.goals.sentences)
            if goal_id == OBJECTIVE_ID:
                drop = {i for i, has_goals in self._objective_sentences() if not has_goals}
                if not drop:              # the objective shares a sentence with other goals
                    raise GoalNotFound(goal_id)
                result = self.goals.set([x for i, x in enumerate(self.goals.sentences) if i not in drop])
            else:
                index = self._sentence_of(goal_id)
                if index is None:
                    raise GoalNotFound(goal_id)
                result = self.goals.remove(index)
        if result.needs_clarification:  # the remaining sentences no longer parse cleanly: nothing changed
            raise GoalNotFound(goal_id)
        removed = [x for x in before if x not in self.goals.sentences]
        with self._cond:
            self._log("goal_removed", f"Removed goal '{'; '.join(removed) or goal_id}'",
                      {"goal_id": goal_id, "removed": removed})
            self._pending_goals = True
            self._cond.notify_all()

    def run(self, speed: float) -> dict:
        """Run at ``speed`` (1 = real time, 600 = 1.5 s per interval); 0 pauses.
        Starting again after the day is over begins a new day."""
        with self._cond:
            speed = max(0.0, float(speed))
            if speed > 0 and self._snap.day_over:
                self._pending_reset = True
            if speed != self._speed:
                self._log("sim_speed", "Paused the simulation" if speed == 0
                          else f"Set the simulation speed to {speed:g}x", {"from": self._speed, "to": speed})
            self._speed = speed
            self._deadline = None
            self._cond.notify_all()
        return self.sim_status()

    def reset(self) -> dict:
        """Pause and start the day again at 00:00."""
        with self._cond:
            self._log("sim_reset", "Restarted the day at 00:00")
            self._speed = 0.0
            self._pending_reset = True
            self._cond.notify_all()
        self.wait_idle()
        return self.sim_status()

    def relax_goal(self, goal_id: str) -> dict:
        """Lower a reserve goal that is behind to what can still be stored by its deadline."""
        with self._cond:
            snap = self._snap
        sim_time = snap.records[-1]["time"] if snap.records else None
        goal = next((g for g in self.goals.config.to_dict()["goals"] if g["id"] == goal_id), None)
        if goal is None:
            raise GoalNotFound(goal_id)
        if goal.get("type") != "reserve" or goal.get("mwh") is None:
            raise Conflict("only an energy reserve goal can be relaxed")
        state = next((g for g in snap.goal_summary if str(g.get("id")) == goal_id), {})
        target = float(goal["mwh"])
        hours = state.get("hours_left")
        if hours is None:
            hours = self._hours_until(goal.get("by"), sim_time)
        usable_h = max(0.0, float(hours) - self.cfg.goal_buffer_min / 60)
        reachable = min(self.cfg.e_max, snap.stored_mwh + self.cfg.p_max_mw * self.cfg.eta_c * usable_h)
        new = min(math.floor(reachable * 2) / 2, target - 0.5)   # in steps of 0.5 MWh, below the target
        if new < 0.5:
            raise Conflict("there is not enough time left to store a useful amount; remove the goal instead")
        index = self._sentence_of(goal_id)
        if index is None:
            raise GoalNotFound(goal_id)
        old = self.goals.sentences[index]
        sentence = relaxed_sentence(old, new)
        if sentence is None:
            raise Conflict(f"could not find the amount in '{old}'")
        with self._goal_lock:
            result = self.goals.set([sentence if i == index else x for i, x in enumerate(self.goals.sentences)])
        if result.needs_clarification:
            raise Conflict(f"'{sentence}' could not be read back as a goal")
        with self._cond:
            for a in self.alerts.list():                         # the alert that asked for it is handled
                if a["goal_id"] == goal_id:
                    self.alerts.ignore(a["id"])
            self._log("goal_relaxed", f"Relaxed '{old}' to '{sentence}'",
                      {"goal_id": goal_id, "from": old, "to": sentence, "from_mwh": target, "to_mwh": new})
            self._pending_goals = True
            self._cond.notify_all()
        return {"goals": self.goal_list(), "message": f"Now: {sentence}."}

    def ignore_alert(self, alert_id: str) -> None:
        with self._cond:
            alert = self.alerts.ignore(alert_id)
            if alert is None:
                raise NotFound(alert_id)
            self._log("alert_ignored", f"Ignored alert: {alert['title']}",
                      {"alert_id": alert_id, "kind": alert["kind"]})

    def set_override(self, time: str, battery_fraction: float, note: str = "") -> dict:
        """Set the action for an interval that has not run yet. 0 = hold (skip the planned action)."""
        with self._cond:
            index = self._future_index(time)
            ahead = index - len(self._snap.records) + 1
            planned = next((s.get("planned") for s in self.upcoming(ahead)["slots"] if s["time"] == time), None)
            o = self.overrides.set(time, battery_fraction, note.strip())
            f = o["battery_fraction"]
            what = "hold" if f == 0 else f"{'charge' if f > 0 else 'discharge'} at {abs(f):.0%}"
            self._log("override_set", f"Set {time} to {what}" + (f" ({o['note']})" if o["note"] else ""),
                      {"time": time, "battery_fraction": f, "note": o["note"], "planned": planned})
        return o

    def clear_override(self, time: str) -> None:
        with self._cond:
            self._future_index(time)
            if self.overrides.remove(time) is None:
                raise NotFound(time)
            self._log("override_cleared", f"Gave {time} back to the AI", {"time": time})

    def flag_decision(self, time: str, reason: str) -> dict:
        with self._cond:
            record = next((r for r in self._snap.records if r.get("time") == time), None)
            if record is None:
                raise NotFound(time)
            flag = self.flags.flag(self.date, record, interval_payload(record), reason)
            self._log("decision_flagged", f"Flagged the {time} decision: {reason or 'no reason given'}",
                      {"time": time, "reason": reason})
        return flag

    def unflag_decision(self, time: str) -> None:
        with self._cond:
            if self.flags.unflag(self.date, time) is None:
                raise NotFound(time)
            self._log("decision_unflagged", f"Removed the flag on {time}", {"time": time})

    def add_alert_listener(self, fn: Callable[[dict], None]) -> None:
        """Call ``fn(alert)`` for every alert raised (from the worker thread: keep it quick)."""
        with self._cond:
            self._alert_listeners.add(fn)

    def remove_alert_listener(self, fn: Callable[[dict], None]) -> None:
        with self._cond:
            self._alert_listeners.discard(fn)

    def advance(self, n: int = 1) -> list[dict]:
        """Run ``n`` intervals right now (paused only). Used to warm up a demo day and by tests."""
        with self._cond:
            if self._speed > 0:
                raise RuntimeError("pause the simulation before advancing it by hand")
        out = []
        for _ in range(n):
            iv = self._step_once()
            if iv is None:
                break
            out.append(iv)
        return out

    def add_listener(self, fn: Callable[[dict], None]) -> None:
        """Call ``fn(interval)`` for every new interval (from the worker thread: keep it quick)."""
        with self._cond:
            self._listeners.add(fn)

    def remove_listener(self, fn: Callable[[dict], None]) -> None:
        with self._cond:
            self._listeners.discard(fn)

    def subscribe(self, heartbeat_s: float = 15.0) -> Iterator[Optional[dict]]:
        """Every new interval as it is decided; None every ``heartbeat_s`` with nothing new."""
        q: queue.Queue = queue.Queue(maxsize=200)

        def put(interval: dict) -> None:
            try:
                q.put_nowait(interval)
            except queue.Full:                      # a stalled reader must not block the site
                pass

        self.add_listener(put)
        try:
            while not self._stopped:
                try:
                    yield q.get(timeout=heartbeat_s)
                except queue.Empty:
                    yield None
        finally:
            self.remove_listener(put)

    def wait_idle(self, timeout: float = 5.0) -> None:
        """Wait until the worker has applied pending commands (reset, goals)."""
        end = _time.monotonic() + timeout
        with self._cond:
            while (self._pending_reset or self._pending_goals) and _time.monotonic() < end:
                self._cond.wait(0.01)

    def close(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
        self._thread.join(timeout=2)

    # ------------------------------------------------------------------ worker

    def _work(self) -> None:
        while True:
            with self._cond:
                if self._stopped:
                    return
                self._apply_pending()
                if self._speed <= 0 or self._snap.day_over:
                    self._cond.wait()
                    continue
                slot = self.cfg.interval_h * 3600 / self._speed
                now = self.clock()
                if self._deadline is None:
                    self._deadline = now + slot
                if now < self._deadline:
                    self._cond.wait(self._deadline - now)
                    continue
            self._step_once()
            with self._cond:
                if self._deadline is not None:
                    # fixed deadlines: no drift; if an interval overran its slot, start the next at once
                    self._deadline = max(self._deadline + slot, self.clock())

    def _apply_pending(self) -> None:
        """Called with the lock held, between intervals."""
        if self._pending_reset:
            self._new_day()
            self._pending_reset = False
        if self._pending_goals:
            self._runner.set_goals(self.goals.config)
            self._pending_goals = False
            self._publish()
        self._cond.notify_all()

    def _step_once(self) -> Optional[dict]:
        with self._cond:
            self._apply_pending()
            runner = self._runner
            if runner.done:
                return None
            self._deciding = runner.index           # too late to override this one: the loop asks now
        try:
            record = runner.step()                  # outside the lock: the LLM may take seconds
        finally:
            with self._cond:
                self._deciding = None
        interval = interval_payload(record)
        with self._cond:
            if runner is not self._runner:          # reset while this interval ran: drop it
                return None
            if runner.done:
                runner.ledger.finalize()
                self._speed = 0.0
            self._publish()
            raised = self._refresh_alerts()
            calls = [(fn, interval) for fn in self._listeners]
            calls += [(fn, alert) for alert in raised for fn in self._alert_listeners]
            self._cond.notify_all()
        for fn, payload in calls:
            try:
                fn(payload)
            except Exception:                       # a broken listener must not stop the site
                pass
        return interval

    def _new_day(self) -> None:
        self._day = self.day_factory()
        self.overrides.clear()          # overrides, alerts and flags belong to one day; the audit stays
        self.alerts.clear()
        self.flags.clear()
        self._runner = DayRunner(self._day, self.goals.config, start_soc_mwh=self.start_soc_mwh,
                                 battery=self.cfg, llm_call=self.llm_call, log_path=self.log_path,
                                 max_import_mw=self.max_import_mw, override_for=self.overrides.get)
        self._publish()
        self._deadline = None

    def _publish(self) -> None:
        """Called with the lock held (or before the worker starts): a fresh snapshot of the runner."""
        r = self._runner
        self._snap = _Snapshot(day=self._day, curve=r.curve, records=list(r.records),
                               metrics=r.ledger.metrics(), goal_summary=r.ledger.goal_summary(),
                               stored_mwh=r.sim.stored_mwh, day_over=r.done)

    def _refresh_alerts(self) -> list[dict]:
        """Called with the lock held, after an interval: the alerts raised just now."""
        snap = self._snap
        try:
            conditions = alert_conditions(
                snap.records, snap.goal_summary, self.goals.config.to_dict().get("goals") or [],
                list(snap.curve.prices), [snap.curve.time_at(i) for i in range(snap.day.n)])
        except Exception:                           # alerts are help, never a reason to stop
            return []
        return self.alerts.update(conditions, snap.records[-1]["time"] if snap.records else None)

    def _log(self, action: str, summary: str, detail: Optional[dict] = None) -> dict:
        """Audit an operator action. Called with the lock held."""
        snap = self._snap
        return self.audit.add(action, summary, sim_time=snap.records[-1]["time"] if snap.records else None,
                              date=self.date, detail=detail)

    def _future_index(self, time: str) -> int:
        """The index of an interval that has not been decided yet."""
        try:
            index = self._runner._index_of(time)
        except Exception:
            raise NotFound(time)
        if index >= self._snap.day.n:
            raise NotFound(time)
        if index < len(self._snap.records):
            raise Conflict(f"{time} has already been decided")
        if self._deciding is not None and index <= self._deciding:
            raise Conflict(f"{time} is being decided right now")
        return index

    @staticmethod
    def _hours_until(by: Optional[str], now: Optional[str]) -> float:
        def minutes(t: str) -> int:
            h, m = t.split(":")
            return int(h) * 60 + int(m)
        if not by:
            return 0.0
        start = minutes(now) + 15 if now else 0
        return max(0.0, (minutes(by) - start) / 60)

    def _objective_sentences(self) -> list[tuple[int, bool]]:
        """(index, also_has_goals) of every sentence that states an objective."""
        out = []
        for i, sentence in enumerate(self.goals.sentences):
            raw = parse_raw(sentence)
            if raw["objective"] is not None:
                out.append((i, any(g.get("type") in ("reserve", "min_sell_price") for g in raw["goals"])))
        return out

    def _sentence_of(self, goal_id: str) -> Optional[int]:
        """The sentence a goal came from, found by the goal's own words."""
        goal = next((g for g in self.goals.config.to_dict()["goals"] if g["id"] == goal_id), None)
        if goal is None:
            return None
        src = goal.get("source_text", "").strip().lower()
        for i, sentence in enumerate(self.goals.sentences):
            if src and src in sentence.lower():
                return i
        return None
