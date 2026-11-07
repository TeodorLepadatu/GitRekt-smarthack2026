"""The backend's engine: runs the day in the background and publishes what the
dashboard reads.

One worker thread owns the DayRunner and runs intervals at the requested speed.
HTTP handlers never touch the runner: they read snapshots the worker publishes
after every interval (so the dashboard stays responsive while the LLM thinks)
and queue commands (new goals, speed, reset) that the worker applies between
intervals, never in the middle of one.

Shapes follow frontend/src/api/types.ts.
"""
from __future__ import annotations

import datetime as _dt
import queue
import threading
import time as _time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

from battery import BatteryConfig
from feature_builder import build_features
from goal_parser import GoalStore
from goal_parser.rules import parse_raw
from loop import DayInput, DayRunner, demo_day

INTERVALS = 96
OBJECTIVE_ID = "objective"
OBJECTIVE_TEXT = {"max_profit": "Maximise profit", "self_consumption": "Use my own energy first"}


class GoalNotFound(KeyError):
    pass


@dataclass
class _Snapshot:
    """Everything the read routes need, replaced as a whole after every interval."""
    intervals: list[dict] = field(default_factory=list)      # decided intervals, API shape
    metrics: dict = field(default_factory=dict)
    goal_states: dict = field(default_factory=dict)          # id -> settlement GoalState dict
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
    ):
        self.goals = goal_store
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
        self._new_day()
        self._thread = threading.Thread(target=self._work, name="day-loop", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ reads

    def state(self) -> dict:
        snap = self._snap
        return {
            "capacity_mwh": self.cfg.capacity_mwh,
            "max_power_mw": self.cfg.p_max_mw,
            "profit_today_eur": snap.metrics.get("profit_today_eur", 0.0),
            "current": snap.intervals[-1] if snap.intervals else None,
            "sim": self.sim_status(),
        }

    def day(self) -> dict:
        snap = self._snap
        decided = snap.intervals
        future = [self._forecast_slot(i, snap.stored_mwh) for i in range(len(decided), INTERVALS)]
        return {
            "date": self.date,
            "now": decided[-1]["time"] if decided else None,
            "intervals": decided + future,
        }

    def metrics(self) -> dict:
        m = self._snap.metrics
        keys = ("profit_today_eur", "energy_cost_eur", "wear_eur", "no_battery_profit_eur", "value_added_eur",
                "energy_sold_mwh", "energy_bought_mwh", "curtailed_mwh", "decisions_total", "decisions_llm",
                "decisions_fallback", "decisions_clipped", "goals_met", "goals_total")
        return {k: m.get(k, 0) for k in keys}

    def goal_list(self) -> list[dict]:
        """The active goals with their status: the objective first, then the hard goals."""
        snap = self._snap
        cfg = self.goals.config.to_dict()
        out = []
        if self._objective_sentences() or cfg["objective"] != "max_profit":   # only an objective the user set
            out.append({"id": OBJECTIVE_ID, "type": "max_profit", "hard": False, "status": "on_track",
                        "source_text": OBJECTIVE_TEXT.get(cfg["objective"], cfg["objective"])})
        for g in cfg["goals"]:
            st = snap.goal_states.get(g["id"], {})
            item = {"id": g["id"], "type": g["type"], "hard": g.get("hard", True),
                    "source_text": g.get("source_text", ""), "status": st.get("status", "on_track")}
            if g["type"] == "reserve":
                item.update(mwh=g["mwh"], by=g["by"], until=g["until"], stored_mwh=round(snap.stored_mwh, 2),
                            need_mwh=st.get("need_mwh"), hours_left=st.get("hours_left"),
                            reached_at=st.get("reached_at"), violated_at=st.get("violated_at"),
                            message=st.get("message"))
            else:
                item.update(eur_per_mwh=g["eur_per_mwh"], violated_at=st.get("violated_at"),
                            message=st.get("message"))
            out.append(item)
        return out

    def sim_status(self) -> dict:
        with self._cond:
            return {"running": self._speed > 0 and not self._snap.day_over, "speed": self._speed}

    # ---------------------------------------------------------------- commands

    def add_goal(self, text: str) -> dict:
        """Parse and add a goal sentence. If it is unclear, nothing changes and a question comes back."""
        with self._goal_lock:
            result = self.goals.add(text)
        if result.needs_clarification:
            return {"goals": self.goal_list(), "question": result.clarifications[0].question}
        with self._cond:
            self._pending_goals = True
            self._cond.notify_all()
        return {"goals": self.goal_list()}

    def delete_goal(self, goal_id: str) -> None:
        """Remove the sentence a goal came from (one sentence can hold several goals)."""
        with self._goal_lock:
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
        with self._cond:
            self._pending_goals = True
            self._cond.notify_all()

    def run(self, speed: float) -> dict:
        """Run at ``speed`` (1 = real time, 600 = 1.5 s per interval); 0 pauses.
        Starting again after the day is over begins a new day."""
        with self._cond:
            speed = max(0.0, float(speed))
            if speed > 0 and self._snap.day_over:
                self._pending_reset = True
            self._speed = speed
            self._deadline = None
            self._cond.notify_all()
        return self.sim_status()

    def reset(self) -> dict:
        """Pause and start the day again at 00:00."""
        with self._cond:
            self._speed = 0.0
            self._pending_reset = True
            self._cond.notify_all()
        self.wait_idle()
        return self.sim_status()

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
            self._publish_goals()
        self._cond.notify_all()

    def _step_once(self) -> Optional[dict]:
        with self._cond:
            self._apply_pending()
            runner = self._runner
            if runner.done:
                return None
        record = runner.step()                      # outside the lock: the LLM may take seconds
        interval = to_interval(record)
        with self._cond:
            if runner is not self._runner:          # reset while this interval ran: drop it
                return None
            snap = self._snap
            self._snap = _Snapshot(
                intervals=[*snap.intervals, interval],
                metrics=runner.ledger.metrics(),
                goal_states=_goal_states(runner),
                stored_mwh=runner.sim.stored_mwh,
                day_over=runner.done,
            )
            if runner.done:
                runner.ledger.finalize()
                self._snap.goal_states = _goal_states(runner)
                self._speed = 0.0
            listeners = list(self._listeners)
            self._cond.notify_all()
        for fn in listeners:
            try:
                fn(interval)
            except Exception:                       # a broken listener must not stop the site
                pass
        return interval

    def _new_day(self) -> None:
        self._day = self.day_factory()
        self._runner = DayRunner(self._day, self.goals.config, start_soc_mwh=self.start_soc_mwh,
                                 battery=self.cfg, llm_call=self.llm_call, log_path=self.log_path)
        self._snap = _Snapshot(metrics=self._runner.ledger.metrics(), stored_mwh=self.start_soc_mwh)
        self._deadline = None

    def _publish_goals(self) -> None:
        self._snap = _Snapshot(intervals=self._snap.intervals, metrics=self._runner.ledger.metrics(),
                               goal_states=_goal_states(self._runner), stored_mwh=self._snap.stored_mwh,
                               day_over=self._snap.day_over)

    def _forecast_slot(self, i: int, stored_mwh: float) -> dict:
        f = self._day.forecasts[i]
        get = (lambda k: f.get(k)) if isinstance(f, dict) else (lambda k: getattr(f, k, None))
        time = self._runner.curve.time_at(i)
        try:
            features = build_features(time=time, forecast=f, prices=self._runner.curve,
                                      stored_mwh=stored_mwh, battery=self.cfg)
            break_even = features["break_even"]
        except Exception:
            break_even = None
        return {"time": time, "price_eur_mwh": self._day.prices[i], "break_even": break_even,
                "demand_mw": get("demand_mw"), "production_mw": get("production_mw"),
                "solar_mw": get("solar_mw") or 0.0, "wind_mw": get("wind_mw") or 0.0}

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


# ---------------------------------------------------------------------- shapes

def to_interval(rec: dict) -> dict:
    """A loop record in the dashboard's Interval shape (frontend/src/api/types.ts)."""
    f, site, d, g = rec["features"], rec["site"], rec["decision"], rec["guard"]
    return {
        "time": rec["time"],
        "price_eur_mwh": rec["price_eur_mwh"],
        "break_even": f.get("break_even"),
        "demand_mw": rec["demand_mw"],                 # actual
        "production_mw": rec["production_mw"],         # actual, before curtailment
        **_split_production(rec["production_mw"], f.get("solar_mw"), f.get("wind_mw")),
        "soc_mwh": rec["soc_after_mwh"],               # at the END of the interval
        "decision": {
            "battery_fraction": d.get("battery_fraction"),
            "curtail": bool(d.get("curtail")),
            "source": d.get("source"),
            "confidence": d.get("confidence") or "low",
            "reasoning": d.get("reasoning") or "",
            "explanation_for_operator": d.get("explanation_for_operator") or "",
            "fallback_reason": d.get("fallback_reason"),
        },
        "guard": {
            "battery_setpoint_mw": site["setpoint_applied_mw"],   # what the battery actually ran
            "curtail_mw": site["curtailed_mw"],                   # what was actually switched off
            "clipped": bool(g.get("clipped")) or bool(site["clipped"]),
            "applied_rules": list(g.get("applied_rules") or []),
        },
        "settlement": {k: rec[k] for k in ("grid_import_mwh", "grid_export_mwh", "energy_cost_eur", "wear_eur",
                                           "profit_eur", "no_battery_profit_eur", "value_added_eur")},
    }


def _split_production(actual_mw: float, solar_forecast: Any, wind_forecast: Any) -> dict:
    """Solar and wind for the dashboard: the forecast's split, scaled so they add up to the
    ACTUAL production (otherwise the energy-flow diagram does not balance)."""
    solar, wind = max(0.0, solar_forecast or 0.0), max(0.0, wind_forecast or 0.0)
    total = solar + wind
    if total <= 0:
        return {"solar_mw": 0.0, "wind_mw": round(actual_mw, 4)} if actual_mw > 0 else {"solar_mw": 0.0, "wind_mw": 0.0}
    k = actual_mw / total
    return {"solar_mw": round(solar * k, 4), "wind_mw": round(actual_mw - round(solar * k, 4), 4)}


def _goal_states(runner: DayRunner) -> dict:
    return {gid: st.to_dict() for gid, st in runner.ledger.goals.states.items()}
