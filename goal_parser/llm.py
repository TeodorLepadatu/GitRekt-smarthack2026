"""Minimal Ollama chat client (stdlib only).

Used only by the goal parser: JSON mode,
temperature 0 and an explicit num_ctx, as the plan requires.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

DEFAULT_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
DEFAULT_MODEL = os.environ.get("GOAL_PARSER_MODEL", "qwen3:4b")


class LLMError(RuntimeError):
    """The LLM could not be reached or returned something that is not JSON."""


class OllamaClient:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str = DEFAULT_HOST,
        *,
        timeout: float = 60.0,
        num_ctx: int = 8192,
    ):
        if not host.startswith(("http://", "https://")):
            host = "http://" + host
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.num_ctx = num_ctx

    def chat_json(self, system: str, user: str, schema: dict[str, Any] | None = None) -> Any:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": schema if schema is not None else "json",
            "think": False,  # Qwen3: no <think> block, just the JSON
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
        }
        req = urllib.request.Request(
            f"{self.host}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            raise LLMError(f"Ollama request failed: {e}") from e
        if "error" in body:
            raise LLMError(f"Ollama error: {body['error']}")
        content = body.get("message", {}).get("content", "")
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            raise LLMError(f"LLM did not return JSON: {content[:200]!r}") from e
