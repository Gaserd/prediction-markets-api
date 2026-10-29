"""Unit tests for the read-only Opinion adapter. No live network calls."""

import json
import logging
from decimal import Decimal
from pathlib import Path

import pytest
import respx
from httpx import Response

from prediction_markets_api import OpinionClient
from prediction_markets_api.exceptions import (
    AuthenticationError,
    NoLiquidityError,
    NotFoundError,
    RateLimitError,
    VenueAPIError,
)
from prediction_markets_api.models.base import MarketStatus, OrderSide

FIXTURES = Path(__file__).parent / "fixtures" / "opinion"
BASE = "https://openapi.opinion.trade/openapi"


def _load(stem: str) -> dict:
    matches = sorted(
        path for path in FIXTURES.glob(f"{stem}_*.json") if not path.name.endswith(".meta.json")
    )
    assert matches, stem
    return json.loads(matches[0].read_text(encoding="utf-8"))


def _client(monkeypatch: pytest.MonkeyPatch) -> OpinionClient:
    monkeypatch.delenv("OPINION_API_KEY", raising=False)
    return OpinionClient()


@pytest.mark.asyncio
async def test_recorded_listing_uses_labels_and_leaves_price_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)
    payload = _load("markets_response")
    with respx.mock:
        respx.get(url__regex=r".*/openapi/market(\?.*)?$").mock(
            return_value=Response(200, json=payload)
        )
        markets = [market async for market in client.list_markets(closed=False, limit=2)]
    await client.close()
    assert len(markets) == 2
    market = markets[0]
    assert market.venue == "opinion"
    assert market.status == MarketStatus.OPEN
    assert market.resolved is False
    assert market.winning_outcome is None
    assert market.liquidity is None
    assert market.currency.value == "USD"
    assert len(market.outcomes) == 2
    assert {outcome.name for outcome in market.outcomes} == {
        market.raw_data["yesLabel"],
        market.raw_data["noLabel"],
    }
    assert all(outcome.price is None for outcome in market.outcomes)
    assert all(outcome.id.startswith(f"{market.id}:") for outcome in market.outcomes)


@pytest.mark.asyncio
async def test_order_book_complement_matches_recorded_no_book(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)
    detail = _load("market_response")
    yes_book = _load("orderbook_response")
    no_book = _load("complement_orderbook")
    raw = detail["result"]["data"]
    market_id = str(raw["marketId"])
    no_id = f"{market_id}:{raw['noTokenId']}"
    with respx.mock:
        market_route = respx.get(url__regex=r".*/openapi/market/\d+$").mock(
            return_value=Response(200, json=detail)
        )
        book_route = respx.get(url__regex=r".*/orderbook.*").mock(
            return_value=Response(200, json=yes_book)
        )
        book = await client.get_order_book(no_id)
        assert market_route.call_count == 1
        assert book_route.call_count == 1
        requested = book_route.calls[0].request.url.params["token_id"]
        assert requested == raw["yesTokenId"]
    await client.close()
    assert book.bids[0].price == Decimal("1") - Decimal(yes_book["result"]["asks"][0]["price"])
    recorded_asks = sorted(
        (Decimal(level["price"]) for level in no_book["result"]["asks"]),
    )
    assert [level.price for level in book.asks] == recorded_asks
    buy = None
    client = _client(monkeypatch)
    with respx.mock:
        respx.get(url__regex=r".*/openapi/market/\d+$").mock(
            return_value=Response(200, json=detail)
        )
        respx.get(url__regex=r".*/orderbook.*").mock(return_value=Response(200, json=yes_book))
        buy = await client.get_price(market_id, OrderSide.BUY)
        sell = await client.get_price(market_id, OrderSide.SELL)
    await client.close()
    assert buy.price == Decimal(yes_book["result"]["asks"][0]["price"])
    assert sell.price == Decimal(yes_book["result"]["bids"][0]["price"])


@pytest.mark.asyncio
async def test_unknown_outcome_is_rejected_before_the_book(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)
    detail = _load("market_response")
    raw = detail["result"]["data"]
    with respx.mock:
        respx.get(url__regex=r".*/openapi/market/\d+$").mock(
            return_value=Response(200, json=detail)
        )
        book_route = respx.get(url__regex=r".*/orderbook.*").mock(
            return_value=Response(200, json=_load("orderbook_response"))
        )
        with pytest.raises(NotFoundError):
            await client.get_order_book(f"{raw['marketId']}:not-a-token")
        assert book_route.call_count == 0
    await client.close()


@pytest.mark.asyncio
async def test_categorical_parent_has_child_outcomes_and_no_bare_book(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)
    missing = _load("error_not_found")
    detail = _load("categorical_detail")
    parent = detail["result"]["data"]
    parent_id = str(parent["marketId"])
    with respx.mock:
        respx.get(url__regex=rf".*/openapi/market/{parent_id}$").mock(
            return_value=Response(200, json=missing)
        )
        respx.get(url__regex=rf".*/openapi/market/categorical/{parent_id}$").mock(
            return_value=Response(200, json=detail)
        )
        market = await client.get_market(parent_id)
        with pytest.raises(NotFoundError, match="outcome id"):
            await client.get_order_book(parent_id)
    await client.close()
    assert market.id == parent_id
    # The categorical detail recorded 2026-10-29 reports Created, while the
    # list reports Activated for the same id. This client maps the payload
    # it actually received.
    assert market.status == MarketStatus.CLOSED
    assert market.venue_status == "Created"
    children = parent["childMarkets"]
    assert len(market.outcomes) == len(children) * 2
    first = children[0]
    assert market.outcomes[0].name == f"{first['marketTitle']}: {first['yesLabel']}"
    assert market.outcomes[0].id == f"{parent_id}:{first['yesTokenId']}"


@pytest.mark.asyncio
async def test_resolved_market_sets_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    payload = _load("resolved_market")
    with respx.mock:
        respx.get(url__regex=r".*/openapi/market(\?.*)?$").mock(
            return_value=Response(200, json=payload)
        )
        markets = [market async for market in client.list_markets(closed=True, limit=1)]
    await client.close()
    market = markets[0]
    assert market.status == MarketStatus.RESOLVED
    assert market.resolved is True
    assert market.winning_outcome is not None
    prices = {outcome.name: outcome.price for outcome in market.outcomes}
    assert prices[market.winning_outcome] == Decimal("1")
    assert sum(price == Decimal("1") for price in prices.values()) == 1


@pytest.mark.asyncio
async def test_trades_require_a_key_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    with respx.mock as router:
        with pytest.raises(AuthenticationError, match="OPINION_API_KEY") as caught:
            async for _ in client.get_trades("8453", limit=1):
                pass
        assert router.calls.call_count == 0
    await client.close()
    assert caught.value.hint is not None
    assert "OPINION_API_KEY" in caught.value.hint


@pytest.mark.asyncio
async def test_unknown_outcome_is_rejected_before_trades(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPINION_API_KEY", "test-key")
    client = OpinionClient()
    detail = _load("market_response")
    raw = detail["result"]["data"]
    with respx.mock:
        respx.get(url__regex=r".*/openapi/market/\d+$").mock(
            return_value=Response(200, json=detail)
        )
        trade_route = respx.get(url__regex=r".*/openapi/trade(\?.*)?$").mock(
            return_value=Response(200, json=_load("trades_response"))
        )
        with pytest.raises(NotFoundError):
            async for _ in client.get_trades(f"{raw['marketId']}:missing-token", limit=1):
                pass
        assert trade_route.call_count == 0
    await client.close()
    assert "test-key" not in repr(client)


@pytest.mark.asyncio
async def test_api_key_is_masked_and_sent_as_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPINION_API_KEY", "super-secret")
    client = OpinionClient()
    assert repr(client) == (
        "OpinionClient(base_url='https://openapi.opinion.trade/openapi', api_key='***')"
    )
    assert "super-secret" not in repr(client)
    payload = _load("markets_response")
    with respx.mock:
        route = respx.get(url__regex=r".*/openapi/market(\?.*)?$").mock(
            return_value=Response(200, json=payload)
        )
        _ = [market async for market in client.list_markets(limit=1)]
    await client.close()
    assert route.calls[0].request.headers["apikey"] == "super-secret"


@pytest.mark.asyncio
async def test_rejected_key_and_unknown_market(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPINION_API_KEY", "nope")
    client = OpinionClient()
    with respx.mock:
        respx.get(url__regex=r".*/openapi/market(\?.*)?$").mock(
            return_value=Response(401, json=_load("error_trade_unauthorized"))
        )
        with pytest.raises(AuthenticationError):
            async for _ in client.list_markets(limit=1):
                pass
        respx.get(url__regex=r".*/openapi/market/999999999$").mock(
            return_value=Response(200, json=_load("error_not_found"))
        )
        respx.get(url__regex=r".*/openapi/market/categorical/999999999$").mock(
            return_value=Response(200, json=_load("error_not_found"))
        )
        with pytest.raises(NotFoundError, match="999999999"):
            await client.get_market("999999999")
    await client.close()


@pytest.mark.asyncio
async def test_rate_limit_and_second_page_server_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    payload = _load("markets_response")
    with respx.mock:
        route = respx.get(url__regex=r".*/openapi/market(\?.*)?$").mock(
            return_value=Response(429, json={"errmsg": "slow down"}, headers={"Retry-After": "3"})
        )
        with pytest.raises(RateLimitError) as caught:
            async for _ in client.list_markets(limit=1):
                pass
        assert caught.value.retry_after == 3
        assert route.call_count == 1
    await client.close()

    client = _client(monkeypatch)
    calls = {"n": 0}

    def side_effect(request):
        calls["n"] += 1
        if request.url.params.get("page") == "2":
            return Response(503, json={"errmsg": "unavailable"})
        # A full page so the client asks for page 2.
        body = json.loads(json.dumps(payload))
        body["result"]["list"] = body["result"]["list"][:1] * 20
        for index, item in enumerate(body["result"]["list"]):
            item["marketId"] = 1000 + index
        return Response(200, json=body)

    with respx.mock:
        respx.get(url__regex=r".*/openapi/market(\?.*)?$").mock(side_effect=side_effect)
        with pytest.raises(VenueAPIError, match=r"page 2 at cursor 2") as caught:
            async for _ in client.list_markets(page_size=20):
                pass
    await client.close()
    assert caught.value.status_code == 503
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_page_size_is_clamped_and_bad_params_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    payload = _load("markets_response")
    with respx.mock:
        route = respx.get(url__regex=r".*/openapi/market(\?.*)?$").mock(
            return_value=Response(200, json=payload)
        )
        _ = [market async for market in client.list_markets(limit=50, page_size=100)]
        params = route.calls[0].request.url.params
        assert params["limit"] == "20"
        assert params["page"] == "1"
        assert params["marketType"] == "2"
        assert "sortBy" not in params
        route.calls.clear()
        _ = [
            market
            async for market in client.list_markets(
                closed=None,
                limit=1,
                venue_params={"market_type": 0, "sort_by": 5, "collection_type": 2},
            )
        ]
        params = route.calls[0].request.url.params
        assert params["marketType"] == "0"
        assert params["sortBy"] == "5"
        assert params["collectionType"] == "2"
        assert "status" not in params
    await client.close()
    client = _client(monkeypatch)
    with pytest.raises(ValueError, match="page_size"):
        async for _ in client.list_markets(page_size=0):
            pass
    with pytest.raises(ValueError, match="unsupported venue_params"):
        async for _ in client.list_markets(venue_params={"label_id": 1}):
            pass
    await client.close()


@pytest.mark.asyncio
async def test_unknown_status_is_not_mapped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = _client(monkeypatch)
    payload = json.loads(json.dumps(_load("markets_response")))
    raw = payload["result"]["list"][0]
    raw["status"] = 99
    raw["statusEnum"] = "Mystery"
    payload["result"]["list"] = [raw]
    with respx.mock:
        respx.get(url__regex=r".*/openapi/market(\?.*)?$").mock(
            return_value=Response(200, json=payload)
        )
        with caplog.at_level(logging.WARNING):
            markets = [market async for market in client.list_markets(closed=None, limit=1)]
    await client.close()
    assert markets[0].status is None
    assert markets[0].resolved is False
    assert markets[0].venue_status == "Mystery"
    assert "Unknown Opinion market status" in caplog.text


@pytest.mark.asyncio
async def test_resolving_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    payload = json.loads(json.dumps(_load("markets_response")))
    raw = payload["result"]["list"][0]
    raw["status"] = 3
    raw["statusEnum"] = "Resolving"
    payload["result"]["list"] = [raw]
    with respx.mock:
        respx.get(url__regex=r".*/openapi/market(\?.*)?$").mock(
            return_value=Response(200, json=payload)
        )
        open_markets = [market async for market in client.list_markets(closed=False, limit=5)]
        all_markets = [market async for market in client.list_markets(closed=None, limit=5)]
    await client.close()
    assert open_markets == []
    assert all_markets[0].status == MarketStatus.CLOSED
    assert all_markets[0].venue_status == "Resolving"
    assert all_markets[0].resolved is False


@pytest.mark.asyncio
async def test_empty_side_has_no_price(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    detail = _load("market_response")
    raw = detail["result"]["data"]
    empty = {
        "errno": 0,
        "errmsg": "",
        "result": {
            "bids": [{"price": "0.4", "size": "2"}],
            "asks": [],
            "timestamp": 1700000000000,
            "tokenId": raw["yesTokenId"],
        },
    }
    with respx.mock:
        respx.get(url__regex=r".*/openapi/market/\d+$").mock(
            return_value=Response(200, json=detail)
        )
        respx.get(url__regex=r".*/orderbook.*").mock(return_value=Response(200, json=empty))
        with pytest.raises(NoLiquidityError):
            await client.get_price(str(raw["marketId"]), OrderSide.BUY)
        sell = await client.get_price(str(raw["marketId"]), OrderSide.SELL)
    await client.close()
    assert sell.price == Decimal("0.4")


@pytest.mark.asyncio
async def test_user_trades_filter_outcome_side(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPINION_API_KEY", "test-key")
    client = OpinionClient()
    detail = _load("market_response")
    raw = detail["result"]["data"]
    market_id = str(raw["marketId"])
    body = {
        "errno": 0,
        "errmsg": "",
        "result": {
            "list": [
                {
                    "txHash": "0x1",
                    "marketId": int(market_id),
                    "side": "Buy",
                    "outcomeSide": 1,
                    "price": "0.42",
                    "shares": "3",
                    "status": 2,
                    "createdAt": 1700000000,
                },
                {
                    "txHash": "0x2",
                    "marketId": int(market_id),
                    "side": "SELL",
                    "outcomeSide": 2,
                    "price": "0.58",
                    "shares": "4",
                    "status": 1,
                    "createdAt": 1700000001,
                },
                {
                    "txHash": "0x3",
                    "marketId": int(market_id),
                    "side": "SELL",
                    "outcomeSide": 2,
                    "price": "0.58",
                    "shares": "5",
                    "status": "Filled",
                    "createdAt": 1700000002000,
                },
            ]
        },
    }
    with respx.mock:
        respx.get(url__regex=r".*/openapi/market/\d+$").mock(
            return_value=Response(200, json=detail)
        )
        route = respx.get(url__regex=r".*/openapi/trade(\?.*)?$").mock(
            return_value=Response(200, json=body)
        )
        trades = [
            trade async for trade in client.get_trades(f"{market_id}:{raw['noTokenId']}", limit=10)
        ]
    await client.close()
    assert [trade.id for trade in trades] == ["0x3"]
    assert trades[0].side == OrderSide.SELL
    assert trades[0].size == Decimal("5")
    assert route.calls[0].request.url.params["chainId"] == "56"
    assert route.calls[0].request.url.params["marketId"] == market_id


@pytest.mark.asyncio
async def test_place_order_does_not_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    with respx.mock as router:
        result = await client.place_order("1", OrderSide.BUY, Decimal("1"), Decimal("0.5"))
        assert result["dry_run"] is True
        assert router.calls.call_count == 0
        with pytest.raises(PermissionError, match="OPINION_API_KEY"):
            await client.place_order(
                "1", OrderSide.BUY, Decimal("1"), Decimal("0.5"), dry_run=False
            )
        assert router.calls.call_count == 0
    await client.close()


@pytest.mark.asyncio
async def test_repeated_page_stops(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = _client(monkeypatch)
    payload = _load("markets_response")
    body = json.loads(json.dumps(payload))
    body["result"]["list"] = body["result"]["list"][:1] * 20
    for index, item in enumerate(body["result"]["list"]):
        item["marketId"] = 50
        item["yesTokenId"] = f"yes-{index}"
    with respx.mock:
        respx.get(url__regex=r".*/openapi/market(\?.*)?$").mock(
            return_value=Response(200, json=body)
        )
        with caplog.at_level(logging.WARNING):
            markets = [market async for market in client.list_markets(page_size=20)]
    await client.close()
    assert len(markets) == 1
    assert "page 2 repeated" in caplog.text
