"""Read-method limit validation and get_trades signature checks."""

from importlib.metadata import version

import pytest
import respx

from prediction_markets_api import __version__
from prediction_markets_api.adapters.base import BaseClient, validate_limit
from prediction_markets_api.adapters.hyperliquid import HyperliquidClient
from prediction_markets_api.adapters.kalshi import KalshiClient
from prediction_markets_api.adapters.limitless import LimitlessClient
from prediction_markets_api.adapters.pascal import PascalClient
from prediction_markets_api.adapters.polymarket import PolymarketClient

READ_CLIENTS: list[type[BaseClient]] = [
    PolymarketClient,
    KalshiClient,
    LimitlessClient,
    HyperliquidClient,
    PascalClient,
]


def test_package_version_is_0_9_0() -> None:
    assert __version__ == "0.9.0"
    assert version("prediction-markets-api") == "0.9.0"


def test_validate_limit_helper() -> None:
    validate_limit(None)
    validate_limit(1)
    for bad in (0, -1):
        with pytest.raises(ValueError, match="limit"):
            validate_limit(bad)


@pytest.mark.parametrize("client_cls", READ_CLIENTS)
async def test_read_methods_reject_non_positive_limit(client_cls: type[BaseClient]) -> None:
    """limit <= 0 raises ValueError on every adapter before any HTTP call."""
    async with client_cls() as client:
        with respx.mock as respx_mock:
            for bad_limit in (0, -1):
                with pytest.raises(ValueError, match="limit"):
                    async for _ in client.list_markets(limit=bad_limit):
                        pass
                with pytest.raises(ValueError, match="limit"):
                    async for _ in client.get_trades("outcome-id", limit=bad_limit):
                        pass
            assert len(respx_mock.calls) == 0


@pytest.mark.parametrize("client_cls", READ_CLIENTS)
async def test_get_trades_unknown_keyword_raises(client_cls: type[BaseClient]) -> None:
    async with client_cls() as client:
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            client.get_trades("outcome-id", not_a_parameter=True)  # type: ignore[call-arg]
