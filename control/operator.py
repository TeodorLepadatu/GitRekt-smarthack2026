"""What the operator at the dashboard can tell the site, on top of the AI.

    override        run this setpoint until HH:MM; the guard still checks it
    emergency hold  battery idle and nothing exported, until released; the AI is not asked
    autonomy        how far the AI is trusted:
                      autonomous  its decisions run as they are
                      supervised  big or unsure moves wait for the operator's click
                      advisory    every move waits for the operator's click

Two threads meet here. The loop asks ``command_for(time)`` at the start of each
interval and, when the autonomy policy says so, blocks in ``review(...)`` until
the operator answers, the timeout runs out, or an override or hold replaces the
proposal. The dashboard's HTTP handlers call the setters. Every change is
announced through ``on_change`` (outside the lock) so the dashboard learns at
once that a proposal is waiting.

This module only holds state and makes the wait possible. What a command does
to the battery is decided in ``loop/day.py``, where the guard is.
"""
from __future__ import annotations

import math
import re
import threading
import time as _time
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable, Optional

MODES = ("autonomous", "supervised", "advisory")
CONFIDENCES = ("low", "medium", "high")

# review() verdicts
APPROVED = "approved"
REJECTED = "rejected"
TIMEOUT = "timeout"
SUPERSEDED = "superseded"   # an override or hold arrived while the proposal waited, or the day was reset

HOLD_MW = 0.005             # below this the AI proposes to hold: nothing to approve
_TIME_RE = re.compile(r"^(\d{2}):(\d{2})$")


class ControlError(ValueError):
    """An operator command that cannot be accepted. The message is shown to the operator."""


class NoPendingApproval(LookupError):
    """There is no proposal with that id waiting (already answered, timed out, or never existed)."""


@dataclass(frozen=True)
class Command:
    """An operator command for one interval: ``override`` (with a setpoint) or ``hold``."""
    kind: str
    setpoint_mw: float = 0.0
    until: Optional[str] = None


@dataclass(frozen=True)
class Autonomy:
    mode: str = "autonomous"
    # supervised: moves bigger than this wait for the operator
    max_auto_mw: float = 1.5
    # supervised: low-confidence moves (including every backup-rule decision) wait too
    ask_on_low_confidence: bool = True
    # how long a proposal waits before the battery holds instead
    timeout_s: float = 60.0

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ControlError(f"mode must be one of {', '.join(MODES)}")
        if not (math.isfinite(self.max_auto_mw) and self.max_auto_mw >= 0):
            raise ControlError("max_auto_mw must be a number >= 0")
        if not (math.isfinite(self.timeout_s) and self.timeout_s > 0):
            raise ControlError("timeout_s must be a number > 0")

    def needs_review(self, setpoint_mw: float, confidence: Optional[str]) -> bool:
        """Must the operator approve this move before it runs? Holding never needs approval."""
        if self.mode == "autonomous" or abs(setpoint_mw) < HOLD_MW:
            return False
        if self.mode == "advisory":
            return True
        return abs(setpoint_mw) > self.max_auto_mw + 1e-9 or (
            self.ask_on_low_confidence and confidence not in ("medium", "high"))


@dataclass(frozen=True)
class Proposal:
    """An AI decision waiting for the operator."""
    id: int
    time: str
    setpoint_mw: float
    source: str
    confidence: str
    explanation_for_operator: str
    deadline: float       # on the controller's clock

    def to_dict(self, now: float) -> dict[str, Any]:
        out = asdict(self)
        out.pop("deadline")
        out["expires_in_s"] = round(max(0.0, self.deadline - now), 1)
        return out


def _minutes(t: str) -> int:
    m = _TIME_RE.match(t) if isinstance(t, str) else None
    if not m:
        raise ControlError(f"time must be 'HH:MM', got {t!r}")
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59) and (h, mi) != (24, 0):
        raise ControlError(f"time must be 'HH:MM', got {t!r}")
    return h * 60 + mi


class OperatorControl:
    """The operator's commands and the AI's pending proposal, shared by the loop and the API."""

    def __init__(
        self,
        *,
        max_power_mw: float,
        interval_min: int = 15,
        autonomy: Optional[Autonomy] = None,
        clock: Callable[[], float] = _time.monotonic,
        on_change: Optional[Callable[[dict], None]] = None,
    ):
        self.max_power_mw = max_power_mw
        self.interval_min = interval_min
        self.clock = clock
        self.on_change = on_change
        self._cond = threading.Condition()
        self._autonomy = autonomy or Autonomy()
        self._override: Optional[Command] = None
        self._hold = False
        self._pending: Optional[Proposal] = None
        self._verdict: Optional[str] = None
        self._next_id = 1

    # ------------------------------------------------------------ operator side

    def set_override(self, setpoint_mw: float, until: str, *, now: Optional[str]) -> dict:
        """Run ``setpoint_mw`` (> 0 charge, < 0 discharge) from the next interval up to ``until``.

        ``now`` is the start of the next interval to run (None once the day is over).
        """
        if now is None:
            raise ControlError("the day is over")
        sp = float(setpoint_mw)
        if not (math.isfinite(sp) and abs(sp) <= self.max_power_mw + 1e-9):
            raise ControlError(f"setpoint must be between -{self.max_power_mw:g} and {self.max_power_mw:g} MW")
        end = _minutes(until)
        if end % self.interval_min:
            raise ControlError(f"{until} is not the start of an interval")
        if end <= _minutes(now):
            raise ControlError(f"{until} has already passed: the next interval starts at {now}")
        with self._cond:
            self._override = Command("override", round(sp, 4), until)
            self._supersede()
        return self._changed()

    def clear_override(self) -> dict:
        """Hand control back to the AI."""
        with self._cond:
            self._override = None
        return self._changed()

    def set_hold(self, on: bool) -> dict:
        """Emergency hold on (battery idle, no export) or released."""
        with self._cond:
            self._hold = bool(on)
            if on:
                self._supersede()
        return self._changed()

    def set_autonomy(self, autonomy: Autonomy) -> dict:
        """Change how far the AI is trusted. A waiting proposal the new policy
        would not ask about is approved at once."""
        with self._cond:
            self._autonomy = autonomy
            p = self._pending
            if p is not None and not autonomy.needs_review(p.setpoint_mw, p.confidence):
                self._resolve(APPROVED)
        return self._changed()

    def answer(self, proposal_id: int, approve: bool) -> dict:
        """The operator's answer to the waiting proposal."""
        with self._cond:
            if self._pending is None or self._pending.id != proposal_id:
                raise NoPendingApproval(proposal_id)
            self._resolve(APPROVED if approve else REJECTED)
        return self._changed()

    def new_day(self) -> None:
        """The day starts again: an override was for the old day's clock, so it goes;
        a waiting proposal is let go. An emergency hold stays until released."""
        with self._cond:
            self._override = None
            self._supersede()
        self._changed()

    def release(self) -> None:
        """Let go of a waiting proposal (the site is shutting down)."""
        with self._cond:
            self._supersede()
        self._changed()

    def snapshot(self) -> dict:
        """The dashboard's ``ControlState``."""
        with self._cond:
            return self._snapshot()

    # ---------------------------------------------------------------- loop side

    @property
    def autonomy(self) -> Autonomy:
        with self._cond:
            return self._autonomy

    def command_for(self, time: str) -> Optional[Command]:
        """The operator's command for the interval starting at ``time``; None = the AI decides.
        An override that has run its course is dropped here."""
        expired = False
        with self._cond:
            if self._hold:
                return Command("hold")
            ov = self._override
            if ov is not None and _minutes(time) >= _minutes(ov.until):
                self._override, ov, expired = None, None, True
        if expired:
            self._changed()
        return ov

    def review(self, *, time: str, setpoint_mw: float, source: str, confidence: str,
               explanation_for_operator: str = "") -> str:
        """Wait for the operator to approve or reject a proposal (blocks the loop).

        Returns ``approved``, ``rejected``, ``timeout`` or ``superseded`` (an override
        or hold arrived, or the day was reset: ask ``command_for`` what to do).
        """
        with self._cond:
            if self._hold or self._override is not None:
                return SUPERSEDED
            pending = self._pending = Proposal(
                id=self._next_id, time=time, setpoint_mw=round(float(setpoint_mw), 4),
                source=str(source), confidence=str(confidence),
                explanation_for_operator=str(explanation_for_operator or ""),
                deadline=self.clock() + self._autonomy.timeout_s,
            )
            self._next_id += 1
            self._verdict = None
        self._changed()                 # the operator may answer before the wait below begins
        with self._cond:
            while self._verdict is None:
                left = pending.deadline - self.clock()
                if left <= 0:
                    self._resolve(TIMEOUT)
                    break
                self._cond.wait(left)
            verdict = self._verdict
            self._verdict = None
        self._changed()
        return verdict

    # ------------------------------------------------------------------ helpers

    def _resolve(self, verdict: str) -> None:
        """With the lock held: answer the waiting proposal and wake the loop."""
        if self._pending is not None:
            self._pending = None
            self._verdict = verdict
            self._cond.notify_all()

    def _supersede(self) -> None:
        self._resolve(SUPERSEDED)

    def _snapshot(self) -> dict:
        ov = self._override
        return {
            "hold": self._hold,
            "override": None if ov is None else {"setpoint_mw": ov.setpoint_mw, "until": ov.until},
            "autonomy": asdict(self._autonomy),
            "pending": None if self._pending is None else self._pending.to_dict(self.clock()),
        }

    def _changed(self) -> dict:
        snap = self.snapshot()
        if self.on_change is not None:
            try:
                self.on_change(snap)
            except Exception:   # a dashboard listener must never stop the site
                pass
        return snap


def autonomy_from(current: Autonomy, **changes: Any) -> Autonomy:
    """``current`` with the given fields changed (None = keep), validated."""
    return replace(current, **{k: v for k, v in changes.items() if v is not None})
