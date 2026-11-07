"""Feature builder: the accountant (plan sections 2, 3 and 5).

It does every piece of arithmetic in the cycle, so that nothing downstream has
to calculate anything. The small LLM only compares the numbers it is handed,
and the guard only clamps them.

    weather/price/calendar APIs -> ML 1 demand, ML 2 production -+
                                                  sensors: SoC  -+--> build_features()
                                            goal parser: config -+          |
                                                                            v
                                      prompt_assembly.render_prompt(features, goal_config)

Input
    time          interval start, "HH:MM"
    forecast      SiteForecast (ML 1 demand, ML 2 production and its range)
    prices        the day's day-ahead prices
    stored_mwh    the battery's state of charge, from the sensors
    goal_config   the goal parser's output, or None
    battery       the site battery; defaults to the plan's 10 MWh / 3 MW unit

Output
    one flat dict holding every field of prompt section 8 and the USER GOALS
    progress list, exactly as ``prompt_assembly.situation`` and
    ``prompt_assembly.user_goals`` expect them.

All four of the plan's section 6 fixes land here or in ``battery.py``:
  1. charge and discharge limits on a 15-minute basis -> ``battery.BatteryConfig``
  2. break-even from the k-th highest later price     -> ``_break_even``
  3. a second break-even before the next cheap refill -> ``_break_even``
  4. curtailment in the energy balance                -> prompt text, no number here
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from battery import BatteryConfig
from common import finite_float
from guard.goals import to_minutes

from .goals import goals_progress
from .prices import PriceCurve, intervals_to_empty

# Wear in EUR per MWh of throughput (plan section 5 settlement check). Holding a
# MWh for later costs one charge and one discharge, so break-even carries 2x.
WEAR_EUR_PER_MWH = 2.5

# Price zone thresholds in EUR/MWh around break-even. These must stay equal to
# prompt_assembly.situation; tests/test_feature_builder.py checks that they do.
NEAR_EUR = 5.0
FAR_ABOVE_EUR = 20.0
CHEAP_RANK = 30

# Forecast uncertainty from ML 2's range, measured against rated battery power:
# what matters is whether the error is big enough to change the action.
UNCERTAINTY_MEDIUM = 0.25
UNCERTAINTY_HIGH = 0.50

# Below this the site is neither in surplus nor in deficit.
BALANCED_MW = 0.005


class FeatureBuilderError(ValueError):
    """The inputs cannot be turned into a feature set."""


@dataclass(frozen=True)
class SiteForecast:
    """What ML 1 and ML 2 predict for this interval.

    ``production_mw`` defaults to ``solar_mw + wind_mw``. The range
    ``prod_low_mw``/``prod_high_mw`` is ML 2's quantile band and drives the
    uncertainty label; give both or neither.
    """

    demand_mw: float
    production_mw: Optional[float] = None
    solar_mw: Optional[float] = None
    wind_mw: Optional[float] = None
    prod_low_mw: Optional[float] = None
    prod_high_mw: Optional[float] = None

    FIELDS = ("demand_mw", "production_mw", "solar_mw", "wind_mw", "prod_low_mw", "prod_high_mw")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "SiteForecast":
        return cls(**{name: data.get(name) for name in cls.FIELDS})


def build_features(
    *,
    time: str,
    forecast: SiteForecast | Mapping[str, Any],
    prices: Any,
    stored_mwh: float,
    goal_config: Any = None,
    battery: Optional[BatteryConfig] = None,
    wear_eur_per_mwh: float = WEAR_EUR_PER_MWH,
) -> dict[str, Any]:
    """Build prompt section 8 and the goal progress for one 15-minute interval."""
    cfg = battery or BatteryConfig()
    if isinstance(forecast, Mapping):
        forecast = SiteForecast.from_mapping(forecast)
    curve = prices if isinstance(prices, PriceCurve) else PriceCurve(prices)

    now_min = to_minutes(time)
    if now_min is None:
        raise FeatureBuilderError(f"time must be 'HH:MM', got {time!r}")
    index = curve.index_at(time)

    stored = _need(stored_mwh, "stored_mwh")
    if not -1e-9 <= stored <= cfg.e_max + 1e-9:
        raise FeatureBuilderError(f"stored_mwh {stored} is outside 0..{cfg.e_max} MWh")

    out: dict[str, Any] = {"time": curve.time_at(index)}
    out.update(_site(forecast, cfg))
    out.update(_prices(curve, index, stored, cfg, wear_eur_per_mwh))
    out.update(_battery(stored, cfg))
    out["goals"] = goals_progress(goal_config, now_min=now_min, stored_mwh=stored, battery=cfg)
    out.update(_labels(out))
    # Settlement and the dashboard need these; the prompt ignores keys it does not know.
    out["interval_h"] = cfg.interval_h
    out["wear_eur_per_mwh"] = wear_eur_per_mwh
    return out


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #


def _site(f: SiteForecast, cfg: BatteryConfig) -> dict[str, Any]:
    """Demand, production, and the net the battery and the grid have to absorb."""
    demand = _need(f.demand_mw, "demand_mw")
    solar, wind = finite_float(f.solar_mw), finite_float(f.wind_mw)
    production = finite_float(f.production_mw)
    if production is None:
        if solar is None and wind is None:
            raise FeatureBuilderError("give production_mw, or solar_mw and wind_mw")
        production = (solar or 0.0) + (wind or 0.0)

    net = production - demand
    out: dict[str, Any] = {
        "demand_mw": round(demand, 4),
        "production_mw": round(production, 4),
        "net_mw": round(net, 4),
        "net_label": "SURPLUS" if net > BALANCED_MW else "DEFICIT" if net < -BALANCED_MW else "BALANCED",
        "matching_fraction": round(net / cfg.p_max_mw, 2),
    }
    if solar is not None:
        out["solar_mw"] = round(solar, 4)
    if wind is not None:
        out["wind_mw"] = round(wind, 4)

    low, high = finite_float(f.prod_low_mw), finite_float(f.prod_high_mw)
    if low is not None and high is not None:
        if low > high:
            low, high = high, low
        low, high = min(low, production), max(high, production)
        out["prod_low_mw"] = round(low, 4)
        out["prod_high_mw"] = round(high, 4)
        out["uncertainty"] = _uncertainty(high - low, cfg.p_max_mw)
    return out


def _uncertainty(band_mw: float, p_max_mw: float) -> str:
    """ML 2's range turned into the label prompt section 4 step 4 reacts to."""
    if p_max_mw <= 0:
        return "HIGH"
    share = band_mw / p_max_mw
    if share <= UNCERTAINTY_MEDIUM:
        return "LOW"
    return "MEDIUM" if share <= UNCERTAINTY_HIGH else "HIGH"


def _battery(stored: float, cfg: BatteryConfig) -> dict[str, Any]:
    """This interval's real limits (plan section 6, fix 1: on a 15-minute basis)."""
    max_c = round(cfg.max_charge_mw(stored), 2)
    max_d = round(cfg.max_discharge_mw(stored), 2)
    return {
        "stored_mwh": round(stored, 4),
        "min_soc_mwh": cfg.e_min_mwh,
        "capacity_mwh": cfg.capacity_mwh,
        "max_charge_mw": max_c,
        "max_discharge_mw": max_d,
        "max_charge_fraction": round(max_c / cfg.p_max_mw, 2),
        "max_discharge_fraction": round(max_d / cfg.p_max_mw, 2),
    }


def _prices(curve: PriceCurve, index: int, stored: float, cfg: BatteryConfig,
            wear: float) -> dict[str, Any]:
    """Price now, how it ranks, the rest of the day, and both break-evens."""
    price = curve.price_at(index)
    out: dict[str, Any] = {
        "price_now": round(price, 2),
        "price_rank": curve.rank(index),
        "price_avg": round(curve.average, 1),
    }
    for kind, highest in (("max", True), ("min", False)):
        found = curve.extreme_later(index, highest=highest)
        if found is not None:
            at, value = found
            out["future_" + kind + "_price"] = round(value, 2)
            out["future_" + kind + "_time"] = curve.time_at(at)

    out.update(_break_even(curve, index, stored, cfg, wear))
    diff = price - out["break_even"]
    side = "ABOVE" if diff > 0 else "BELOW"
    out["price_vs_breakeven"] = "EUR " + _eur(abs(diff)) + " " + side
    return out


def _break_even(curve: PriceCurve, index: int, stored: float, cfg: BatteryConfig,
                wear: float) -> dict[str, Any]:
    """What a stored MWh is worth if it is kept for later, after losses and wear.

        break_even = round_trip_efficiency x later price - 2 x wear

    Fix 2: the later price is the k-th highest, where k is how many intervals
    full-power discharge needs to empty the usable energy. Valuing all of it at
    the single best price overstates a battery that is fairly full.

    Fix 3: when a cheap refill comes first, holding past it earns nothing extra,
    so a second break-even is computed over the intervals before that refill.
    It is reported only when it actually lowers the bar.
    """
    deliverable = max(0.0, stored - cfg.e_min_mwh) * cfg.eta_d
    k = intervals_to_empty(deliverable, cfg.p_max_mw, cfg.interval_h)
    later = curve.later(index)

    best = curve.kth_highest(later, k)
    if best is None:
        # Last interval of the day: nothing left to hold for, so selling wins.
        best = curve.price_at(index)
    out: dict[str, Any] = {
        "break_even": round(cfg.round_trip_eff * best - 2 * wear, 1),
        "break_even_k": k,
        "break_even_price": round(best, 2),
    }

    refill = curve.next_cheap_after(index)
    if refill is not None:
        before = curve.kth_highest(curve.later(index, end=refill), k)
        if before is not None:
            value = round(cfg.round_trip_eff * before - 2 * wear, 1)
            if value < out["break_even"] - 0.05:
                out["break_even_before_refill"] = value
                out["next_refill_time"] = curve.time_at(refill)
    return out


def _labels(f: Mapping[str, Any]) -> dict[str, Any]:
    """The labels prompt sections 3 and 4 are written in.

    Comparisons only. ``prompt_assembly.situation.labels`` derives the same
    three values when a feature set does not carry them, and the two must agree.
    """
    price = f["price_now"]
    # Fix 3: with a cheap refill first, that break-even is the one that applies.
    break_even = f.get("break_even_before_refill", f["break_even"])
    diff = price - break_even
    if diff > FAR_ABOVE_EUR:
        zone = "FAR ABOVE"
    elif diff > NEAR_EUR:
        zone = "ABOVE"
    elif diff >= -NEAR_EUR:
        zone = "NEAR"
    else:
        zone = "BELOW"
    return {
        "price_zone": zone,
        "price_cheap": price < 0 or f["price_rank"] <= CHEAP_RANK,
        "surplus_left": f["net_mw"] > f["max_charge_mw"],
    }


# --------------------------------------------------------------------------- #


def _need(value: Any, name: str) -> float:
    out = finite_float(value)
    if out is None:
        raise FeatureBuilderError(f"{name} must be a finite number, got {value!r}")
    return out


def _eur(x: float) -> str:
    """Whole euros unless there is a real fractional part, as prompt_assembly prints them."""
    x = round(x, 1)
    return f"{x:.0f}" if abs(x - round(x)) < 1e-9 else f"{x:.1f}"
