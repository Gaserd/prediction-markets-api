"""Tests for the Hyperliquid Outcomes adapter using recorded API fixtures."""

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from prediction_markets_api.adapters.hyperliquid import HyperliquidClient
from prediction_markets_api.exceptions import (
    NoLiquidityError,
    NotFoundError,
    RateLimitError,
    VenueAPIError,
)
from prediction_markets_api.models.base import Currency, MarketStatus, OrderSide

FIXTURES = Path(__file__).parent / "fixtures" / "hyperliquid"
INFO_URL = f"{HyperliquidClient.API_BASE}/info"


def _load(name: str) -> object:
    with open(FIXTURES / name) as handle:
        return json.load(handle)


@pytest.fixture
def markets_response() -> dict:
    data = _load("markets_response.json")
    assert isinstance(data, dict)
    return data


@pytest.fixture
def templates_response() -> list:
    data = _load("outcome_templates.json")
    assert isinstance(data, list)
    return data


@pytest.fixture
def mids_response() -> dict:
    data = _load("mids_response.json")
    assert isinstance(data, dict)
    return data


@pytest.fixture
def orderbook_response() -> dict:
    data = _load("orderbook_response.json")
    assert isinstance(data, dict)
    return data


@pytest.fixture
def orderbook_meta() -> dict:
    data = _load("orderbook_response.meta.json")
    assert isinstance(data, dict)
    return data


@pytest.fixture
def trades_response() -> list:
    data = _load("trades_response.json")
    assert isinstance(data, list)
    return data


@pytest.fixture
def trades_meta() -> dict:
    data = _load("trades_response.meta.json")
    assert isinstance(data, dict)
    return data


def _route(request: httpx.Request, routes: dict[str, object]) -> httpx.Response:
    body = json.loads(request.content)
    info_type = body["type"]
    if info_type not in routes:
        return httpx.Response(500, json={"error": f"unexpected {info_type}"})
    payload = routes[info_type]
    if payload is None:
        # httpx treats json=None as an empty body, not JSON null.
        return httpx.Response(200, content=b"null", headers={"content-type": "application/json"})
    return httpx.Response(200, json=payload)


@pytest.mark.asyncio
async def test_list_and_get_market(markets_response, templates_response, mids_response, respx_mock):
    """Listing uses one outcomeMeta call and renders template plus recurring names."""

    def side_effect(request: httpx.Request) -> httpx.Response:
        return _route(
            request,
            {
                "outcomeMeta": markets_response,
                "outcomeTemplates": templates_response,
                "allMids": mids_response,
            },
        )

    route = respx_mock.post(INFO_URL).mock(side_effect=side_effect)

    async with HyperliquidClient() as client:
        markets = [market async for market in client.list_markets(limit=None)]
        listing_calls = list(route.calls)
        btc_id = next(
            market.id
            for market in markets
            if str(market.raw_data.get("description", "")).startswith("class:priceBinary")
            and "underlying:BTC|" in str(market.raw_data.get("description", ""))
        )
        fetched = await client.get_market(btc_id)

    assert fetched.id == btc_id
    assert route.called
    bodies = [json.loads(call.request.content) for call in listing_calls]
    assert sum(1 for body in bodies if body["type"] == "outcomeMeta") == 1
    assert {market.venue for market in markets} == {"hyperliquid"}
    assert len({market.id for market in markets}) == len(markets)

    btc = next(
        market
        for market in markets
        if str(market.raw_data.get("description", "")).startswith("class:priceBinary")
        and "underlying:BTC|" in str(market.raw_data.get("description", ""))
    )
    assert btc.question.startswith("BTC above ")
    assert [outcome.name for outcome in btc.outcomes] == ["Yes", "No"]
    assert btc.currency == Currency.USDC
    assert btc.liquidity is None
    assert btc.outcomes[0].price == Decimal(mids_response[btc.outcomes[0].id])
    assert btc.outcomes[0].raw_price == mids_response[btc.outcomes[0].id]
    assert btc.status == MarketStatus.OPEN
    assert btc.resolved is False

    bucket = next(
        market
        for market in markets
        if str(market.raw_data.get("description", "")).startswith("class:priceBucket")
    )
    assert bucket.id.startswith("q")
    assert bucket.question.startswith("BTC price bucket at ")
    thresholds = dict(
        part.split(":", 1) for part in str(bucket.description).split("|") if ":" in part
    )
    lower, upper = thresholds["priceThresholds"].split(",")
    assert [outcome.name for outcome in bucket.outcomes[:3]] == [
        f"Below {lower}",
        f"{lower} to {upper}",
        f"{upper} or above",
    ]
    assert bucket.outcomes[-1].name == "Other"

    contest = next(market for market in markets if "SC Freiburg" in market.question)
    assert "FC Bayern Munich" in contest.question
    assert all(outcome.name for outcome in contest.outcomes)
    assert any(outcome.name == "Napoli" for market in markets for outcome in market.outcomes)
    assert fetched.question == btc.question


@pytest.mark.asyncio
async def test_closed_true_makes_no_request(respx_mock):
    route = respx_mock.post(INFO_URL).mock(return_value=httpx.Response(200, json={}))
    async with HyperliquidClient() as client:
        with pytest.raises(
            NotImplementedError, match="does not provide an endpoint for listing closed"
        ):
            async for _ in client.list_markets(closed=True, limit=1):
                pass
    assert not route.called


@pytest.mark.asyncio
async def test_limit_zero_makes_no_request(respx_mock):
    route = respx_mock.post(INFO_URL).mock(return_value=httpx.Response(200, json={}))
    async with HyperliquidClient() as client:
        with pytest.raises(ValueError, match="limit"):
            async for _ in client.list_markets(limit=0):
                pass
        with pytest.raises(ValueError, match="limit"):
            async for _ in client.get_trades("#1", limit=-1):
                pass
    assert not route.called


@pytest.mark.asyncio
async def test_venue_filter(markets_response, templates_response, mids_response, respx_mock):
    def side_effect(request: httpx.Request) -> httpx.Response:
        return _route(
            request,
            {
                "outcomeMeta": markets_response,
                "outcomeTemplates": templates_response,
                "allMids": mids_response,
            },
        )

    respx_mock.post(INFO_URL).mock(side_effect=side_effect)
    async with HyperliquidClient() as client:
        all_markets = [market async for market in client.list_markets(limit=None)]
        filtered = [
            market
            async for market in client.list_markets(limit=None, venue_params={"venue": "out"})
        ]
    assert filtered
    assert len(filtered) < len(all_markets)
    assert all(market.venue == "hyperliquid" for market in filtered)


@pytest.mark.asyncio
async def test_order_book_and_price(orderbook_response, orderbook_meta, respx_mock):
    coin = orderbook_meta["token_id"]
    respx_mock.post(INFO_URL).mock(return_value=httpx.Response(200, json=orderbook_response))

    async with HyperliquidClient() as client:
        book = await client.get_order_book(coin)
        buy = await client.get_price(coin, OrderSide.BUY)
        sell = await client.get_price(coin, OrderSide.SELL)

    assert book.outcome_id == coin
    assert book.currency == Currency.USDC
    assert book.timestamp.tzinfo is not None
    assert book.bids and book.asks
    assert book.bids[0].price >= book.bids[-1].price
    assert book.asks[0].price <= book.asks[-1].price
    assert buy.price == book.asks[0].price
    assert sell.price == book.bids[0].price
    assert buy.raw_price == book.asks[0].raw_price
    request = json.loads(respx_mock.calls[0].request.content)
    assert request == {"type": "l2Book", "coin": coin}


@pytest.mark.asyncio
async def test_trades(trades_response, trades_meta, respx_mock):
    coin = trades_meta["token_id"]
    respx_mock.post(INFO_URL).mock(return_value=httpx.Response(200, json=trades_response))

    async with HyperliquidClient() as client:
        trades = [trade async for trade in client.get_trades(coin, limit=5)]

    assert len(trades) == min(5, len(trades_response))
    assert trades[0].id == str(trades_response[0]["tid"])
    assert trades[0].currency == Currency.USDC
    assert trades[0].timestamp.tzinfo is not None
    assert trades[0].side == (
        OrderSide.BUY if trades_response[0]["side"] == "B" else OrderSide.SELL
    )
    body = json.loads(respx_mock.calls[0].request.content)
    assert body == {"type": "recentTrades", "coin": coin}


@pytest.mark.asyncio
async def test_settled_outcome(markets_response, templates_response, respx_mock):
    settled = _load("settled_outcome.json")
    assert isinstance(settled, dict)

    def side_effect(request: httpx.Request) -> httpx.Response:
        return _route(
            request,
            {
                "outcomeMeta": markets_response,
                "outcomeTemplates": templates_response,
                "settledOutcome": settled,
            },
        )

    respx_mock.post(INFO_URL).mock(side_effect=side_effect)
    async with HyperliquidClient() as client:
        market = await client.get_market("95")

    assert market.resolved is True
    assert market.status == MarketStatus.RESOLVED
    assert market.venue_status == "settled"
    assert market.winning_outcome == "No"
    assert market.outcomes[0].price == Decimal("0")
    assert market.outcomes[1].price == Decimal("1")


@pytest.mark.asyncio
async def test_unknown_market(markets_response, templates_response, respx_mock):
    def side_effect(request: httpx.Request) -> httpx.Response:
        return _route(
            request,
            {
                "outcomeMeta": markets_response,
                "outcomeTemplates": templates_response,
                "settledOutcome": None,
            },
        )

    respx_mock.post(INFO_URL).mock(side_effect=side_effect)
    async with HyperliquidClient() as client:
        with pytest.raises(NotFoundError):
            await client.get_market("99999999")


@pytest.mark.asyncio
async def test_empty_book_is_no_liquidity(
    markets_response, templates_response, mids_response, respx_mock
):
    empty_path = FIXTURES / "empty_orderbook.json"
    if not empty_path.exists():
        pytest.skip("no empty order book was recorded")
    empty = _load("empty_orderbook.json")
    meta = _load("empty_orderbook.meta.json")
    assert isinstance(meta, dict)
    coin = meta["token_id"]

    def side_effect(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["type"] == "l2Book":
            return httpx.Response(200, json=empty)
        return _route(
            request,
            {
                "outcomeMeta": markets_response,
                "outcomeTemplates": templates_response,
                "allMids": mids_response,
                "settledOutcome": None,
            },
        )

    respx_mock.post(INFO_URL).mock(side_effect=side_effect)
    async with HyperliquidClient() as client:
        book = await client.get_order_book(coin)
        assert book.bids == []
        assert book.asks == []
        with pytest.raises(NoLiquidityError):
            await client.get_price(coin, OrderSide.BUY)


@pytest.mark.asyncio
async def test_missing_book_is_not_found(respx_mock):
    respx_mock.post(INFO_URL).mock(
        return_value=httpx.Response(
            200, content=b"null", headers={"content-type": "application/json"}
        )
    )
    async with HyperliquidClient() as client:
        with pytest.raises(NotFoundError):
            await client.get_order_book("#999999999")


@pytest.mark.asyncio
async def test_rate_limit_and_server_error(respx_mock):
    respx_mock.post(INFO_URL).mock(return_value=httpx.Response(429, json={"error": "slow down"}))
    async with HyperliquidClient() as client:
        with pytest.raises(RateLimitError):
            async for _ in client.list_markets(limit=1):
                pass

    respx_mock.post(INFO_URL).mock(return_value=httpx.Response(503, json={"error": "down"}))
    async with HyperliquidClient() as client:
        with pytest.raises(VenueAPIError):
            async for _ in client.list_markets(limit=1):
                pass


@pytest.mark.asyncio
async def test_question_id_has_no_book():
    async with HyperliquidClient() as client:
        with pytest.raises(ValueError, match="question id"):
            await client.get_order_book("q505")


@pytest.mark.asyncio
async def test_expired_listing_is_closed_but_not_resolved(respx_mock):
    """An outcome still listed after expiry is closed, not resolved."""
    payload = {
        "outcomes": [
            {
                "outcome": 1,
                "name": "Recurring",
                "description": "class:priceBinary|underlying:BTC|expiry:20200101-0000|targetPrice:1|period:1d",
                "sideSpecs": [{"name": "Yes"}, {"name": "No"}],
                "quoteToken": "USDC",
            }
        ],
        "questions": [],
    }
    respx_mock.post(INFO_URL).mock(
        side_effect=lambda request: _route(
            request,
            {"outcomeMeta": payload, "outcomeTemplates": [], "allMids": {}},
        )
    )
    async with HyperliquidClient() as client:
        closed = [market async for market in client.list_markets(closed=None)]
        opened = [market async for market in client.list_markets(closed=False)]

    assert len(closed) == 1
    assert closed[0].status == MarketStatus.CLOSED
    assert closed[0].resolved is False
    assert closed[0].winning_outcome is None
    assert opened == []


@pytest.mark.asyncio
async def test_place_order_dry_run_and_max_size():
    async with HyperliquidClient(max_order_size=Decimal("5")) as client:
        preview = await client.place_order(
            outcome_id="#10",
            side=OrderSide.BUY,
            size=Decimal("1"),
            price=Decimal("0.4"),
        )
        assert preview["dry_run"] is True
        with pytest.raises(RuntimeError, match="exceeds max_order_size"):
            await client.place_order(
                outcome_id="#10",
                side=OrderSide.BUY,
                size=Decimal("6"),
                price=Decimal("0.4"),
                dry_run=False,
            )
