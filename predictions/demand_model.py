"""ML 1: national electricity demand (actual_total_load).

    from predictions import DemandForecaster
    model = DemandForecaster.load()                  # models/demand_model.pkl
    model.predict([ts1, ts2, ...])                   # any 15-min timestamps up to 7 days ahead
"""

from __future__ import annotations

from predictions.core import BaseForecaster


class DemandForecaster(BaseForecaster):
    NAME = "demand_model"
    SPECS = {
        # divided by the 7-day mean; long-lead base = same quarter-hour last week
        "actual_total_load": dict(scale="mean_7d", seasonal="lag_week"),
    }
    SERIES_COL = {"demand": "actual_total_load"}

    def _series(self, p):
        return {"demand": p["actual_total_load"]}

    def _columns(self, fc):
        d = fc["demand"]
        return {"demand_mw": d[:, 1], "demand_low_mw": d[:, 0], "demand_high_mw": d[:, 2]}
