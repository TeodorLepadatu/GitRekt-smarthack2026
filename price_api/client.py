"""Client for the mock price API: one local day as the loop wants it.

    from price_api import fetch_day
    day = fetch_day("2026-09-28", "http://127.0.0.1:8100")
    day.day_ahead      # 96 x EUR/MWh
    day.imbalance      # 96 x settlement.ImbalancePrices

Any failure (unreachable, timeout, HTTP error, malformed body) raises
:class:`PriceApiError`, so the caller has one thing to catch for its fallback.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

import httpx

from settlement import ImbalancePrices

N = 96


class PriceApiError(RuntimeError):
    """The price API did not give a usable day."""


@dataclass(frozen=True)
class DayPrices:
    date: str
    timestamps: list[int]
    day_ahead: list[float]
    imbalance: list[ImbalancePrices]
    source: dict[str, str] = field(default_factory=dict)  # {"day_ahead": "entsoe" | "synthetic" | "mixed", ...}

    @property
    def synthetic(self) -> bool:
        """True when any price of the day was generated rather than real."""
        return any(v in ("synthetic", "mixed") for v in self.source.values())


def fetch_day(
    date: str,
    api: Union[str, httpx.Client],
    *,
    synthetic: bool = True,
    timeout: float = 10.0,
) -> DayPrices:
    """The 96 intervals of ``date`` from the API at ``api``.

    ``api``: the base URL, or an ``httpx.Client`` whose ``base_url`` points at it
    (FastAPI's ``TestClient`` is one, so tests need no server).
    """
    path = f"/prices/day/{date}"
    params = {"synthetic": str(bool(synthetic)).lower()}
    try:
        if isinstance(api, httpx.Client):
            r = api.get(path, params=params)
        else:
            r = httpx.get(api.rstrip("/") + path, params=params, timeout=timeout)
    except httpx.HTTPError as exc:
        raise PriceApiError(f"price API not reachable: {exc}") from exc
    if r.status_code != 200:
        raise PriceApiError(f"price API answered {r.status_code} for {date}: {r.text[:200]}")
    try:
        body = r.json()
        rows = body["intervals"]
        if len(rows) != N:
            raise ValueError(f"{len(rows)} intervals, expected {N}")
        return DayPrices(
            date=str(body.get("date", date)),
            timestamps=[int(x["timestamp"]) for x in rows],
            day_ahead=[float(x["day_ahead_eur_mwh"]) for x in rows],
            imbalance=[ImbalancePrices(short_eur_mwh=float(x["imbalance_short_eur_mwh"]),
                                       long_eur_mwh=float(x["imbalance_long_eur_mwh"])) for x in rows],
            source=dict(body.get("source") or {}),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise PriceApiError(f"price API sent an unusable day for {date}: {exc}") from exc
