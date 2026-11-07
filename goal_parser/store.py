"""Holds the user's goal sentences and the active GoalConfig.

The parser runs only when the sentences change. A result with open questions is
not activated: the previous config stays in force until the user clarifies, so
the guard never enforces a half-understood set of goals. The active config is
saved to JSON and reloaded on start.
"""

from __future__ import annotations

import json
from pathlib import Path

from .parser import GoalParser
from .schema import GoalConfig, ParseResult


class GoalStore:
    def __init__(self, parser: GoalParser, path: str | Path | None = None) -> None:
        self.parser = parser
        self.path = Path(path) if path else None
        self.sentences: list[str] = []
        self.config = GoalConfig()
        self._load()

    def set(self, sentences: list[str]) -> ParseResult:
        """Replace the goal sentences. Activates the new config only if nothing is unclear."""
        sentences = [s.strip() for s in sentences if s and s.strip()]
        if sentences == self.sentences:
            return ParseResult(self.config, source="stored")

        result = self.parser.parse(sentences)
        if not result.needs_clarification:
            self.sentences, self.config = sentences, result.config
            self._save()
        return result

    def add(self, sentence: str) -> ParseResult:
        return self.set([*self.sentences, sentence])

    def remove(self, index: int) -> ParseResult:
        return self.set([s for i, s in enumerate(self.sentences) if i != index])

    def _load(self) -> None:
        if self.path and self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.sentences = data["sentences"]
            self.config = GoalConfig.from_dict(data["config"])

    def _save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {"sentences": self.sentences, "config": self.config.to_dict()}
            self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
