"""Thin Ollama /api/chat client: temperature 0, JSON-schema constrained output, explicit num_ctx."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx

from config import Settings


class LLMError(RuntimeError):
    """Raised when the LLM could not be reached or returned an unusable HTTP response."""


@dataclass
class ChatResult:
    content: str
    latency_s: float
    model: str
    thinking: str | None = None


class ChatClient(Protocol):
    """Anything that can answer a chat with JSON text. Lets tests swap in a fake."""

    model: str

    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any], *, tag: str = "") -> ChatResult: ...


class OllamaClient:
    def __init__(self, model: str, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.model = model
        self._http = httpx.Client(base_url=self.settings.ollama_host, timeout=self.settings.timeout_s)

    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any], *, tag: str = "") -> ChatResult:
        s = self.settings
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": schema,  # Ollama structured outputs: constrains decoding to this JSON schema
            "options": {"temperature": 0, "seed": s.seed, "num_ctx": s.num_ctx},
        }
        if s.think is not None:
            body["think"] = s.think

        start = time.perf_counter()
        try:
            resp = self._http.post("/api/chat", json=body)
            if resp.status_code == 400 and "think" in body and "think" in resp.text.lower():
                # Model doesn't support the think flag; retry without it.
                body.pop("think")
                resp = self._http.post("/api/chat", json=body)
            resp.raise_for_status()
        except httpx.TimeoutException as exc:
            self._log(tag, messages, None, time.perf_counter() - start, f"timeout: {exc}")
            raise LLMError(f"Ollama timed out after {s.timeout_s}s") from exc
        except httpx.HTTPError as exc:
            detail = exc.response.text if isinstance(exc, httpx.HTTPStatusError) else str(exc)
            self._log(tag, messages, None, time.perf_counter() - start, detail)
            raise LLMError(f"Ollama request failed: {detail}") from exc

        latency = time.perf_counter() - start
        msg = resp.json().get("message", {})
        result = ChatResult(
            content=msg.get("content", ""),
            latency_s=latency,
            model=self.model,
            thinking=msg.get("thinking") or None,
        )
        self._log(tag, messages, result, latency, None)
        return result

    def _log(self, tag: str, messages: list[dict[str, str]], result: ChatResult | None, latency: float, error: str | None) -> None:
        """Append every prompt and response to a JSONL file (repeatability + retraining data)."""
        path = self.settings.llm_log_path
        if not path:
            return
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tag": tag,
            "model": self.model,
            "messages": messages,
            "response": result.content if result else None,
            "thinking": result.thinking if result else None,
            "latency_s": round(latency, 3),
            "error": error,
        }
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass  # logging must never break a control cycle
