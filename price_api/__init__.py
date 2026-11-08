"""Mock energy price API (server: ``price_api.app``) and its client.

Only the client is imported here, so ``loop`` can use it without pulling in FastAPI.
"""
from .client import DayPrices, PriceApiError, fetch_day

__all__ = ["DayPrices", "PriceApiError", "fetch_day"]
