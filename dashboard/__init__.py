"""The dashboard's side of the API: the loop's objects as `frontend/src/api/types.ts`.

    from dashboard import day_payload, interval_payload, state_payload

    record = runner.step()
    sse_event = interval_payload(record)      # the SSE "cycle" event
    snapshot = day_payload(runner)            # GET /api/day

See :mod:`dashboard.contract` for what maps to what.
"""

from .contract import (
    day_payload,
    forecast_payload,
    goals_payload,
    interval_payload,
    metrics_payload,
    state_payload,
)

__all__ = [
    "interval_payload",
    "forecast_payload",
    "day_payload",
    "state_payload",
    "metrics_payload",
    "goals_payload",
]
