"""Tests that verify adapters call correct URLs with correct parameters."""

import re

import httpx
import pytest

from prediction_markets_api import KalshiClient, LimitlessClient, PolymarketClient
from prediction_markets_api.models.base import OrderSide


@pytest.mark.asyncio
async def test_polymarket_list_markets_url(respx_mock):
    """Test Polymarket list_markets uses correct URL and params."""
    route = respx_mock.get(
        "https://gamma-api.polymarket.com/markets/keyset",
        params={"closed": "false", "limit": 10},
    ).mock(return_value=httpx.Response(200, json={"markets": [], "nextCursor": None}))

    async with PolymarketClient() as client:
        markets = [m async for m in client.list_markets(closed=False, limit=10, page_size=10)]

    assert route.called
    assert len(markets) == 0


@pytest.mark.asyncio
async def test_polymarket_get_market_url(respx_mock):
    """Test Polymarket get_market uses correct URL and params."""
    market_id = "test-condition-id"
    route = respx_mock.get(
        "https://gamma-api.polymarket.com/markets",
        params={"condition_ids": market_id, "limit": 1},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "markets": [
                    {
                        "id": market_id,
                        "question": "Test?",
                        "outcomes": ["Yes", "No"],
                        "outcomePrices": ["0.5", "0.5"],
                        "clobTokenIds": ["123", "456"],
                        "closed": False,
                    }
                ]
            },
        )
    )

    async with PolymarketClient() as client:
        await client.get_market(market_id)

    assert route.called


@pytest.mark.asyncio
async def test_polymarket_get_order_book_url(respx_mock):
    """Test Polymarket get_order_book uses correct URL and params."""
    token_id = "test-token-id"
    route = respx_mock.get(
        "https://clob.polymarket.com/book",
        params={"token_id": token_id},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "asset_id": token_id,
                "bids": [{"price": "0.5", "size": "100"}],
                "asks": [{"price": "0.6", "size": "100"}],
                "timestamp": "1609459200000",
            },
        )
    )

    async with PolymarketClient() as client:
        await client.get_order_book(token_id)

    assert route.called


@pytest.mark.asyncio
async def test_polymarket_get_price_url(respx_mock):
    """Test Polymarket get_price uses correct URL and params."""
    token_id = "test-token-id"
    route = respx_mock.get(
        "https://clob.polymarket.com/price",
        params={"token_id": token_id, "side": "SELL"},  # BUY inverts to SELL
    ).mock(return_value=httpx.Response(200, json={"price": "0.65"}))

    async with PolymarketClient() as client:
        await client.get_price(token_id, OrderSide.BUY)

    assert route.called


@pytest.mark.asyncio
async def test_polymarket_get_trades_url(respx_mock):
    """Test Polymarket get_trades uses correct URL and params."""
    token_id = "test-token-id"
    route = respx_mock.get(
        "https://data-api.polymarket.com/v2/trades",
        params={"asset_id": token_id, "limit": 10},
    ).mock(return_value=httpx.Response(200, json={"data": [], "nextCursor": None}))

    async with PolymarketClient() as client:
        trades = [t async for t in client.get_trades(token_id, limit=10)]

    assert route.called
    assert len(trades) == 0


@pytest.mark.asyncio
async def test_kalshi_list_markets_url(respx_mock):
    """Test Kalshi list_markets uses correct URL and params."""
    route = respx_mock.get(
        "https://api.elections.kalshi.com/trade-api/v2/markets",
        params={"status": "open", "limit": 10, "mve_filter": "exclude"},
    ).mock(return_value=httpx.Response(200, json={"markets": [], "cursor": None}))

    async with KalshiClient() as client:
        markets = [m async for m in client.list_markets(closed=False, limit=10, page_size=10)]

    assert route.called
    assert len(markets) == 0


@pytest.mark.asyncio
async def test_kalshi_get_market_url(respx_mock):
    """Test Kalshi get_market uses correct URL."""
    ticker = "TEST-MARKET"
    route = respx_mock.get(f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}").mock(
        return_value=httpx.Response(
            200,
            json={
                "market": {
                    "ticker": ticker,
                    "title": "Test",
                    "yes_ask": 50,
                    "no_bid": 50,
                    "status": "active",
                }
            },
        )
    )

    async with KalshiClient() as client:
        await client.get_market(ticker)

    assert route.called


@pytest.mark.asyncio
async def test_kalshi_get_order_book_url(respx_mock):
    """Test Kalshi get_order_book uses correct URL."""
    ticker = "TEST-MARKET"
    route = respx_mock.get(
        f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "orderbook_fp": {
                    "yes_dollars": [["0.45", "100.00"]],  # bid
                    "no_dollars": [["0.54", "100.00"]],  # becomes ask after 1-p transform
                }
            },
        )
    )

    async with KalshiClient() as client:
        await client.get_order_book(ticker)

    assert route.called


@pytest.mark.asyncio
async def test_kalshi_get_trades_url(respx_mock):
    """Test Kalshi get_trades uses correct URL and params (CRITICAL: ticker as query param)."""
    ticker = "TEST-MARKET"
    route = respx_mock.get(
        "https://api.elections.kalshi.com/trade-api/v2/markets/trades",
        params={"ticker": ticker, "limit": 10},
    ).mock(return_value=httpx.Response(200, json={"trades": [], "cursor": None}))

    async with KalshiClient() as client:
        trades = [t async for t in client.get_trades(ticker, limit=10)]

    assert route.called
    assert len(trades) == 0


@pytest.mark.asyncio
async def test_limitless_list_markets_url(respx_mock):
    """Test Limitless list_markets uses correct URL and params."""
    route = respx_mock.get(
        "https://api.limitless.exchange/markets/active",
        params={"limit": 25, "page": 1},
    ).mock(return_value=httpx.Response(200, json={"data": [], "totalMarketsCount": 0}))

    async with LimitlessClient() as client:
        markets = [m async for m in client.list_markets(limit=1)]

    assert route.called
    assert len(markets) == 0


@pytest.mark.asyncio
async def test_limitless_get_market_url(respx_mock):
    """Test Limitless get_market uses correct URL."""
    slug = "test-market"
    route = respx_mock.get(f"https://api.limitless.exchange/markets/{slug}").mock(
        return_value=httpx.Response(
            200,
            json={
                "slug": slug,
                "title": "Test",
                "prices": [50, 50],
                "status": "FUNDED",
            },
        )
    )

    async with LimitlessClient() as client:
        await client.get_market(slug)

    assert route.called


@pytest.mark.asyncio
async def test_limitless_get_order_book_url(respx_mock):
    """Test Limitless get_order_book uses correct URL."""
    slug = "test-market"
    route = respx_mock.get(f"https://api.limitless.exchange/markets/{slug}/orderbook").mock(
        return_value=httpx.Response(
            200,
            json={"bids": [{"price": 0.5, "quantity": 100000}], "asks": []},
        )
    )

    async with LimitlessClient() as client:
        await client.get_order_book(slug)

    assert route.called


@pytest.mark.asyncio
async def test_limitless_get_trades_url(respx_mock):
    """Test Limitless get_trades uses correct URL and params."""
    slug = "test-market"
    route = respx_mock.get(
        f"https://api.limitless.exchange/markets/{slug}/events",
        params={"limit": 10},
    ).mock(return_value=httpx.Response(200, json={"events": []}))

    async with LimitlessClient() as client:
        trades = [t async for t in client.get_trades(slug, limit=10)]

    assert route.called
    assert len(trades) == 0


@pytest.mark.asyncio
async def test_kalshi_get_price_not_found_raises_not_found_error(respx_mock):
    """Test Kalshi get_price with bad ID raises NotFoundError (via get_order_book)."""
    from prediction_markets_api.exceptions import NotFoundError

    respx_mock.get(re.compile(r".*/orderbook")).mock(
        return_value=httpx.Response(404, json={"error": "Not found"})
    )

    async with KalshiClient() as client:
        with pytest.raises(NotFoundError, match="not found"):
            await client.get_price("NONEXISTENT", OrderSide.BUY)
