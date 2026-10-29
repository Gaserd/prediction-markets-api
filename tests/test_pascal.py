"""Tests for the Pascal adapter using recorded API fixtures."""

import json
import logging
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from prediction_markets_api.adapters.pascal import PascalClient
from prediction_markets_api.exceptions import (
    NoLiquidityError,
    NotFoundError,
    RateLimitError,
    VenueAPIError,
)
from prediction_markets_api.models.base import Currency, MarketStatus, OrderSide

FIXTURES = Path(__file__).parent / "fixtures" / "pascal"
BASE = PascalClient.API_BASE
SYMBOL = "ACA_HOUSE_2026.NOTEXT_DEM"
EMPTY_SYMBOL = "AI_BEST_MATH_26OCT31.MICROSOFT"
BID_ONLY = "BUND_2027_GLDN.DUNDAV"
ASK_ONLY = "AI_BEST_CHINA_26OCT31.MEITUAN"


def _load(name: str) -> dict[str, object]:
    path = FIXTURES / f"{name}_2026-10-29.json"
    with open(path) as handle:
        payload: dict[str, object] = json.load(handle)
    return payload


def _markets_route(payload: dict[str, object]):
    return respx.get(f"{BASE}/api/v1/markets").mock(return_value=httpx.Response(200, json=payload))


@pytest.mark.asyncio
async def test_list_markets_prices_and_empty_book() -> None:
    """Two-sided books use the mid. An empty book is price None, not mark_price."""
    payload = _load("markets_response")
    async with PascalClient() as client:
        with respx.mock:
            route = _markets_route(payload)
            markets = [market async for market in client.list_markets(closed=False, limit=10)]

    assert route.call_count == 1
    by_id = {market.id: market for market in markets}
    assert set(by_id) == {SYMBOL, EMPTY_SYMBOL, BID_ONLY, ASK_ONLY}

    two = by_id[SYMBOL]
    assert two.venue == "pascal"
    assert two.question == "ACA credits and 2026 House winner: Not extended, Democrats"
    assert two.status == MarketStatus.OPEN
    assert two.resolved is False
    assert two.currency == Currency.USD
    assert two.liquidity is None
    assert two.volume == Decimal("1370.517000")
    assert two.created_at.tzinfo is not None
    assert "is_mece" not in two.raw_data
    assert "is_mece" not in type(two).model_fields
    long = two.outcomes[0]
    short = two.outcomes[1]
    assert long.id == SYMBOL
    assert long.name == "Yes"
    assert long.price == Decimal("0.925000")
    assert long.raw_price == "0.925000"
    assert short.id == f"{SYMBOL}:short"
    assert short.price == Decimal("0.075000")
    assert short.raw_price is not None

    empty = by_id[EMPTY_SYMBOL]
    assert empty.raw_data.get("mark_price") == "0.052000"
    assert empty.outcomes[0].price is None
    assert empty.outcomes[0].raw_price is None
    assert empty.outcomes[1].price is None
    assert empty.outcomes[1].raw_price is None

    bid_only = by_id[BID_ONLY]
    assert bid_only.raw_data.get("mark_price") == "0.079000"
    assert "best_ask" not in bid_only.raw_data or bid_only.raw_data.get("best_ask") is None
    assert bid_only.outcomes[0].price is None
    assert bid_only.outcomes[0].raw_price is None
    assert bid_only.outcomes[1].price is None

    ask_only = by_id[ASK_ONLY]
    assert ask_only.raw_data.get("mark_price") == "0.007000"
    assert ask_only.outcomes[0].price is None
    assert ask_only.outcomes[0].raw_price is None
    assert ask_only.outcomes[1].price is None


@pytest.mark.asyncio
async def test_is_mece_stays_on_raw_data_only() -> None:
    """The optional flag is preserved when present and ignored when absent."""
    payload = _load("markets_response")
    data = payload["data"]
    assert isinstance(data, list)
    first = dict(data[0])
    second = dict(data[1])
    first["is_mece"] = True
    assert "is_mece" not in second
    body = {**payload, "data": [first, second]}
    async with PascalClient() as client:
        with respx.mock:
            _markets_route(body)
            markets = [market async for market in client.list_markets(closed=False, limit=2)]
    assert markets[0].raw_data["is_mece"] is True
    assert "is_mece" not in markets[1].raw_data
    assert "is_mece" not in type(markets[0]).model_fields


@pytest.mark.asyncio
async def test_list_markets_limit_does_not_fetch_resolved() -> None:
    payload = _load("markets_response")
    async with PascalClient() as client:
        with respx.mock:
            live = _markets_route(payload)
            resolved = respx.get(f"{BASE}/api/v1/markets/resolved").mock(
                return_value=httpx.Response(200, json=payload)
            )
            markets = [market async for market in client.list_markets(limit=1)]
    assert len(markets) == 1
    assert live.call_count == 1
    assert resolved.call_count == 0


@pytest.mark.asyncio
async def test_list_resolved_sends_order_by_and_winning_outcome() -> None:
    payload = _load("resolved_markets")
    async with PascalClient() as client:
        with respx.mock:
            route = respx.get(f"{BASE}/api/v1/markets/resolved").mock(
                return_value=httpx.Response(200, json=payload)
            )
            markets = [market async for market in client.list_markets(closed=True, limit=10)]
    assert route.call_count == 1
    params = dict(route.calls[0].request.url.params)
    assert params["order_by"] == "resolution_time"
    assert params["lookback"] == "24h"
    assert params["limit"] == "50"
    market = markets[0]
    assert market.id == "MLB_2026_WS.TB"
    assert market.status == MarketStatus.RESOLVED
    assert market.resolved is True
    assert market.winning_outcome == "No"
    assert market.outcomes[0].price == Decimal("0")
    assert market.outcomes[0].raw_price == "0.000000"
    assert market.outcomes[1].price == Decimal("1")
    assert market.outcomes[1].name == "No"


@pytest.mark.asyncio
async def test_text_search_sends_order_by_and_rejects_long_query() -> None:
    payload = _load("text_search")
    async with PascalClient() as client:
        with respx.mock:
            route = respx.post(f"{BASE}/api/v1/markets/text-search").mock(
                return_value=httpx.Response(200, json=payload)
            )
            markets = [
                market
                async for market in client.list_markets(
                    limit=2,
                    venue_params={"query": "world series"},
                )
            ]
            sent = json.loads(route.calls[0].request.content)
            assert sent["query"] == "world series"
            assert sent["order_by"] == "volume_all_time"
            assert sent["status"] == "all"
            assert sent["limit"] == 2
            assert len(markets) == 2
            assert markets[0].status == MarketStatus.RESOLVED

    async with PascalClient() as client:
        with respx.mock as router:
            with pytest.raises(ValueError, match="1-32"):
                async for _ in client.list_markets(venue_params={"query": "a" * 33}):
                    pass
            with pytest.raises(ValueError, match="1-32"):
                async for _ in client.list_markets(venue_params={"query": "   "}):
                    pass
            assert len(router.calls) == 0

        with respx.mock:
            explicit = respx.post(f"{BASE}/api/v1/markets/text-search").mock(
                return_value=httpx.Response(200, json={"status": "success", "data": []})
            )
            found = [
                market
                async for market in client.list_markets(
                    closed=False,
                    venue_params={"query": "é" * 32, "order_by": "volume_24h"},
                )
            ]
            body = json.loads(explicit.calls[0].request.content)
            assert found == []
            assert body["order_by"] == "volume_24h"
            assert body["status"] == "live"
            assert body["query"] == "é" * 32
            assert len(body["query"]) == 32


@pytest.mark.asyncio
async def test_get_market_found_and_missing() -> None:
    payload = _load("search_market")
    async with PascalClient() as client:
        with respx.mock:
            respx.post(f"{BASE}/api/v1/markets/search").mock(
                return_value=httpx.Response(200, json=payload)
            )
            market = await client.get_market(SYMBOL)
            assert market.id == SYMBOL

        with respx.mock:
            respx.post(f"{BASE}/api/v1/markets/search").mock(
                return_value=httpx.Response(200, json={"status": "success", "data": []})
            )
            with pytest.raises(NotFoundError):
                await client.get_market("DOES_NOT_EXIST.NOPE")


@pytest.mark.asyncio
async def test_order_book_price_and_short_side() -> None:
    payload = _load("orderbook_response")
    async with PascalClient() as client:
        with respx.mock:
            respx.get(url__regex=r".*/books.*").mock(return_value=httpx.Response(200, json=payload))
            book = await client.get_order_book(SYMBOL)
            buy = await client.get_price(SYMBOL, OrderSide.BUY)
            sell = await client.get_price(SYMBOL, OrderSide.SELL)
            short = await client.get_order_book(f"{SYMBOL}:short")
    assert book.bids[0].price == Decimal("0.910000")
    assert book.bids[0].size == Decimal("8979")
    assert book.asks[0].price == Decimal("0.940000")
    assert book.currency == Currency.USD
    assert book.timestamp.tzinfo is not None
    assert buy.price == book.asks[0].price
    assert sell.price == book.bids[0].price
    assert short.bids[0].price == Decimal("1") - book.asks[0].price
    assert short.asks[0].price == Decimal("1") - book.bids[0].price
    assert short.bids[0].size == book.asks[0].size


@pytest.mark.asyncio
async def test_empty_book_is_no_liquidity_unknown_symbol_is_not_found() -> None:
    empty = _load("empty_orderbook")
    missing = _load("error_symbol_not_found")
    search = _load("search_market")
    # Point the recorded search body at the empty-book symbol by reusing a 200
    # search of that market from the listing fixture.
    listing = _load("markets_response")
    data = listing["data"]
    assert isinstance(data, list)
    empty_market = next(item for item in data if item["symbol"] == EMPTY_SYMBOL)
    found = {**search, "data": [empty_market]}
    async with PascalClient() as client:
        with respx.mock:
            respx.get(url__regex=r".*/books.*").mock(return_value=httpx.Response(200, json=empty))
            respx.post(f"{BASE}/api/v1/markets/search").mock(
                return_value=httpx.Response(200, json=found)
            )
            book = await client.get_order_book(EMPTY_SYMBOL)
            assert book.bids == []
            assert book.asks == []
            with pytest.raises(NoLiquidityError):
                await client.get_price(EMPTY_SYMBOL, OrderSide.BUY)

        with respx.mock:
            respx.get(url__regex=r".*/books.*").mock(return_value=httpx.Response(422, json=missing))
            with pytest.raises(NotFoundError):
                await client.get_order_book("DOES_NOT_EXIST.NOPE")


@pytest.mark.asyncio
async def test_one_sided_ask_is_not_a_price() -> None:
    """Only an ask, including 0.999, is not a price and mark_price is ignored."""
    payload = _load("markets_response")
    data = payload["data"]
    assert isinstance(data, list)
    source = next(item for item in data if item["symbol"] == ASK_ONLY)
    market_raw = dict(source)
    market_raw.pop("best_bid", None)
    market_raw["best_ask"] = "0.999000"
    market_raw["mark_price"] = "0.500000"
    body = {**payload, "data": [market_raw]}
    async with PascalClient() as client:
        with respx.mock:
            _markets_route(body)
            markets = [market async for market in client.list_markets(closed=False, limit=1)]
    assert len(markets) == 1
    long = markets[0].outcomes[0]
    short = markets[0].outcomes[1]
    assert long.price is None
    assert long.raw_price is None
    assert short.price is None
    assert short.raw_price is None
    assert markets[0].raw_data["best_ask"] == "0.999000"
    assert markets[0].raw_data["mark_price"] == "0.500000"


@pytest.mark.asyncio
async def test_short_trades_invert_price_and_side() -> None:
    """A :short print is the complement of the long print. The long print is unchanged."""
    payload = _load("trades_response")
    async with PascalClient() as client:
        with respx.mock:
            respx.get(url__regex=r".*/trades.*").mock(
                return_value=httpx.Response(200, json=payload)
            )
            short_limited = [trade async for trade in client.get_trades(f"{SYMBOL}:short", limit=1)]
        with respx.mock:
            respx.get(url__regex=r".*/trades.*").mock(
                return_value=httpx.Response(200, json=payload)
            )
            short_all = [trade async for trade in client.iter_trades(f"{SYMBOL}:short")]
        with respx.mock:
            respx.get(url__regex=r".*/trades.*").mock(
                return_value=httpx.Response(200, json=payload)
            )
            long_limited = [trade async for trade in client.get_trades(SYMBOL, limit=1)]
    assert len(short_limited) == 1
    assert short_limited[0].outcome_id == f"{SYMBOL}:short"
    assert short_limited[0].price == Decimal("0.100000")
    assert short_limited[0].side == OrderSide.BUY
    assert short_limited[0].size == Decimal("5")
    assert short_limited[0].raw_data["price"] == "0.900000"
    assert short_limited[0].raw_data["taker_side"] == "ASK"
    assert len(short_all) == 3
    assert short_all[0].price == Decimal("0.100000")
    assert short_all[0].side == OrderSide.BUY
    assert short_all[1].price == Decimal("0.090000")
    assert short_all[1].side == OrderSide.BUY
    assert long_limited[0].outcome_id == SYMBOL
    assert long_limited[0].price == Decimal("0.900000")
    assert long_limited[0].side == OrderSide.SELL


def _trade_page(trade_id: str, price: str, cursor: str) -> dict[str, object]:
    return {
        "status": "success",
        "data": {
            "items": [
                {
                    "trade_id": trade_id,
                    "symbol": SYMBOL,
                    "taker_side": "BID",
                    "price": price,
                    "size": "1",
                    "trade_ts_ms": "1792425997189",
                }
            ],
            "next_cursor": cursor,
        },
    }


@pytest.mark.asyncio
async def test_iter_trades_stops_on_alternating_cursors(caplog: pytest.LogCaptureFixture) -> None:
    """Cursors A -> B -> A stop after the repeat, with one warning and no raise."""

    def respond(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("before_cursor")
        if cursor is None:
            body = _trade_page("1", "0.400000", "cursor-a")
        elif cursor == "cursor-a":
            body = _trade_page("2", "0.410000", "cursor-b")
        else:
            body = _trade_page("1", "0.400000", "cursor-a")
        return httpx.Response(200, json=body)

    async with PascalClient() as client:
        with respx.mock:
            route = respx.get(url__regex=r".*/trades.*").mock(side_effect=respond)
            with caplog.at_level(logging.WARNING, logger="prediction_markets_api.adapters.pascal"):
                trades = [trade async for trade in client.iter_trades(SYMBOL)]
    assert route.call_count == 3
    assert [trade.id for trade in trades] == ["1", "2"]
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "cursor-a" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_trades_limit_and_cursor() -> None:
    first = _load("trades_response")
    second = {
        "status": "success",
        "data": {"items": []},
        "server_time_ms": "1793256950000",
    }
    async with PascalClient() as client:
        with respx.mock:
            route = respx.get(url__regex=r".*/trades.*").mock(
                side_effect=[
                    httpx.Response(200, json=first),
                    httpx.Response(200, json=second),
                ]
            )
            limited = [trade async for trade in client.get_trades(SYMBOL, limit=2)]
            assert len(limited) == 2
            assert route.call_count == 1
            assert limited[0].side == OrderSide.SELL
            assert limited[0].price == Decimal("0.900000")
            assert limited[0].size == Decimal("5")
            assert limited[0].timestamp.tzinfo is not None
            assert limited[0].raw_data["trade_id"] == "128702"

        with respx.mock:
            respx.get(url__regex=r".*/trades.*").mock(
                side_effect=[
                    httpx.Response(200, json=first),
                    httpx.Response(200, json=second),
                ]
            )
            all_trades = [trade async for trade in client.iter_trades(SYMBOL)]
    assert len(all_trades) == 3


@pytest.mark.asyncio
async def test_error_mapping() -> None:
    invalid = _load("error_invalid_request")
    async with PascalClient() as client:
        with respx.mock:
            respx.get(f"{BASE}/api/v1/markets").mock(
                return_value=httpx.Response(
                    429,
                    json={
                        "status": "error",
                        "data": {"code": "RATE_LIMITED", "message": "slow down"},
                    },
                    headers={"Retry-After": "3"},
                )
            )
            with pytest.raises(RateLimitError) as rate:
                async for _ in client.list_markets(closed=False, limit=1):
                    pass
            assert rate.value.retry_after == 3

        with respx.mock:
            respx.get(f"{BASE}/api/v1/markets").mock(
                return_value=httpx.Response(
                    503,
                    json={
                        "status": "error",
                        "data": {"code": "SERVICE_UNAVAILABLE", "message": "down"},
                    },
                )
            )
            with pytest.raises(VenueAPIError) as api_error:
                async for _ in client.list_markets(closed=False, limit=1):
                    pass
            assert not isinstance(api_error.value, RateLimitError)
            assert api_error.value.status_code == 503

        with respx.mock:
            respx.get(f"{BASE}/api/v1/markets").mock(return_value=httpx.Response(422, json=invalid))
            with pytest.raises(VenueAPIError) as invalid_error:
                async for _ in client.list_markets(closed=False, limit=1):
                    pass
            assert invalid_error.value.status_code == 422
            assert "query too long" in str(invalid_error.value)


@pytest.mark.asyncio
async def test_crossed_book_and_place_order_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    crossed = {
        "status": "success",
        "data": {
            "books": {
                SYMBOL: {
                    "bids": [["0.700000", "1"]],
                    "asks": [["0.400000", "1"]],
                }
            }
        },
        "server_time_ms": "1793256950000",
    }
    async with PascalClient(max_order_size=Decimal("5")) as client:
        with respx.mock:
            respx.get(url__regex=r".*/books.*").mock(return_value=httpx.Response(200, json=crossed))
            with pytest.raises(ValueError, match="crossed"):
                await client.get_order_book(SYMBOL)

        with respx.mock as router:
            dry = await client.place_order(SYMBOL, OrderSide.BUY, Decimal("1"), Decimal("0.4"))
            assert dry["dry_run"] is True
            assert len(router.calls) == 0
            with pytest.raises(RuntimeError, match="max_order_size"):
                await client.place_order(
                    SYMBOL, OrderSide.BUY, Decimal("6"), Decimal("0.4"), dry_run=False
                )
            with pytest.raises(PermissionError):
                await client.place_order(
                    SYMBOL, OrderSide.BUY, Decimal("1"), Decimal("0.4"), dry_run=False
                )
        monkeypatch.setenv("PASCAL_API_KEY", "test-key")
    async with PascalClient() as client:
        result = await client.place_order(
            SYMBOL, OrderSide.BUY, Decimal("1"), Decimal("0.4"), dry_run=False
        )
    assert result["status"] == "not_implemented"


@pytest.mark.asyncio
async def test_bad_limit_makes_no_request() -> None:
    async with PascalClient() as client:
        with respx.mock as router:
            with pytest.raises(ValueError, match="limit"):
                async for _ in client.list_markets(limit=0):
                    pass
            with pytest.raises(ValueError, match="limit"):
                async for _ in client.get_trades(SYMBOL, limit=-1):
                    pass
            assert len(router.calls) == 0
