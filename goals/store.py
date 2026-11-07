"""Holds the user's goal sentences and the active GoalConfig.

The parser runs only when the sentences change. A config with open questions is not activated:
the previous config stays in force until the user clarifies.
"""

from __future__ import annotations

import json
from pathlib import Path

from goals.parser import GoalParser, ParseResult
from goals.schema import GoalConfig


class GoalStore:
    def __init__(self, parser: GoalParser, path: str | Path | None = None) -> None:
        self.parser = parser
        self.path = Path(path) if path else None
        self.sentences: list[str] = []
        self.config = GoalConfig()
        self._load()

    def set(self, sentences: list[str]) -> ParseResult:
        sentences = [s.strip() for s in sentences if s.strip()]
        if sentences == self.sentences:
            return ParseResult(self.config)

        result = self.parser.parse(sentences)
        if result.ok:
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
            self.config = GoalConfig.model_validate(data["config"])

    def _save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {"sentences": self.sentences, "config": self.config.model_dump()}
            self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
