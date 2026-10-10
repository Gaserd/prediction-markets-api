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
async def test_list_markets_respects_limit(respx_mock):
    """Test that list_markets respects the limit parameter."""
    # Create a response with many markets
    markets_data = load_fixture("markets_response.json")
    # Make sure we have at least 5 markets in the fixture
    while len(markets_data["markets"]) < 5:
        markets_data["markets"].append(markets_data["markets"][0].copy())

    respx_mock.get(
        "https://api.elections.kalshi.com/trade-api/v2/markets"
    ).mock(return_value=httpx.Response(200, json=markets_data))

    # Test limit=2
    async with KalshiClient() as client:
        markets = []
        async for market in client.list_markets(limit=2):
            markets.append(market)

    assert len(markets) <= 2


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
        # Get prices from the order book
        buy_price = await client.get_price(ticker, OrderSide.BUY)
        sell_price = await client.get_price(ticker, OrderSide.SELL)

        # Verify prices exist and have raw_price
        assert buy_price.price > Decimal("0")
        assert sell_price.price > Decimal("0")
        assert buy_price.raw_price is not None
        assert sell_price.raw_price is not None


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


@pytest.mark.asyncio
async def test_price_matches_order_book_top(respx_mock):
    """Test that get_price matches the top of the order book.

    Fixtures recorded back-to-back from the same market.
    """
    markets_data = load_fixture("markets_response.json")
    ticker = markets_data["markets"][0]["ticker"]
    orderbook_data = load_fixture("orderbook_response.json")
    prices_data = load_fixture("prices_response.json")

    respx_mock.get(
        f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook"
    ).mock(return_value=httpx.Response(200, json=orderbook_data))

    async with KalshiClient() as client:
        book = await client.get_order_book(ticker)

        # Get prices from order book
        if book.bids:
            sell_price_from_book = book.bids[0].price
            sell_price = await client.get_price(ticker, OrderSide.SELL)
            assert sell_price.price == sell_price_from_book
            # Verify against fixture
            if prices_data.get("sell"):
                assert float(sell_price.price) == prices_data["sell"]["price"]

        if book.asks:
            buy_price_from_book = book.asks[0].price
            buy_price = await client.get_price(ticker, OrderSide.BUY)
            assert buy_price.price == buy_price_from_book
            # Verify against fixture
            if prices_data.get("buy"):
                assert float(buy_price.price) == prices_data["buy"]["price"]


@pytest.mark.asyncio
async def test_excludes_multivariate_by_default(respx_mock):
    """Test that mve_filter=exclude is sent by default to exclude KXMVE* markets server-side."""
    markets_data = load_fixture("markets_response.json")

    # Verify the request includes mve_filter=exclude
    route = respx_mock.get(
        "https://api.elections.kalshi.com/trade-api/v2/markets"
    ).mock(return_value=httpx.Response(200, json=markets_data))

    async with KalshiClient() as client:
        markets = []
        async for market in client.list_markets(limit=10):
            markets.append(market)

    # Verify mve_filter=exclude was sent (server-side filtering)
    assert route.called
    request = route.calls.last.request
    assert "mve_filter=exclude" in str(request.url), "mve_filter=exclude should be sent by default"


@pytest.mark.asyncio
async def test_includes_multivariate_when_opted_in(respx_mock):
    """Test that multivariate markets can be included with include_multivariate=True."""
    markets_data = load_fixture("markets_response.json")

    # Verify the request does NOT include mve_filter when opted in
    route = respx_mock.get(
        "https://api.elections.kalshi.com/trade-api/v2/markets"
    ).mock(return_value=httpx.Response(200, json=markets_data))

    async with KalshiClient() as client:
        markets = []
        async for market in client.list_markets(limit=10, include_multivariate=True):
            markets.append(market)

    # Verify mve_filter was NOT sent
    assert route.called
    request = route.calls.last.request
    assert "mve_filter" not in str(request.url)


@pytest.mark.asyncio
async def test_list_markets_returns_exact_limit(respx_mock):
    """Test that list_markets(limit=N) returns exactly N markets without extra page requests."""
    # Create fixture with enough markets
    markets_data = load_fixture("markets_response.json")

    # Ensure we have at least 5 markets
    while len(markets_data["markets"]) < 5:
        markets_data["markets"].append(markets_data["markets"][0].copy())

    # Mock the response
    route = respx_mock.get(
        "https://api.elections.kalshi.com/trade-api/v2/markets"
    ).mock(return_value=httpx.Response(200, json=markets_data))

    async with KalshiClient() as client:
        markets = []
        async for market in client.list_markets(limit=3):
            markets.append(market)

    # Should return exactly 3 markets
    assert len(markets) == 3

    # Should only make one API request (no pagination needed)
    assert route.call_count == 1
