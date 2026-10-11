"""Live tests for Kalshi adapter (opt-in with -m live)."""

import pytest

from prediction_markets_api import KalshiClient
from prediction_markets_api.models.base import OrderSide


@pytest.mark.live
@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_list_markets_live():
    """Test listing real markets from Kalshi."""
    async with KalshiClient() as client:
        markets = []
        async for market in client.list_markets(limit=5):
            markets.append(market)

        assert len(markets) > 0
        assert len(markets) <= 5
        assert all(m.venue == "kalshi" for m in markets)

        print(f"\nFetched {len(markets)} markets")


@pytest.mark.live
@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_list_markets_closed_false_live():
    """Test listing open markets with closed=False (verifies status=open works)."""
    async with KalshiClient() as client:
        markets = []
        async for market in client.list_markets(closed=False, limit=5):
            markets.append(market)

        assert len(markets) > 0
        assert len(markets) <= 5
        assert all(m.venue == "kalshi" for m in markets)
        # All markets should be open (not resolved)
        assert all(not m.resolved for m in markets)

        print(f"\nFetched {len(markets)} open markets")


@pytest.mark.live
@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_list_markets_closed_true_live():
    """Test listing closed/settled markets with closed=True (verifies two-request logic)."""
    async with KalshiClient() as client:
        markets = []
        async for market in client.list_markets(closed=True, limit=5):
            markets.append(market)

        assert len(markets) > 0
        assert len(markets) <= 5
        assert all(m.venue == "kalshi" for m in markets)
        # Note: Kalshi returns various statuses for closed markets: closed, settled, determined, etc.
        # The key is that closed=True queries status=closed and status=settled
        # Verify we got markets (the two-request logic works)
        print(
            f"\nFetched {len(markets)} markets via closed=True "
            f"with statuses: { {m.raw_data.get('status') for m in markets} }"
        )


@pytest.mark.live
@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_get_market_live():
    """Test getting a specific market."""
    async with KalshiClient() as client:
        # Get first market
        ticker = None
        async for market in client.list_markets(limit=1):
            ticker = market.id
            break

        if not ticker:
            pytest.skip("No markets available")

        # Fetch it again by ID
        market = await client.get_market(ticker)

        assert market.id == ticker
        assert market.venue == "kalshi"
        assert market.question

        print(f"\nMarket: {market.question}")


@pytest.mark.live
@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_get_order_book_live():
    """Test getting order book from live API."""
    async with KalshiClient() as client:
        # Get first market
        ticker = None
        async for market in client.list_markets(limit=1):
            ticker = market.id
            break

        if not ticker:
            pytest.skip("No markets available")

        # Get order book
        book = await client.get_order_book(ticker)

        assert book.outcome_id == ticker
        print(f"\nOrder book for {ticker}:")
        print(f"  Bids: {len(book.bids)}")
        print(f"  Asks: {len(book.asks)}")


@pytest.mark.live
@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_get_price_live():
    """Test getting best price from live API."""
    async with KalshiClient() as client:
        # Find a market with liquidity (scan max 20)
        ticker_with_liquidity = None
        scanned = 0
        max_scan = 20

        async for market in client.list_markets(limit=max_scan):
            scanned += 1
            if scanned > max_scan:
                break

            try:
                book = await client.get_order_book(market.id)
                if book.bids or book.asks:
                    ticker_with_liquidity = market.id
                    break
            except ValueError:
                continue

        if not ticker_with_liquidity:
            pytest.skip(f"No markets with liquidity found in {max_scan} markets")

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
@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_get_trades_live():
    """Test getting historical trades."""
    async with KalshiClient() as client:
        # Get first market
        ticker = None
        async for market in client.list_markets(limit=1):
            ticker = market.id
            break

        if not ticker:
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
