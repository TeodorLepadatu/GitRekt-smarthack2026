"""decide(prompt, features) -> validated decision (plan phase 4 deliverable).

Flow:  call LLM (with timeout) -> validate -> on any failure, rule-based fallback.
It always returns a Decision and never raises, so the 15-minute cycle keeps going.

The LLM call is injectable (llm_call=...), so tests and teammates can plug in
a fake model or their own client. The default is llm.DecisionAgent, which uses
the shared Ollama client and config.Settings (JSON schema, temperature 0, num_ctx 8192).
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable, Mapping, Optional

from .fallback import fallback_decision
from .schema import (
    DEFAULT_TIMEOUT_S,
    REASON_LLM_ERROR,
    REASON_TIMEOUT,
    Decision,
    ValidationError,
)
from .validator import validate

LLMCall = Callable[[str], Any]

_default_agent = None


def default_llm_call(prompt: str) -> str:
    """The decision model via llm.DecisionAgent (created on first use)."""
    global _default_agent
    if _default_agent is None:
        from llm import DecisionAgent  # lazy: the validator itself needs no HTTP client

        _default_agent = DecisionAgent()
    return _default_agent(prompt)


def _call_with_timeout(llm_call: LLMCall, prompt: str, timeout_s: float) -> Any:
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(llm_call, prompt).result(timeout=timeout_s)
    finally:
        # Don't wait for a hung call; the thread is abandoned.
        pool.shutdown(wait=False, cancel_futures=True)


def decide(
    prompt: str,
    features: Optional[Mapping[str, Any]] = None,
    llm_call: Optional[LLMCall] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    logger: Optional[Callable[[dict], None]] = None,
) -> Decision:
    """Get a validated decision for one 15-minute interval. Never raises.

    prompt:    the complete prompt from prompt assembly
    features:  feature-builder values for this interval (used by the fallback)
    llm_call:  function prompt -> raw output (raise on failure); defaults to llm.DecisionAgent
    logger:    optional callback receiving a log record (prompt, raw, decision)
    """
    if llm_call is None:
        llm_call = default_llm_call

    start = time.perf_counter()
    raw: Any = None
    try:
        raw = _call_with_timeout(llm_call, prompt, timeout_s)
        decision = validate(raw)
    except FutureTimeout:
        decision = fallback_decision(features, REASON_TIMEOUT, f"> {timeout_s:g} s")
    except ValidationError as exc:
        decision = fallback_decision(features, exc.reason, exc.detail)
    except Exception as exc:  # network error, model not found, etc.
        decision = fallback_decision(features, REASON_LLM_ERROR, repr(exc)[:200])

    decision.latency_s = round(time.perf_counter() - start, 3)

    if logger is not None:
        try:
            logger({"prompt": prompt, "raw_output": raw, "decision": decision.to_dict()})
        except Exception:
            pass  # logging must never break the control loop
    return decision
