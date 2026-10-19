"""Tests for Kalshi adapter."""

import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from prediction_markets_api import KalshiClient
from prediction_markets_api.models.base import Currency, Market, MarketStatus, OrderSide

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
    respx_mock.route(url__regex=r".*status=closed.*").mock(
        return_value=httpx.Response(200, json=closed_response)
    )

    respx_mock.route(url__regex=r".*status=settled.*").mock(
        return_value=httpx.Response(200, json=settled_response)
    )

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
    respx_mock.route(url__regex=r".*status=closed.*").mock(
        return_value=httpx.Response(200, json=closed_response)
    )

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

    respx_mock.route(url__regex=r".*status=closed.*").mock(
        return_value=httpx.Response(200, json=closed_response)
    )

    respx_mock.route(url__regex=r".*status=settled.*").mock(
        return_value=httpx.Response(200, json=settled_response)
    )

    async with KalshiClient() as client:
        markets = []
        async for market in client.list_markets(closed=True, limit=10):
            markets.append(market)

    # Should only yield 2 unique markets (duplicate filtered out)
    assert len(markets) == 2
    tickers = [m.id for m in markets]
    assert duplicate_ticker in tickers
    assert "SETTLED-02" in tickers


@pytest.mark.asyncio
async def test_status_mapping_from_fixtures():
    """Test that Kalshi status fixtures map to correct normalized statuses."""
    import json
    from pathlib import Path

    fixtures_dir = Path("tests/fixtures/kalshi/statuses")
    meta_path = fixtures_dir / "meta.json"

    assert meta_path.exists(), "Status fixtures meta.json not found"

    with open(meta_path) as f:
        meta = json.load(f)

    async with KalshiClient() as client:
        for status_name, expected_data in meta["statuses"].items():
            fixture_path = fixtures_dir / f"{status_name}.json"
            assert fixture_path.exists(), f"Fixture {status_name}.json not found"

            with open(fixture_path) as f:
                market_data = json.load(f)

            market = client._parse_market(market_data)

            # Check venue_status
            assert market.venue_status == status_name, (
                f"Expected venue_status={status_name}, got {market.venue_status}"
            )

            # Check normalized status
            expected_status = expected_data["expected_normalized_status"]
            if expected_status == "open":
                assert market.status == MarketStatus.OPEN
            elif expected_status == "closed":
                assert market.status == MarketStatus.CLOSED
            elif expected_status == "resolved":
                assert market.status == MarketStatus.RESOLVED

            # Check resolved flag
            assert market.resolved == expected_data["expected_resolved"]

            print(
                f"✓ {status_name}: venue_status={market.venue_status}, status={market.status}, resolved={market.resolved}"
            )


@pytest.mark.asyncio
async def test_unknown_status_logs_warning(caplog):
    """Test that unknown Kalshi status logs a warning."""
    async with KalshiClient() as client:
        # Create market data with unknown status
        market_data = {
            "ticker": "TEST-UNKNOWN",
            "title": "Test Market",
            "open_time": "2026-01-01T00:00:00Z",
            "status": "UNKNOWN_STATUS",
        }

        with caplog.at_level("WARNING"):
            market = client._parse_market(market_data)

        # Should have logged a warning
        assert any(
            "Unknown Kalshi market status 'UNKNOWN_STATUS'" in record.message
            for record in caplog.records
        )

        # Status should be None (not normalized)
        assert market.status is None
        assert market.venue_status == "UNKNOWN_STATUS"


@pytest.mark.asyncio
async def test_determined_and_finalized_are_resolved():
    """Test that determined and finalized markets are marked as resolved."""
    async with KalshiClient() as client:
        for status_name in ["determined", "finalized"]:
            market_data = {
                "ticker": f"TEST-{status_name.upper()}",
                "title": "Test Market",
                "open_time": "2026-01-01T00:00:00Z",
                "status": status_name,
            }

            market = client._parse_market(market_data)

            assert market.resolved is True, f"{status_name} should be resolved=True"
            assert market.status == MarketStatus.RESOLVED, f"{status_name} should map to RESOLVED"


@pytest.mark.asyncio
async def test_closed_none_excludes_initialized(respx_mock):
    """Test that closed=None filters out initialized markets client-side."""
    mock_response = {
        "markets": [
            {
                "ticker": "INITIALIZED-01",
                "title": "Initialized Market",
                "open_time": "2026-01-01T00:00:00Z",
                "status": "initialized",
            },
            {
                "ticker": "ACTIVE-01",
                "title": "Active Market",
                "open_time": "2026-01-02T00:00:00Z",
                "status": "active",
            },
            {
                "ticker": "DETERMINED-01",
                "title": "Determined Market",
                "open_time": "2026-01-03T00:00:00Z",
                "status": "determined",
            },
        ],
        "cursor": None,
    }

    respx_mock.get("https://api.elections.kalshi.com/trade-api/v2/markets").mock(
        return_value=httpx.Response(200, json=mock_response)
    )

    async with KalshiClient() as client:
        markets = []
        async for market in client.list_markets(closed=None, limit=10):
            markets.append(market)

    # Should only get active and determined (initialized filtered out)
    assert len(markets) == 2
    assert all(m.venue_status != "initialized" for m in markets)
    tickers = [m.id for m in markets]
    assert "ACTIVE-01" in tickers
    assert "DETERMINED-01" in tickers
    assert "INITIALIZED-01" not in tickers


@pytest.mark.asyncio
async def test_closed_none_applies_limit_after_filtering(respx_mock):
    """Test that closed=None applies limit AFTER filtering out initialized markets."""
    # Return more markets than the limit, including some initialized
    mock_response = {
        "markets": [
            {
                "ticker": f"INIT-{i:02d}",
                "title": f"Init {i}",
                "open_time": "2026-01-01T00:00:00Z",
                "status": "initialized",
            }
            for i in range(5)
        ]
        + [
            {
                "ticker": f"ACTIVE-{i:02d}",
                "title": f"Active {i}",
                "open_time": "2026-01-01T00:00:00Z",
                "status": "active",
            }
            for i in range(5)
        ],
        "cursor": None,
    }

    respx_mock.get("https://api.elections.kalshi.com/trade-api/v2/markets").mock(
        return_value=httpx.Response(200, json=mock_response)
    )

    async with KalshiClient() as client:
        markets = []
        async for market in client.list_markets(closed=None, limit=3):
            markets.append(market)

    # Should get exactly 3 markets (all active, initialized filtered out before limit)
    assert len(markets) == 3
    assert all(m.venue_status == "active" for m in markets)


@pytest.mark.asyncio
async def test_get_price_not_found(respx_mock):
    """Test get_price with 404 error."""
    from prediction_markets_api.exceptions import NotFoundError

    respx_mock.get(re.compile(r".*/markets/.*/orderbook")).mock(
        return_value=httpx.Response(404, json={"error": "Not found"})
    )

    async with KalshiClient() as client:
        with pytest.raises(NotFoundError, match="not found"):
            await client.get_price("NONEXISTENT", OrderSide.BUY)


@pytest.mark.asyncio
async def test_list_markets_500_error(respx_mock):
    """Test list_markets with 500 error."""
    from prediction_markets_api.exceptions import VenueAPIError

    respx_mock.get("https://api.elections.kalshi.com/trade-api/v2/markets").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    async with KalshiClient() as client:
        with pytest.raises(VenueAPIError, match="Failed to fetch markets"):
            async for _ in client.list_markets(limit=1):
                pass


@pytest.mark.asyncio
async def test_rate_limit_error(respx_mock):
    """Test 429 rate limit error."""
    from prediction_markets_api.exceptions import RateLimitError

    respx_mock.get(re.compile(r".*/markets/.*")).mock(
        return_value=httpx.Response(429, text="Rate limit exceeded")
    )

    async with KalshiClient() as client:
        with pytest.raises(RateLimitError, match="Rate limit"):
            await client.get_market("SOME-MARKET")
