"""Live tests for Limitless adapter (opt-in with -m live)."""

import pytest

from prediction_markets_api import LimitlessClient
from prediction_markets_api.models.base import OrderSide


@pytest.mark.live
@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_list_markets_live():
    """Test listing real markets from Limitless."""
    async with LimitlessClient() as client:
        markets = []
        async for market in client.list_markets(limit=5):
            markets.append(market)

        assert len(markets) > 0
        assert len(markets) <= 5
        assert all(m.venue == "limitless" for m in markets)

        print(f"\nFetched {len(markets)} markets")


@pytest.mark.live
@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_get_market_live():
    """Test getting a specific market."""
    async with LimitlessClient() as client:
        # Get first market
        slug = None
        async for market in client.list_markets(limit=1):
            slug = market.id
            break

        if not slug:
            pytest.skip("No markets available")

        # Fetch it again by ID
        market = await client.get_market(slug)

        assert market.id == slug
        assert market.venue == "limitless"
        assert market.question

        print(f"\nMarket: {market.question}")


@pytest.mark.live
@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_get_order_book_live():
    """Test getting order book from live API."""
    async with LimitlessClient() as client:
        # Find a CLOB market (scan max 20)
        clob_slug = None
        scanned = 0
        max_scan = 20

        async for market in client.list_markets(limit=max_scan):
            scanned += 1
            if scanned > max_scan:
                break

            try:
                book = await client.get_order_book(market.id)
                clob_slug = market.id
                break
            except ValueError:
                # May be AMM or non-tradeable
                continue

        if not clob_slug:
            pytest.skip(f"No CLOB markets found in {max_scan} markets")

        # Get order book
        book = await client.get_order_book(clob_slug)

        assert book.outcome_id == clob_slug
        print(f"\nOrder book for {clob_slug}:")
        print(f"  Bids: {len(book.bids)}")
        print(f"  Asks: {len(book.asks)}")


@pytest.mark.live
@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_get_price_live():
    """Test getting best price from live API."""
    async with LimitlessClient() as client:
        # Find a market with liquidity (scan max 20)
        slug_with_liquidity = None
        scanned = 0
        max_scan = 20

        async for market in client.list_markets(limit=max_scan):
            scanned += 1
            if scanned > max_scan:
                break

            try:
                book = await client.get_order_book(market.id)
                if book.bids and book.asks:
                    slug_with_liquidity = market.id
                    break
            except ValueError:
                continue

        if not slug_with_liquidity:
            pytest.skip(f"No markets with liquidity found in {max_scan} markets")

        # Get prices
        buy_price = None
        sell_price = None

        try:
            buy_price = await client.get_price(slug_with_liquidity, OrderSide.BUY)
            print(f"\nBuy price (best ask): {buy_price.price}")
        except ValueError as e:
            print(f"\nNo buy liquidity: {e}")

        try:
            sell_price = await client.get_price(slug_with_liquidity, OrderSide.SELL)
            print(f"Sell price (best bid): {sell_price.price}")
        except ValueError as e:
            print(f"No sell liquidity: {e}")

        # At least one should work
        assert buy_price is not None or sell_price is not None


@pytest.mark.live
@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_get_price_matches_book_live():
    """Test that get_price matches top of book on live data."""
    async with LimitlessClient() as client:
        # Find a market with orders on both sides (scan max 20)
        slug_with_both_sides = None
        scanned = 0
        max_scan = 20

        async for market in client.list_markets(limit=max_scan):
            scanned += 1
            if scanned > max_scan:
                break

            try:
                book = await client.get_order_book(market.id)
                if book.bids and book.asks:
                    slug_with_both_sides = market.id
                    break
            except ValueError:
                continue

        if not slug_with_both_sides:
            pytest.skip(f"No markets with orders on both sides found in {max_scan} markets")

        # Get book and prices back-to-back
        book = await client.get_order_book(slug_with_both_sides)
        buy_price = await client.get_price(slug_with_both_sides, OrderSide.BUY)
        sell_price = await client.get_price(slug_with_both_sides, OrderSide.SELL)

        # Verify get_price(BUY) returns best ask (top of ask side)
        assert buy_price.price == book.asks[0].price
        assert buy_price.raw_price == book.asks[0].raw_price

        # Verify get_price(SELL) returns best bid (top of bid side)
        assert sell_price.price == book.bids[0].price
        assert sell_price.raw_price == book.bids[0].raw_price

        print(f"\nPrice consistency verified for {slug_with_both_sides}")
        print(f"  Best bid: {sell_price.price}")
        print(f"  Best ask: {buy_price.price}")


@pytest.mark.live
@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_get_trades_live():
    """Test getting historical trades."""
    async with LimitlessClient() as client:
        # Get first market
        slug = None
        async for market in client.list_markets(limit=1):
            slug = market.id
            break

        if not slug:
            pytest.skip("No markets available")

        # Get trades (may be empty)
        trades = []
        try:
            async for trade in client.get_trades(slug, limit=5):
                trades.append(trade)
        except ValueError:
            # Trades endpoint may not have data for all markets
            pass

        print(f"\nFetched {len(trades)} trades for {slug}")
