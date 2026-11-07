"""Runtime settings, read from environment variables with sensible defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool | None) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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

    # Site parameters the goal parser uses for sanity checks.
    battery_capacity_mwh: float = field(
        default_factory=lambda: float(os.environ.get("ENERGY_AGENT_BATTERY_MWH", "10"))
    )

    goals_path: str = field(default_factory=lambda: os.environ.get("ENERGY_AGENT_GOALS_PATH", "data/goals.json"))
    llm_log_path: str | None = field(
        default_factory=lambda: os.environ.get("ENERGY_AGENT_LLM_LOG", "logs/llm_calls.jsonl") or None
    )
