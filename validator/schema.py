"""Data contracts for the Validator + fallback stage.

Input  (from the small LLM, see plan section 5 "Small LLM -> validator"):
    {"reasoning": str, "battery_fraction": float, "curtail": bool,
     "confidence": "low" | "medium" | "high", "explanation_for_operator": str}

Output (to the guard layer):
    Decision.to_dict() -> {"battery_fraction", "curtail", "source", ...}

ASSUMPTIONS TO CONFIRM WITH THE TEAM (all kept in this file):
  * Sign convention: battery_fraction > 0 = CHARGE, < 0 = DISCHARGE,
    as a fraction of max power (same as the plan's matching_fraction).
  * Feature names the fallback reads come from the feature builder contract
    (plan section 5 "Feature builder -> prompt assembly").
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

# --- battery_fraction range -------------------------------------------------
FRACTION_MIN = -1.0  # full discharge
FRACTION_MAX = 1.0   # full charge
# Values just outside the range (e.g. 1.02 from rounding) are clamped instead
# of rejected. Anything further out counts as invalid and goes to the fallback.
FRACTION_TOLERANCE = 0.05

# --- other fields -------------------------------------------------------------
CONFIDENCE_VALUES = ("low", "medium", "high")
DEFAULT_CONFIDENCE = "low"

# Fields the guard cannot work without: if missing/invalid -> fallback.
CRITICAL_FIELDS = ("battery_fraction", "curtail")
# Fields that are nice to have: if missing/invalid -> default + warning.
TEXT_FIELDS = ("reasoning", "explanation_for_operator")

# --- timing -------------------------------------------------------------------
DEFAULT_TIMEOUT_S = 60.0  # a cycle is 15 min; a 4B model answers in seconds

# --- sources / fallback reasons (logged, used for the reliability metrics) -----
SOURCE_LLM = "llm"
SOURCE_FALLBACK = "fallback"

REASON_TIMEOUT = "timeout"
REASON_LLM_ERROR = "llm_error"
REASON_EMPTY = "empty_output"
REASON_BAD_JSON = "bad_json"
REASON_NOT_OBJECT = "not_a_json_object"
REASON_MISSING_FIELD = "missing_field"
REASON_BAD_TYPE = "bad_type"
REASON_OUT_OF_RANGE = "out_of_range"


@dataclass
class Decision:
    """A validated battery decision, ready for the guard layer."""

    battery_fraction: float
    curtail: bool
    source: str  # SOURCE_LLM | SOURCE_FALLBACK
    confidence: str = DEFAULT_CONFIDENCE
    reasoning: str = ""
    explanation_for_operator: str = ""
    fallback_reason: Optional[str] = None  # why the fallback was used
    fallback_detail: Optional[str] = None  # e.g. which field was wrong
    warnings: list[str] = field(default_factory=list)
    latency_s: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


class ValidationError(Exception):
    """Raised when LLM output cannot be turned into a Decision."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail
