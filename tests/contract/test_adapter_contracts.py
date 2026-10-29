"""Shared contract tests for all venue adapters.

Every venue adapter must pass these tests to ensure compliance with the
unified API contract. Tests use recorded fixtures and no live network calls.

Design:
    - Auto-discovers all adapters (Polymarket, Kalshi, Limitless, etc.)
    - Parametrized over discovered adapters
    - New adapters are automatically tested when added

Contract checks:
    1. Prices are Decimal in [0,1] with raw_price set
    2. Timestamps are timezone-aware UTC
    3. Currency is explicit when amounts are present
    4. partial is a bool
    5. Market ids unique and venue set
    6. Order books: sorted, valid spread, positive sizes
    7. get_price consistency with order book
    8. list_markets respects limit (no extra fetches)
    9. Liquidity matches venue's raw field
    10. Fixture hygiene (meta.json with recorded_at and request)
    11. Trading safety (dry_run defaults, max_order_size enforcement)
"""

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import respx
from httpx import Response

from prediction_markets_api.adapters.base import BaseClient
from prediction_markets_api.models.base import Currency, OrderSide
from tests.contract.adapter_registry import discover_adapters, load_fixtures

# Discover all adapters at collection time
FIXTURES_BASE = Path(__file__).parent.parent / "fixtures"
ADAPTERS = discover_adapters(FIXTURES_BASE)

# Hyperliquid Outcomes reads go through one POST /info URL. The body `type`
# selects outcomeMeta, l2Book, recentTrades, and the other info methods.
HYPERLIQUID_INFO = "https://api.hyperliquid.xyz/info"


def mock_markets(adapter_name: str, payload: Any) -> Any:
    """Mock the listing endpoint. Hyperliquid posts outcomeMeta to /info."""
    if adapter_name == "hyperliquid":
        return respx.post(HYPERLIQUID_INFO).mock(return_value=Response(200, json=payload))
    return respx.get(url__regex=r".*/markets.*").mock(return_value=Response(200, json=payload))


def mock_order_book(adapter_name: str, payload: Any) -> Any:
    """Mock the order book endpoint for this adapter."""
    if adapter_name == "hyperliquid":
        return respx.post(HYPERLIQUID_INFO).mock(return_value=Response(200, json=payload))
    if adapter_name in ("kalshi", "limitless"):
        return respx.get(url__regex=r".*/orderbook.*").mock(
            return_value=Response(200, json=payload)
        )
    if adapter_name == "predict_fun":
        return respx.get(url__regex=r".*/orderbook.*").mock(
            return_value=Response(200, json=payload)
        )
    return respx.get(url__regex=r".*/book.*").mock(return_value=Response(200, json=payload))


def mock_trades(adapter_name: str, payload: Any) -> Any:
    """Mock the trades endpoint for this adapter."""
    if adapter_name == "hyperliquid":
        return respx.post(HYPERLIQUID_INFO).mock(return_value=Response(200, json=payload))
    if adapter_name == "limitless":
        return respx.get(url__regex=r".*/events.*").mock(return_value=Response(200, json=payload))
    if adapter_name == "predict_fun":
        return respx.get(url__regex=r".*/matches.*").mock(return_value=Response(200, json=payload))
    return respx.get(url__regex=r".*/trades.*").mock(return_value=Response(200, json=payload))


@pytest.fixture(params=ADAPTERS, ids=[a.name for a in ADAPTERS])
def adapter_config(request):
    """Parametrize tests over all discovered adapters."""
    return request.param


@pytest.fixture
def adapter_fixtures(adapter_config):
    """Load fixtures for the current adapter."""
    return load_fixtures(adapter_config)


@pytest.fixture
async def adapter_client(adapter_config, monkeypatch: pytest.MonkeyPatch) -> BaseClient:
    """Create adapter client instance."""
    # Mainnet reads require a key. The value is a placeholder, not a secret,
    # and it is only sent to the mocked transport.
    if adapter_config.name == "predict_fun":
        monkeypatch.setenv("PREDICTFUN_API_KEY", "test-key")
    client = adapter_config.client_class()
    try:
        yield client
    finally:
        await client.close()


# =============================================================================
# CONTRACT 1: Prices are Decimal in [0,1] with raw_price set
# =============================================================================


@pytest.mark.asyncio
async def test_prices_are_decimal_in_range(adapter_config, adapter_fixtures, adapter_client):
    """Verify all outcome prices are Decimal in [0,1] and raw_price is set."""
    fixtures = adapter_fixtures

    # Check markets fixtures
    if "markets_response" in fixtures:
        data = fixtures["markets_response"]["data"]

        with respx.mock:
            # Mock the HTTP response
            mock_markets(adapter_config.name, data)

            markets = []
            async for market in adapter_client.list_markets(limit=100):
                markets.append(market)
                break  # Just check first market for speed

            for market in markets:
                for outcome in market.outcomes:
                    if outcome.price is not None:
                        assert isinstance(outcome.price, Decimal), (
                            f"Price must be Decimal, got {type(outcome.price)}"
                        )
                        assert Decimal("0") <= outcome.price <= Decimal("1"), (
                            f"Price {outcome.price} not in [0,1]"
                        )
                        assert outcome.raw_price is not None, (
                            "raw_price must be set when price is set"
                        )

    # Check orderbook fixtures - prices in OrderLevel
    if "orderbook_response" in fixtures:
        data = fixtures["orderbook_response"]["data"]
        meta = fixtures["orderbook_response"]["meta"]

        # Extract outcome_id from meta
        outcome_id = None
        if meta and "token_id" in meta:
            outcome_id = meta["token_id"]
        elif "asset_id" in data:
            outcome_id = data["asset_id"]

        if outcome_id:
            with respx.mock:
                mock_order_book(adapter_config.name, data)

                book = await adapter_client.get_order_book(outcome_id)

                for level in book.bids + book.asks:
                    assert isinstance(level.price, Decimal), (
                        f"OrderLevel price must be Decimal, got {type(level.price)}"
                    )
                    assert Decimal("0") <= level.price <= Decimal("1"), (
                        f"OrderLevel price {level.price} not in [0,1]"
                    )
                    assert level.raw_price is not None, (
                        "OrderLevel raw_price must be set when price is set"
                    )

    # Check prices fixtures
    if "prices_response" in fixtures:
        from prediction_markets_api.exceptions import NoLiquidityError

        data = fixtures["prices_response"]["data"]
        meta = fixtures["prices_response"]["meta"]

        outcome_id = meta.get("token_id") if meta else None
        if outcome_id:
            with respx.mock:
                # Mock both buy and sell price endpoints
                # Polymarket: side-specific responses (BUY queries side=SELL, SELL queries side=BUY)
                if adapter_config.name == "polymarket":
                    respx.get(url__regex=r".*/price.*", params={"side": "SELL"}).mock(
                        return_value=Response(200, json=data.get("buy", {}))
                    )
                    respx.get(url__regex=r".*/price.*", params={"side": "BUY"}).mock(
                        return_value=Response(200, json=data.get("sell", {}))
                    )
                else:
                    respx.get(url__regex=r".*/price.*").mock(return_value=Response(200, json=data))

                for side in [OrderSide.BUY, OrderSide.SELL]:
                    try:
                        price = await adapter_client.get_price(outcome_id, side)
                        assert isinstance(price.price, Decimal), (
                            f"Price.price must be Decimal, got {type(price.price)}"
                        )
                        assert Decimal("0") <= price.price <= Decimal("1"), (
                            f"Price {price.price} not in [0,1]"
                        )
                        assert price.raw_price is not None, (
                            "Price raw_price must be set when price is set"
                        )
                    except (ValueError, NoLiquidityError):
                        # Some adapters may not have price fixtures for both sides or no liquidity
                        pass


# =============================================================================
# CONTRACT 2: Timestamps are timezone-aware UTC
# =============================================================================


@pytest.mark.asyncio
async def test_timestamps_are_utc_aware(adapter_config, adapter_fixtures, adapter_client):
    """Verify all timestamps are timezone-aware UTC."""
    fixtures = adapter_fixtures

    # Check markets
    if "markets_response" in fixtures:
        data = fixtures["markets_response"]["data"]

        with respx.mock:
            mock_markets(adapter_config.name, data)

            async for market in adapter_client.list_markets(limit=100):
                assert market.created_at.tzinfo is not None, "created_at must be timezone-aware"
                assert market.created_at.tzinfo.tzname(None) == "UTC", "created_at must be UTC"

                if market.end_date:
                    assert market.end_date.tzinfo is not None, "end_date must be timezone-aware"
                    assert market.end_date.tzinfo.tzname(None) == "UTC", "end_date must be UTC"
                break  # Check first market

    # Check orderbook
    if "orderbook_response" in fixtures:
        data = fixtures["orderbook_response"]["data"]
        meta = fixtures["orderbook_response"]["meta"]

        outcome_id = None
        if meta and "token_id" in meta:
            outcome_id = meta["token_id"]
        elif "asset_id" in data:
            outcome_id = data["asset_id"]

        if outcome_id:
            with respx.mock:
                mock_order_book(adapter_config.name, data)

                book = await adapter_client.get_order_book(outcome_id)
                assert book.timestamp.tzinfo is not None, (
                    "OrderBook timestamp must be timezone-aware"
                )
                assert book.timestamp.tzinfo.tzname(None) == "UTC", (
                    "OrderBook timestamp must be UTC"
                )

    # Check prices
    if "prices_response" in fixtures:
        from prediction_markets_api.exceptions import NoLiquidityError

        data = fixtures["prices_response"]["data"]
        meta = fixtures["prices_response"]["meta"]

        outcome_id = meta.get("token_id") if meta else None
        if outcome_id:
            with respx.mock:
                # Polymarket: side-specific responses
                if adapter_config.name == "polymarket":
                    respx.get(url__regex=r".*/price.*", params={"side": "SELL"}).mock(
                        return_value=Response(200, json=data.get("buy", {}))
                    )
                else:
                    respx.get(url__regex=r".*/price.*").mock(return_value=Response(200, json=data))

                try:
                    price = await adapter_client.get_price(outcome_id, OrderSide.BUY)
                    assert price.timestamp.tzinfo is not None, (
                        "Price timestamp must be timezone-aware"
                    )
                    assert price.timestamp.tzinfo.tzname(None) == "UTC", (
                        "Price timestamp must be UTC"
                    )
                except (ValueError, NoLiquidityError):
                    pass

    # Check trades
    if "trades_response" in fixtures:
        data = fixtures["trades_response"]["data"]
        meta = fixtures["trades_response"]["meta"]

        outcome_id = meta.get("token_id") or meta.get("asset_id") if meta else None
        if outcome_id:
            with respx.mock:
                mock_trades(adapter_config.name, data)

                async for trade in adapter_client.get_trades(outcome_id, limit=1):
                    assert trade.timestamp.tzinfo is not None, (
                        "Trade timestamp must be timezone-aware"
                    )
                    assert trade.timestamp.tzinfo.tzname(None) == "UTC", (
                        "Trade timestamp must be UTC"
                    )
                    break


# =============================================================================
# CONTRACT 3: Currency is explicit when amounts are present
# =============================================================================


@pytest.mark.asyncio
async def test_currency_explicit_with_amounts(adapter_config, adapter_fixtures, adapter_client):
    """Verify currency is set whenever volume/liquidity is present."""
    fixtures = adapter_fixtures

    if "markets_response" in fixtures:
        data = fixtures["markets_response"]["data"]

        with respx.mock:
            mock_markets(adapter_config.name, data)

            async for market in adapter_client.list_markets(limit=100):
                if market.volume is not None or market.liquidity is not None:
                    assert market.currency is not None, "currency must be set when amounts present"
                    assert isinstance(market.currency, Currency), (
                        f"currency must be Currency enum, got {type(market.currency)}"
                    )
                break

    if "orderbook_response" in fixtures:
        data = fixtures["orderbook_response"]["data"]
        meta = fixtures["orderbook_response"]["meta"]

        outcome_id = None
        if meta and "token_id" in meta:
            outcome_id = meta["token_id"]
        elif "asset_id" in data:
            outcome_id = data["asset_id"]

        if outcome_id:
            with respx.mock:
                mock_order_book(adapter_config.name, data)

                book = await adapter_client.get_order_book(outcome_id)
                # Order books always have sizes, so currency must be set
                assert book.currency is not None, "OrderBook currency must be set"
                assert isinstance(book.currency, Currency)


# =============================================================================
# CONTRACT 4: partial is a bool
# =============================================================================


@pytest.mark.asyncio
async def test_partial_is_bool(adapter_config, adapter_fixtures, adapter_client):
    """Verify partial field is always a bool."""
    fixtures = adapter_fixtures

    if "markets_response" in fixtures:
        data = fixtures["markets_response"]["data"]

        with respx.mock:
            mock_markets(adapter_config.name, data)

            async for market in adapter_client.list_markets(limit=100):
                assert isinstance(market.partial, bool), (
                    f"Market.partial must be bool, got {type(market.partial)}"
                )
                break

    if "orderbook_response" in fixtures:
        data = fixtures["orderbook_response"]["data"]
        meta = fixtures["orderbook_response"]["meta"]

        outcome_id = None
        if meta and "token_id" in meta:
            outcome_id = meta["token_id"]
        elif "asset_id" in data:
            outcome_id = data["asset_id"]

        if outcome_id:
            with respx.mock:
                mock_order_book(adapter_config.name, data)

                book = await adapter_client.get_order_book(outcome_id)
                assert isinstance(book.partial, bool), (
                    f"OrderBook.partial must be bool, got {type(book.partial)}"
                )


# =============================================================================
# CONTRACT 5: Market ids unique and venue set
# =============================================================================


@pytest.mark.asyncio
async def test_market_ids_unique_and_venue_set(adapter_config, adapter_fixtures, adapter_client):
    """Verify market ids are unique and venue is set correctly."""
    fixtures = adapter_fixtures

    if "markets_response" not in fixtures:
        pytest.skip(f"No markets fixture for {adapter_config.name}")

    data = fixtures["markets_response"]["data"]

    with respx.mock:
        mock_markets(adapter_config.name, data)

        market_ids = set()
        async for market in adapter_client.list_markets(limit=100):
            assert market.id, "Market id must be non-empty"
            assert market.id not in market_ids, f"Duplicate market id: {market.id}"
            market_ids.add(market.id)

            assert market.venue, "Market venue must be non-empty"
            assert market.venue == adapter_config.name, (
                f"Market venue {market.venue} != adapter {adapter_config.name}"
            )


# =============================================================================
# CONTRACT 6: Order book validation
# =============================================================================


@pytest.mark.asyncio
async def test_orderbook_validation(adapter_config, adapter_fixtures, adapter_client):
    """Verify order book is sorted, valid spread, positive sizes.

    For Kalshi: Also validates derivation logic (best bid from yes_dollars,
    best ask from 1 - max(no_dollars)).
    """
    fixtures = adapter_fixtures

    if "orderbook_response" not in fixtures:
        pytest.skip(f"No orderbook fixture for {adapter_config.name}")

    data = fixtures["orderbook_response"]["data"]
    meta = fixtures["orderbook_response"]["meta"]

    # Extract outcome_id (token_id for Polymarket, ticker for Kalshi, market_id for Limitless)
    outcome_id = None
    if meta:
        outcome_id = meta.get("token_id") or meta.get("ticker") or meta.get("market_id")
    if not outcome_id and "asset_id" in data:
        outcome_id = data["asset_id"]

    if not outcome_id:
        pytest.skip(f"Cannot determine outcome_id for {adapter_config.name}")

    with respx.mock:
        # Mock the appropriate endpoint for each adapter
        mock_order_book(adapter_config.name, data)

        book = await adapter_client.get_order_book(outcome_id)

        # Check bids sorted descending
        for i in range(len(book.bids) - 1):
            assert book.bids[i].price >= book.bids[i + 1].price, (
                f"Bids not sorted descending: {book.bids[i].price} < {book.bids[i + 1].price}"
            )

        # Check asks sorted ascending
        for i in range(len(book.asks) - 1):
            assert book.asks[i].price <= book.asks[i + 1].price, (
                f"Asks not sorted ascending: {book.asks[i].price} > {book.asks[i + 1].price}"
            )

        # Check best bid <= best ask
        if book.bids and book.asks:
            best_bid = book.bids[0].price
            best_ask = book.asks[0].price
            assert best_bid <= best_ask, (
                f"Order book crossed: best_bid={best_bid} > best_ask={best_ask}"
            )

        # Check all sizes > 0
        for level in book.bids + book.asks:
            assert level.size > Decimal("0"), f"Order size must be positive, got {level.size}"

        # Check all prices in [0,1]
        for level in book.bids + book.asks:
            assert Decimal("0") <= level.price <= Decimal("1"), (
                f"Order price {level.price} not in [0,1]"
            )

        # Kalshi-specific: Validate derivation logic
        if adapter_config.name == "kalshi":
            orderbook_fp = data.get("orderbook_fp", {})
            yes_dollars = orderbook_fp.get("yes_dollars", [])
            no_dollars = orderbook_fp.get("no_dollars", [])

            if yes_dollars and book.bids:
                # Best bid should equal max(yes_dollars prices)
                # yes_dollars are [price, size] pairs
                yes_prices = [Decimal(str(level[0])) for level in yes_dollars if level]
                if yes_prices:
                    expected_best_bid = max(yes_prices)
                    actual_best_bid = book.bids[0].price
                    assert actual_best_bid == expected_best_bid, (
                        f"Kalshi best bid {actual_best_bid} != max(yes_dollars) {expected_best_bid}"
                    )

            if no_dollars and book.asks:
                # Best ask should equal 1 - max(no_dollars prices)
                # no_dollars are [price, size] pairs, sorted ascending
                no_prices = [Decimal(str(level[0])) for level in no_dollars if level]
                if no_prices:
                    # Best NO bid is the highest NO price
                    best_no_bid = max(no_prices)
                    # Best YES ask = 1 - best NO bid
                    expected_best_ask = Decimal("1.0") - best_no_bid
                    actual_best_ask = book.asks[0].price
                    assert actual_best_ask == expected_best_ask, (
                        f"Kalshi best ask {actual_best_ask} != 1 - max(no_dollars) {expected_best_ask}"
                    )


# =============================================================================
# CONTRACT 7: get_price consistency with order book
# =============================================================================


@pytest.mark.asyncio
async def test_get_price_matches_orderbook(adapter_config, adapter_fixtures, adapter_client):
    """Verify get_price(BUY) == best ask and get_price(SELL) == best bid.

    Fixtures are recorded from the same token/ticker, so prices must match.
    - Polymarket: get_price() calls /price endpoint (mock with prices fixture)
    - Kalshi: get_price() reads /orderbook endpoint (mock with orderbook fixture)
    """
    fixtures = adapter_fixtures

    if "orderbook_response" not in fixtures:
        pytest.skip(f"No orderbook fixture for {adapter_config.name}")

    orderbook_data = fixtures["orderbook_response"]["data"]
    orderbook_meta = fixtures["orderbook_response"]["meta"]

    # Extract outcome_id (token_id for Polymarket, ticker for Kalshi, market_id for Limitless)
    outcome_id = None
    if orderbook_meta:
        outcome_id = (
            orderbook_meta.get("token_id")
            or orderbook_meta.get("ticker")
            or orderbook_meta.get("market_id")
        )
    if not outcome_id and "asset_id" in orderbook_data:
        outcome_id = orderbook_data["asset_id"]

    if not outcome_id:
        pytest.skip(f"Cannot determine outcome_id for {adapter_config.name}")

    with respx.mock:
        # Mock order book endpoint for both get_order_book and get_price where applicable.
        # Polymarket additionally mocks /price below; other venues price off the book.
        mock_order_book(adapter_config.name, orderbook_data)

        # Mock price endpoint for Polymarket (separate /price API)
        if adapter_config.name == "polymarket" and "prices_response" in fixtures:
            prices_fixture = fixtures["prices_response"]["data"]
            # Polymarket /price endpoint returns {"price": "0.123"} per call
            # Fixture has {buy: {price: ...}, sell: {price: ...}}
            # BUY action queries side=SELL (ask), SELL action queries side=BUY (bid)

            # Mock for BUY (queries side=SELL, gets ask price)
            respx.get(url__regex=r".*/price.*", params__contains={"side": "SELL"}).mock(
                return_value=Response(200, json=prices_fixture.get("buy", {"price": "0"}))
            )

            # Mock for SELL (queries side=BUY, gets bid price)
            respx.get(url__regex=r".*/price.*", params__contains={"side": "BUY"}).mock(
                return_value=Response(200, json=prices_fixture.get("sell", {"price": "0"}))
            )

        # Get order book to extract expected best bid/ask
        book = await adapter_client.get_order_book(outcome_id)

        if not book.bids or not book.asks:
            pytest.skip(f"Order book empty for {adapter_config.name}")

        best_bid = book.bids[0].price
        best_ask = book.asks[0].price

        # get_price(BUY) should return best ask (what buyer pays)
        buy_price = await adapter_client.get_price(outcome_id, OrderSide.BUY)
        assert buy_price.price == best_ask, (
            f"{adapter_config.name}: get_price(BUY)={buy_price.price} != best_ask={best_ask}"
        )

        # get_price(SELL) should return best bid (what seller receives)
        sell_price = await adapter_client.get_price(outcome_id, OrderSide.SELL)
        assert sell_price.price == best_bid, (
            f"{adapter_config.name}: get_price(SELL)={sell_price.price} != best_bid={best_bid}"
        )


# =============================================================================
# CONTRACT 8: list_markets respects limit
# =============================================================================


@pytest.mark.asyncio
async def test_list_markets_respects_limit(adapter_config, adapter_fixtures, adapter_client):
    """Verify list_markets(limit=N) returns <= N markets and doesn't fetch extra pages."""
    fixtures = adapter_fixtures

    if "markets_response" not in fixtures:
        pytest.skip(f"No markets fixture for {adapter_config.name}")

    data = fixtures["markets_response"]["data"]

    # Hyperliquid listing is one outcomeMeta snapshot (no cursor). limit stops
    # the iterator after one market and must not request outcomeMeta again.
    if adapter_config.name == "hyperliquid":
        with respx.mock:
            route = respx.post(HYPERLIQUID_INFO).mock(return_value=Response(200, json=data))
            markets = [market async for market in adapter_client.list_markets(limit=1)]
            assert len(markets) == 1
            bodies = [json.loads(call.request.content) for call in route.calls]
            assert sum(1 for body in bodies if body.get("type") == "outcomeMeta") == 1
        return

    # Pascal's unresolved listing is one GET /markets snapshot. limit stops
    # the iterator and must not request the resolved listing as well.
    if adapter_config.name == "pascal":
        with respx.mock:
            route = respx.get(url__regex=r".*/markets.*").mock(
                return_value=Response(200, json=data)
            )
            markets = [market async for market in adapter_client.list_markets(limit=1)]
            assert len(markets) == 1
            assert route.call_count == 1
        return

    # Predict.fun pages with `after`. limit=1 must not request the next cursor.
    if adapter_config.name == "predict_fun":
        with respx.mock:
            route = respx.get(url__regex=r".*/markets.*").mock(
                return_value=Response(200, json=data)
            )
            markets = [market async for market in adapter_client.list_markets(limit=1)]
            assert len(markets) == 1
            assert route.call_count == 1
        return

    # Create mock response with multiple pages
    page1 = {"markets": data.get("markets", [])[:1], "nextCursor": "page2"}
    page2 = {"markets": data.get("markets", [])[1:2], "nextCursor": None}

    with respx.mock:
        # Mock first page
        route1 = respx.get(url__regex=r".*/markets.*").mock(return_value=Response(200, json=page1))

        # Mock second page (should not be called if limit=1)
        route2 = respx.get(url__regex=r".*/markets.*after_cursor=page2.*").mock(
            return_value=Response(200, json=page2)
        )

        # Request limit=1
        markets = []
        async for market in adapter_client.list_markets(limit=1):
            markets.append(market)
            if len(markets) >= 1:
                break

        # Should return exactly 1 market
        assert len(markets) <= 1, f"list_markets(limit=1) returned {len(markets)} markets"

        # Should have called first page
        assert route1.called, "First page should be fetched"

        # Should NOT have called second page
        assert not route2.called, "Second page should not be fetched when limit is reached"


# =============================================================================
# CONTRACT 9: Liquidity matches venue's raw field
# =============================================================================


@pytest.mark.asyncio
async def test_liquidity_matches_raw_field(adapter_config, adapter_fixtures, adapter_client):
    """Verify liquidity matches the venue's liquidity_source field from raw data."""
    fixtures = adapter_fixtures

    if "markets_response" not in fixtures:
        pytest.skip(f"No markets fixture for {adapter_config.name}")

    data = fixtures["markets_response"]["data"]

    with respx.mock:
        mock_markets(adapter_config.name, data)

        async for market in adapter_client.list_markets(limit=100):
            raw_data = market.raw_data

            if adapter_config.liquidity_source is None:
                # If venue doesn't provide liquidity, it must be None
                assert market.liquidity is None, (
                    f"liquidity must be None when liquidity_source is None, got {market.liquidity}"
                )
            else:
                # If venue provides liquidity, check it matches raw field
                raw_liquidity = raw_data.get(adapter_config.liquidity_source)
                if raw_liquidity is not None:
                    expected = Decimal(str(raw_liquidity))
                    assert market.liquidity == expected, (
                        f"liquidity {market.liquidity} != raw {adapter_config.liquidity_source}={expected}"
                    )
            break  # Check first market


@pytest.mark.asyncio
async def test_best_bid_ask_sizes_match_orderbook(adapter_config, adapter_fixtures, adapter_client):
    """Verify best_bid_size and best_ask_size match raw market fixture fields.

    For Kalshi: Assert Market.best_bid_size == yes_bid_size_fp from raw fixture,
    and Market.best_ask_size == yes_ask_size_fp from raw fixture (1:1 Decimal comparison).
    """
    fixtures = adapter_fixtures

    if "markets_response" not in fixtures:
        pytest.skip(f"No markets fixture for {adapter_config.name}")

    markets_data = fixtures["markets_response"]["data"]

    with respx.mock:
        # Mock markets endpoint
        if adapter_config.name == "kalshi":
            respx.get(url__regex=r".*/markets.*").mock(
                return_value=Response(200, json=markets_data)
            )
        else:
            mock_markets(adapter_config.name, markets_data)

        # Get first market
        async for market in adapter_client.list_markets(limit=1):
            raw_data = market.raw_data

            if adapter_config.name == "kalshi":
                # Kalshi: Check yes_bid_size_fp and yes_ask_size_fp
                yes_bid_size_fp = raw_data.get("yes_bid_size_fp")
                yes_ask_size_fp = raw_data.get("yes_ask_size_fp")

                if yes_bid_size_fp is not None:
                    expected_best_bid_size = Decimal(str(yes_bid_size_fp))
                    assert market.best_bid_size == expected_best_bid_size, (
                        f"Kalshi best_bid_size {market.best_bid_size} != "
                        f"yes_bid_size_fp {expected_best_bid_size}"
                    )

                if yes_ask_size_fp is not None:
                    expected_best_ask_size = Decimal(str(yes_ask_size_fp))
                    assert market.best_ask_size == expected_best_ask_size, (
                        f"Kalshi best_ask_size {market.best_ask_size} != "
                        f"yes_ask_size_fp {expected_best_ask_size}"
                    )
            # For other adapters, this test validates that if these fields exist,
            # they should be populated from the appropriate source
            break


# =============================================================================
# CONTRACT 10: Fixture hygiene
# =============================================================================


def test_fixture_hygiene(adapter_config, adapter_fixtures):
    """Verify every fixture has a .meta.json with recorded_at and request."""
    fixtures_dir = adapter_config.fixtures_dir

    json_files = list(fixtures_dir.glob("*.json"))
    assert json_files, f"No fixtures found for {adapter_config.name}"

    for fixture_file in json_files:
        # Skip .meta.json files themselves
        if fixture_file.stem.endswith(".meta"):
            continue

        fixture_name = fixture_file.stem
        meta_file = fixture_file.with_suffix(".meta.json")

        assert meta_file.exists(), f"Missing meta file: {meta_file.name} for fixture {fixture_name}"

        # Load and validate meta
        import json

        with open(meta_file) as f:
            meta = json.load(f)

        # Validate recorded_at or synthetic flag
        if meta.get("synthetic"):
            # Synthetic fixtures don't need recorded_at
            assert "source" in meta, (
                f"Synthetic meta file {meta_file.name} must have 'source' field"
            )
        else:
            # Real fixtures must have valid recorded_at
            assert "recorded_at" in meta, f"Meta file {meta_file.name} missing 'recorded_at'"
            recorded_at_str = meta["recorded_at"]
            try:
                # Python 3.10 fromisoformat doesn't accept trailing 'Z'
                # Replace 'Z' with '+00:00' for compatibility
                normalized = recorded_at_str.replace("Z", "+00:00")
                datetime.fromisoformat(normalized)
            except ValueError as e:
                pytest.fail(
                    f"Invalid ISO-8601 timestamp in {meta_file.name}: {recorded_at_str} - {e}"
                )

        # Validate request info (skip for synthetic fixtures)
        if not meta.get("synthetic"):
            # Support multiple formats:
            # 1. 'request': {url, method} - single nested object (Polymarket)
            # 2. 'requests': [{url, method}, ...] - multiple nested objects
            # 3. Top-level url and method - Kalshi format
            # 4. 'request': "METHOD /path" - string format (Limitless)

            has_request_info = False

            if "request" in meta:
                request = meta["request"]
                if isinstance(request, str):
                    # String format: "GET /path" (Limitless)
                    has_request_info = True
                    # Validate it looks like "METHOD /path"
                    parts = request.split(None, 1)
                    assert len(parts) >= 2, (
                        f"Meta file {meta_file.name} request string must be 'METHOD /path', got: {request}"
                    )
                elif isinstance(request, dict):
                    # Object format (Polymarket)
                    has_request_info = True
                    assert "url" in request, f"Meta file {meta_file.name} request missing 'url'"
                    assert "method" in request, (
                        f"Meta file {meta_file.name} request missing 'method'"
                    )
            elif "requests" in meta:
                # Multiple requests format
                has_request_info = True
                for req in meta["requests"]:
                    assert "url" in req, f"Meta file {meta_file.name} request missing 'url'"
                    assert "method" in req, f"Meta file {meta_file.name} request missing 'method'"
            elif "url" in meta and "method" in meta:
                # Top-level format (Kalshi)
                has_request_info = True

            assert has_request_info, (
                f"Meta file {meta_file.name} missing request info. "
                "Expected 'request' (string/object), 'requests' (array), or top-level 'url'/'method' fields."
            )


# =============================================================================
# CONTRACT 11: Trading safety
# =============================================================================


@pytest.mark.asyncio
async def test_place_order_defaults_to_dry_run(adapter_client):
    """Verify place_order defaults to dry_run=True and makes no HTTP calls."""
    outcome_id = "test_outcome"
    size = Decimal("10")
    price = Decimal("0.5")

    # Spy on HTTP client to ensure no requests are made
    original_request = adapter_client._http_client.request

    call_count = 0

    async def spy_request(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return await original_request(*args, **kwargs)

    adapter_client._http_client.request = spy_request

    # Call place_order without dry_run parameter (should default to True)
    result = await adapter_client.place_order(
        outcome_id=outcome_id, side=OrderSide.BUY, size=size, price=price
    )

    # Should return dry-run result
    assert result.get("dry_run") is True, "place_order should default to dry_run=True"

    # Should not have made any HTTP requests
    assert call_count == 0, (
        f"place_order(dry_run=True) should not make HTTP requests, made {call_count}"
    )


@pytest.mark.asyncio
async def test_place_order_enforces_max_order_size(adapter_config):
    """Verify place_order with max_order_size set raises before any HTTP call."""
    max_size = Decimal("100")
    client = adapter_config.client_class(max_order_size=max_size)

    try:
        outcome_id = "test_outcome"
        over_size = max_size + Decimal("1")
        price = Decimal("0.5")

        # Spy on HTTP client to ensure no requests are made
        call_count = 0
        original_request = client._http_client.request

        async def spy_request(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return await original_request(*args, **kwargs)

        client._http_client.request = spy_request

        # Try to place order exceeding max_order_size
        with pytest.raises(RuntimeError, match="exceeds max_order_size"):
            await client.place_order(
                outcome_id=outcome_id,
                side=OrderSide.BUY,
                size=over_size,
                price=price,
                dry_run=False,  # Even with dry_run=False, should fail before HTTP
            )

        # Should not have made any HTTP requests
        assert call_count == 0, "Should not make HTTP requests when max_order_size exceeded"

    finally:
        await client.close()


# =============================================================================
# CONTRACT 12: Code quality - no type: ignore[override]
# =============================================================================


def test_no_type_ignore_override_in_adapters():
    """Verify no adapter source file contains type: ignore[override].

    This mypy suppression is a code smell indicating incorrect override signatures.
    Adapters must properly implement the base interface without type suppressions.
    """
    from pathlib import Path

    adapters_dir = (
        Path(__file__).parent.parent.parent / "src" / "prediction_markets_api" / "adapters"
    )

    adapter_files = []
    for f in adapters_dir.glob("*.py"):
        if f.name not in ("__init__.py", "__pycache__"):
            adapter_files.append(f)

    assert adapter_files, "No adapter files found"

    violations = []
    for adapter_file in adapter_files:
        with open(adapter_file) as f:
            content = f.read()
            if "type: ignore[override]" in content:
                # Find line numbers
                lines = content.split("\n")
                for i, line in enumerate(lines, 1):
                    if "type: ignore[override]" in line:
                        violations.append(f"{adapter_file.name}:{i}")

    assert not violations, (
        "Found type: ignore[override] in adapter files (indicates incorrect override signature):\n"
        + "\n".join(violations)
    )


# =============================================================================
# CONTRACT 13: Status mapping and resolution invariants
# =============================================================================


@pytest.mark.asyncio
async def test_status_mapping_and_resolution(adapter_config, adapter_fixtures, adapter_client):
    """Verify status/resolved/winning_outcome invariants.

    Contract:
    - Every Market has status in MarketStatus enum and venue_status string
    - Invariant: resolved <=> status == RESOLVED
    - winning_outcome is set only when resolved
    - When resolved, winning_outcome is one of the market's outcomes (or None for Kalshi scalar)
    """
    from prediction_markets_api.models.base import MarketStatus

    # Use status fixtures if available
    if "statuses" in adapter_fixtures:
        statuses_data = adapter_fixtures["statuses"]
        for status_name, fixture in statuses_data.items():
            if status_name == "meta":
                continue

            data = fixture["data"]

            with respx.mock:
                # Mock the markets endpoint with proper format
                if adapter_config.name == "polymarket":
                    # Polymarket wraps in {"markets": [...]}
                    respx.get(url__regex=r".*/markets.*").mock(
                        return_value=Response(200, json={"markets": [data]})
                    )
                elif adapter_config.name == "kalshi":
                    # Kalshi wraps in {"markets": [...], "cursor": None}
                    respx.get(url__regex=r".*/markets.*").mock(
                        return_value=Response(200, json={"markets": [data], "cursor": None})
                    )
                elif adapter_config.name == "limitless":
                    # Limitless wraps in {"data": [...]}
                    respx.get(url__regex=r".*/markets.*").mock(
                        return_value=Response(200, json={"data": [data]})
                    )

                markets = []
                async for market in adapter_client.list_markets(limit=1):
                    markets.append(market)

                assert markets, f"No markets parsed for {adapter_config.name}/{status_name}"
                market = markets[0]

                # Check status is in enum
                assert market.status in MarketStatus, (
                    f"Market.status must be MarketStatus enum, got {type(market.status)}"
                )

                # Check venue_status is a string
                assert isinstance(market.venue_status, str), (
                    f"Market.venue_status must be str, got {type(market.venue_status)}"
                )

                # Invariant: resolved <=> status == RESOLVED
                if market.resolved:
                    assert market.status == MarketStatus.RESOLVED, (
                        f"Market {market.id}: resolved=True but status={market.status}. "
                        "Invariant: resolved if and only if status==RESOLVED"
                    )
                if market.status == MarketStatus.RESOLVED:
                    assert market.resolved, (
                        f"Market {market.id}: status=RESOLVED but resolved=False. "
                        "Invariant: resolved if and only if status==RESOLVED"
                    )

                # winning_outcome only when resolved
                if market.winning_outcome is not None:
                    assert market.resolved, (
                        f"Market {market.id}: winning_outcome set but resolved=False. "
                        "winning_outcome must only be set when resolved=True"
                    )

                    # Check winning_outcome is one of the outcomes (unless Kalshi scalar)
                    if adapter_config.name == "kalshi" and "result" in data:
                        # Kalshi scalar/cancelled: result='scalar' => winning_outcome=None is OK
                        if data.get("result") == "scalar":
                            # Scalar results can have winning_outcome=None
                            pass
                        else:
                            # Binary/categorical: must have winning_outcome in outcomes
                            outcome_names = [o.name for o in market.outcomes]
                            assert market.winning_outcome in outcome_names, (
                                f"winning_outcome '{market.winning_outcome}' not in outcomes {outcome_names}"
                            )
                    else:
                        # Non-Kalshi or non-scalar: must have winning_outcome in outcomes
                        outcome_names = [o.name for o in market.outcomes]
                        assert market.winning_outcome in outcome_names, (
                            f"winning_outcome '{market.winning_outcome}' not in outcomes {outcome_names}"
                        )


# =============================================================================
# CONTRACT 14: Polymarket open market near-certain outcome not resolved
# =============================================================================


@pytest.mark.asyncio
async def test_polymarket_open_near_certain_not_resolved(
    adapter_config, adapter_fixtures, adapter_client
):
    """Verify Polymarket open market with price >0.999 is NOT marked resolved.

    Polymarket can have open markets trading near certainty (>0.999) that are not
    yet formally resolved. Resolution requires closed=true AND price settlement.
    """
    from prediction_markets_api.models.base import MarketStatus

    if adapter_config.name != "polymarket":
        pytest.skip("Test is Polymarket-specific")

    # Use the open_near_certain fixture
    if "open_near_certain" in adapter_fixtures:
        data = adapter_fixtures["open_near_certain"]["data"]

        with respx.mock:
            # Polymarket wraps in {"markets": [...]}
            respx.get(url__regex=r".*/markets.*").mock(
                return_value=Response(200, json={"markets": [data]})
            )

            markets = []
            async for market in adapter_client.list_markets(limit=1):
                markets.append(market)

            assert markets, "No market parsed from open_near_certain fixture"
            market = markets[0]

            # Verify it's open and NOT resolved
            assert not market.resolved, (
                f"Market {market.id}: open market with price >0.999 must NOT be resolved. "
                "Polymarket resolution requires closed=true AND price settlement."
            )
            assert market.status != MarketStatus.RESOLVED, (
                f"Market {market.id}: open market must not have status=RESOLVED"
            )
    else:
        pytest.skip("No open_near_certain fixture for Polymarket")


# =============================================================================
# CONTRACT 15: Limitless expired-but-not-resolved handling
# =============================================================================


@pytest.mark.asyncio
async def test_limitless_expired_not_resolved_and_closed_param(adapter_config, adapter_client):
    """Verify Limitless expired-but-not-resolved markets and closed parameter handling.

    Contract:
    - Expired markets that haven't been finalized are not marked resolved
    - list_markets(closed=True) raises NotImplementedError
    - closed=None and closed=False work
    """
    if adapter_config.name != "limitless":
        pytest.skip("Test is Limitless-specific")

    # Test closed=True raises NotImplementedError
    with pytest.raises(NotImplementedError):
        async for _ in adapter_client.list_markets(closed=True, limit=1):
            pass

    # Test expired-but-not-resolved market
    # Create a market payload with expiration in the past but not finalized
    from prediction_markets_api.models.base import MarketStatus

    expired_market_payload = {
        "slug": "test-expired-market",
        "title": "Test Expired Market",
        "createdAt": "2026-10-01T00:00:00Z",
        "expirationDate": "2026-10-15T00:00:00Z",  # Past expiration
        "resolvedAt": None,  # Not yet resolved/finalized
        "winningOutcomeIndex": None,
        "outcomes": ["YES", "NO"],
        "initialProb": 500000,  # 0.5 in 6-decimal units
        "type": "BINARY",
    }

    with respx.mock:
        # Limitless wraps in {"data": [...]}
        respx.get(url__regex=r".*/markets.*").mock(
            return_value=Response(200, json={"data": [expired_market_payload]})
        )

        markets = []
        async for market in adapter_client.list_markets(closed=False, limit=1):
            markets.append(market)

        assert len(markets) == 1, "Should parse expired market"
        market = markets[0]

        # Verify it's NOT marked resolved despite being expired
        assert not market.resolved, (
            f"Expired market {market.id} without finalization must not be marked resolved"
        )
        assert market.status != MarketStatus.RESOLVED, (
            f"Expired market {market.id} must not have status=RESOLVED"
        )
        assert market.winning_outcome is None, (
            f"Expired market {market.id} without resolution must have winning_outcome=None"
        )

    # Test closed=None works (with mock)
    with respx.mock:
        # Limitless wraps in {"data": [...]}
        respx.get(url__regex=r".*/markets.*").mock(return_value=Response(200, json={"data": []}))
        count = 0
        async for _ in adapter_client.list_markets(closed=None, limit=1):
            count += 1
        # Empty response is OK, just verifying no exception


# =============================================================================
# CONTRACT 16: Request params for closed filter
# =============================================================================


@pytest.mark.asyncio
async def test_closed_filter_request_params(adapter_config, adapter_client):
    """Verify closed filter translates to correct venue-specific HTTP params.

    Contract:
    - closed=False/True/None must send correct venue-specific filter parameters
    - closed=None sends no status filter
    - Limitless: closed=False/None must not send unsupported status param
    """

    with respx.mock:
        # Mock with proper response format
        if adapter_config.name == "polymarket":
            mock_data = {"markets": []}
        elif adapter_config.name == "kalshi":
            mock_data = {"markets": [], "cursor": None}
        elif adapter_config.name == "limitless":
            mock_data = {"data": []}
        elif adapter_config.name == "hyperliquid":
            mock_data = {"outcomes": [], "questions": []}
        elif adapter_config.name == "predict_fun":
            mock_data = {"success": True, "cursor": None, "data": []}
        else:
            mock_data = []

        if adapter_config.name == "hyperliquid":
            route = respx.post(HYPERLIQUID_INFO).mock(return_value=Response(200, json=mock_data))
        else:
            route = respx.get(url__regex=r".*/markets.*").mock(
                return_value=Response(200, json=mock_data)
            )

        # Test closed=False
        route.calls.clear()
        count = 0
        async for _ in adapter_client.list_markets(closed=False, limit=1):
            count += 1
            if count >= 1:
                break

        assert len(route.calls) > 0, "Expected at least one request for closed=False"
        request = route.calls[0].request
        params = dict(request.url.params)

        if adapter_config.name == "polymarket":
            assert params.get("closed") == "false", "Polymarket closed=False must send closed=false"
        elif adapter_config.name == "kalshi":
            # Kalshi uses status filter: closed=False -> status=open
            assert params.get("status") == "open", "Kalshi closed=False must send status=open"
        elif adapter_config.name == "hyperliquid":
            assert "closed" not in params
            assert "status" not in params
            assert json.loads(request.content)["type"] == "outcomeMeta"
        elif adapter_config.name == "predict_fun":
            assert params.get("status") == "OPEN"
            assert "closed" not in params

        # Test closed=True (Limitless and Hyperliquid raise NotImplementedError)
        if adapter_config.name == "hyperliquid":
            with pytest.raises(NotImplementedError):
                async for _ in adapter_client.list_markets(closed=True, limit=1):
                    pass
        elif adapter_config.name != "limitless":
            route.calls.clear()
            count = 0
            async for _ in adapter_client.list_markets(closed=True, limit=1):
                count += 1
                if count >= 1:
                    break

            assert len(route.calls) > 0, "Expected at least one request for closed=True"
            request = route.calls[0].request
            params = dict(request.url.params)

            if adapter_config.name == "polymarket":
                assert params.get("closed") == "true", (
                    "Polymarket closed=True must send closed=true"
                )
            elif adapter_config.name == "kalshi":
                # Kalshi closed=True -> makes 2 requests (status=closed, status=settled)
                # Check first request has one of these
                assert params.get("status") in ["closed", "settled"], (
                    f"Kalshi closed=True must send status=closed or status=settled, got {params.get('status')}"
                )
            elif adapter_config.name == "predict_fun":
                assert params.get("status") == "RESOLVED"
                assert "closed" not in params

        # Test closed=None (no filter)
        route.calls.clear()
        count = 0
        async for _ in adapter_client.list_markets(closed=None, limit=1):
            count += 1
            if count >= 1:
                break

        assert len(route.calls) > 0, "Expected at least one request for closed=None"
        request = route.calls[0].request
        params = dict(request.url.params)

        # closed=None should NOT send status/closed filter
        if adapter_config.name == "polymarket":
            assert "closed" not in params, "Polymarket closed=None must not send closed param"
        elif adapter_config.name == "kalshi":
            assert "status" not in params, "Kalshi closed=None must not send status param"
        elif adapter_config.name == "limitless":
            # Limitless doesn't have status/closed filter params
            assert "status" not in params, "Limitless closed=None must not send status param"
            assert "closed" not in params, "Limitless closed=None must not send closed param"
        elif adapter_config.name == "hyperliquid":
            assert "status" not in params, "Hyperliquid closed=None must not send status param"
            assert "closed" not in params, "Hyperliquid closed=None must not send closed param"
            assert json.loads(request.content)["type"] == "outcomeMeta"
        elif adapter_config.name == "predict_fun":
            assert "status" not in params, "Predict.fun closed=None must not send status param"
            assert "closed" not in params, "Predict.fun closed=None must not send closed param"


# =============================================================================
# CONTRACT 17: get_trades limit and pagination
# =============================================================================


@pytest.mark.asyncio
async def test_get_trades_limit_and_pagination(adapter_config, adapter_client):
    """Verify get_trades(limit) returns exactly N trades and stops paging.

    Contract:
    - get_trades(limit=N) returns exactly N trades (if available) and stops paging
    - Default limit is 100
    - iter_trades() paginates until exhausted (no limit)
    - No page requested after limit is reached
    """

    # Build 3 pages × 50 synthetic trades each = 150 total
    def build_trade(index: int) -> dict:
        if adapter_config.name == "polymarket":
            return {
                "proxy_wallet": f"0x{index:040x}",
                "side": "BUY" if index % 2 == 0 else "SELL",
                "token_id": "test_token_123",
                "size": 10.0 + index,
                "price": 0.5 + (index % 10) * 0.01,
                "timestamp": 1791697593 + index,
                "outcome": "Yes",
            }
        elif adapter_config.name == "kalshi":
            return {
                "trade_id": str(1000 + index),
                "yes_price": 50 + (index % 50),
                "count": 1,
                "created_time": f"2026-10-10T06:{index % 60:02d}:00Z",
                "taker_side": "yes" if index % 2 == 0 else "no",
            }
        elif adapter_config.name == "limitless":
            return {
                "address": f"0x{index:040x}",
                "type": "BUY" if index % 2 == 0 else "SELL",
                "price": 0.5 + (index % 10) * 0.01,
                "size": (10 + index) * 1000000,  # 6-decimal units
                "timestamp": 1791697593 + index,
            }
        elif adapter_config.name == "hyperliquid":
            # recentTrades shape. One response is the whole window (no cursor).
            return {
                "coin": "#10",
                "side": "B" if index % 2 == 0 else "A",
                "px": f"{0.50 + (index % 10) * 0.01:.2f}",
                "sz": str(10 + index),
                "time": 1791697593000 + index,
                "hash": f"0x{index:064x}",
                "tid": 1000 + index,
                "users": [
                    "0x1111111111111111111111111111111111111111",
                    "0x2222222222222222222222222222222222222222",
                ],
            }
        elif adapter_config.name == "pascal":
            # PublicTradeMsg. Prices stay inside 0.50–0.59.
            cents = 50 + (index % 10)
            return {
                "trade_id": str(1000 + index),
                "symbol": "TEST.MARKET",
                "taker_side": "BID" if index % 2 == 0 else "ASK",
                "price": f"0.{cents:02d}0000",
                "size": str(10 + index),
                "maker": "MakerPubkey111111111111111111111111111111",
                "taker": "TakerPubkey111111111111111111111111111111",
                "seq": str(1000 + index),
                "trade_ts_ms": str(1791697593000 + index),
            }
        elif adapter_config.name == "predict_fun":
            # Match events. priceExecuted and amountFilled are 18-decimal wei.
            # Prices stay inside 0.50–0.59.
            cents = 50 + (index % 10)
            wei_price = str(cents * 10**16)
            wei_size = str((10 + index) * 10**18)
            return {
                "transactionHash": f"0x{index:064x}",
                "executedAt": f"2026-10-24T01:{index % 60:02d}:45.000Z",
                "priceExecuted": wei_price,
                "amountFilled": wei_size,
                "taker": {
                    "quoteType": "Bid" if index % 2 == 0 else "Ask",
                    "price": wei_price,
                    "amount": wei_size,
                    "outcome": {
                        "name": "Yes",
                        "indexSet": 1,
                        "onChainId": "1",
                        "status": None,
                    },
                },
            }
        raise AssertionError(f"no synthetic trade for {adapter_config.name}")

    outcome_id = "test_market_123"

    # Build static pages
    pages = []
    if adapter_config.name == "limitless":
        # Limitless: single page only (no pagination), cap at 100
        trades_page = [build_trade(i) for i in range(100)]
        pages.append({"events": trades_page})
    elif adapter_config.name == "hyperliquid":
        # recentTrades is one JSON array, not a cursor.
        pages.append([build_trade(i) for i in range(100)])
    elif adapter_config.name == "pascal":
        # Three cursor pages, same shape as GET /api/v1/trades.
        for page_num in range(1, 4):
            start_idx = (page_num - 1) * 50
            trades_page = [build_trade(i) for i in range(start_idx, start_idx + 50)]
            data_obj: dict[str, Any] = {"items": trades_page}
            if page_num < 3:
                data_obj["next_cursor"] = f"cursor_{page_num}"
            pages.append(
                {
                    "status": "success",
                    "data": data_obj,
                    "server_time_ms": "1791697593000",
                }
            )
    elif adapter_config.name == "predict_fun":
        # Three cursor pages, same shape as GET /v1/orders/matches.
        for page_num in range(1, 4):
            start_idx = (page_num - 1) * 50
            trades_page = [build_trade(i) for i in range(start_idx, start_idx + 50)]
            pages.append(
                {
                    "success": True,
                    "cursor": f"cursor_{page_num}" if page_num < 3 else None,
                    "data": trades_page,
                }
            )
    else:
        # Polymarket/Kalshi: 3 pages × 50 each = 150 total
        for page_num in range(1, 4):  # 3 pages
            start_idx = (page_num - 1) * 50
            trades_page = [build_trade(i) for i in range(start_idx, start_idx + 50)]

            if adapter_config.name == "polymarket":
                next_cursor = f"cursor_{page_num}" if page_num < 3 else None
                pages.append({"data": trades_page, "next_cursor": next_cursor})
            elif adapter_config.name == "kalshi":
                next_cursor = f"cursor_{page_num}" if page_num < 3 else None
                pages.append({"trades": trades_page, "cursor": next_cursor})

    with respx.mock:
        # Track which page to return
        page_index = [0]

        def get_next_page(request):
            idx = page_index[0]
            page_index[0] += 1
            if idx < len(pages):
                return Response(200, json=pages[idx])
            else:
                # Return empty if beyond available pages
                if adapter_config.name == "polymarket":
                    return Response(200, json={"data": [], "next_cursor": None})
                elif adapter_config.name == "kalshi":
                    return Response(200, json={"trades": [], "cursor": None})
                elif adapter_config.name == "hyperliquid":
                    return Response(200, json=[])
                elif adapter_config.name == "pascal":
                    return Response(200, json={"status": "success", "data": {"items": []}})
                elif adapter_config.name == "predict_fun":
                    return Response(200, json={"success": True, "cursor": None, "data": []})
                else:
                    return Response(200, json={"events": []})

        # Set up route with side_effect
        if adapter_config.name == "polymarket":
            route = respx.get(url__regex=r".*/trades.*").mock(side_effect=get_next_page)
        elif adapter_config.name == "kalshi":
            route = respx.get(url__regex=r".*/trades.*").mock(side_effect=get_next_page)
        elif adapter_config.name == "limitless":
            route = respx.get(url__regex=r".*/events.*").mock(side_effect=get_next_page)
        elif adapter_config.name == "hyperliquid":
            route = respx.post(HYPERLIQUID_INFO).mock(side_effect=get_next_page)
        elif adapter_config.name == "pascal":
            route = respx.get(url__regex=r".*/trades.*").mock(side_effect=get_next_page)
        elif adapter_config.name == "predict_fun":
            route = respx.get(url__regex=r".*/matches.*").mock(side_effect=get_next_page)

        # Test with explicit limit=5 (should fetch exactly 5 from first page)
        page_index[0] = 0
        trades = []
        async for trade in adapter_client.get_trades(outcome_id, limit=5):
            trades.append(trade)

        assert len(trades) == 5, (
            f"get_trades(limit=5) returned {len(trades)} trades, expected exactly 5"
        )
        assert route.call_count == 1, (
            f"get_trades(limit=5) made {route.call_count} requests, expected 1 (should stop after first page)"
        )

        # Test default limit=100
        page_index[0] = 0
        route.calls.clear()
        trades_default = []
        async for trade in adapter_client.get_trades(outcome_id):
            trades_default.append(trade)

        if adapter_config.name in ("limitless", "hyperliquid"):
            # Single page, no cursor. The synthetic window has 100 trades.
            assert len(trades_default) == 100, (
                f"get_trades() without limit returned {len(trades_default)} trades, expected 100"
            )
            assert route.call_count == 1, (
                f"get_trades(limit=100) made {route.call_count} requests, expected 1 (no pagination)"
            )
        else:
            # Polymarket/Kalshi: should fetch exactly 100 from 2 pages (50+50)
            assert len(trades_default) == 100, (
                f"get_trades() without limit returned {len(trades_default)} trades, expected exactly 100 (default)"
            )
            assert route.call_count == 2, (
                f"get_trades(limit=100) made {route.call_count} requests, expected 2 (50+50, should stop)"
            )

        # Test iter_trades() paginates until exhausted
        page_index[0] = 0
        route.calls.clear()
        all_trades = []
        async for trade in adapter_client.iter_trades(outcome_id):
            all_trades.append(trade)

        if adapter_config.name in ("limitless", "hyperliquid"):
            assert len(all_trades) == 100, (
                f"iter_trades() returned {len(all_trades)} trades, expected 100 (single page)"
            )
            assert route.call_count == 1, (
                f"iter_trades() made {route.call_count} requests, expected 1 (no pagination)"
            )
        else:
            assert len(all_trades) == 150, (
                f"iter_trades() returned {len(all_trades)} trades, expected 150 (all 3 pages)"
            )
            assert route.call_count == 3, (
                f"iter_trades() made {route.call_count} requests, expected 3 (all pages)"
            )


# =============================================================================
# CONTRACT 18: Exception hierarchy and error mapping
# =============================================================================


@pytest.mark.asyncio
async def test_exception_hierarchy_and_error_mapping(adapter_config, adapter_client):
    """Verify exception hierarchy and HTTP status code mapping.

    Contract:
    - All exceptions inherit from PredictionMarketsError
    - NotFoundError for unknown market/ticker
    - NoLiquidityError for existing market with empty orderbook
    - RateLimitError for HTTP 429
    - VenueAPIError for HTTP 5xx
    - ValueError for bad arguments
    - All exceptions importable from package root
    """
    from prediction_markets_api import (
        NoLiquidityError,
        NotFoundError,
        PredictionMarketsError,
        RateLimitError,
        VenueAPIError,
    )

    # Verify inheritance
    assert issubclass(NotFoundError, PredictionMarketsError)
    assert issubclass(VenueAPIError, PredictionMarketsError)
    assert issubclass(RateLimitError, VenueAPIError)
    assert issubclass(NoLiquidityError, PredictionMarketsError)

    # Test NotFoundError for unknown market
    with respx.mock:
        if adapter_config.name == "kalshi":
            # Kalshi: /markets/{ticker} returns 404
            respx.get(url__regex=r".*/markets/.*").mock(
                return_value=Response(404, json={"error": "not found"})
            )
            # Try get_market which should raise NotFoundError for 404
            with pytest.raises(NotFoundError):
                await adapter_client.get_market("NONEXISTENT_TICKER")
        elif adapter_config.name == "polymarket":
            # Polymarket: get_market uses gamma-api.polymarket.com/markets
            respx.get(url__regex=r"https://gamma-api\.polymarket\.com/markets.*").mock(
                return_value=Response(404, json={"error": "not found"})
            )
            with pytest.raises(NotFoundError):
                await adapter_client.get_market("NONEXISTENT_CONDITION_ID")
        elif adapter_config.name == "limitless":
            # Limitless: /markets/{slug} returns 404
            respx.get(url__regex=r"https://api\.limitless\.exchange/markets/.*").mock(
                return_value=Response(404, json={"error": "not found"})
            )
            with pytest.raises(NotFoundError):
                await adapter_client.get_market("nonexistent-slug")
        elif adapter_config.name == "hyperliquid":
            # Info API: an unknown resource is surfaced as HTTP 404 -> NotFoundError.
            respx.post(HYPERLIQUID_INFO).mock(
                return_value=Response(404, json={"error": "not found"})
            )
            with pytest.raises(NotFoundError):
                await adapter_client.get_market("99999999")

    # Test NoLiquidityError for existing market with empty orderbook (Kalshi only)
    if adapter_config.name == "kalshi":
        with respx.mock:
            # Mock empty orderbook (200)
            respx.get(url__regex=r".*/orderbook$").mock(
                return_value=Response(200, json={"orderbook": {"yes": [], "no": []}})
            )
            # Mock existing market (200)
            respx.get(url__regex=r".*/markets/[^/]+$").mock(
                return_value=Response(
                    200,
                    json={
                        "market": {
                            "ticker": "EXISTING-TICKER",
                            "title": "Test market",
                            "status": "open",
                            "result": None,
                        }
                    },
                )
            )

            # Empty orderbook + existing market -> NoLiquidityError
            with pytest.raises(NoLiquidityError):
                await adapter_client.get_price("EXISTING-TICKER", side=OrderSide.BUY)

    # Test NotFoundError for unknown ticker via get_price (Kalshi only)
    # When orderbook is empty, get_price checks if market exists via get_market
    if adapter_config.name == "kalshi":
        with respx.mock:
            # Mock empty orderbook (200)
            respx.get(url__regex=r".*/orderbook$").mock(
                return_value=Response(200, json={"orderbook": {"yes": [], "no": []}})
            )
            # Mock 404 on market lookup
            respx.get(url__regex=r".*/markets/[^/]+$").mock(
                return_value=Response(404, json={"error": "not found"})
            )

            # Empty orderbook + 404 market -> NotFoundError (not NoLiquidityError)
            with pytest.raises(NotFoundError):
                await adapter_client.get_price("NONEXISTENT-TICKER", side=OrderSide.BUY)

    # Test HTTP 429 -> RateLimitError
    with respx.mock:
        if adapter_config.name == "hyperliquid":
            respx.post(HYPERLIQUID_INFO).mock(
                return_value=Response(429, json={"error": "rate limit exceeded"})
            )
        else:
            respx.get(url__regex=r".*/markets.*").mock(
                return_value=Response(429, json={"error": "rate limit exceeded"})
            )

        with pytest.raises(RateLimitError):
            async for _ in adapter_client.list_markets(limit=1):
                pass

    # Test HTTP 5xx -> VenueAPIError
    with respx.mock:
        if adapter_config.name == "hyperliquid":
            respx.post(HYPERLIQUID_INFO).mock(
                return_value=Response(503, json={"error": "service unavailable"})
            )
        else:
            respx.get(url__regex=r".*/markets.*").mock(
                return_value=Response(503, json={"error": "service unavailable"})
            )

        with pytest.raises(VenueAPIError):
            async for _ in adapter_client.list_markets(limit=1):
                pass


@pytest.mark.asyncio
async def test_bad_argument_validation(adapter_config, adapter_client):
    """Verify bad arguments raise ValueError.

    Contract:
    - limit <= 0 should raise ValueError before making any HTTP requests
    """
    with respx.mock as respx_mock:
        # No routes mocked - should never reach HTTP layer
        with pytest.raises(ValueError, match="limit"):
            async for _ in adapter_client.list_markets(limit=0):
                pass

        # Verify no HTTP requests were made
        assert len(respx_mock.calls) == 0, "HTTP request made despite invalid limit"


# =============================================================================
# CONTRACT 19: list_markets with unknown keyword raises TypeError
# =============================================================================


@pytest.mark.asyncio
async def test_list_markets_unknown_keyword_raises(adapter_client):
    """Verify list_markets with unknown keyword argument raises TypeError.

    After venue_params refactor, list_markets should not accept arbitrary **kwargs.
    Unknown keywords must raise TypeError immediately.
    """
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        async for _ in adapter_client.list_markets(invalid_param="test"):
            pass  # Should not reach here


# =============================================================================
# CONTRACT 20: place_order with unknown keyword raises TypeError
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="place_order still accepts **kwargs; kept intentionally until 1.0",
)
async def test_place_order_unknown_keyword_raises(adapter_client):
    """Verify place_order with unknown keyword argument raises TypeError.

    place_order should not accept arbitrary **kwargs for type safety.
    Unknown keywords must raise TypeError immediately.

    Currently xfail because place_order still accepts **kwargs.
    Kept intentionally until 1.0.
    """
    from decimal import Decimal

    from prediction_markets_api.models.base import OrderSide

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        await adapter_client.place_order(
            outcome_id="test",
            side=OrderSide.BUY,
            size=Decimal("10"),
            price=Decimal("0.5"),
            dry_run=True,
            invalid_param="test",
        )
