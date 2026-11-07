"""Demand (ML 1) and production (ML 2) forecasting.

    from predictions import DemandForecaster, ProductionForecaster
    demand = DemandForecaster.load()
    demand.predict([ts1, ts2, ...])

HTTP API: uvicorn predictions.api:app   (see predictions/api.py)
"""

from predictions.demand_model import DemandForecaster
from predictions.production_model import ProductionForecaster

__all__ = ["DemandForecaster", "ProductionForecaster"]
