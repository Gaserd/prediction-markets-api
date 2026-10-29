"""Tests for the Predict.fun adapter using recorded API fixtures.

Fixtures were recorded on 2026-10-29. Market, book, and trade payloads are from
the public testnet. The 401 payload is from mainnet without an API key.
"""

import json
import logging
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from prediction_markets_api.adapters.predict_fun import PredictFunClient
from prediction_markets_api.exceptions import (
    AuthenticationError,
    NoLiquidityError,
    NotFoundError,
    RateLimitError,
    VenueAPIError,
)
from prediction_markets_api.models.base import Currency, MarketStatus, OrderSide
from prediction_markets_api.utils import parse_timestamp

FIXTURES = Path(__file__).parent / "fixtures" / "predict_fun"
DATE = "2026-10-29"
SECRET = "super-secret-predict-key"


def _load(name: str) -> dict[str, object]:
    with open(FIXTURES / f"{name}_{DATE}.json") as handle:
        payload: dict[str, object] = json.load(handle)
    return payload


def _client(
    monkeypatch: pytest.MonkeyPatch, *, key: str | None, base_url: str | None = None
) -> PredictFunClient:
    if key is None:
        monkeypatch.delenv("PREDICTFUN_API_KEY", raising=False)
    else:
        monkeypatch.setenv("PREDICTFUN_API_KEY", key)
    return PredictFunClient(base_url=base_url)


@pytest.mark.asyncio
async def test_list_markets_two_sided_mid_and_raw_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """A two-sided book uses the mid. The raw market object is kept."""
    payload = _load("markets_response")
    async with _client(monkeypatch, key="test-key") as client:
        with respx.mock:
            route = respx.get(f"{PredictFunClient.API_BASE}/v1/markets").mock(
                return_value=httpx.Response(200, json=payload)
            )
            markets = [market async for market in client.list_markets(closed=False, limit=2)]

    assert route.call_count == 1
    assert route.calls[0].request.url.params["status"] == "OPEN"
    assert route.calls[0].request.url.params["first"] == "2"
    sent = route.calls[0].request.headers.get("x-api-key")
    assert sent == "test-key"
    assert len(markets) == 2
    market = markets[0]
    assert market.id == "437"
    assert market.venue == "predict_fun"
    assert market.question == "Will Trump nominate David Malpass as the next Fed chair?"
    assert market.status == MarketStatus.OPEN
    assert market.resolved is False
    assert market.currency == Currency.USD
    assert market.liquidity is None
    assert market.volume is None
    assert market.created_at.tzinfo is not None
    assert market.created_at.year == 2025
    assert market.raw_data["id"] == 437
    assert "success" not in market.raw_data
    assert market.raw_data["decimalPrecision"] == 2
    yes, no = market.outcomes
    assert yes.name == "Definitely"
    assert yes.id.startswith("437:1:")
    assert yes.price == Decimal("0.35")
    assert yes.raw_price == "0.35"
    assert no.name == "Maybe"
    assert no.price == Decimal("0.65")
    assert no.price == Decimal("1") - yes.price
    assert market.best_bid_size == Decimal("123154.3476")
    assert market.best_ask_size == Decimal("519999.6282")


@pytest.mark.asyncio
async def test_one_sided_book_is_not_a_price(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a bid or only an ask leaves price and raw_price unset."""
    payload = _load("onesided_market")
    async with _client(monkeypatch, key="test-key") as client:
        with respx.mock:
            respx.get(f"{PredictFunClient.API_BASE}/v1/markets/446").mock(
                return_value=httpx.Response(200, json=payload)
            )
            market = await client.get_market("446")
    assert market.id == "446"
    by_name = {outcome.name: outcome for outcome in market.outcomes}
    assert by_name["Yes"].price is None
    assert by_name["Yes"].raw_price is None
    assert by_name["No"].price is None
    assert by_name["No"].raw_price is None
    assert market.best_bid_size is None
    assert market.best_ask_size == Decimal("219468.9127")


@pytest.mark.asyncio
async def test_order_book_complement_and_price(monkeypatch: pytest.MonkeyPatch) -> None:
    """IndexSet 1 is the published book. IndexSet 2 is the complement."""
    markets = _load("markets_response")
    book_payload = _load("orderbook_response")
    data = markets["data"]
    assert isinstance(data, list)
    raw = data[0]
    assert isinstance(raw, dict)
    outcomes = raw["outcomes"]
    assert isinstance(outcomes, list)
    complement = outcomes[1]
    assert isinstance(complement, dict)
    complement_id = f"437:{complement['indexSet']}:{complement['onChainId']}"
    async with _client(monkeypatch, key="test-key") as client:
        with respx.mock:
            respx.get(f"{PredictFunClient.API_BASE}/v1/markets/437/orderbook").mock(
                return_value=httpx.Response(200, json=book_payload)
            )
            book = await client.get_order_book("437")
            flipped = await client.get_order_book(complement_id)
            buy = await client.get_price("437", OrderSide.BUY)
            sell = await client.get_price("437", OrderSide.SELL)
    assert book.market_id == "437"
    assert book.bids[0].price == Decimal("0.33")
    assert book.asks[0].price == Decimal("0.37")
    assert book.bids[0].raw_price == "0.33"
    assert book.currency == Currency.USD
    assert book.tick_size is None
    assert book.last_trade_price is None
    assert book.partial is False
    assert book.timestamp == parse_timestamp("1793264357131", is_milliseconds=True)
    assert [level.price for level in book.bids] == sorted(
        (level.price for level in book.bids), reverse=True
    )
    assert flipped.bids[0].price == Decimal("0.63")
    assert flipped.asks[0].price == Decimal("0.67")
    assert flipped.bids[0].size == book.asks[0].size
    assert buy.price == book.asks[0].price
    assert sell.price == book.bids[0].price


@pytest.mark.asyncio
async def test_one_sided_order_book_liquidity(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty side is NoLiquidityError. The other side is executable."""
    payload = _load("onesided_orderbook")
    market = _load("onesided_market")
    data = market["data"]
    assert isinstance(data, dict)
    outcomes = data["outcomes"]
    assert isinstance(outcomes, list)
    no = next(item for item in outcomes if isinstance(item, dict) and item.get("name") == "No")
    no_id = f"446:{no['indexSet']}:{no['onChainId']}"
    async with _client(monkeypatch, key="test-key") as client:
        with respx.mock:
            respx.get(f"{PredictFunClient.API_BASE}/v1/markets/446/orderbook").mock(
                return_value=httpx.Response(200, json=payload)
            )
            book = await client.get_order_book("446")
            assert book.bids == []
            assert book.asks[0].price == Decimal("0.07")
            buy = await client.get_price("446", OrderSide.BUY)
            assert buy.price == Decimal("0.07")
            with pytest.raises(NoLiquidityError) as sell_error:
                await client.get_price("446", OrderSide.SELL)
            flipped = await client.get_order_book(no_id)
            assert flipped.asks == []
            assert flipped.bids[0].price == Decimal("0.93")
            with pytest.raises(NoLiquidityError):
                await client.get_price(no_id, OrderSide.BUY)
    assert sell_error.value.side == "SELL"
    assert sell_error.value.market_id == "446"


@pytest.mark.asyncio
async def test_resolved_market_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    """The winning outcome is 1 and the other outcome is 0."""
    payload = _load("resolved_markets")
    async with _client(monkeypatch, key="test-key") as client:
        with respx.mock:
            respx.get(f"{PredictFunClient.API_BASE}/v1/markets").mock(
                return_value=httpx.Response(200, json=payload)
            )
            markets = [market async for market in client.list_markets(closed=True, limit=1)]
    assert len(markets) == 1
    market = markets[0]
    assert market.status == MarketStatus.RESOLVED
    assert market.resolved is True
    assert market.winning_outcome == "Up"
    by_name = {outcome.name: outcome for outcome in market.outcomes}
    assert by_name["Up"].price == Decimal("1")
    assert by_name["Up"].raw_price == "1"
    assert by_name["Down"].price == Decimal("0")
    assert by_name["Down"].raw_price == "0"
    assert market.raw_data["decimalPrecision"] == 3


@pytest.mark.asyncio
async def test_trades_wei_and_outcome_filter(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Match wei strings become probabilities and share counts. The taker outcome wins."""
    payload = _load("trades_response")
    data = payload["data"]
    assert isinstance(data, list)
    first = data[0]
    assert isinstance(first, dict)
    taker = first["taker"]
    assert isinstance(taker, dict)
    outcome = taker["outcome"]
    assert isinstance(outcome, dict)
    outcome_id = f"437:{outcome['indexSet']}:{outcome['onChainId']}"
    cursor = payload["cursor"]
    assert isinstance(cursor, str)
    async with _client(monkeypatch, key="test-key") as client:
        with respx.mock:
            route = respx.get(f"{PredictFunClient.API_BASE}/v1/orders/matches").mock(
                return_value=httpx.Response(200, json=payload)
            )
            trades = [trade async for trade in client.get_trades("437", limit=2)]
            assert route.call_count == 1
            assert route.calls[0].request.url.params.get("after") is None
            with caplog.at_level(
                logging.WARNING, logger="prediction_markets_api.adapters.predict_fun"
            ):
                filtered = [trade async for trade in client.get_trades(outcome_id, limit=5)]
    # The recorded page has two prints and a cursor. limit=2 fills on that page.
    # The outcome filter matches one print, so limit=5 follows the cursor once.
    # The fixture replays the same cursor and pagination stops.
    assert route.call_count == 3
    assert [call.request.url.params.get("after") for call in route.calls] == [
        None,
        None,
        cursor,
    ]
    assert (
        sum(
            "cursor" in record.message and "repeated" in record.message for record in caplog.records
        )
        == 1
    )
    assert trades[0].side == OrderSide.SELL
    assert trades[0].price == Decimal("0.63")
    assert trades[0].size == Decimal("30")
    assert trades[0].outcome_id == outcome_id
    assert trades[0].currency == Currency.USD
    assert trades[0].timestamp.tzinfo is not None
    assert trades[0].raw_data["priceExecuted"] == "630000000000000000"
    assert trades[1].side == OrderSide.BUY
    assert trades[1].price == Decimal("0.36")
    assert trades[1].size == Decimal("100")
    assert ":1:" in trades[1].outcome_id
    assert [trade.id for trade in filtered] == [trades[0].id]
    assert SECRET not in json.dumps(trades[0].raw_data)


@pytest.mark.asyncio
async def test_limit_does_not_follow_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _load("markets_response")
    async with _client(monkeypatch, key="test-key") as client:
        with respx.mock:
            route = respx.get(f"{PredictFunClient.API_BASE}/v1/markets").mock(
                return_value=httpx.Response(200, json=payload)
            )
            markets = [market async for market in client.list_markets(limit=1)]
    assert len(markets) == 1
    assert route.call_count == 1
    assert "after" not in route.calls[0].request.url.params


@pytest.mark.asyncio
async def test_repeated_trade_cursor_stops(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    payload = _load("trades_response")
    async with _client(monkeypatch, key="test-key") as client:
        with respx.mock:
            route = respx.get(f"{PredictFunClient.API_BASE}/v1/orders/matches").mock(
                return_value=httpx.Response(200, json=payload)
            )
            with caplog.at_level(
                logging.WARNING, logger="prediction_markets_api.adapters.predict_fun"
            ):
                trades = [trade async for trade in client.iter_trades("437")]
    assert len(trades) == 2
    assert route.call_count == 2
    assert (
        sum(
            "cursor" in record.message and "repeated" in record.message for record in caplog.records
        )
        == 1
    )
    assert SECRET not in caplog.text


@pytest.mark.asyncio
async def test_mainnet_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing mainnet key fails before any HTTP request and does not leak a value."""
    async with _client(monkeypatch, key=None) as client:
        assert "api_key=None" in repr(client)
        with respx.mock:
            route = respx.get(url__regex=r".*").mock(return_value=httpx.Response(500))
            with pytest.raises(AuthenticationError, match="PREDICTFUN_API_KEY") as caught:
                async for _ in client.list_markets(limit=1):
                    pass
    assert route.call_count == 0
    assert "x-api-key" in str(caught.value)
    assert SECRET not in str(caught.value)


@pytest.mark.asyncio
async def test_api_key_is_masked_and_not_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    payload = _load("markets_response")
    async with _client(monkeypatch, key=SECRET) as client:
        assert SECRET not in repr(client)
        assert "api_key='***'" in repr(client)
        with respx.mock:
            route = respx.get(f"{PredictFunClient.API_BASE}/v1/markets").mock(
                return_value=httpx.Response(200, json=payload)
            )
            with caplog.at_level(
                logging.DEBUG, logger="prediction_markets_api.adapters.predict_fun"
            ):
                _ = [market async for market in client.list_markets(limit=1)]
    assert route.calls[0].request.headers["x-api-key"] == SECRET
    assert SECRET not in caplog.text


@pytest.mark.asyncio
async def test_rejected_key_and_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    denied = _load("error_unauthorized")
    missing = _load("error_not_found")
    async with _client(monkeypatch, key=SECRET) as client:
        with respx.mock:
            respx.get(f"{PredictFunClient.API_BASE}/v1/markets").mock(
                return_value=httpx.Response(401, json=denied)
            )
            with pytest.raises(AuthenticationError, match="401") as caught:
                async for _ in client.list_markets(limit=1):
                    pass
        assert SECRET not in str(caught.value)
        with respx.mock:
            respx.get(f"{PredictFunClient.API_BASE}/v1/markets/999999999").mock(
                return_value=httpx.Response(404, json=missing)
            )
            with pytest.raises(NotFoundError):
                await client.get_market("999999999")


@pytest.mark.asyncio
async def test_rate_limit_and_server_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _client(monkeypatch, key="test-key") as client:
        with respx.mock:
            respx.get(f"{PredictFunClient.API_BASE}/v1/markets").mock(
                return_value=httpx.Response(
                    429,
                    json={"success": False, "code": 429, "message": "rate limit"},
                    headers={"ratelimit-reset": "55"},
                )
            )
            with pytest.raises(RateLimitError) as limited:
                async for _ in client.list_markets(limit=1):
                    pass
        assert limited.value.retry_after == 55
        assert limited.value.status_code == 429
        with respx.mock:
            respx.get(f"{PredictFunClient.API_BASE}/v1/markets").mock(
                return_value=httpx.Response(503, json={"success": False, "message": "unavailable"})
            )
            with pytest.raises(VenueAPIError) as failed:
                async for _ in client.list_markets(limit=1):
                    pass
    assert not isinstance(failed.value, RateLimitError)
    assert failed.value.status_code == 503


@pytest.mark.asyncio
async def test_testnet_reads_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _load("markets_response")
    async with _client(monkeypatch, key=None, base_url=PredictFunClient.TESTNET_API_BASE) as client:
        with respx.mock:
            route = respx.get(f"{PredictFunClient.TESTNET_API_BASE}/v1/markets").mock(
                return_value=httpx.Response(200, json=payload)
            )
            markets = [market async for market in client.list_markets(closed=None, limit=1)]
    assert markets
    assert "x-api-key" not in route.calls[0].request.headers
    assert "status" not in route.calls[0].request.url.params


@pytest.mark.asyncio
async def test_unknown_market_matches_are_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty match page for an unknown id is NotFoundError, not an empty trade list."""
    missing = _load("error_not_found")
    async with _client(monkeypatch, key="test-key") as client:
        with respx.mock:
            respx.get(f"{PredictFunClient.API_BASE}/v1/orders/matches").mock(
                return_value=httpx.Response(200, json={"success": True, "cursor": None, "data": []})
            )
            respx.get(f"{PredictFunClient.API_BASE}/v1/markets/999999999").mock(
                return_value=httpx.Response(404, json=missing)
            )
            with pytest.raises(NotFoundError):
                _ = [trade async for trade in client.get_trades("999999999", limit=1)]


@pytest.mark.asyncio
async def test_sort_param_and_bad_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"success": True, "cursor": None, "data": []}
    async with _client(monkeypatch, key="test-key") as client:
        with respx.mock:
            route = respx.get(f"{PredictFunClient.API_BASE}/v1/markets").mock(
                return_value=httpx.Response(200, json=payload)
            )
            _ = [
                market
                async for market in client.list_markets(
                    limit=1, venue_params={"sort": "VOLUME_TOTAL_DESC", "tag_ids": ["1", "2"]}
                )
            ]
            assert route.calls[0].request.url.params["sort"] == "VOLUME_TOTAL_DESC"
            assert route.calls[0].request.url.params["tagIds"] == "1,2"
            with pytest.raises(ValueError, match="unsupported venue_params"):
                async for _ in client.list_markets(limit=1, venue_params={"status": "OPEN"}):
                    pass
            assert route.call_count == 1
        with pytest.raises(ValueError, match="outcome id"):
            await client.get_order_book("437:1")
        with pytest.raises(NotFoundError):
            await client.get_order_book("437:3:1")
        book_payload = {
            "success": True,
            "data": {
                "marketId": 1,
                "updateTimestampMs": 1793264357131,
                "bids": [[0.6, 1]],
                "asks": [[0.4, 1]],
            },
        }
        with respx.mock:
            respx.get(f"{PredictFunClient.API_BASE}/v1/markets/1/orderbook").mock(
                return_value=httpx.Response(200, json=book_payload)
            )
            with pytest.raises(ValueError, match="crossed"):
                await client.get_order_book("1")


@pytest.mark.asyncio
async def test_place_order_does_not_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _client(monkeypatch, key=None) as client:
        with respx.mock as transport:
            result = await client.place_order("437", OrderSide.BUY, Decimal("1"), Decimal("0.4"))
            assert result["dry_run"] is True
            assert transport.calls == []
            with pytest.raises(PermissionError, match="PREDICTFUN_API_KEY"):
                await client.place_order(
                    "437", OrderSide.BUY, Decimal("1"), Decimal("0.4"), dry_run=False
                )
            assert transport.calls == []
    async with _client(monkeypatch, key=SECRET) as client:
        assert SECRET not in repr(client)
        with respx.mock as transport:
            result = await client.place_order(
                "437", OrderSide.BUY, Decimal("1"), Decimal("0.4"), dry_run=False
            )
        assert result["status"] == "not_implemented"
        assert transport.calls == []
        assert SECRET not in json.dumps(result)
