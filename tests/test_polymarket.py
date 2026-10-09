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
    assert market1.question == "Will Bitcoin be above $100,000 on December 31, 2026?"
    assert market1.currency == Currency.USDC
    assert len(market1.outcomes) == 2
    assert market1.outcomes[0].name == "Yes"
    assert market1.outcomes[0].price == Decimal("0.65")
    assert market1.outcomes[1].name == "No"
    assert market1.outcomes[1].price == Decimal("0.35")
    assert market1.volume == Decimal("1250000.50")
    assert market1.liquidity == Decimal("45000.25")

    market2 = markets[1]
    assert market2.question == "Will Tesla stock reach $500 by June 30, 2026?"
    assert len(market2.outcomes) == 2


@pytest.mark.asyncio
async def test_get_market(markets_response, respx_mock):
    """Test getting a single market."""
    condition_id = "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"

    single_market_response = {
        "data": [markets_response["data"][0]],
        "next_cursor": None,
    }

    respx_mock.get("https://gamma-api.polymarket.com/markets").mock(
        return_value=httpx.Response(200, json=single_market_response)
    )

    async with PolymarketClient() as client:
        market = await client.get_market(condition_id)

    assert market.id == condition_id
    assert market.question == "Will Bitcoin be above $100,000 on December 31, 2026?"
    assert len(market.outcomes) == 2


@pytest.mark.asyncio
async def test_get_order_book(orderbook_response, respx_mock):
    """Test getting order book with mocked response."""
    outcome_id = "71321045679252212594626385532706912750332785719425328963137931232"

    respx_mock.get("https://clob.polymarket.com/book").mock(
        return_value=httpx.Response(200, json=orderbook_response)
    )

    async with PolymarketClient() as client:
        order_book = await client.get_order_book(outcome_id)

    assert order_book.outcome_id == outcome_id
    assert len(order_book.bids) == 3
    assert len(order_book.asks) == 3

    assert order_book.bids[0].price == Decimal("0.64")
    assert order_book.bids[0].size == Decimal("1500.00")

    assert order_book.asks[0].price == Decimal("0.66")
    assert order_book.asks[0].size == Decimal("1200.00")

    assert order_book.last_trade_price == Decimal("0.65")
    assert order_book.min_order_size == Decimal("1.00")
    assert order_book.tick_size == Decimal("0.01")
    assert order_book.currency == Currency.USDC

    order_book.validate_not_crossed()


@pytest.mark.asyncio
async def test_get_price(prices_response, respx_mock):
    """Test getting best executable price."""
    outcome_id = "71321045679252212594626385532706912750332785719425328963137931232"

    respx_mock.get("https://clob.polymarket.com/prices").mock(
        return_value=httpx.Response(200, json=prices_response)
    )

    async with PolymarketClient() as client:
        buy_price = await client.get_price(outcome_id, OrderSide.BUY)
        sell_price = await client.get_price(outcome_id, OrderSide.SELL)

    assert buy_price.outcome_id == outcome_id
    assert buy_price.side == OrderSide.BUY
    assert buy_price.price == Decimal("0.66")
    assert buy_price.currency == Currency.USDC

    assert sell_price.outcome_id == outcome_id
    assert sell_price.side == OrderSide.SELL
    assert sell_price.price == Decimal("0.64")
    assert sell_price.currency == Currency.USDC


@pytest.mark.asyncio
async def test_get_trades(trades_response, respx_mock):
    """Test getting historical trades."""
    outcome_id = "71321045679252212594626385532706912750332785719425328963137931232"

    respx_mock.get("https://data-api.polymarket.com/v2/trades").mock(
        return_value=httpx.Response(200, json=trades_response)
    )

    async with PolymarketClient() as client:
        trades = []
        async for trade in client.get_trades(outcome_id, limit=10):
            trades.append(trade)

    assert len(trades) == 2

    trade1 = trades[0]
    assert trade1.id == "trade_1"
    assert trade1.outcome_id == outcome_id
    assert trade1.side == OrderSide.BUY
    assert trade1.price == Decimal("0.65")
    assert trade1.size == Decimal("100.00")
    assert trade1.currency == Currency.USDC

    trade2 = trades[1]
    assert trade2.id == "trade_2"
    assert trade2.side == OrderSide.SELL
    assert trade2.price == Decimal("0.64")
    assert trade2.size == Decimal("150.00")


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
        return_value=httpx.Response(200, json={"data": [], "next_cursor": None})
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
