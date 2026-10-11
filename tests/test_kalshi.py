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
    respx_mock.get("https://api.elections.kalshi.com/trade-api/v2/markets").mock(
        return_value=httpx.Response(200, json=markets_data)
    )

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

    respx_mock.get("https://api.elections.kalshi.com/trade-api/v2/markets").mock(
        return_value=httpx.Response(200, json=markets_data)
    )

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

    respx_mock.get(f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}").mock(
        return_value=httpx.Response(200, json={"market": markets_data["markets"][0]})
    )

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

    respx_mock.get(f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}/trades").mock(
        return_value=httpx.Response(200, json=trades_data)
    )

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
async def test_get_trades_rejects_unknown_kwargs(respx_mock):
    """Test that get_trades raises TypeError for unknown keyword arguments."""
    ticker = "TEST-TICKER"

    async with KalshiClient() as client:
        with pytest.raises(TypeError):
            async for _ in client.get_trades(ticker, unknown_param=123):
                pass


@pytest.mark.asyncio
async def test_list_markets_with_category(respx_mock):
    """Test list_markets with category filter."""
    mock_response = {
        "markets": [
            {
                "ticker": "TEST-01",
                "title": "Test Market 1",
                "open_time": "2026-01-01T00:00:00Z",
                "status": "active",
            }
        ],
        "cursor": None,
    }

    respx_mock.get("https://api.elections.kalshi.com/trade-api/v2/markets").mock(
        return_value=httpx.Response(200, json=mock_response)
    )

    async with KalshiClient() as client:
        markets = []
        async for market in client.list_markets(category="politics"):
            markets.append(market)

    assert len(markets) == 1
    requests = respx_mock.calls
    assert len(requests) == 1
    assert "category=politics" in requests[0].request.url.query.decode()


@pytest.mark.asyncio
async def test_list_markets_closed_false_uses_status_open(respx_mock):
    """Test that closed=False maps to status=open (Kalshi valid status)."""
    mock_response = {
        "markets": [
            {
                "ticker": "TEST-01",
                "title": "Test Market 1",
                "open_time": "2026-01-01T00:00:00Z",
                "status": "open",
            }
        ],
        "cursor": None,
    }

    respx_mock.get("https://api.elections.kalshi.com/trade-api/v2/markets").mock(
        return_value=httpx.Response(200, json=mock_response)
    )

    async with KalshiClient() as client:
        markets = []
        async for market in client.list_markets(closed=False, limit=5):
            markets.append(market)

    assert len(markets) == 1
    requests = respx_mock.calls
    assert len(requests) == 1
    query = requests[0].request.url.query.decode()
    # Must use status=open (valid Kalshi status: unopened, open, closed, settled)
    assert "status=open" in query
    # Must NOT use status=active (invalid, causes 400)
    assert "status=active" not in query
    # Should also have mve_filter=exclude by default
    assert "mve_filter=exclude" in query


@pytest.mark.asyncio
async def test_list_markets_closed_true_two_requests(respx_mock):
    """Test that closed=True makes two requests: status=closed and status=settled."""
    closed_response = {
        "markets": [
            {
                "ticker": "CLOSED-01",
                "title": "Closed Market 1",
                "open_time": "2026-01-01T00:00:00Z",
                "status": "closed",
            },
            {
                "ticker": "CLOSED-02",
                "title": "Closed Market 2",
                "open_time": "2026-01-02T00:00:00Z",
                "status": "closed",
            },
        ],
        "cursor": None,
    }

    settled_response = {
        "markets": [
            {
                "ticker": "SETTLED-01",
                "title": "Settled Market 1",
                "open_time": "2026-01-03T00:00:00Z",
                "status": "settled",
            }
        ],
        "cursor": None,
    }

    # Mock both requests
    respx_mock.route(
        url__regex=r".*status=closed.*"
    ).mock(return_value=httpx.Response(200, json=closed_response))

    respx_mock.route(
        url__regex=r".*status=settled.*"
    ).mock(return_value=httpx.Response(200, json=settled_response))

    async with KalshiClient() as client:
        markets = []
        async for market in client.list_markets(closed=True, limit=10):
            markets.append(market)

    # Should get all 3 markets (2 closed + 1 settled)
    assert len(markets) == 3

    # Should have made 2 requests
    requests = respx_mock.calls
    assert len(requests) == 2

    # Verify first request was for closed
    assert "status=closed" in requests[0].request.url.query.decode()
    # Verify second request was for settled
    assert "status=settled" in requests[1].request.url.query.decode()


@pytest.mark.asyncio
async def test_list_markets_closed_true_limit_short_circuit(respx_mock):
    """Test that closed=True stops after first request if limit is reached."""
    closed_response = {
        "markets": [
            {
                "ticker": f"CLOSED-{i:02d}",
                "title": f"Closed Market {i}",
                "open_time": "2026-01-01T00:00:00Z",
                "status": "closed",
            }
            for i in range(1, 6)  # 5 markets
        ],
        "cursor": None,
    }

    # Mock closed request
    respx_mock.route(
        url__regex=r".*status=closed.*"
    ).mock(return_value=httpx.Response(200, json=closed_response))

    async with KalshiClient() as client:
        markets = []
        async for market in client.list_markets(closed=True, limit=3):
            markets.append(market)

    # Should only get 3 markets (limit reached)
    assert len(markets) == 3

    # Should have made only 1 request (closed), not 2, because limit was reached
    requests = respx_mock.calls
    assert len(requests) == 1
    assert "status=closed" in requests[0].request.url.query.decode()


@pytest.mark.asyncio
async def test_list_markets_deduplicates_across_requests(respx_mock):
    """Test that duplicate markets across status requests are deduplicated."""
    # Same market appears in both responses (shouldn't happen in practice, but handle it)
    duplicate_ticker = "DUPLICATE-01"

    closed_response = {
        "markets": [
            {
                "ticker": duplicate_ticker,
                "title": "Duplicate Market",
                "open_time": "2026-01-01T00:00:00Z",
                "status": "closed",
            }
        ],
        "cursor": None,
    }

    settled_response = {
        "markets": [
            {
                "ticker": duplicate_ticker,
                "title": "Duplicate Market",
                "open_time": "2026-01-01T00:00:00Z",
                "status": "settled",
            },
            {
                "ticker": "SETTLED-02",
                "title": "Unique Settled",
                "open_time": "2026-01-02T00:00:00Z",
                "status": "settled",
            },
        ],
        "cursor": None,
    }

    respx_mock.route(
        url__regex=r".*status=closed.*"
    ).mock(return_value=httpx.Response(200, json=closed_response))

    respx_mock.route(
        url__regex=r".*status=settled.*"
    ).mock(return_value=httpx.Response(200, json=settled_response))

    async with KalshiClient() as client:
        markets = []
        async for market in client.list_markets(closed=True, limit=10):
            markets.append(market)

    # Should only yield 2 unique markets (duplicate filtered out)
    assert len(markets) == 2
    tickers = [m.id for m in markets]
    assert duplicate_ticker in tickers
    assert "SETTLED-02" in tickers
