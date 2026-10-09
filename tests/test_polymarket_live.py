"""Live smoke tests for Polymarket (opt-in, skipped by default).

Run with: pytest -m live
"""

from decimal import Decimal

import pytest

from prediction_markets_api.adapters.polymarket import PolymarketClient
from prediction_markets_api.models.base import OrderSide


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_list_markets():
    """Test listing real markets from Polymarket."""
    async with PolymarketClient(timeout=60.0) as client:
        markets = []
        async for market in client.list_markets(closed=False, limit=5):
            markets.append(market)
            if len(markets) >= 3:
                break

        assert len(markets) > 0

        for market in markets:
            assert market.venue == "polymarket"
            assert market.question
            assert market.id
            assert len(market.outcomes) >= 2

            print(f"\nMarket: {market.question}")
            print(f"  ID: {market.id}")
            print(f"  Outcomes: {len(market.outcomes)}")
            for outcome in market.outcomes:
                print(f"    - {outcome.name}: {outcome.price}")


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_order_book():
    """Test fetching real order book from Polymarket."""
    async with PolymarketClient(timeout=60.0) as client:
        markets = []
        async for market in client.list_markets(closed=False, limit=1):
            markets.append(market)
            break

        assert len(markets) == 1
        market = markets[0]

        assert len(market.outcomes) > 0
        outcome = market.outcomes[0]

        order_book = await client.get_order_book(outcome.id)

        assert order_book.outcome_id == outcome.id
        assert len(order_book.bids) > 0 or len(order_book.asks) > 0

        print(f"\nOrder Book for {outcome.name}:")
        print(f"  Bids: {len(order_book.bids)}")
        if order_book.bids:
            print(f"    Best: {order_book.bids[0].price} x {order_book.bids[0].size}")
        print(f"  Asks: {len(order_book.asks)}")
        if order_book.asks:
            print(f"    Best: {order_book.asks[0].price} x {order_book.asks[0].size}")

        if order_book.bids and order_book.asks:
            order_book.validate_not_crossed()


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_prices():
    """Test fetching real prices from Polymarket."""
    async with PolymarketClient(timeout=60.0) as client:
        markets = []
        async for market in client.list_markets(closed=False, limit=1):
            markets.append(market)
            break

        assert len(markets) == 1
        market = markets[0]

        assert len(market.outcomes) > 0
        outcome = market.outcomes[0]

        buy_price = await client.get_price(outcome.id, OrderSide.BUY)
        sell_price = await client.get_price(outcome.id, OrderSide.SELL)

        assert buy_price.outcome_id == outcome.id
        assert sell_price.outcome_id == outcome.id
        assert buy_price.price >= Decimal("0")
        assert buy_price.price <= Decimal("1")
        assert sell_price.price >= Decimal("0")
        assert sell_price.price <= Decimal("1")

    print(f"\nPrices for {outcome.name}:")
    print(f"  BUY (best ask): {buy_price.price}")
    print(f"  SELL (best bid): {sell_price.price}")
    print(f"  Spread: {abs(buy_price.price - sell_price.price)}")


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_dry_run_order():
    """Test dry-run order (safe, no actual trading)."""
    async with PolymarketClient(max_order_size=Decimal("1000")) as client:
        markets = []
        async for market in client.list_markets(closed=False, limit=1):
            markets.append(market)
            break

        assert len(markets) == 1
        market = markets[0]

        assert len(market.outcomes) > 0
        outcome = market.outcomes[0]

        result = await client.place_order(
            outcome_id=outcome.id,
            side=OrderSide.BUY,
            size=Decimal("10"),
            price=Decimal("0.50"),
            dry_run=True,
        )

        assert result["dry_run"] is True
        assert result["status"] == "simulated"

        print("\nDry-run order result:")
        print(f"  Outcome: {outcome.name}")
        print(f"  Status: {result['status']}")
        print(f"  Message: {result['message']}")
