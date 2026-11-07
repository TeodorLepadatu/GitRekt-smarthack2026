"""ML 2: national electricity production.

Three series are modelled separately because they have different drivers:
  solar             irradiance, cloud, sun angle; divided by the 30-day max (tracks PV growth)
  wind_onshore      wind speed; divided by the 365-day max
  other_production  total - solar - wind (nuclear, gas, lignite, hydro, biomass, storage):
                    dispatched to follow demand; divided by the 7-day mean
total = solar + wind + other; ranges are combined assuming independent errors.

    from predictions import ProductionForecaster
    model = ProductionForecaster.load()              # models/production_model.pkl
    model.predict([ts1, ts2, ...])
"""

from __future__ import annotations

from predictions.core import BaseForecaster, combine


class ProductionForecaster(BaseForecaster):
    NAME = "production_model"
    SPECS = {
        "solar": dict(scale="max_30d", seasonal="lag_day"),
        "wind_onshore": dict(scale="max_365d", seasonal="mean_1d"),
        "other_production": dict(scale="mean_7d", seasonal="lag_week"),
    }
    SERIES_COL = {"solar": "solar", "wind": "wind_onshore", "renewables": "renewables",
                  "total_production": "total_production"}

    def _series(self, p):
        s, w, o = p["solar"], p["wind_onshore"], p["other_production"]
        return {"solar": s, "wind": w, "renewables": combine([s, w]),
                "total_production": combine([s, w, o])}

    def _columns(self, fc):
        s, w, r, t = fc["solar"], fc["wind"], fc["renewables"], fc["total_production"]
        return {
            "solar_mw": s[:, 1], "solar_low_mw": s[:, 0], "solar_high_mw": s[:, 2],
            "wind_mw": w[:, 1], "wind_low_mw": w[:, 0], "wind_high_mw": w[:, 2],
            "prod_low_mw": r[:, 0], "prod_high_mw": r[:, 2],          # solar + wind range
            "total_mw": t[:, 1], "total_low_mw": t[:, 0], "total_high_mw": t[:, 2],
        }
