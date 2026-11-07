"""Section 8: CURRENT SITUATION, rendered from the feature builder's values.

Prompt assembly does no arithmetic: every number shown here comes from the
feature builder (plan section 5, "Feature builder -> prompt assembly"). The
same renderer is used for the solved examples, so they always share one format.

Input handling:
- a missing or broken REQUIRED value raises PromptAssemblyError (no decision
  can be made without it; the validator's fallback policy takes over);
- a broken OPTIONAL value is left out and reported in ``warnings``, so one bad
  extra field never stops the 15-minute cycle.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from .fmt import PromptAssemblyError, eur, frac, is_hhmm, mw, num, signed_frac, signed_mw

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
    "imbalance_risk_eur",      # what this interval's forecast band costs if nothing absorbs it
    "imbalance_short_premium",  # EUR/MWh paid above the price for energy promised and not delivered
    "imbalance_long_discount",  # EUR/MWh lost on energy delivered beyond the plan
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

NET_LABELS = ("SURPLUS", "DEFICIT", "BALANCED")
UNCERTAINTY_LEVELS = ("LOW", "MEDIUM", "HIGH")
PRICE_ZONES = ("FAR ABOVE", "ABOVE", "NEAR", "BELOW")

# Price-zone thresholds in EUR/MWh around break-even (section 4 of the prompt).
NEAR_EUR = 5
FAR_ABOVE_EUR = 20
CHEAP_RANK = 30

_PRICE_VS_BE_RE = re.compile(r"^EUR \d+(?:\.\d+)? (?:ABOVE|BELOW)$")


def check_features(features: Mapping[str, Any]) -> None:
    """Raise PromptAssemblyError unless every required field is present and valid."""
    if not isinstance(features, Mapping):
        raise PromptAssemblyError(f"features must be a mapping, got {type(features).__name__}")
    missing = [f for f in REQUIRED_FIELDS if features.get(f) is None]
    if missing:
        raise PromptAssemblyError(f"features is missing required fields: {', '.join(missing)}")
    if not is_hhmm(features["time"]):
        raise PromptAssemblyError(f"time must be an 'HH:MM' string, got {features['time']!r}")
    for f in REQUIRED_FIELDS[1:]:
        num(features[f], f)


class _Reader:
    """Reads optional fields, dropping (and reporting) any that are broken."""

    def __init__(self, f: Mapping[str, Any], warnings: list[str]):
        self.f, self.warnings = f, warnings

    def number(self, key: str) -> float | None:
        value = self.f.get(key)
        if value is None:
            return None
        try:
            return num(value, key)
        except PromptAssemblyError as e:
            self.warnings.append(f"ignored {key}: {e}")
            return None

    def time(self, key: str) -> str | None:
        value = self.f.get(key)
        if value is None:
            return None
        if is_hhmm(value):
            return value
        self.warnings.append(f"ignored {key}: not an 'HH:MM' time ({value!r})")
        return None

    def choice(self, key: str, allowed: tuple[str, ...]) -> str | None:
        value = self.f.get(key)
        if value is None:
            return None
        norm = re.sub(r"[\s_-]+", " ", str(value)).strip().upper()
        if norm in allowed:
            return norm
        self.warnings.append(f"ignored {key}: {value!r} is not one of {', '.join(allowed)}")
        return None

    def flag(self, key: str) -> bool | None:
        value = self.f.get(key)
        if value is None or isinstance(value, bool):
            return value
        self.warnings.append(f"ignored {key}: expected true/false, got {value!r}")
        return None


def labels(features: Mapping[str, Any], warnings: list[str] | None = None) -> dict[str, Any]:
    """Labels the decision rules are written in. Comparisons only: no number
    shown to the LLM is computed here. The feature builder may send
    ``price_zone``, ``price_cheap`` or ``surplus_left`` itself; then its value is used."""
    r = _Reader(features, warnings if warnings is not None else [])
    price = num(features["price_now"], "price_now")
    # Plan section 6, fix 3: with a cheap refill coming first, that break-even applies.
    refill = r.number("break_even_before_refill")
    be = refill if refill is not None else num(features["break_even"], "break_even")
    diff = price - be

    zone = r.choice("price_zone", PRICE_ZONES)
    if zone is None:
        if diff > FAR_ABOVE_EUR:
            zone = "FAR ABOVE"
        elif diff > NEAR_EUR:
            zone = "ABOVE"
        elif diff >= -NEAR_EUR:
            zone = "NEAR"
        else:
            zone = "BELOW"

    cheap = r.flag("price_cheap")
    if cheap is None:
        rank = r.number("price_rank")
        cheap = price < 0 or (rank is not None and rank <= CHEAP_RANK)

    left = r.flag("surplus_left")
    if left is None:
        left = num(features["net_mw"], "net_mw") > num(features["max_charge_mw"], "max_charge_mw")

    return {"price_zone": zone, "price_negative": price < 0, "price_cheap": cheap, "surplus_left": left}


def render_situation(
    features: Mapping[str, Any],
    *,
    title: str = "CURRENT SITUATION",
    warnings: list[str] | None = None,
) -> str:
    check_features(features)
    f = features
    w = warnings if warnings is not None else []
    r = _Reader(f, w)

    lines = [f"{title} (interval starting {f['time']})"]

    # Site balance
    prod = f"production {mw(f['production_mw'])} MW"
    low, high = r.number("prod_low_mw"), r.number("prod_high_mw")
    if low is not None and high is not None:
        prod += f" (forecast range {mw(low)}–{mw(high)} MW)"
    net = f"net {signed_mw(f['net_mw'])} MW"
    if (label := r.choice("net_label", NET_LABELS)):
        net += f" ({label})"
    lines.append(f"- Site: demand {mw(f['demand_mw'])} MW, {prod} -> {net}")
    if (unc := r.choice("uncertainty", UNCERTAINTY_LEVELS)):
        line = f"- Forecast uncertainty: {unc}"
        risk = r.number("imbalance_risk_eur")
        if risk is not None:
            line += f", worth EUR {eur(risk)} if nothing absorbs it"
        lines.append(line)
    short, long = r.number("imbalance_short_premium"), r.number("imbalance_long_discount")
    if short is not None and long is not None:
        lines.append(f"- Deviating from the plan: EUR {eur(short)}/MWh extra when short, "
                     f"EUR {eur(long)}/MWh lost when long")

    # Prices
    price = f"- Price now: EUR {eur(f['price_now'])}/MWh"
    extra = []
    if (rank := r.number("price_rank")) is not None:
        extra.append(f"rank {eur(rank)}/100 today")
    if (avg := r.number("price_avg")) is not None:
        extra.append(f"day average EUR {eur(avg)}")
    if extra:
        price += f" ({', '.join(extra)})"
    lines.append(price)
    later = []
    for kind, word in (("max", "highest"), ("min", "lowest")):
        p = r.number(f"future_{kind}_price")
        if p is not None:
            t = r.time(f"future_{kind}_time")
            later.append(f"{word} EUR {eur(p)}" + (f" at {t}" if t else ""))
    if later:
        lines.append(f"- Later today: {'; '.join(later)}")

    be = f"- Break-even: EUR {eur(f['break_even'])}/MWh"
    pvb = f.get("price_vs_breakeven")
    if pvb is not None:
        if isinstance(pvb, str) and _PRICE_VS_BE_RE.match(pvb):
            be += f" -> price now is {pvb} break-even"
        else:
            w.append(f"ignored price_vs_breakeven: expected 'EUR <n> ABOVE|BELOW', got {pvb!r}")
    lines.append(be)
    if (refill := r.number("break_even_before_refill")) is not None:
        s = f"- Break-even before next refill: EUR {eur(refill)}/MWh"
        if (t := r.time("next_refill_time")):
            s += f" (cheap period at {t})"
        lines.append(s)

    # Battery
    stored = f"- Battery: {mw(f['stored_mwh'])} MWh stored"
    bounds = []
    if (lo := r.number("min_soc_mwh")) is not None:
        bounds.append(f"min {mw(lo)}")
    if (cap := r.number("capacity_mwh")) is not None:
        bounds.append(f"max {mw(cap)}")
    if bounds:
        stored += f" ({', '.join(bounds)} MWh)"
    lines.append(stored)
    lines.append("- This interval: " + _limit(f, r, "charge") + "; " + _limit(f, r, "discharge"))
    lines.append(f"- Matching fraction: {signed_frac(f['matching_fraction'])}")

    lab = labels(f, [])  # warnings for these fields were already collected above
    yes = lambda b: "YES" if b else "NO"  # noqa: E731
    lines.append(f"- Labels: price zone {lab['price_zone']}; price negative {yes(lab['price_negative'])}; "
                 f"price cheap {yes(lab['price_cheap'])}; surplus left after charge limit {yes(lab['surplus_left'])}")
    for key in ("price_zone", "price_cheap", "surplus_left"):  # report bad label inputs once
        before = len(w)
        (r.choice(key, PRICE_ZONES) if key == "price_zone" else r.flag(key))
        if len(w) > before:
            w[-1] += " (computed here instead)"
    return "\n".join(lines)


def _limit(f: Mapping[str, Any], r: _Reader, kind: str) -> str:
    s = f"{kind} up to {mw(f[f'max_{kind}_mw'])} MW"
    fraction = r.number(f"max_{kind}_fraction")
    if fraction is not None:
        s += f" (battery_fraction {'' if kind == 'charge' else '-'}{frac(fraction)})"
    return s
