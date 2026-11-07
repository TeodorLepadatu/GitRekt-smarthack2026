"""Parse and check the raw LLM output.

validate(raw) returns a Decision with source="llm" or raises ValidationError.
It does NOT convert to MW, enforce SoC limits or hard goals: that is the
guard layer's job. It only guarantees a well-formed, in-range decision.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from .schema import (
    CONFIDENCE_VALUES,
    DEFAULT_CONFIDENCE,
    FRACTION_MAX,
    FRACTION_MIN,
    FRACTION_TOLERANCE,
    REASON_BAD_JSON,
    REASON_BAD_TYPE,
    REASON_EMPTY,
    REASON_MISSING_FIELD,
    REASON_NOT_OBJECT,
    REASON_OUT_OF_RANGE,
    SOURCE_LLM,
    TEXT_FIELDS,
    Decision,
    ValidationError,
)

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def extract_json(raw: Any) -> dict:
    """Turn raw LLM output into a dict, tolerating common small-model noise.

    Handles: dict already parsed, ```json fences, Qwen3 <think> blocks, and
    text before/after the JSON object.
    """
    if isinstance(raw, dict):
        return raw
    if raw is None:
        raise ValidationError(REASON_EMPTY)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        raise ValidationError(REASON_BAD_JSON, f"unexpected type {type(raw).__name__}")

    text = _THINK_RE.sub("", raw).strip()
    if not text:
        raise ValidationError(REASON_EMPTY)

    fence = _FENCE_RE.match(text)
    if fence:
        text = fence.group(1).strip()

    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        obj = _first_json_object(text)

    if not isinstance(obj, dict):
        raise ValidationError(REASON_NOT_OBJECT, type(obj).__name__)
    return obj


def _first_json_object(text: str) -> Any:
    """Find the first decodable {...} object inside surrounding text."""
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = decoder.raw_decode(text, start)
            return obj
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
    raise ValidationError(REASON_BAD_JSON, text[:80])


def _check_fraction(value: Any, warnings: list[str]) -> float:
    # bool is a subclass of int in Python: reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(REASON_BAD_TYPE, f"battery_fraction={value!r}")
    value = float(value)
    if not math.isfinite(value):
        raise ValidationError(REASON_OUT_OF_RANGE, f"battery_fraction={value}")
    if value < FRACTION_MIN - FRACTION_TOLERANCE or value > FRACTION_MAX + FRACTION_TOLERANCE:
        raise ValidationError(REASON_OUT_OF_RANGE, f"battery_fraction={value}")
    if value < FRACTION_MIN or value > FRACTION_MAX:
        clamped = min(FRACTION_MAX, max(FRACTION_MIN, value))
        warnings.append(f"battery_fraction {value} clamped to {clamped}")
        value = clamped
    return value


def _check_curtail(value: Any, warnings: list[str]) -> bool:
    if isinstance(value, bool):
        return value
    # Small models sometimes write "false" / "true" as strings.
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        warnings.append(f"curtail given as string {value!r}")
        return value.strip().lower() == "true"
    raise ValidationError(REASON_BAD_TYPE, f"curtail={value!r}")


def _check_confidence(value: Any, warnings: list[str]) -> str:
    if isinstance(value, str) and value.strip().lower() in CONFIDENCE_VALUES:
        return value.strip().lower()
    warnings.append(f"confidence {value!r} invalid, using {DEFAULT_CONFIDENCE!r}")
    return DEFAULT_CONFIDENCE


def validate(raw: Any) -> Decision:
    """Validate raw LLM output. Raises ValidationError if it is unusable."""
    obj = extract_json(raw)
    warnings: list[str] = []

    for name in ("battery_fraction", "curtail"):
        if name not in obj:
            raise ValidationError(REASON_MISSING_FIELD, name)

    fraction = _check_fraction(obj["battery_fraction"], warnings)
    curtail = _check_curtail(obj["curtail"], warnings)
    confidence = _check_confidence(obj.get("confidence"), warnings)

    texts = {}
    for name in TEXT_FIELDS:
        value = obj.get(name)
        if isinstance(value, str):
            texts[name] = value.strip()
        else:
            texts[name] = ""
            warnings.append(f"{name} missing or not a string")

    return Decision(
        battery_fraction=fraction,
        curtail=curtail,
        source=SOURCE_LLM,
        confidence=confidence,
        reasoning=texts["reasoning"],
        explanation_for_operator=texts["explanation_for_operator"],
        warnings=warnings,
    )
