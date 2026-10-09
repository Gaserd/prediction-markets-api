"""Live tests for Kalshi adapter (opt-in with -m live)."""

import pytest

from prediction_markets_api import KalshiClient
from prediction_markets_api.models.base import OrderSide


@pytest.mark.live
@pytest.mark.asyncio
async def test_list_markets_live():
    """Test listing real markets from Kalshi."""
    async with KalshiClient() as client:
        markets = []
        async for market in client.list_markets(limit=5):
            markets.append(market)
            if len(markets) >= 3:
                break

        assert len(markets) > 0
        assert all(m.venue == "kalshi" for m in markets)

        print(f"\nFetched {len(markets)} markets:")
        for market in markets:
            print(f"  {market.id}: {market.question}")


@pytest.mark.live
@pytest.mark.asyncio
async def test_get_market_live():
    """Test getting a specific market."""
    async with KalshiClient() as client:
        # Get first market
        async for market in client.list_markets(limit=1):
            ticker = market.id
            break
        else:
            pytest.skip("No markets available")

        # Fetch it again by ID
        market = await client.get_market(ticker)

        assert market.id == ticker
        assert market.venue == "kalshi"
        assert market.question

        print(f"\nMarket: {market.question}")
        print(f"  ID: {market.id}")
        print(f"  Created: {market.created_at}")
        print(f"  Outcomes: {len(market.outcomes)}")


@pytest.mark.live
@pytest.mark.asyncio
async def test_get_order_book_live():
    """Test getting order book from live API."""
    async with KalshiClient() as client:
        # Get first market
        async for market in client.list_markets(limit=1):
            ticker = market.id
            break
        else:
            pytest.skip("No markets available")

        # Get order book
        book = await client.get_order_book(ticker)

        assert book.outcome_id == ticker
        print(f"\nOrder book for {ticker}:")
        print(f"  Bids: {len(book.bids)}")
        print(f"  Asks: {len(book.asks)}")

        if book.bids:
            print(f"  Best bid: {book.bids[0].price}")
        if book.asks:
            print(f"  Best ask: {book.asks[0].price}")


@pytest.mark.live
@pytest.mark.asyncio
async def test_get_price_live():
    """Test getting best price from live API."""
    async with KalshiClient() as client:
        # Find a market with liquidity
        ticker_with_liquidity = None
        async for market in client.list_markets(limit=20):
            try:
                book = await client.get_order_book(market.id)
                if book.bids or book.asks:
                    ticker_with_liquidity = market.id
                    break
            except Exception:
                continue

        if not ticker_with_liquidity:
            pytest.skip("No markets with liquidity found")

        # Get prices
        buy_price = None
        sell_price = None

        try:
            buy_price = await client.get_price(ticker_with_liquidity, OrderSide.BUY)
            print(f"\nBuy price (best ask): {buy_price.price}")
        except ValueError as e:
            print(f"\nNo buy liquidity: {e}")

        try:
            sell_price = await client.get_price(ticker_with_liquidity, OrderSide.SELL)
            print(f"Sell price (best bid): {sell_price.price}")
        except ValueError as e:
            print(f"No sell liquidity: {e}")

        # At least one should work
        assert buy_price is not None or sell_price is not None


@pytest.mark.live
@pytest.mark.asyncio
async def test_get_trades_live():
    """Test getting historical trades."""
    async with KalshiClient() as client:
        # Get first market
        async for market in client.list_markets(limit=1):
            ticker = market.id
            break
        else:
            pytest.skip("No markets available")

        # Get trades (may be empty)
        trades = []
        try:
            async for trade in client.get_trades(ticker, limit=5):
                trades.append(trade)
        except ValueError:
            # Trades endpoint may not exist for all markets
            pass

        print(f"\nFetched {len(trades)} trades for {ticker}")
        if trades:
            print(f"  Latest: {trades[0].timestamp} - {trades[0].side.value} @ {trades[0].price}")
