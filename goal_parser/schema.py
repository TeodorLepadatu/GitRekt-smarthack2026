"""Goal config schema: the data contract from the goal parser to the feature
builder, guard and settlement (plan section 5).

Everything here is deterministic. The LLM (or the rule parser) only produces a
*raw* goal with numbers and units exactly as the user wrote them; this module
converts units, normalises times, checks ranges and decides hard vs. soft.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

OBJECTIVES = ("max_profit", "self_consumption")
DEFAULT_OBJECTIVE = "max_profit"
# "Keep 2 MWh backup overnight" -> reach it by 22:00, hold it until 06:00.
OVERNIGHT_WINDOW = ("22:00", "06:00")
# EU day-ahead harmonised price limits; a minimum price outside this is almost
# always a unit mistake (e.g. "50 EUR/kWh" meant per MWh).
MARKET_PRICE_RANGE = (-500, 4000)

_ENERGY_UNITS = {"mwh": 1.0, "kwh": 0.001}
_PRICE_UNITS = {"eur/mwh": 1.0, "eur/kwh": 1000.0, "ct/kwh": 10.0}


class GoalValidationError(ValueError):
    """A goal could not be built. ``question`` is what to ask the user back."""

    def __init__(self, question: str):
        super().__init__(question)
        self.question = question


# --------------------------------------------------------------------------- #
# Normalisation helpers
# --------------------------------------------------------------------------- #

_TIME_RE = re.compile(r"^(\d{1,2})(?:[:.h](\d{2}))?\s*(am|pm|h)?$")


def normalize_time(value: Any) -> str:
    """Return ``"HH:MM"`` for inputs like ``"18:00"``, ``"6 pm"``, ``"18h"``."""
    if not isinstance(value, str):
        raise ValueError(f"not a time: {value!r}")
    v = value.strip().lower()
    if v in ("noon", "midday"):
        return "12:00"
    if v == "midnight":
        return "00:00"
    m = _TIME_RE.match(v)
    if not m:
        raise ValueError(f"not a time: {value!r}")
    hour, minute, suffix = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if suffix in ("am", "pm"):
        if not 1 <= hour <= 12:
            raise ValueError(f"not a time: {value!r}")
        hour = hour % 12 + (12 if suffix == "pm" else 0)
    if hour == 24 and minute == 0:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"not a time: {value!r}")
    return f"{hour:02d}:{minute:02d}"


def _normalize_unit(unit: str) -> str:
    u = unit.strip().lower().replace(" ", "")
    u = u.replace("€", "eur").replace("per", "/")
    u = re.sub(r"euros?", "eur", u)
    u = re.sub(r"cents?|¢", "ct", u)
    return u


def to_mwh(amount: float, unit: str) -> float:
    factor = _ENERGY_UNITS.get(_normalize_unit(unit))
    if factor is None:
        raise ValueError(f"unknown energy unit: {unit!r}")
    return amount * factor


def to_eur_per_mwh(amount: float, unit: str) -> float:
    factor = _PRICE_UNITS.get(_normalize_unit(unit))
    if factor is None:
        raise ValueError(f"unknown price unit: {unit!r}")
    return amount * factor


def parse_number(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    if isinstance(value, (int, float)):
        out = float(value)
    elif isinstance(value, str):
        out = float(value.strip().replace(",", "."))
    else:
        raise ValueError(f"not a number: {value!r}")
    if not math.isfinite(out):
        raise ValueError(f"not a finite number: {value!r}")
    return out


def _clean(x: float) -> float | int:
    """6.0 -> 6 so the JSON reads like the contract; keep real decimals."""
    x = round(x, 6)
    return int(x) if x.is_integer() else x


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Goal:
    id: str
    type: str
    source_text: str
    hard: bool = True
    mwh: float | None = None
    by: str | None = None
    until: str | None = None
    eur_per_mwh: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "type": self.type}
        if self.type == "reserve":
            d.update(mwh=self.mwh, by=self.by, until=self.until)
        elif self.type == "min_sell_price":
            d["eur_per_mwh"] = self.eur_per_mwh
        d["hard"] = self.hard
        d["source_text"] = self.source_text
        return d


@dataclass
class GoalConfig:
    objective: str = DEFAULT_OBJECTIVE
    goals: list[Goal] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.objective not in OBJECTIVES:
            raise ValueError(f"unknown objective: {self.objective!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"objective": self.objective, "goals": [g.to_dict() for g in self.goals]}


@dataclass(frozen=True)
class Clarification:
    source_text: str
    question: str

    def to_dict(self) -> dict[str, str]:
        return {"source_text": self.source_text, "question": self.question}


@dataclass
class ParseResult:
    """Output of the goal parser.

    ``config`` always holds everything that *was* understood, so the rest of
    the pipeline can keep running while the user answers ``clarifications``.
    """

    config: GoalConfig
    clarifications: list[Clarification] = field(default_factory=list)
    source: str = "rules"  # "llm" | "rules"

    @property
    def needs_clarification(self) -> bool:
        return bool(self.clarifications)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "needs_clarification" if self.needs_clarification else "ok",
            "source": self.source,
            "config": self.config.to_dict(),
            "clarifications": [c.to_dict() for c in self.clarifications],
        }


# --------------------------------------------------------------------------- #
# Raw goal -> validated Goal
# --------------------------------------------------------------------------- #


def build_goal(raw: dict[str, Any], goal_id: str, *, capacity_mwh: float | None = None) -> Goal:
    """Turn a raw goal (numbers + units as written) into a validated :class:`Goal`.

    Raises :class:`GoalValidationError` carrying a question for the user when
    the goal is incomplete, vague or impossible.
    """
    source = str(raw.get("source_text") or "").strip()
    gtype = raw.get("type")
    if gtype == "reserve":
        return _build_reserve(raw, goal_id, source, capacity_mwh)
    if gtype == "min_sell_price":
        return _build_min_sell_price(raw, goal_id, source)
    raise GoalValidationError(
        f'I could not turn "{source}" into a goal. You can set an objective '
        '("maximise profit", "use my own energy first"), a reserve '
        '("store 6 MWh by 18:00") or a minimum sell price ("never sell below EUR 50/MWh").'
    )


_BARE_HOUR_RE = re.compile(r"^\s*(\d{1,2})\s*$")


def _ambiguous_hour(value: Any) -> int | None:
    """``"6"`` could be 06:00 or 18:00; ``"18"``, ``"6 pm"`` and ``"06:00"`` are clear."""
    if isinstance(value, str) and (m := _BARE_HOUR_RE.match(value)):
        h = int(m.group(1))
        if 1 <= h <= 12:
            return h
    return None


def _build_reserve(raw: dict, goal_id: str, source: str, capacity_mwh: float | None) -> Goal:
    try:
        amount = parse_number(raw.get("amount"))
    except ValueError:
        raise GoalValidationError(f'How much energy (in MWh) should be stored for "{source}"?')
    unit = raw.get("unit")
    if not unit:
        raise GoalValidationError(f'Is the {_clean(amount)} in "{source}" MWh or kWh?')
    try:
        mwh = to_mwh(amount, unit)
    except ValueError:
        raise GoalValidationError(f'I do not know the unit "{unit}" in "{source}". Please use MWh or kWh.')
    if mwh <= 0:
        raise GoalValidationError(f'The reserve in "{source}" must be more than 0 MWh. How much should I keep?')
    if capacity_mwh is not None and mwh > capacity_mwh:
        raise GoalValidationError(
            f"The battery holds at most {_clean(capacity_mwh)} MWh, so {_clean(mwh)} MWh cannot be "
            "stored. What amount should I use instead?"
        )

    by_raw, until_raw = raw.get("by"), raw.get("until")
    if not by_raw:
        if until_raw:
            raise GoalValidationError(
                f"From what time should the {_clean(mwh)} MWh reserve start (it lasts until {until_raw})?"
            )
        raise GoalValidationError(f"By what time should {_clean(mwh)} MWh be stored?")
    for t in (by_raw, until_raw):
        if (h := _ambiguous_hour(t)) is not None:
            other = "00:00 (midnight)" if h == 12 else f"{h + 12:02d}:00"
            raise GoalValidationError(f'In "{source}", do you mean {h:02d}:00 or {other}?')
    try:
        by = normalize_time(by_raw)
        until = normalize_time(until_raw) if until_raw else by
    except ValueError:
        raise GoalValidationError(f'I could not read the time in "{source}". Please give it as HH:MM.')

    return Goal(id=goal_id, type="reserve", source_text=source, hard=True,
                mwh=_clean(mwh), by=by, until=until)


def _build_min_sell_price(raw: dict, goal_id: str, source: str) -> Goal:
    try:
        amount = parse_number(raw.get("amount"))
    except ValueError:
        raise GoalValidationError(f'What is the lowest price (EUR/MWh) you want to sell at for "{source}"?')
    unit = raw.get("unit") or "EUR/MWh"  # day-ahead prices are quoted per MWh
    try:
        eur = to_eur_per_mwh(amount, unit)
    except ValueError:
        raise GoalValidationError(
            f'I do not know the price unit "{unit}" in "{source}". Please use EUR/MWh, EUR/kWh or ct/kWh.'
        )
    if not MARKET_PRICE_RANGE[0] <= eur <= MARKET_PRICE_RANGE[1]:
        raise GoalValidationError(
            f'"{source}" means {_clean(eur)} EUR/MWh, which is outside the market price range '
            f"({MARKET_PRICE_RANGE[0]} to {MARKET_PRICE_RANGE[1]} EUR/MWh). Did you mean a different unit?"
        )
    return Goal(id=goal_id, type="min_sell_price", source_text=source, hard=True,
                eur_per_mwh=_clean(eur))
