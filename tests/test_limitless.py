"""Tests for Limitless adapter using recorded API fixtures."""

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from prediction_markets_api.adapters.limitless import LimitlessClient
from prediction_markets_api.models.base import Currency, MarketStatus, OrderSide


@pytest.fixture
def limitless_client():
    """Create Limitless client for testing."""
    return LimitlessClient(timeout=10.0)


@pytest.fixture
def fixtures_dir():
    """Path to Limitless fixtures directory."""
    return Path(__file__).parent / "fixtures" / "limitless"


@pytest.fixture
def markets_response(fixtures_dir):
    """Load markets response fixture."""
    with open(fixtures_dir / "markets_response.json") as f:
        return json.load(f)


@pytest.fixture
def orderbook_response(fixtures_dir):
    """Load orderbook response fixture."""
    with open(fixtures_dir / "orderbook_response.json") as f:
        return json.load(f)


@pytest.fixture
def trades_response(fixtures_dir):
    """Load trades response fixture."""
    with open(fixtures_dir / "trades_response.json") as f:
        return json.load(f)


@pytest.mark.asyncio
async def test_list_markets(limitless_client, markets_response):
    """Test list_markets with recorded fixture."""
    mock_response = MagicMock()
    mock_response.json.return_value = markets_response
    mock_response.raise_for_status = MagicMock()

    with patch.object(
        limitless_client._http_client, "get", new=AsyncMock(return_value=mock_response)
    ):
        markets = []
        async for market in limitless_client.list_markets(limit=3):
            markets.append(market)

        assert len(markets) == 3
        assert all(m.venue == "limitless" for m in markets)
        assert all(m.currency == Currency.USDC for m in markets)

        # Check first market
        first = markets[0]
        assert first.id == "btc-up-or-down-5-min-1791612600"
        assert first.question == "BTC Up or Down - 5 Min"
        assert len(first.outcomes) == 2
        assert first.outcomes[0].name == "Yes"
        assert first.outcomes[1].name == "No"

        # Verify prices are normalized to 0..1
        assert first.outcomes[0].price == Decimal("0.4845") / Decimal("100")
        assert first.outcomes[1].price == Decimal("0.5155") / Decimal("100")


@pytest.mark.asyncio
async def test_get_market(limitless_client, markets_response):
    """Test get_market with recorded fixture."""
    market_data = markets_response["data"][0]

    mock_response = MagicMock()
    mock_response.json.return_value = market_data
    mock_response.raise_for_status = MagicMock()

    with patch.object(
        limitless_client._http_client, "get", new=AsyncMock(return_value=mock_response)
    ):
        market = await limitless_client.get_market("btc-up-or-down-5-min-1791612600")

        assert market.id == "btc-up-or-down-5-min-1791612600"
        assert market.venue == "limitless"
        assert market.question == "BTC Up or Down - 5 Min"
        assert market.currency == Currency.USDC


@pytest.mark.asyncio
async def test_get_order_book(limitless_client, orderbook_response):
    """Test get_order_book with recorded fixture."""
    mock_response = MagicMock()
    mock_response.json.return_value = orderbook_response
    mock_response.raise_for_status = MagicMock()

    with patch.object(
        limitless_client._http_client, "get", new=AsyncMock(return_value=mock_response)
    ):
        book = await limitless_client.get_order_book("btc-up-or-down-5-min-1791612600")

        assert book.outcome_id == "btc-up-or-down-5-min-1791612600"
        assert book.market_id == "btc-up-or-down-5-min-1791612600"
        assert book.currency == Currency.USDC

        # Verify bids are sorted descending (best first)
        assert len(book.bids) > 0
        for i in range(len(book.bids) - 1):
            assert book.bids[i].price >= book.bids[i + 1].price

        # Verify asks are sorted ascending (best first)
        assert len(book.asks) > 0
        for i in range(len(book.asks) - 1):
            assert book.asks[i].price <= book.asks[i + 1].price

        # Verify book is not crossed
        if book.bids and book.asks:
            assert book.bids[0].price < book.asks[0].price

        # Verify sizes are converted from raw 6-decimal units
        # Fixture has bid size 1030000000, which should be 1030 shares
        assert book.bids[0].size == Decimal("1030")


@pytest.mark.asyncio
async def test_get_order_book_and_price_consistency(limitless_client, orderbook_response):
    """Test that get_price returns top of book from get_order_book."""
    mock_response = MagicMock()
    mock_response.json.return_value = orderbook_response
    mock_response.raise_for_status = MagicMock()

    with patch.object(
        limitless_client._http_client, "get", new=AsyncMock(return_value=mock_response)
    ):
        # Get order book
        book = await limitless_client.get_order_book("btc-up-or-down-5-min-1791612600")

        # Get prices
        buy_price = await limitless_client.get_price(
            "btc-up-or-down-5-min-1791612600", OrderSide.BUY
        )
        sell_price = await limitless_client.get_price(
            "btc-up-or-down-5-min-1791612600", OrderSide.SELL
        )

        # Verify get_price(BUY) returns best ask
        assert buy_price.price == book.asks[0].price
        assert buy_price.raw_price == book.asks[0].raw_price

        # Verify get_price(SELL) returns best bid
        assert sell_price.price == book.bids[0].price
        assert sell_price.raw_price == book.bids[0].raw_price


@pytest.mark.asyncio
async def test_get_price_buy(limitless_client, orderbook_response):
    """Test get_price for BUY side."""
    mock_response = MagicMock()
    mock_response.json.return_value = orderbook_response
    mock_response.raise_for_status = MagicMock()

    with patch.object(
        limitless_client._http_client, "get", new=AsyncMock(return_value=mock_response)
    ):
        price = await limitless_client.get_price("btc-up-or-down-5-min-1791612600", OrderSide.BUY)

        assert price.side == OrderSide.BUY
        assert price.outcome_id == "btc-up-or-down-5-min-1791612600"
        assert price.currency == Currency.USDC
        # BUY should return best ask (lowest ask price = 0.968)
        assert price.price == Decimal("0.968")


@pytest.mark.asyncio
async def test_get_price_sell(limitless_client, orderbook_response):
    """Test get_price for SELL side."""
    mock_response = MagicMock()
    mock_response.json.return_value = orderbook_response
    mock_response.raise_for_status = MagicMock()

    with patch.object(
        limitless_client._http_client, "get", new=AsyncMock(return_value=mock_response)
    ):
        price = await limitless_client.get_price("btc-up-or-down-5-min-1791612600", OrderSide.SELL)

        assert price.side == OrderSide.SELL
        assert price.outcome_id == "btc-up-or-down-5-min-1791612600"
        assert price.currency == Currency.USDC
        # SELL should return best bid (highest bid price = 0.001)
        assert price.price == Decimal("0.001")


@pytest.mark.asyncio
async def test_get_trades(limitless_client, trades_response):
    """Test get_trades with recorded fixture."""
    mock_response = MagicMock()
    mock_response.json.return_value = trades_response
    mock_response.raise_for_status = MagicMock()

    with patch.object(
        limitless_client._http_client, "get", new=AsyncMock(return_value=mock_response)
    ):
        trades = []
        async for trade in limitless_client.get_trades("btc-up-or-down-5-min-1791612600", limit=10):
            trades.append(trade)

        assert len(trades) == 1
        assert all(t.currency == Currency.USDC for t in trades)

        first_trade = trades[0]
        assert first_trade.market_id == "btc-up-or-down-5-min-1791612600"
        assert first_trade.side == OrderSide.BUY  # side: 0 = BUY
        assert first_trade.price == Decimal("0.74")
        # Size should be converted from raw 6-decimal units: 8000000 -> 8.0
        assert first_trade.size == Decimal("8.0")


@pytest.mark.asyncio
async def test_place_order_dry_run(limitless_client):
    """Test place_order in dry-run mode."""
    result = await limitless_client.place_order(
        outcome_id="btc-up-or-down-5-min-1791612600",
        side=OrderSide.BUY,
        size=Decimal("100"),
        price=Decimal("0.5"),
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert result["status"] == "simulated"
    assert result["outcome_id"] == "btc-up-or-down-5-min-1791612600"


@pytest.mark.asyncio
async def test_place_order_validation(limitless_client):
    """Test place_order validation."""
    # Invalid price (> 1)
    with pytest.raises(ValueError, match="Price must be between 0 and 1"):
        await limitless_client.place_order(
            outcome_id="test",
            side=OrderSide.BUY,
            size=Decimal("100"),
            price=Decimal("1.5"),
            dry_run=True,
        )

    # Invalid size (negative)
    with pytest.raises(ValueError, match="Size must be positive"):
        await limitless_client.place_order(
            outcome_id="test",
            side=OrderSide.BUY,
            size=Decimal("-10"),
            price=Decimal("0.5"),
            dry_run=True,
        )


@pytest.mark.asyncio
async def test_get_trades_rejects_unknown_kwargs():
    """Test that get_trades raises TypeError for unknown keyword arguments."""
    async with LimitlessClient() as client:
        with pytest.raises(TypeError):
            async for _ in client.get_trades("test-slug", unknown_param=123):
                pass


@pytest.mark.asyncio
async def test_close(limitless_client):
    """Test close method."""
    await limitless_client.close()
    # Just verify it doesn't raise


@pytest.mark.asyncio
async def test_status_mapping_from_fixtures():
    """Test that Limitless status fixtures map to correct normalized statuses."""
    import json
    from pathlib import Path

    from prediction_markets_api.models.base import MarketStatus

    fixtures_dir = Path("tests/fixtures/limitless/statuses")
    meta_path = fixtures_dir / "meta.json"

    assert meta_path.exists(), "Status fixtures meta.json not found"

    with open(meta_path) as f:
        meta = json.load(f)

    async with LimitlessClient() as client:
        for status_name, expected_data in meta["statuses"].items():
            fixture_path = fixtures_dir / f"{status_name}.json"
            assert fixture_path.exists(), f"Fixture {status_name}.json not found"

            with open(fixture_path) as f:
                market_data = json.load(f)

            market = client._parse_market(market_data)

            # Check venue_status
            assert market.venue_status == status_name, f"Expected venue_status={status_name}, got {market.venue_status}"

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

            print(f"✓ {status_name}: venue_status={market.venue_status}, status={market.status}, resolved={market.resolved}")


@pytest.mark.asyncio
async def test_unknown_status_logs_warning(caplog):
    """Test that unknown Limitless status logs a warning."""
    async with LimitlessClient() as client:
        market_data = {
            "slug": "test-unknown",
            "title": "Test Market",
            "status": "UNKNOWN_STATUS",
            "expired": False,
            "prices": [50, 50],
            "createdAt": "2026-01-01T00:00:00Z",
        }

        with caplog.at_level("WARNING"):
            market = client._parse_market(market_data)

        # Should have logged a warning
        assert any("Unknown Limitless market status 'UNKNOWN_STATUS'" in record.message for record in caplog.records)

        # Status should be None
        assert market.status is None
        assert market.venue_status == "UNKNOWN_STATUS"


@pytest.mark.asyncio
async def test_resolved_market_with_result():
    """Test that Limitless marks winning outcome when result is present."""
    async with LimitlessClient() as client:
        # Market with result="yes"
        market_data = {
            "slug": "test-resolved-yes",
            "title": "Test Resolved Market (Yes wins)",
            "status": "RESOLVED",
            "expired": False,
            "result": "yes",
            "prices": [100, 0],
            "createdAt": "2026-01-01T00:00:00Z",
        }

        market = client._parse_market(market_data)
        assert market.venue_status == "RESOLVED"
        assert market.status == MarketStatus.RESOLVED
        assert market.resolved is True
        assert market.raw_data["winning_outcome"] == "Yes"

        # Market with result="no"
        market_data["result"] = "no"
        market_data["prices"] = [0, 100]

        market = client._parse_market(market_data)
        assert market.resolved is True
        assert market.raw_data["winning_outcome"] == "No"
