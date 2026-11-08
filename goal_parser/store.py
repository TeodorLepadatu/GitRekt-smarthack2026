"""Holds the user's goal sentences and the active GoalConfig.

Every sentence is parsed on its own, once, and the result is kept. Adding a
goal parses only the new sentence; removing one parses nothing. So a slow or
unsteady LLM can never turn an add into a question about an older goal, or
make a removal fail because the remaining text now reads differently.

A sentence with open questions is not activated: the previous config stays in
force until the user clarifies, so the guard never enforces a half-understood
set of goals. The active config (and each sentence's parse) is saved to JSON
and reloaded on start.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

from .parser import GoalParser
from .schema import DEFAULT_OBJECTIVE, OBJECTIVES, Goal, GoalConfig, ParseResult


@dataclass(frozen=True)
class _Part:
    """What one sentence says: an objective (or None) and its goals (ids not assigned yet)."""
    objective: Optional[str]
    goals: tuple[Goal, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"objective": self.objective, "goals": [g.to_dict() for g in self.goals]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "_Part":
        objective = data.get("objective")
        return cls(objective if objective in OBJECTIVES else None,
                   tuple(Goal(**g) for g in data.get("goals") or []))


def _key(goal: Goal) -> tuple:
    """Two statements of the same goal: the later one wins (as in the parser)."""
    return (goal.type,) if goal.type == "min_sell_price" else (goal.type, goal.by, goal.until)


class GoalStore:
    def __init__(self, parser: GoalParser, path: str | Path | None = None) -> None:
        self.parser = parser
        self.path = Path(path) if path else None
        self.sentences: list[str] = []
        self.config = GoalConfig()
        self._parts: dict[str, _Part] = {}       # sentence -> its parse
        self._origin: dict[str, int] = {}        # goal id -> index of the sentence it came from
        self._load()

    # ------------------------------------------------------------------ edits

    def set(self, sentences: list[str]) -> ParseResult:
        """Replace the goal sentences. Activates the new config only if nothing is unclear."""
        sentences = [s.strip() for s in sentences if s and s.strip()]
        if sentences == self.sentences:
            return ParseResult(self.config, source="stored")

        parts = dict(self._parts)
        sources = set()
        for s in sentences:
            if s in parts:
                continue
            result = self.parser.parse(s)
            sources.add(result.source)
            if result.needs_clarification:
                return result                     # nothing changes until the user clarifies
            parts[s] = _Part(result.objective_given, tuple(result.config.goals))

        self._parts = {s: parts[s] for s in sentences}
        self.sentences = sentences
        self.config, self._origin = self._combine()
        self._save()
        return ParseResult(self.config, source=next(iter(sources)) if len(sources) == 1 else "stored")

    def add(self, sentence: str) -> ParseResult:
        return self.set([*self.sentences, sentence])

    def remove(self, index: int) -> ParseResult:
        return self.set([s for i, s in enumerate(self.sentences) if i != index])

    # ------------------------------------------------------------------ reads

    def sentence_of(self, goal_id: str) -> Optional[int]:
        """The index of the sentence a goal came from (None if there is no such goal)."""
        return self._origin.get(goal_id)

    def objective_of(self, index: int) -> Optional[str]:
        """The objective sentence ``index`` states, or None."""
        part = self._parts.get(self.sentences[index]) if 0 <= index < len(self.sentences) else None
        return part.objective if part else None

    def goal_count_of(self, index: int) -> int:
        part = self._parts.get(self.sentences[index]) if 0 <= index < len(self.sentences) else None
        return len(part.goals) if part else 0

    # ---------------------------------------------------------------- helpers

    def _combine(self) -> tuple[GoalConfig, dict[str, int]]:
        objective = DEFAULT_OBJECTIVE
        by_key: dict[tuple, tuple[Goal, int]] = {}
        for i, s in enumerate(self.sentences):
            part = self._parts[s]
            if part.objective:
                objective = part.objective        # the last stated objective wins
            for g in part.goals:
                by_key[_key(g)] = (g, i)
        goals, origin = [], {}
        for n, (g, i) in enumerate(by_key.values(), start=1):
            goals.append(replace(g, id=f"g{n}"))
            origin[f"g{n}"] = i
        return GoalConfig(objective=objective, goals=goals), origin

    def _load(self) -> None:
        if not (self.path and self.path.exists()):
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.sentences = list(data.get("sentences") or [])
        self.config = GoalConfig.from_dict(data["config"])
        saved = data.get("parts")
        if isinstance(saved, list) and len(saved) == len(self.sentences):
            self._parts = {s: _Part.from_dict(p) for s, p in zip(self.sentences, saved)}
            self.config, self._origin = self._combine()
        else:
            # an older file without per-sentence parses: parse each sentence once now
            sentences, config = self.sentences, self.config
            self.sentences = []
            if self.set(sentences).needs_clarification:   # keep what was saved; edits re-parse
                self.sentences, self.config = sentences, config

    def _save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {"sentences": self.sentences, "config": self.config.to_dict(),
                    "parts": [self._parts[s].to_dict() for s in self.sentences]}
            self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
