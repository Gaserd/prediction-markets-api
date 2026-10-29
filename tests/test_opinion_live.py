"""Live smoke tests for Opinion (opt-in with -m live).

Public market, order book, and price reads do not need an API key.
``GET /openapi/trade`` is the caller's own fills and skips when
``OPINION_API_KEY`` is unset. These tests do not print a key. Normal CI
deselects this module with ``-m "not live"``.
"""

import os
from decimal import Decimal

import pytest

from prediction_markets_api import OpinionClient
from prediction_markets_api.exceptions import AuthenticationError
from prediction_markets_api.models.base import MarketStatus, OrderSide


@pytest.mark.live
@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_list_and_get_market_live() -> None:
    """List a few open markets and fetch one by id. No API key."""
    async with OpinionClient() as client:
        markets = [market async for market in client.list_markets(closed=False, limit=3)]
        assert markets
        assert len(markets) <= 3
        assert all(market.venue == "opinion" for market in markets)
        assert all(market.status == MarketStatus.OPEN for market in markets)
        fetched = await client.get_market(markets[0].id)
        assert fetched.id == markets[0].id
        assert fetched.outcomes


@pytest.mark.live
@pytest.mark.timeout(90)
@pytest.mark.asyncio
async def test_order_book_and_price_live() -> None:
    """Read one binary book. The NO book is the complement of the YES book."""
    async with OpinionClient() as client:
        target = None
        async for market in client.list_markets(
            closed=False, limit=12, venue_params={"market_type": 0}
        ):
            if len(market.outcomes) != 2:
                continue
            book = await client.get_order_book(market.id)
            if book.bids and book.asks:
                target = market
                break
        if target is None:
            pytest.skip("No two-sided Opinion binary book in the first 12 markets")
        yes_book = await client.get_order_book(target.outcomes[0].id)
        no_book = await client.get_order_book(target.outcomes[1].id)
        buy = await client.get_price(target.id, OrderSide.BUY)
        sell = await client.get_price(target.id, OrderSide.SELL)
        assert buy.price == yes_book.asks[0].price
        assert sell.price == yes_book.bids[0].price
        if yes_book.bids and no_book.asks:
            assert no_book.asks[0].price == Decimal("1") - yes_book.bids[0].price
        if yes_book.asks and no_book.bids:
            assert no_book.bids[0].price == Decimal("1") - yes_book.asks[0].price


@pytest.mark.live
@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_user_trades_live() -> None:
    """Read the caller's fills when a key is configured. Skip otherwise."""
    if not os.getenv("OPINION_API_KEY"):
        pytest.skip("OPINION_API_KEY not set; Opinion user trade history requires it")
    async with OpinionClient() as client:
        markets = [market async for market in client.list_markets(closed=False, limit=1)]
        assert markets
        trades = [trade async for trade in client.get_trades(markets[0].id, limit=3)]
        assert len(trades) <= 3
        for trade in trades:
            assert Decimal("0") <= trade.price <= Decimal("1")
            assert trade.timestamp.tzinfo is not None


@pytest.mark.live
@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_missing_trade_key_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing key fails before the trade request, including on the live marker."""
    monkeypatch.delenv("OPINION_API_KEY", raising=False)
    async with OpinionClient() as client:
        with pytest.raises(AuthenticationError, match="OPINION_API_KEY"):
            async for _ in client.get_trades("1", limit=1):
                pass
