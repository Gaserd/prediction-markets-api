"""Tests for Kalshi adapter."""

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from prediction_markets_api import KalshiClient
from prediction_markets_api.models.base import Currency, Market, OrderSide

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "kalshi"


def load_fixture(filename: str) -> dict:
    """Load a JSON fixture."""
    with open(FIXTURES_DIR / filename) as f:
        return json.load(f)


@pytest.mark.asyncio
async def test_list_markets(respx_mock):
    """Test listing markets from Kalshi."""
    # Load and mock the API response
    markets_data = load_fixture("markets_response.json")
    respx_mock.get(
        "https://api.elections.kalshi.com/trade-api/v2/markets"
    ).mock(return_value=httpx.Response(200, json=markets_data))

    # Fetch markets
    async with KalshiClient() as client:
        markets = []
        async for market in client.list_markets(limit=3):
            markets.append(market)

    # Validate
    assert len(markets) == 3
    assert all(isinstance(m, Market) for m in markets)
    assert all(m.venue == "kalshi" for m in markets)
    assert all(m.currency == Currency.USD for m in markets)

    # Check first market
    first_market = markets[0]
    assert first_market.id
    assert first_market.question
    assert isinstance(first_market.created_at, datetime)
    assert first_market.created_at.tzinfo is not None


@pytest.mark.asyncio
async def test_get_market(respx_mock):
    """Test getting a specific market."""
    markets_data = load_fixture("markets_response.json")
    ticker = markets_data["markets"][0]["ticker"]

    respx_mock.get(
        f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}"
    ).mock(return_value=httpx.Response(200, json={"market": markets_data["markets"][0]}))

    async with KalshiClient() as client:
        market = await client.get_market(ticker)

    assert market.id == ticker
    assert market.venue == "kalshi"
    assert market.question
    assert len(market.outcomes) == 2  # Binary market: Yes/No
    assert market.outcomes[0].name == "Yes"
    assert market.outcomes[1].name == "No"


@pytest.mark.asyncio
async def test_get_order_book(respx_mock):
    """Test getting order book."""
    markets_data = load_fixture("markets_response.json")
    ticker = markets_data["markets"][0]["ticker"]
    orderbook_data = load_fixture("orderbook_response.json")

    respx_mock.get(
        f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook"
    ).mock(return_value=httpx.Response(200, json=orderbook_data))

    async with KalshiClient() as client:
        book = await client.get_order_book(ticker)

    assert book.outcome_id == ticker
    assert book.market_id == ticker
    assert isinstance(book.timestamp, datetime)
    assert book.timestamp.tzinfo is not None
    assert book.currency == Currency.USD

    # Empty orderbook is valid
    assert isinstance(book.bids, list)
    assert isinstance(book.asks, list)


@pytest.mark.asyncio
async def test_get_price(respx_mock):
    """Test getting best price (derived from orderbook)."""
    markets_data = load_fixture("markets_response.json")
    ticker = markets_data["markets"][0]["ticker"]
    orderbook_data = load_fixture("orderbook_response.json")

    respx_mock.get(
        f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook"
    ).mock(return_value=httpx.Response(200, json=orderbook_data))

    async with KalshiClient() as client:
        # For empty book, should raise ValueError
        with pytest.raises(ValueError, match="No liquidity"):
            await client.get_price(ticker, OrderSide.BUY)


@pytest.mark.asyncio
async def test_get_trades(respx_mock):
    """Test getting historical trades."""
    markets_data = load_fixture("markets_response.json")
    ticker = markets_data["markets"][0]["ticker"]
    trades_data = load_fixture("trades_response.json")

    respx_mock.get(
        f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}/trades"
    ).mock(return_value=httpx.Response(200, json=trades_data))

    async with KalshiClient() as client:
        trades = []
        async for trade in client.get_trades(ticker, limit=10):
            trades.append(trade)

    # Empty trades is valid
    assert isinstance(trades, list)


@pytest.mark.asyncio
async def test_place_order_dry_run():
    """Test placing order in dry-run mode."""
    async with KalshiClient() as client:
        result = await client.place_order(
            outcome_id="TEST-TICKER",
            side=OrderSide.BUY,
            size=Decimal("10"),
            price=Decimal("0.55"),
            dry_run=True,
        )

    assert result["dry_run"] is True
    assert result["status"] == "simulated"


@pytest.mark.asyncio
async def test_place_order_real_requires_api_keys():
    """Test that real orders require API keys."""
    async with KalshiClient() as client:
        with pytest.raises(PermissionError, match="API credentials"):
            await client.place_order(
                outcome_id="TEST-TICKER",
                side=OrderSide.BUY,
                size=Decimal("10"),
                price=Decimal("0.55"),
                dry_run=False,
            )


@pytest.mark.asyncio
async def test_price_validation():
    """Test order price validation."""
    async with KalshiClient() as client:
        with pytest.raises(ValueError, match="Price must be between 0 and 1"):
            await client.place_order(
                outcome_id="TEST-TICKER",
                side=OrderSide.BUY,
                size=Decimal("10"),
                price=Decimal("1.5"),  # Invalid
            )


@pytest.mark.asyncio
async def test_size_validation():
    """Test order size validation."""
    async with KalshiClient() as client:
        with pytest.raises(ValueError, match="Size must be positive"):
            await client.place_order(
                outcome_id="TEST-TICKER",
                side=OrderSide.BUY,
                size=Decimal("-10"),  # Invalid
                price=Decimal("0.5"),
            )


@pytest.mark.asyncio
async def test_max_order_size_limit():
    """Test max order size enforcement."""
    async with KalshiClient(max_order_size=Decimal("100")) as client:
        with pytest.raises(RuntimeError, match="exceeds max_order_size"):
            await client.place_order(
                outcome_id="TEST-TICKER",
                side=OrderSide.BUY,
                size=Decimal("200"),  # Exceeds limit
                price=Decimal("0.5"),
            )
