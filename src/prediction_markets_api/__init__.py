"""Unified Python client for prediction markets."""

from prediction_markets_api.adapters.kalshi import KalshiClient
from prediction_markets_api.adapters.limitless import LimitlessClient
from prediction_markets_api.adapters.polymarket import PolymarketClient
from prediction_markets_api.models.base import (
    Currency,
    Market,
    OrderBook,
    OrderLevel,
    OrderSide,
    Outcome,
    Price,
    Trade,
)

__version__ = "0.1.0"

__all__ = [
    "PolymarketClient",
    "KalshiClient",
    "LimitlessClient",
    "Market",
    "Outcome",
    "OrderBook",
    "OrderLevel",
    "OrderSide",
    "Price",
    "Trade",
    "Currency",
]
