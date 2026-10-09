"""Tests for Polymarket adapter."""

from decimal import Decimal

import httpx
import pytest

from prediction_markets_api.adapters.polymarket import PolymarketClient
from prediction_markets_api.models.base import Currency, OrderSide


@pytest.mark.asyncio
async def test_list_markets(markets_response, respx_mock):
    """Test listing markets with mocked API response."""
    respx_mock.get("https://gamma-api.polymarket.com/markets/keyset").mock(
        return_value=httpx.Response(200, json=markets_response)
    )

    async with PolymarketClient() as client:
        markets = []
        async for market in client.list_markets(closed=False, limit=10):
            markets.append(market)

    assert len(markets) == 2

    market1 = markets[0]
    assert market1.venue == "polymarket"
    assert market1.question == "Xi Jinping out before 2027?"
    assert market1.currency == Currency.USDC
    assert len(market1.outcomes) == 2
    assert market1.outcomes[0].name == "Yes"
    assert market1.outcomes[0].price == Decimal("0.0305")
    assert market1.outcomes[1].name == "No"
    assert market1.outcomes[1].price == Decimal("0.9695")
    assert market1.volume == Decimal("14667546.324205998")
    assert market1.liquidity == Decimal("607943.92913")

    market2 = markets[1]
    assert market2.question == "Will Gavin Newsom win the 2028 Democratic presidential nomination?"
    assert len(market2.outcomes) == 2


@pytest.mark.asyncio
async def test_get_market(markets_response, respx_mock):
    """Test getting a single market."""
    condition_id = "0xa467b14d51f01b957109d9cbb1d6c124fab2a089d52ed8f471d23c2812e743b7"

    single_market_response = {
        "markets": [markets_response["markets"][0]],
        "nextCursor": None,
    }

    respx_mock.get("https://gamma-api.polymarket.com/markets").mock(
        return_value=httpx.Response(200, json=single_market_response)
    )

    async with PolymarketClient() as client:
        market = await client.get_market(condition_id)

    assert market.id == condition_id
    assert market.question == "Xi Jinping out before 2027?"
    assert len(market.outcomes) == 2


@pytest.mark.asyncio
async def test_get_order_book(orderbook_response, respx_mock):
    """Test getting order book with mocked response."""
    outcome_id = "32338220190071351435772801779725302244575775216413325951443816017994629993401"

    respx_mock.get("https://clob.polymarket.com/book").mock(
        return_value=httpx.Response(200, json=orderbook_response)
    )

    async with PolymarketClient() as client:
        order_book = await client.get_order_book(outcome_id)

    assert order_book.outcome_id == outcome_id
    assert len(order_book.bids) == 3
    assert len(order_book.asks) == 3

    assert order_book.bids[0].price == Decimal("0.03")
    assert order_book.bids[0].size == Decimal("1769.01")

    assert order_book.asks[0].price == Decimal("0.031")
    assert order_book.asks[0].size == Decimal("4270.45")

    assert order_book.last_trade_price == Decimal("0.969")
    assert order_book.min_order_size == Decimal("5")
    assert order_book.tick_size == Decimal("0.001")
    assert order_book.currency == Currency.USDC

    order_book.validate_not_crossed()


@pytest.mark.asyncio
async def test_get_price(prices_response, respx_mock):
    """Test getting best executable price."""
    outcome_id = "32338220190071351435772801779725302244575775216413325951443816017994629993401"

    respx_mock.get("https://clob.polymarket.com/price").mock(
        return_value=httpx.Response(200, json=prices_response)
    )

    async with PolymarketClient() as client:
        buy_price = await client.get_price(outcome_id, OrderSide.BUY)

    assert buy_price.outcome_id == outcome_id
    assert buy_price.side == OrderSide.BUY
    assert buy_price.price == Decimal("0.03")
    assert buy_price.currency == Currency.USDC


@pytest.mark.asyncio
async def test_get_trades(trades_response, respx_mock):
    """Test getting historical trades."""
    outcome_id = "32338220190071351435772801779725302244575775216413325951443816017994629993401"

    respx_mock.get("https://data-api.polymarket.com/v2/trades").mock(
        return_value=httpx.Response(200, json=trades_response)
    )

    async with PolymarketClient() as client:
        trades = []
        async for trade in client.get_trades(outcome_id, limit=10):
            trades.append(trade)

    assert len(trades) == 2

    trade1 = trades[0]
    assert trade1.id == "0xbeef3f30b4188470347542ecd64b5470418e620e"
    assert trade1.outcome_id == outcome_id
    assert trade1.side == OrderSide.BUY
    assert trade1.price == Decimal("0.999")
    assert trade1.size == Decimal("10.0")
    assert trade1.currency == Currency.USDC

    trade2 = trades[1]
    assert trade2.side == OrderSide.BUY
    assert trade2.price == Decimal("0.5")
    assert trade2.size == Decimal("45.94")


@pytest.mark.asyncio
async def test_place_order_dry_run():
    """Test placing order in dry-run mode (default)."""
    async with PolymarketClient() as client:
        result = await client.place_order(
            outcome_id="token_123",
            side=OrderSide.BUY,
            size=Decimal("100"),
            price=Decimal("0.65"),
        )

    assert result["dry_run"] is True
    assert result["status"] == "simulated"
    assert "not sent" in result["message"]


@pytest.mark.asyncio
async def test_place_order_validation():
    """Test order validation."""
    async with PolymarketClient() as client:
        with pytest.raises(ValueError, match="Price must be between 0 and 1"):
            await client.place_order(
                outcome_id="token_123",
                side=OrderSide.BUY,
                size=Decimal("100"),
                price=Decimal("1.5"),
            )

        with pytest.raises(ValueError, match="Size must be positive"):
            await client.place_order(
                outcome_id="token_123",
                side=OrderSide.BUY,
                size=Decimal("-10"),
                price=Decimal("0.65"),
            )


@pytest.mark.asyncio
async def test_place_order_max_size_limit():
    """Test max order size enforcement."""
    async with PolymarketClient(max_order_size=Decimal("1000")) as client:
        with pytest.raises(RuntimeError, match="exceeds max_order_size"):
            await client.place_order(
                outcome_id="token_123",
                side=OrderSide.BUY,
                size=Decimal("2000"),
                price=Decimal("0.65"),
            )


@pytest.mark.asyncio
async def test_place_order_requires_api_keys():
    """Test that real orders require API keys."""
    async with PolymarketClient() as client:
        with pytest.raises(PermissionError, match="API credentials"):
            await client.place_order(
                outcome_id="token_123",
                side=OrderSide.BUY,
                size=Decimal("100"),
                price=Decimal("0.65"),
                dry_run=False,
            )


@pytest.mark.asyncio
async def test_market_not_found(respx_mock):
    """Test handling of market not found."""
    respx_mock.get("https://gamma-api.polymarket.com/markets").mock(
        return_value=httpx.Response(200, json={"markets": [], "nextCursor": None})
    )

    async with PolymarketClient() as client:
        with pytest.raises(ValueError, match="not found"):
            await client.get_market("nonexistent_market")


@pytest.mark.asyncio
async def test_api_error_handling(respx_mock):
    """Test API error handling."""
    respx_mock.get("https://gamma-api.polymarket.com/markets/keyset").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    async with PolymarketClient() as client:
        with pytest.raises(ValueError, match="Failed to fetch markets"):
            markets = []
            async for market in client.list_markets():
                markets.append(market)


@pytest.mark.asyncio
async def test_partial_market_with_invalid_outcomes(respx_mock):
    """Test handling of market data with malformed outcomes field."""
    partial_response = {
        "markets": [
            {
                "id": "123",
                "question": "Test market?",
                "conditionId": "0xabc123",
                "createdAt": "2026-01-01T00:00:00Z",
                "outcomes": "not a valid json",
                "outcomePrices": "[0.5, 0.5]",
                "clobTokenIds": "[123, 456]",
            }
        ],
        "nextCursor": None,
    }

    respx_mock.get("https://gamma-api.polymarket.com/markets/keyset").mock(
        return_value=httpx.Response(200, json=partial_response)
    )

    async with PolymarketClient() as client:
        markets = []
        async for market in client.list_markets():
            markets.append(market)

    assert len(markets) == 1
    market = markets[0]
    assert market.question == "Test market?"
    assert len(market.outcomes) == 0
