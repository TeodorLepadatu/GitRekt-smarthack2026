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
import queue
import threading
import time as _time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Optional

from battery import BatteryConfig
from dashboard import day_payload, goals_payload, interval_payload, metrics_payload, state_payload
from goal_parser import GoalStore
from goal_parser.rules import parse_raw
from loop import DayInput, DayRunner, demo_day

OBJECTIVE_ID = "objective"
OBJECTIVE_TEXT = {"max_profit": "Maximise profit", "self_consumption": "Use my own energy first"}


class GoalNotFound(KeyError):
    pass


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
            self._publish()
        self._cond.notify_all()

    def _step_once(self) -> Optional[dict]:
        with self._cond:
            self._apply_pending()
            runner = self._runner
            if runner.done:
                return None
        record = runner.step()                      # outside the lock: the LLM may take seconds
        interval = interval_payload(record)
        with self._cond:
            if runner is not self._runner:          # reset while this interval ran: drop it
                return None
            if runner.done:
                runner.ledger.finalize()
                self._speed = 0.0
            self._publish()
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
        self._publish()
        self._deadline = None

    def _publish(self) -> None:
        """Called with the lock held (or before the worker starts): a fresh snapshot of the runner."""
        r = self._runner
        self._snap = _Snapshot(day=self._day, curve=r.curve, records=list(r.records),
                               metrics=r.ledger.metrics(), goal_summary=r.ledger.goal_summary(),
                               stored_mwh=r.sim.stored_mwh, day_over=r.done)

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
