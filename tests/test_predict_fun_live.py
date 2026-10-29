"""Live smoke tests for Predict.fun mainnet (opt-in with -m live).

Mainnet reads require ``PREDICTFUN_API_KEY``. These tests skip when it is unset.
They do not print the key.
"""

import os
from decimal import Decimal

import pytest

from prediction_markets_api import PredictFunClient
from prediction_markets_api.models.base import MarketStatus, OrderSide


def _require_key() -> None:
    if not os.getenv("PREDICTFUN_API_KEY"):
        pytest.skip("PREDICTFUN_API_KEY not set; Predict.fun mainnet reads require it")


@pytest.mark.live
@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_list_and_get_market_live() -> None:
    """List a few open markets and fetch one by id."""
    _require_key()
    async with PredictFunClient() as client:
        markets = [market async for market in client.list_markets(closed=False, limit=3)]
        assert markets
        assert len(markets) <= 3
        assert all(market.venue == "predict_fun" for market in markets)
        assert all(market.status == MarketStatus.OPEN for market in markets)
        fetched = await client.get_market(markets[0].id)
        assert fetched.id == markets[0].id


@pytest.mark.live
@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_order_book_price_and_trades_live() -> None:
    """Read a two-sided book and a short trade window."""
    _require_key()
    async with PredictFunClient() as client:
        target = None
        async for market in client.list_markets(closed=False, limit=15):
            if not market.outcomes:
                continue
            book = await client.get_order_book(market.outcomes[0].id)
            if book.bids and book.asks:
                target = market.outcomes[0].id
                break
        if target is None:
            pytest.skip("No two-sided Predict.fun book in the first 15 markets")
        book = await client.get_order_book(target)
        buy = await client.get_price(target, OrderSide.BUY)
        sell = await client.get_price(target, OrderSide.SELL)
        assert buy.price == book.asks[0].price
        assert sell.price == book.bids[0].price
        trades = [trade async for trade in client.get_trades(target, limit=3)]
        assert len(trades) <= 3
        for trade in trades:
            assert Decimal("0") <= trade.price <= Decimal("1")
            assert trade.timestamp.tzinfo is not None
