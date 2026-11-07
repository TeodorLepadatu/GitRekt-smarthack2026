"""Goal parser: plain-language goals -> goal config JSON (plan sections 4-5).

Flow: LLM extracts raw goals -> code checks them against the user's text,
validates and converts -> on any LLM failure the rule parser takes over.
Anything vague becomes a clarification question instead of a guess. Runs only
when the goal text changes.
"""
from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Any, Iterable, Protocol

from .prompt import OUTPUT_SCHEMA, SYSTEM_PROMPT, render_user_message
from .rules import parse_raw, times_in, unit_amounts
from .schema import (
    DEFAULT_OBJECTIVE,
    OBJECTIVES,
    Clarification,
    Goal,
    GoalConfig,
    GoalValidationError,
    ParseResult,
    ambiguous_hour,
    build_goal,
    normalize_time,
    parse_number,
)

log = logging.getLogger(__name__)

MAX_TEXT_CHARS = 2000  # goals are a few sentences; keeps the prompt well inside num_ctx


class JSONChatLLM(Protocol):
    def chat_json(self, system: str, user: str, schema: dict[str, Any] | None = None) -> Any: ...


class MalformedOutput(ValueError):
    """The LLM output has the wrong shape, invents values, or skips part of the text."""


def parse_goals(
    text: str | Iterable[str],
    *,
    llm: JSONChatLLM | None = None,
    battery_capacity_mwh: float | None = None,
) -> ParseResult:
    """Parse goal sentences into a :class:`ParseResult`.

    ``llm`` is anything with ``chat_json`` (e.g. :class:`OllamaClient`). Without
    it, or when it fails, the deterministic rule parser is used.

    When the same goal is stated twice (two minimum prices, or two reserves for
    the same window), the later statement wins, so "Never sell below 50.
    Actually, make it never below 60" gives 60.
    """
    text = _join(text)
    if not text:
        return ParseResult(GoalConfig(), [], source="rules")
    if len(text) > MAX_TEXT_CHARS:
        return ParseResult(GoalConfig(), [Clarification(
            "", f"That is a lot of text ({len(text)} characters). Please list your goals in a few "
                "short sentences.")], source="rules")

    if llm is not None:
        try:
            raw = llm.chat_json(SYSTEM_PROMPT, render_user_message(text), OUTPUT_SCHEMA)
            _check_llm_output(raw, text)
            return _interpret(raw, capacity_mwh=battery_capacity_mwh, source="llm")
        except MalformedOutput as e:
            log.warning("goal parser: rejected LLM output (%s); using rule parser", e)
        except Exception as e:  # timeout, connection error, bad JSON, ...
            log.warning("goal parser: LLM failed (%s); using rule parser", e)

    return _interpret(parse_raw(text), capacity_mwh=battery_capacity_mwh, source="rules")


class GoalParser:
    """Keeps the current goal text and re-parses only when it changes.

    Typical chat loop::

        result = parser.parse("Keep some energy for the evening")
        for c in result.clarifications:      # ask c.question, get the reply
            result = parser.answer(c, "3 MWh by 19:00")
    """

    def __init__(self, llm: JSONChatLLM | None = None, *, battery_capacity_mwh: float | None = None):
        self.llm = llm
        self.battery_capacity_mwh = battery_capacity_mwh
        self._text: str | None = None
        self._result: ParseResult | None = None

    @property
    def text(self) -> str | None:
        return self._text

    @property
    def result(self) -> ParseResult | None:
        return self._result

    def parse(self, text: str | Iterable[str]) -> ParseResult:
        joined = _join(text)
        if self._result is None or _key(joined) != _key(self._text or ""):
            self._result = parse_goals(joined, llm=self.llm, battery_capacity_mwh=self.battery_capacity_mwh)
        self._text = joined
        return self._result

    def answer(self, clarification: Clarification, reply: str) -> ParseResult:
        """Fold the user's reply to a clarification into the goal text and re-parse."""
        if self._text is None:
            raise RuntimeError("parse() must be called before answer()")
        reply = reply.strip()
        src = clarification.source_text
        if src and src in self._text:
            text = self._text.replace(src, f"{src} ({reply})", 1)
        else:
            text = f"{self._text}\n{reply}"
        return self.parse(text)


# --------------------------------------------------------------------------- #


def _join(text: str | Iterable[str]) -> str:
    if isinstance(text, str):
        return text.strip()
    return "\n".join(t.strip() for t in text if t and t.strip())


def _key(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _interpret(raw: Any, *, capacity_mwh: float | None, source: str) -> ParseResult:
    _check_shape(raw)
    clarifications = [
        Clarification(str(c.get("source_text") or "").strip(), str(c["question"]).strip())
        for c in raw.get("clarifications") or []
    ]

    by_key: dict[tuple, Goal] = {}  # later statement of the same goal replaces the earlier one
    for rg in raw.get("goals") or []:
        try:
            goal = build_goal(rg, "", capacity_mwh=capacity_mwh)
        except GoalValidationError as e:
            clarifications.append(Clarification(str(rg.get("source_text") or "").strip(), e.question))
            continue
        key = (goal.type,) if goal.type == "min_sell_price" else (goal.type, goal.by, goal.until)
        by_key[key] = goal

    goals = [replace(g, id=f"g{i}") for i, g in enumerate(by_key.values(), start=1)]
    return ParseResult(
        config=GoalConfig(objective=raw.get("objective") or DEFAULT_OBJECTIVE, goals=goals),
        clarifications=clarifications,
        source=source,
    )


def _check_shape(raw: Any) -> None:
    if not isinstance(raw, dict):
        raise MalformedOutput(f"expected an object, got {type(raw).__name__}")
    objective = raw.get("objective")
    if objective is not None and objective not in OBJECTIVES:
        raise MalformedOutput(f"unknown objective {objective!r}")
    goals, clar = raw.get("goals") or [], raw.get("clarifications") or []
    if not isinstance(goals, list) or not isinstance(clar, list):
        raise MalformedOutput("goals and clarifications must be lists")
    for g in goals:
        if not isinstance(g, dict):
            raise MalformedOutput(f"bad goal {g!r}")
    for c in clar:
        if not isinstance(c, dict) or not str(c.get("question") or "").strip():
            raise MalformedOutput(f"bad clarification {c!r}")


_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def _numbers_in(text: str) -> set[float]:
    return {float(n.replace(",", ".")) for n in _NUMBER_RE.findall(text)}


def _check_llm_output(raw: Any, text: str) -> None:
    """The LLM proposes, the code disposes: every value must come from the user's
    text, and no goal the user wrote may silently disappear."""
    _check_shape(raw)
    said_numbers = _numbers_in(text)
    said_times = times_in(text)
    used: set[float] = set()

    for g in raw.get("goals") or []:
        amount = g.get("amount")
        if amount is not None:
            try:
                value = parse_number(amount)
            except ValueError:
                raise MalformedOutput(f"amount {amount!r} is not a number")
            if not any(abs(value - n) < 1e-9 for n in said_numbers):
                raise MalformedOutput(f"amount {value:g} does not appear in the user's text")
            used.add(value)
        for field in ("by", "until"):
            t = g.get(field)
            if not t or ambiguous_hour(t) is not None:
                continue  # copied as written ("6"): build_goal asks am or pm
            try:
                hhmm = normalize_time(t)
            except ValueError:
                continue  # build_goal turns an unreadable time into a question
            if hhmm not in said_times:
                raise MalformedOutput(f"{field}={hhmm} is not a time the user wrote")

    asked_about = " ".join(
        f"{c.get('source_text') or ''} {c.get('question') or ''}" for c in raw.get("clarifications") or []
    )
    used |= _numbers_in(asked_about)
    missing = {a for a in unit_amounts(text) if not any(abs(a - u) < 1e-9 for u in used)}
    if missing:
        raise MalformedOutput(f"LLM skipped amounts {sorted(missing)} from the user's text")
