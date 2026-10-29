"""Unified Python client for prediction markets."""

from prediction_markets_api.adapters.hyperliquid import HyperliquidClient
from prediction_markets_api.adapters.kalshi import KalshiClient
from prediction_markets_api.adapters.limitless import LimitlessClient
from prediction_markets_api.adapters.pascal import PascalClient
from prediction_markets_api.adapters.polymarket import PolymarketClient
from prediction_markets_api.exceptions import (
    AuthenticationError,
    NoLiquidityError,
    NotFoundError,
    OrderRejectedError,
    PredictionMarketsError,
    RateLimitError,
    VenueAPIError,
)
from prediction_markets_api.models.base import (
    Currency,
    Market,
    Order,
    OrderBook,
    OrderLevel,
    OrderSide,
    OrderStatus,
    OrderType,
    Outcome,
    Position,
    Price,
    Trade,
)

__version__ = "0.9.0"

__all__ = [
    "PolymarketClient",
    "KalshiClient",
    "LimitlessClient",
    "HyperliquidClient",
    "PascalClient",
    "Market",
    "Outcome",
    "OrderBook",
    "OrderLevel",
    "OrderSide",
    "OrderType",
    "OrderStatus",
    "Order",
    "Position",
    "Price",
    "Trade",
    "Currency",
    "PredictionMarketsError",
    "NotFoundError",
    "VenueAPIError",
    "RateLimitError",
    "NoLiquidityError",
    "AuthenticationError",
    "OrderRejectedError",
]
