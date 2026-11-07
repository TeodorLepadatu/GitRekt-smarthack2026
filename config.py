"""Runtime settings, read from environment variables with sensible defaults.

The single place for the Ollama host, model names, timeouts and the site battery.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from battery import BatteryConfig


def _env_bool(name: str, default: bool | None) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_limit(name: str) -> float | None:
    """A non-negative MW limit, or None when unset, empty, "none" or "off" (= no limit)."""
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in {"", "none", "off"}:
        return None
    value = float(raw)
    if not value >= 0:
        raise ValueError(f"{name} must be a number >= 0 MW, got {raw!r}")
    return value


@dataclass(frozen=True)
class Settings:
    ollama_host: str = field(default_factory=lambda: os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    # Model used for the per-cycle battery decision.
    decision_model: str = field(default_factory=lambda: os.environ.get("ENERGY_AGENT_MODEL", "qwen3.5:4b"))
    # Model used for goal parsing (runs only when goals change); defaults to the decision model.
    goal_model: str = field(
        default_factory=lambda: os.environ.get("ENERGY_AGENT_GOAL_MODEL")
        or os.environ.get("ENERGY_AGENT_MODEL", "qwen3.5:4b")
    )
    timeout_s: float = field(default_factory=lambda: float(os.environ.get("ENERGY_AGENT_TIMEOUT_S", "60")))
    # Ollama's default context can be 4k; the decision prompt is ~2.5k tokens, so set it explicitly.
    num_ctx: int = field(default_factory=lambda: int(os.environ.get("ENERGY_AGENT_NUM_CTX", "8192")))
    # Qwen3-family models think by default; off keeps latency low. None = don't send the flag.
    think: bool | None = field(default_factory=lambda: _env_bool("ENERGY_AGENT_THINK", False))
    seed: int = 0

    # The site's battery: used by the guard and by the goal parser's capacity check.
    battery: BatteryConfig = field(
        default_factory=lambda: BatteryConfig(
            capacity_mwh=float(os.environ.get("ENERGY_AGENT_BATTERY_MWH", "10")),
            p_max_mw=float(os.environ.get("ENERGY_AGENT_BATTERY_MW", "3")),
        )
    )

    # Peak shaving: the most the site may import from the grid, MW (15-minute average).
    # None = no cap, behaviour unchanged. Enforced by the guard, recorded by the settlement.
    max_import_mw: float | None = field(default_factory=lambda: _env_limit("ENERGY_AGENT_MAX_IMPORT_MW"))

    goals_path: str = field(default_factory=lambda: os.environ.get("ENERGY_AGENT_GOALS_PATH", "data/goals.json"))
    settlement_log_path: str | None = field(
        default_factory=lambda: os.environ.get("ENERGY_AGENT_SETTLEMENT_LOG", "logs/settlement.jsonl") or None
    )
    llm_log_path: str | None = field(
        default_factory=lambda: os.environ.get("ENERGY_AGENT_LLM_LOG", "logs/llm_calls.jsonl") or None
    )
