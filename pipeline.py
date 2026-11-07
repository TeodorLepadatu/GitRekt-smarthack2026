"""One 15-minute control cycle, end to end (plan section 2):

    features + goal config ─► prompt assembly ─► decision LLM ─► validator / fallback ─► guard ─► setpoints

The goal config comes from the goal parser (``goal_parser.GoalStore.config``) and
is re-parsed only when the user's goals change, never inside this cycle.
``features`` is the feature builder's output for the interval; the guard reads
its ``time``, ``stored_mwh``, ``net_mw``, ``price_now`` and ``production_mw``.

``run_cycle`` never raises: a prompt that cannot be built goes straight to the
rule-based fallback, and the validator and guard never raise either.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from battery import BatteryConfig
from config import Settings
from guard import GuardResult, guard
from prompt_assembly import render_prompt
from validator import Decision, decide, fallback_decision
from validator.schema import DEFAULT_TIMEOUT_S, REASON_PROMPT_ERROR


@dataclass
class CycleResult:
    prompt: Optional[str]  # None when prompt assembly failed
    decision: Decision     # validator output (source "llm" or "fallback")
    setpoint: GuardResult  # what goes to the site

    def to_dict(self) -> dict:
        return {"prompt": self.prompt, "decision": self.decision.to_dict(), "setpoint": self.setpoint.to_dict()}


def run_cycle(
    features: Mapping[str, Any],
    goal_config: Any = None,
    *,
    llm_call: Optional[Callable[[str], Any]] = None,
    battery: Optional[BatteryConfig] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    logger: Optional[Callable[[dict], None]] = None,
) -> CycleResult:
    """Run one cycle. ``goal_config`` may be a GoalConfig, its dict, or None.

    ``llm_call`` defaults to llm.DecisionAgent; ``battery`` to ``Settings().battery``.
    """
    battery = battery or Settings().battery
    try:
        prompt: Optional[str] = render_prompt(features, goal_config)
    except Exception as exc:  # missing feature, bad goal config, ...
        prompt = None
        decision = fallback_decision(features if isinstance(features, Mapping) else None,
                                     REASON_PROMPT_ERROR, str(exc)[:200])
    else:
        decision = decide(prompt, features, llm_call=llm_call, timeout_s=timeout_s, logger=logger)
    return CycleResult(prompt, decision, guard(decision, features, goal_config, battery))
