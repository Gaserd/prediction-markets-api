"""Live smoke tests for Hyperliquid Outcomes (opt-in with -m live)."""

from decimal import Decimal

import pytest

from prediction_markets_api import HyperliquidClient
from prediction_markets_api.exceptions import NotFoundError
from prediction_markets_api.models.base import MarketStatus, OrderSide


@pytest.mark.live
@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_list_markets_live():
    """Test listing real HIP-4 markets."""
    async with HyperliquidClient() as client:
        markets = []
        async for market in client.list_markets(limit=5):
            markets.append(market)

    assert len(markets) > 0
    assert len(markets) <= 5
    assert all(market.venue == "hyperliquid" for market in markets)
    assert all(market.created_at.tzinfo is not None for market in markets)


@pytest.mark.live
@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_list_markets_closed_true_live():
    """Settled markets are not listable."""
    async with HyperliquidClient() as client:
        with pytest.raises(NotImplementedError):
            async for _ in client.list_markets(closed=True, limit=1):
                pass


@pytest.mark.live
@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_get_market_live():
    """Test fetching one listed market by id."""
    async with HyperliquidClient() as client:
        market_id = None
        async for market in client.list_markets(closed=False, limit=1):
            market_id = market.id
            break
        if market_id is None:
            pytest.skip("No open Hyperliquid markets")

        fetched = await client.get_market(market_id)
        assert fetched.id == market_id
        assert fetched.venue == "hyperliquid"
        assert fetched.question


@pytest.mark.live
@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_get_order_book_and_price_live():
    """Test the book and best prices for a market that has both sides."""
    async with HyperliquidClient() as client:
        target = None
        scanned = 0
        async for market in client.list_markets(closed=False, limit=30):
            scanned += 1
            outcome_id = market.outcomes[0].id if market.outcomes else None
            if outcome_id is None:
                continue
            try:
                book = await client.get_order_book(outcome_id)
            except NotFoundError:
                continue
            if book.bids and book.asks:
                target = outcome_id
                break
        if target is None:
            pytest.skip(f"No two-sided book in {scanned} markets")

        book = await client.get_order_book(target)
        buy = await client.get_price(target, OrderSide.BUY)
        sell = await client.get_price(target, OrderSide.SELL)
        assert buy.price == book.asks[0].price
        assert sell.price == book.bids[0].price
        assert Decimal("0") <= sell.price <= buy.price <= Decimal("1")


@pytest.mark.live
@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_get_trades_live():
    """recentTrades returns a short window (possibly empty for a quiet coin)."""
    async with HyperliquidClient() as client:
        outcome_id = None
        async for market in client.list_markets(closed=False, limit=10):
            if market.outcomes:
                outcome_id = market.outcomes[0].id
                break
        if outcome_id is None:
            pytest.skip("No outcomes to query trades for")

        trades = []
        async for trade in client.get_trades(outcome_id, limit=5):
            trades.append(trade)
        assert len(trades) <= 5
        for trade in trades:
            assert trade.timestamp.tzinfo is not None
            assert Decimal("0") <= trade.price <= Decimal("1")


@pytest.mark.live
@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_get_settled_market_live():
    """Outcome 95 is a historical settled BTC binary (No won)."""
    async with HyperliquidClient() as client:
        market = await client.get_market("95")
        assert market.id == "95"
        assert market.resolved is True
        assert market.status == MarketStatus.RESOLVED
        assert market.winning_outcome == "No"
