"""A made-up but believable day at the site's scale (battery 10 MWh / 3 MW), for
running the loop before the real ML models and price feeds exist.

Prices: night around EUR 80, a morning bump at 08:00, negative prices over midday
solar, an evening peak around EUR 165. Demand 1.5-4.5 MW, solar up to about 6 MW
at noon, some wind. Actuals are the forecast plus seeded noise, so forecast
errors show up in the books. The same seed always gives the same day.
"""
from __future__ import annotations

import math
import random

from feature_builder import SiteForecast

from .day import DayInput

N = 96  # 15-minute intervals


def _price(h: float) -> float:
    p = 80 + 10 * math.cos((h - 3) / 24 * 2 * math.pi)       # night baseline
    p += 35 * math.exp(-((h - 8) ** 2) / 2)                    # morning bump
    p -= 110 * math.exp(-((h - 12.5) ** 2) / 3)                # midday solar glut (negative)
    p += 85 * math.exp(-((h - 19.5) ** 2) / 2.5)               # evening peak
    return round(p, 2)


def _demand(h: float) -> float:
    return 1.5 + 1.6 * math.exp(-((h - 9) ** 2) / 6) + 2.0 * math.exp(-((h - 19) ** 2) / 5)


def _solar(h: float) -> float:
    return max(0.0, 6.2 * math.sin((h - 6) / 13 * math.pi)) if 6 <= h <= 19 else 0.0


def _wind(h: float) -> float:
    return 0.9 + 0.6 * math.sin(h / 24 * 2 * math.pi + 1.0)


def demo_day(seed: int = 7, date: str = "demo") -> DayInput:
    rng = random.Random(seed)
    prices, forecasts, actuals = [], [], []
    for i in range(N):
        h = i / 4 + 0.125  # middle of the interval
        demand, solar, wind = _demand(h), _solar(h), _wind(h)
        production = solar + wind
        band = 0.15 + 0.12 * solar + 0.35 * wind               # ML 2 range: wider with more sun and wind
        prices.append(_price(h))
        forecasts.append(SiteForecast(
            demand_mw=round(demand, 3), production_mw=round(production, 3),
            solar_mw=round(solar, 3), wind_mw=round(wind, 3),
            prod_low_mw=round(max(0.0, production - band), 3), prod_high_mw=round(production + band, 3),
        ))
        actuals.append({
            "demand_mw": round(max(0.0, demand + rng.gauss(0, 0.12)), 3),
            "production_mw": round(max(0.0, production + rng.gauss(0, band / 2)), 3),
        })
    return DayInput(prices=prices, forecasts=forecasts, actuals=actuals, date=date)
