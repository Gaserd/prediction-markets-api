"""Live smoke tests for Pascal (opt-in with -m live)."""

from decimal import Decimal

import pytest

from prediction_markets_api import PascalClient
from prediction_markets_api.models.base import MarketStatus, OrderSide


@pytest.mark.live
@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_list_and_get_market_live() -> None:
    """List a few unresolved markets and fetch one by symbol."""
    async with PascalClient() as client:
        markets = [market async for market in client.list_markets(closed=False, limit=3)]
        assert markets
        assert len(markets) <= 3
        assert all(market.venue == "pascal" for market in markets)
        assert all(market.status == MarketStatus.OPEN for market in markets)
        fetched = await client.get_market(markets[0].id)
        assert fetched.id == markets[0].id


@pytest.mark.live
@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_order_book_price_and_trades_live() -> None:
    """Read a two-sided book and a short trade window."""
    async with PascalClient() as client:
        target = None
        async for market in client.list_markets(closed=False, limit=30):
            if not market.outcomes:
                continue
            book = await client.get_order_book(market.outcomes[0].id)
            if book.bids and book.asks:
                target = market.outcomes[0].id
                break
        if target is None:
            pytest.skip("No two-sided Pascal book in the first 30 markets")
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


@pytest.mark.live
@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_resolved_listing_live() -> None:
    """The resolved endpoint is public and lookback-limited."""
    async with PascalClient() as client:
        markets = [market async for market in client.list_markets(closed=True, limit=2)]
    assert len(markets) <= 2
    for market in markets:
        assert market.resolved is True
        assert market.status == MarketStatus.RESOLVED
