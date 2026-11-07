"""Feature builder: every number the rest of the cycle reads (plan section 2).

    from feature_builder import SiteForecast, build_features
    from pipeline import run_cycle

    features = build_features(
        time="15:00",
        forecast=SiteForecast(demand_mw=4.6, production_mw=5.0,
                              prod_low_mw=4.4, prod_high_mw=5.6),
        prices=day_ahead_prices,      # today's 96 EUR/MWh values
        stored_mwh=3.5,
        goal_config=store.config,
    )
    result = run_cycle(features, store.config)
"""

from .builder import (
    WEAR_EUR_PER_MWH,
    FeatureBuilderError,
    SiteForecast,
    build_features,
)
from .goals import even_pace_fraction, goals_progress, latest_start_minutes, reserve_progress
from .prices import PriceCurve, PriceCurveError, hhmm, intervals_to_empty

__all__ = [
    "build_features",
    "SiteForecast",
    "FeatureBuilderError",
    "WEAR_EUR_PER_MWH",
    "PriceCurve",
    "PriceCurveError",
    "hhmm",
    "intervals_to_empty",
    "goals_progress",
    "reserve_progress",
    "even_pace_fraction",
    "latest_start_minutes",
]
