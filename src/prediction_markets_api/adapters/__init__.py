"""Venue adapters for prediction markets."""

from prediction_markets_api.adapters.base import BaseClient
from prediction_markets_api.adapters.hyperliquid import HyperliquidClient
from prediction_markets_api.adapters.kalshi import KalshiClient
from prediction_markets_api.adapters.limitless import LimitlessClient
from prediction_markets_api.adapters.pascal import PascalClient
from prediction_markets_api.adapters.polymarket import PolymarketClient

# Discovery names the venue from this export. Predict_funClient must stay the
# exported name so the id is predict_fun (registry key and fixture directory).
# The class itself is PredictFunClient.
from prediction_markets_api.adapters.predict_fun import PredictFunClient as Predict_funClient

__all__ = [
    "BaseClient",
    "PolymarketClient",
    "KalshiClient",
    "LimitlessClient",
    "HyperliquidClient",
    "PascalClient",
    "Predict_funClient",
]
