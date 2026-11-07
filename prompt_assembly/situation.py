"""Section 8: CURRENT SITUATION, rendered from the feature builder's values.

Prompt assembly does no arithmetic: every number shown here comes from the
feature builder (plan section 5, "Feature builder -> prompt assembly"). The
same renderer is used for the solved examples, so they always share one format.
"""
from __future__ import annotations

from typing import Any, Mapping

from .fmt import PromptAssemblyError, eur, frac, mw, num, signed_frac, signed_mw

REQUIRED_FIELDS = (
    "time",
    "demand_mw",
    "production_mw",
    "net_mw",
    "price_now",
    "break_even",
    "stored_mwh",
    "max_charge_mw",
    "max_discharge_mw",
    "matching_fraction",
)

OPTIONAL_FIELDS = (
    "net_label",               # "SURPLUS" | "DEFICIT" | "BALANCED"
    "prod_low_mw",             # ML 2 forecast range
    "prod_high_mw",
    "uncertainty",             # "LOW" | "MEDIUM" | "HIGH"
    "price_rank",              # 0 = cheapest interval today, 100 = most expensive
    "price_avg",
    "future_max_price",
    "future_max_time",
    "future_min_price",
    "future_min_time",
    "price_vs_breakeven",      # e.g. "EUR 53 BELOW"
    "break_even_before_refill",  # plan section 6, fix 3
    "next_refill_time",
    "min_soc_mwh",
    "capacity_mwh",
    "max_charge_fraction",     # max_charge_mw / rated power
    "max_discharge_fraction",
    "goals",                   # goal progress, rendered in USER GOALS
    "price_zone",              # labels: derived here when not sent (see labels())
    "price_cheap",
    "surplus_left",
)


def check_features(features: Mapping[str, Any]) -> None:
    if not isinstance(features, Mapping):
        raise PromptAssemblyError(f"features must be a mapping, got {type(features).__name__}")
    missing = [f for f in REQUIRED_FIELDS if features.get(f) is None]
    if missing:
        raise PromptAssemblyError(f"features is missing required fields: {', '.join(missing)}")
    if not isinstance(features["time"], str):
        raise PromptAssemblyError(f"time must be an 'HH:MM' string, got {features['time']!r}")
    for f in REQUIRED_FIELDS[1:]:
        num(features[f], f)


def render_situation(features: Mapping[str, Any], *, title: str = "CURRENT SITUATION") -> str:
    check_features(features)
    f = features
    has = lambda k: f.get(k) is not None  # noqa: E731

    lines = [f"{title} (interval starting {f['time']})"]

    # Site balance
    prod = f"production {mw(num(f['production_mw'], 'production_mw'))} MW"
    if has("prod_low_mw") and has("prod_high_mw"):
        prod += f" (forecast range {mw(num(f['prod_low_mw'], 'prod_low_mw'))}–{mw(num(f['prod_high_mw'], 'prod_high_mw'))} MW)"
    net = f"net {signed_mw(num(f['net_mw'], 'net_mw'))} MW"
    if has("net_label"):
        net += f" ({f['net_label']})"
    lines.append(f"- Site: demand {mw(num(f['demand_mw'], 'demand_mw'))} MW, {prod} -> {net}")
    if has("uncertainty"):
        lines.append(f"- Forecast uncertainty: {f['uncertainty']}")

    # Prices
    price = f"- Price now: EUR {eur(num(f['price_now'], 'price_now'))}/MWh"
    extra = []
    if has("price_rank"):
        extra.append(f"rank {eur(num(f['price_rank'], 'price_rank'))}/100 today")
    if has("price_avg"):
        extra.append(f"day average EUR {eur(num(f['price_avg'], 'price_avg'))}")
    if extra:
        price += f" ({', '.join(extra)})"
    lines.append(price)
    later = []
    if has("future_max_price"):
        s = f"highest EUR {eur(num(f['future_max_price'], 'future_max_price'))}"
        later.append(s + (f" at {f['future_max_time']}" if has("future_max_time") else ""))
    if has("future_min_price"):
        s = f"lowest EUR {eur(num(f['future_min_price'], 'future_min_price'))}"
        later.append(s + (f" at {f['future_min_time']}" if has("future_min_time") else ""))
    if later:
        lines.append(f"- Later today: {'; '.join(later)}")

    be = f"- Break-even: EUR {eur(num(f['break_even'], 'break_even'))}/MWh"
    if has("price_vs_breakeven"):
        be += f" -> price now is {f['price_vs_breakeven']} break-even"
    lines.append(be)
    if has("break_even_before_refill"):
        s = f"- Break-even before next refill: EUR {eur(num(f['break_even_before_refill'], 'break_even_before_refill'))}/MWh"
        if has("next_refill_time"):
            s += f" (cheap period at {f['next_refill_time']})"
        lines.append(s)

    # Battery
    stored = f"- Battery: {mw(num(f['stored_mwh'], 'stored_mwh'))} MWh stored"
    bounds = []
    if has("min_soc_mwh"):
        bounds.append(f"min {mw(num(f['min_soc_mwh'], 'min_soc_mwh'))}")
    if has("capacity_mwh"):
        bounds.append(f"max {mw(num(f['capacity_mwh'], 'capacity_mwh'))}")
    if bounds:
        stored += f" ({', '.join(bounds)} MWh)"
    lines.append(stored)
    lines.append("- This interval: " + _limit(f, "charge") + "; " + _limit(f, "discharge"))
    lines.append(
        f"- Matching fraction: {signed_frac(num(f['matching_fraction'], 'matching_fraction'))}"
    )
    lines.append(_labels(f))
    return "\n".join(lines)


# Price-zone thresholds in EUR/MWh around break-even (section 4 of the prompt).
NEAR_EUR = 5
FAR_ABOVE_EUR = 20
CHEAP_RANK = 30


def labels(f: Mapping[str, Any]) -> dict[str, Any]:
    """Labels the decision rules are written in. Comparisons only: no number
    shown to the LLM is computed here. The feature builder may send any of
    them itself (same keys); then its value is used."""
    price = num(f["price_now"], "price_now")
    # Plan section 6, fix 3: with a cheap refill coming first, that break-even applies.
    ref = f.get("break_even_before_refill")
    be = num(ref, "break_even_before_refill") if ref is not None else num(f["break_even"], "break_even")
    diff = price - be
    if f.get("price_zone"):
        zone = str(f["price_zone"]).upper()
    elif diff > FAR_ABOVE_EUR:
        zone = "FAR ABOVE"
    elif diff > NEAR_EUR:
        zone = "ABOVE"
    elif diff >= -NEAR_EUR:
        zone = "NEAR"
    else:
        zone = "BELOW"

    cheap = f.get("price_cheap")
    if cheap is None:
        cheap = price < 0 or (f.get("price_rank") is not None and num(f["price_rank"], "price_rank") <= CHEAP_RANK)

    left = f.get("surplus_left")
    if left is None:
        left = num(f["net_mw"], "net_mw") > num(f["max_charge_mw"], "max_charge_mw")

    return {"price_zone": zone, "price_negative": price < 0, "price_cheap": bool(cheap), "surplus_left": bool(left)}


def _labels(f: Mapping[str, Any]) -> str:
    lab = labels(f)
    yes = lambda b: "YES" if b else "NO"  # noqa: E731
    return (f"- Labels: price zone {lab['price_zone']}; price negative {yes(lab['price_negative'])}; "
            f"price cheap {yes(lab['price_cheap'])}; surplus left after charge limit {yes(lab['surplus_left'])}")


def _limit(f: Mapping[str, Any], kind: str) -> str:
    s = f"{kind} up to {mw(num(f[f'max_{kind}_mw'], f'max_{kind}_mw'))} MW"
    key = f"max_{kind}_fraction"
    if f.get(key) is not None:
        s += f" (battery_fraction {'' if kind == 'charge' else '-'}{frac(num(f[key], key))})"
    return s
