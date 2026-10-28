"""Tests for the Hyperliquid Outcomes adapter using recorded API fixtures."""

import asyncio
import json
import logging
import warnings
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from prediction_markets_api.adapters.hyperliquid import HyperliquidClient, coin_from_outcome_id
from prediction_markets_api.exceptions import (
    NoLiquidityError,
    NotFoundError,
    RateLimitError,
    VenueAPIError,
)
from prediction_markets_api.models.base import Currency, MarketStatus, OrderSide

FIXTURES = Path(__file__).parent / "fixtures" / "hyperliquid"
INFO_URL = f"{HyperliquidClient.API_BASE}/info"
# markets_response.meta.json recorded_at. The BTC daily in that snapshot
# expires 2026-10-24 06:00 UTC, so a later wall clock marks it closed.
_RECORDED_AT = datetime(2026, 10, 23, 9, 26, 7, tzinfo=timezone.utc)


class _FrozenDateTime(datetime):
    """datetime stand-in whose now() is the fixture snapshot."""

    @classmethod
    def now(cls, tz: timezone | None = None) -> datetime:  # type: ignore[override]
        if tz is None:
            return _RECORDED_AT.replace(tzinfo=None)
        return _RECORDED_AT.astimezone(tz)


@pytest.fixture(autouse=True)
def _freeze_hyperliquid_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep listing status stable against the recorded snapshot."""
    monkeypatch.setattr(
        "prediction_markets_api.adapters.hyperliquid.datetime",
        _FrozenDateTime,
    )


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
    if info_type == "l2Book":
        # Only coins the test passed in routes["l2Book"] succeed. Anything else
        # is a 500 so a swallowed book error cannot look like an empty book.
        books = routes.get("l2Book")
        coin = body.get("coin")
        if isinstance(books, dict) and isinstance(coin, str) and coin in books:
            return httpx.Response(200, json=books[coin])
        return httpx.Response(500, json={"error": f"unexpected l2Book {coin}"})
    if info_type not in routes:
        return httpx.Response(500, json={"error": f"unexpected {info_type}"})
    payload = routes[info_type]
    if payload is None:
        # httpx treats json=None as an empty body, not JSON null.
        return httpx.Response(200, content=b"null", headers={"content-type": "application/json"})
    return httpx.Response(200, json=payload)


def _empty_l2_books(markets_response: dict) -> dict[str, dict]:
    """Recorded empty book, with the coin rewritten for every question fallback."""
    recorded = _load("empty_orderbook.json")
    assert isinstance(recorded, dict)
    books: dict[str, dict] = {}
    for question in markets_response["questions"]:
        fallback = question.get("fallbackOutcome")
        if not isinstance(fallback, int):
            continue
        coin = f"#{10 * fallback}"
        payload = dict(recorded)
        payload["coin"] = coin
        books[coin] = payload
    return books


def _listing_routes(
    markets_response: dict, templates_response: list, mids_response: dict
) -> dict[str, object]:
    return {
        "outcomeMeta": markets_response,
        "outcomeTemplates": templates_response,
        "allMids": mids_response,
        "l2Book": _empty_l2_books(markets_response),
    }


@pytest.mark.asyncio
async def test_list_and_get_market(markets_response, templates_response, mids_response, respx_mock):
    """Listing uses one outcomeMeta call and renders template plus recurring names."""

    routes = _listing_routes(markets_response, templates_response, mids_response)

    def side_effect(request: httpx.Request) -> httpx.Response:
        return _route(request, routes)

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
    books = routes["l2Book"]
    assert isinstance(books, dict)
    l2_coins = [body["coin"] for body in bodies if body["type"] == "l2Book"]
    assert set(l2_coins) <= set(books)


@pytest.mark.asyncio
async def test_ballon_dor_excludes_untradable_other_fallback(
    markets_response, templates_response, mids_response, respx_mock
):
    """8658 is the tradable Other. Fallback 8656 is a 0.5 mid with no book.

    Listing both as Other pushes the probability sum to about 1.5. The recorded
    Ballon d'Or question keeps 8658 and drops 8656. Contest and price-bucket
    fallbacks are the only Other leg and stay.
    """

    routes = _listing_routes(markets_response, templates_response, mids_response)

    def side_effect(request: httpx.Request) -> httpx.Response:
        return _route(request, routes)

    route = respx_mock.post(INFO_URL).mock(side_effect=side_effect)
    async with HyperliquidClient() as client:
        markets = [market async for market in client.list_markets(limit=None)]
        award = next(market for market in markets if "Ballon d'Or" in market.question)
        fetched = await client.get_market(award.id)
        contest = next(market for market in markets if market.id == "q503")

    books = routes["l2Book"]
    assert isinstance(books, dict)
    l2_coins = [
        body["coin"] for body in _info_bodies(list(route.calls)) if body["type"] == "l2Book"
    ]
    assert set(l2_coins) <= set(books)

    others = [outcome for outcome in award.outcomes if outcome.name == "Other"]
    assert [outcome.id for outcome in others] == ["#86580"]
    assert all(outcome.id != "#86560" for outcome in award.outcomes)
    assert [outcome.id for outcome in fetched.outcomes] == [
        outcome.id for outcome in award.outcomes
    ]
    listed_sum = sum(
        (outcome.price for outcome in award.outcomes if outcome.price is not None),
        Decimal("0"),
    )
    fallback_mid = Decimal(mids_response["#86560"])
    assert fallback_mid == Decimal("0.5")
    assert others[0].price == Decimal(mids_response["#86580"])
    assert listed_sum < Decimal("1.2")
    assert listed_sum + fallback_mid > Decimal("1.4")
    # q503's only Other is its fallback, so that leg stays.
    assert [outcome.name for outcome in contest.outcomes].count("Other") == 1
    assert contest.outcomes[-1].id == "#156640"


def _info_bodies(calls: list) -> list[dict]:
    return [json.loads(call.request.content) for call in calls]


@pytest.mark.asyncio
async def test_empty_fallback_book_price_is_none(
    markets_response, templates_response, mids_response, respx_mock
):
    """A recorded empty fallback book is priced None; a quoted leg keeps its mid.

    ``empty_orderbook.json`` is the l2Book for fallback outcome 15291
    (question 490, coin ``#152910``). Other fallback books in this snapshot
    were empty too; the test reuses those recorded levels and only rewrites
    the coin. Outcome 8656 is the Ballon d'Or fallback already omitted from
    its question.
    """
    empty = _load("empty_orderbook.json")
    empty_meta = _load("empty_orderbook.meta.json")
    assert isinstance(empty, dict)
    assert isinstance(empty_meta, dict)
    recorded_coin = empty_meta["token_id"]
    assert recorded_coin == "#152910"
    assert mids_response[recorded_coin] == "0.5"

    def side_effect(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("type") == "l2Book":
            payload = dict(empty)
            payload["coin"] = body.get("coin")
            return httpx.Response(200, json=payload)
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
        listing_bodies = _info_bodies(list(route.calls))
        question = await client.get_market("q490")
        direct = await client.get_market("15291")
        omitted = await client.get_market("8656")

    btc = next(
        market
        for market in markets
        if str(market.raw_data.get("description", "")).startswith("class:priceBinary")
        and "underlying:BTC|" in str(market.raw_data.get("description", ""))
    )
    assert btc.outcomes[0].price == Decimal(mids_response[btc.outcomes[0].id])
    assert btc.outcomes[0].raw_price == mids_response[btc.outcomes[0].id]

    fallback_coins = {
        f"#{10 * question_row['fallbackOutcome']}" for question_row in markets_response["questions"]
    }
    kept_half = [
        outcome
        for market in markets
        for outcome in market.outcomes
        if outcome.id not in fallback_coins and outcome.price == Decimal("0.5")
    ]
    assert kept_half, "a non-fallback mid of 0.5 must stay priced"

    none_outcomes = [
        outcome for market in markets for outcome in market.outcomes if outcome.price is None
    ]
    assert len(none_outcomes) == 25
    assert {outcome.id for outcome in none_outcomes} == fallback_coins - {"#86560"}
    assert all(outcome.raw_price is None for outcome in none_outcomes)
    questions_with_none = [
        market for market in markets if any(outcome.price is None for outcome in market.outcomes)
    ]
    assert len(questions_with_none) == 25

    leg = next(outcome for outcome in question.outcomes if outcome.id == recorded_coin)
    assert leg.price is None
    assert leg.raw_price is None
    priced_sum = sum(
        (outcome.price for outcome in question.outcomes if outcome.price is not None),
        Decimal("0"),
    )
    assert priced_sum <= Decimal("1.02")
    assert all(
        outcome.price is not None for outcome in question.outcomes if outcome.id != recorded_coin
    )
    assert [outcome.price for outcome in direct.outcomes] == [None, None]
    assert [outcome.raw_price for outcome in direct.outcomes] == [None, None]
    assert [outcome.price for outcome in omitted.outcomes] == [None, None]

    l2_coins = [body["coin"] for body in listing_bodies if body["type"] == "l2Book"]
    assert set(l2_coins) == fallback_coins - {"#86560"}
    assert len(l2_coins) == 25
    assert recorded_coin in l2_coins
    assert "#86560" not in l2_coins
    assert btc.outcomes[0].id not in l2_coins
    assert sum(1 for body in listing_bodies if body["type"] == "outcomeMeta") == 1


@pytest.mark.asyncio
async def test_fallback_with_book_keeps_allmids_mid(
    markets_response, templates_response, mids_response, orderbook_response, respx_mock
):
    """Levels on the fallback book keep the allMids mid. The book is not the price."""

    def side_effect(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("type") == "l2Book" and body.get("coin") == "#152910":
            return httpx.Response(200, json=orderbook_response)
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
        market = await client.get_market("q490")

    l2_coins = [
        body["coin"] for body in _info_bodies(list(route.calls)) if body["type"] == "l2Book"
    ]
    assert l2_coins == ["#152910"]
    fallback = next(outcome for outcome in market.outcomes if outcome.id == "#152910")
    assert fallback.price == Decimal(mids_response["#152910"])
    assert fallback.raw_price == mids_response["#152910"]
    assert orderbook_response["levels"][0], "fixture book must have bids"


@pytest.mark.asyncio
async def test_limit_checks_one_fallback_book(
    markets_response, templates_response, mids_response, respx_mock
):
    """limit=1 stops after the first question, so only that fallback book is fetched."""

    routes = _listing_routes(markets_response, templates_response, mids_response)

    def side_effect(request: httpx.Request) -> httpx.Response:
        return _route(request, routes)

    route = respx_mock.post(INFO_URL).mock(side_effect=side_effect)
    async with HyperliquidClient() as client:
        markets = [market async for market in client.list_markets(limit=1)]

    assert len(markets) == 1
    bodies = _info_bodies(list(route.calls))
    l2 = [body for body in bodies if body["type"] == "l2Book"]
    assert len(l2) == 1
    first_fallback = markets_response["questions"][0]["fallbackOutcome"]
    assert l2[0]["coin"] == f"#{10 * first_fallback}"
    books = routes["l2Book"]
    assert isinstance(books, dict)
    assert {body["coin"] for body in l2} <= set(books)


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
    routes = _listing_routes(markets_response, templates_response, mids_response)

    def side_effect(request: httpx.Request) -> httpx.Response:
        return _route(request, routes)

    route = respx_mock.post(INFO_URL).mock(side_effect=side_effect)
    async with HyperliquidClient() as client:
        all_markets = [market async for market in client.list_markets(limit=None)]
        filtered = [
            market
            async for market in client.list_markets(limit=None, venue_params={"venue": "out"})
        ]
    books = routes["l2Book"]
    assert isinstance(books, dict)
    l2_coins = [
        body["coin"] for body in _info_bodies(list(route.calls)) if body["type"] == "l2Book"
    ]
    assert set(l2_coins) <= set(books)
    assert filtered
    assert len(filtered) < len(all_markets)
    assert all(market.venue == "hyperliquid" for market in filtered)


def test_side_1_coin_keeps_side_digit():
    """Side 1 is ``10 * outcome + 1``, not ``10 * outcome``."""
    assert coin_from_outcome_id("16049:1") == "#160491"
    assert coin_from_outcome_id("16049") == "#160490"
    assert coin_from_outcome_id("16049:0") == "#160490"


@pytest.mark.asyncio
async def test_order_book_side_1_requests_sided_coin(respx_mock):
    """A book lookup for side 1 must post ``#160491``, not ``#160490``."""
    book = {
        "coin": "#160491",
        "time": 1760000000000,
        "levels": [
            [{"px": "0.41", "sz": "3", "n": 1}],
            [{"px": "0.59", "sz": "4", "n": 1}],
        ],
    }
    respx_mock.post(INFO_URL).mock(return_value=httpx.Response(200, json=book))

    async with HyperliquidClient() as client:
        result = await client.get_order_book("16049:1")

    assert result.outcome_id == "16049:1"
    assert result.bids[0].price == Decimal("0.41")
    body = json.loads(respx_mock.calls[0].request.content)
    assert body == {"type": "l2Book", "coin": "#160491"}


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


def _outcome(market, coin: str):
    return next(item for item in market.outcomes if item.id == coin)


def _question_leg(markets: list, question_id: str, coin: str):
    market = next(item for item in markets if item.id == question_id)
    return _outcome(market, coin)


def _l2_error(kind: str) -> httpx.Response:
    if kind == "429":
        return httpx.Response(429, json={"error": "slow down"})
    if kind == "500":
        return httpx.Response(500, json={"error": "down"})
    raise AssertionError(kind)


@pytest.mark.asyncio
async def test_fallback_book_is_reread_each_call(
    markets_response, templates_response, mids_response, orderbook_response, respx_mock
):
    """A later book is not stuck on the first check, and an emptied book is not stuck on the mid.

    One client is reused. Empty then live must publish the mid, and live then
    empty must publish None. Each transition issues another l2Book.
    """
    books = _empty_l2_books(markets_response)
    live = dict(orderbook_response)
    live["coin"] = "#152910"
    mode = {"live": False}
    seen: list[str] = []

    def side_effect(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("type") == "l2Book":
            coin = body.get("coin")
            if isinstance(coin, str):
                seen.append(coin)
            if coin == "#152910" and mode["live"]:
                return httpx.Response(200, json=live)
            if isinstance(coin, str) and coin in books:
                return httpx.Response(200, json=books[coin])
            return httpx.Response(500, json={"error": f"unexpected l2Book {coin}"})
        return _route(
            request,
            {
                "outcomeMeta": markets_response,
                "outcomeTemplates": templates_response,
                "allMids": mids_response,
            },
        )

    respx_mock.post(INFO_URL).mock(side_effect=side_effect)
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        async with HyperliquidClient() as client:
            mode["live"] = False
            empty_get = await client.get_market("q490")
            assert seen.count("#152910") == 1
            mode["live"] = True
            live_get = await client.get_market("q490")
            assert seen.count("#152910") == 2

            still_live = await client.get_market("q490")
            assert seen.count("#152910") == 3
            mode["live"] = False
            emptied = await client.get_market("q490")
            assert seen.count("#152910") == 4

            listed_empty = [market async for market in client.list_markets()]
            assert seen.count("#152910") == 5
            mode["live"] = True
            listed_live = [market async for market in client.list_markets()]
            assert seen.count("#152910") == 6

    mid = Decimal(mids_response["#152910"])
    assert _outcome(empty_get, "#152910").price is None
    assert _outcome(empty_get, "#152910").raw_price is None
    assert _outcome(live_get, "#152910").price == mid
    assert _outcome(live_get, "#152910").raw_price == mids_response["#152910"]
    assert _outcome(still_live, "#152910").price == mid
    assert _outcome(emptied, "#152910").price is None
    assert _outcome(emptied, "#152910").raw_price is None
    assert _question_leg(listed_empty, "q490", "#152910").price is None
    assert _question_leg(listed_live, "q490", "#152910").price == mid
    assert [market.id for market in listed_empty] == [market.id for market in listed_live]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["429", "500", "timeout"])
async def test_list_markets_continues_when_l2book_fails(
    markets_response, templates_response, mids_response, respx_mock, kind, caplog
):
    """One bad fallback book does not drop the rest of the listing."""
    books = _empty_l2_books(markets_response)
    failed = {
        f"#{10 * markets_response['questions'][0]['fallbackOutcome']}",
        "#152910",
    }
    assert failed <= set(books)
    mode = {"fail": False}

    def side_effect(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("type") == "l2Book":
            coin = body.get("coin")
            if mode["fail"] and coin in failed:
                if kind == "timeout":
                    raise httpx.TimeoutException("timed out")
                return _l2_error(kind)
            if isinstance(coin, str) and coin in books:
                return httpx.Response(200, json=books[coin])
            return httpx.Response(500, json={"error": f"unexpected l2Book {coin}"})
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
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            baseline = [market async for market in client.list_markets()]
        mode["fail"] = True
        caplog.set_level(logging.WARNING)
        with pytest.warns(UserWarning, match="2 fallback legs") as caught:
            markets = [market async for market in client.list_markets()]

    assert len(caught) == 1
    assert "2 fallback legs" in caplog.text
    assert len(markets) == len(baseline)
    last_id = f"q{markets_response['questions'][-1]['question']}"
    assert last_id in {market.id for market in markets}
    for coin in failed:
        leg = next(
            outcome for market in markets for outcome in market.outcomes if outcome.id == coin
        )
        assert leg.price is None
        assert leg.raw_price is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["429", "500", "timeout"])
async def test_get_market_continues_when_l2book_fails(
    markets_response, templates_response, mids_response, respx_mock, kind, caplog
):
    """A failed book check prices that leg None and still returns the question."""

    def side_effect(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("type") == "l2Book":
            if body.get("coin") != "#152910":
                return httpx.Response(500, json={"error": f"unexpected l2Book {body.get('coin')}"})
            if kind == "timeout":
                raise httpx.TimeoutException("timed out")
            return _l2_error(kind)
        return _route(
            request,
            {
                "outcomeMeta": markets_response,
                "outcomeTemplates": templates_response,
                "allMids": mids_response,
            },
        )

    respx_mock.post(INFO_URL).mock(side_effect=side_effect)
    caplog.set_level(logging.WARNING)
    with pytest.warns(UserWarning, match="1 fallback leg") as caught:
        async with HyperliquidClient() as client:
            market = await client.get_market("q490")

    assert len(caught) == 1
    assert "1 fallback leg" in caplog.text
    fallback = _outcome(market, "#152910")
    assert fallback.price is None
    assert fallback.raw_price is None
    assert any(outcome.price is not None for outcome in market.outcomes)


@pytest.mark.asyncio
async def test_l2book_checks_stay_within_eight_in_flight(
    markets_response, templates_response, mids_response, respx_mock
):
    """More than eight fallbacks overlap, but never more than eight at once."""
    books = _empty_l2_books(markets_response)
    assert len(books) > 8
    lock = asyncio.Lock()
    state = {"in_flight": 0, "peak": 0}

    async def side_effect(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("type") == "l2Book":
            coin = body.get("coin")
            if not isinstance(coin, str) or coin not in books:
                return httpx.Response(500, json={"error": f"unexpected l2Book {coin}"})
            async with lock:
                state["in_flight"] += 1
                state["peak"] = max(state["peak"], state["in_flight"])
            try:
                await asyncio.sleep(0.05)
                return httpx.Response(200, json=books[coin])
            finally:
                async with lock:
                    state["in_flight"] -= 1
        return _route(
            request,
            {
                "outcomeMeta": markets_response,
                "outcomeTemplates": templates_response,
                "allMids": mids_response,
            },
        )

    respx_mock.post(INFO_URL).mock(side_effect=side_effect)
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        async with HyperliquidClient() as client:
            markets = [market async for market in client.list_markets()]
            again = [market async for market in client.list_markets()]

    assert state["peak"] == 8
    question_ids = [f"q{row['question']}" for row in markets_response["questions"]]
    assert [market.id for market in markets if market.id.startswith("q")] == question_ids
    assert [market.id for market in again] == [market.id for market in markets]


def _spec_outcome(outcome_id: int, name: str) -> dict:
    return {
        "outcome": outcome_id,
        "name": name,
        "description": "team",
        "sideSpecs": [{"name": "Yes"}, {"name": "No"}],
        "quoteToken": "USDC",
    }


def _spec_question(question_id: object, named: int, fallback: int) -> dict:
    return {
        "question": question_id,
        "name": f"Question {question_id}",
        "description": "match",
        "namedOutcomes": [named],
        "fallbackOutcome": fallback,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2, 3])
async def test_limit_skips_unparseable_question(respx_mock, limit):
    """A question that cannot be parsed does not consume a limit slot.

    The broken question is the first one, so it sits inside the window for
    limit 1, 2, and 3. Each call still returns that many later markets.
    """
    good = [
        _spec_question(10, 10, 100),
        _spec_question(11, 11, 110),
        _spec_question(12, 12, 120),
        _spec_question(13, 13, 130),
    ]
    broken = _spec_question("broken", 1, 2)
    outcomes = [_spec_outcome(1, "Leak"), _spec_outcome(2, "Leak fallback")]
    for question in good:
        named = question["namedOutcomes"][0]
        outcomes.append(_spec_outcome(named, f"Named {named}"))
        outcomes.append(_spec_outcome(question["fallbackOutcome"], f"Fallback {named}"))
    outcomes.append(_spec_outcome(999, "Standalone"))
    payload = {"outcomes": outcomes, "questions": [broken, *good]}
    books = {
        f"#{10 * question['fallbackOutcome']}": {
            "coin": f"#{10 * question['fallbackOutcome']}",
            "time": 1,
            "levels": [[], []],
        }
        for question in good
    }

    def side_effect(request: httpx.Request) -> httpx.Response:
        return _route(
            request,
            {
                "outcomeMeta": payload,
                "outcomeTemplates": [],
                "allMids": {},
                "l2Book": books,
            },
        )

    route = respx_mock.post(INFO_URL).mock(side_effect=side_effect)
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        async with HyperliquidClient() as client:
            markets = [market async for market in client.list_markets(limit=limit)]

    expected = [f"q{question['question']}" for question in good[:limit]]
    assert [market.id for market in markets] == expected
    assert len(markets) == limit
    assert all(market.id != "999" for market in markets)
    l2_coins = [
        body["coin"] for body in _info_bodies(list(route.calls)) if body["type"] == "l2Book"
    ]
    assert l2_coins == [f"#{10 * question['fallbackOutcome']}" for question in good[:limit]]


@pytest.mark.asyncio
async def test_l2book_404_prices_that_leg_none_and_finishes(
    markets_response, templates_response, mids_response, respx_mock, caplog
):
    """A 404 on one fallback book is a failed check, not a missing market.

    That leg is None, the question's other legs keep their mids, one warning
    names the single failure, and every other fallback book is still requested.
    """
    books = _empty_l2_books(markets_response)
    failed = "#152910"
    assert failed in books
    seen: list[str] = []

    def side_effect(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("type") == "l2Book":
            coin = body.get("coin")
            if isinstance(coin, str):
                seen.append(coin)
            if coin == failed:
                return httpx.Response(404, json={"error": "not found"})
            if isinstance(coin, str) and coin in books:
                return httpx.Response(200, json=books[coin])
            return httpx.Response(500, json={"error": f"unexpected l2Book {coin}"})
        return _route(
            request,
            {
                "outcomeMeta": markets_response,
                "outcomeTemplates": templates_response,
                "allMids": mids_response,
            },
        )

    respx_mock.post(INFO_URL).mock(side_effect=side_effect)
    caplog.set_level(logging.WARNING)
    with pytest.warns(UserWarning, match="1 fallback leg") as caught:
        async with HyperliquidClient() as client:
            markets = [market async for market in client.list_markets()]

    assert len(caught) == 1
    assert "1 fallback leg" in caplog.text
    question = next(market for market in markets if market.id == "q490")
    fallback = _outcome(question, failed)
    assert fallback.price is None
    assert fallback.raw_price is None
    others = [outcome for outcome in question.outcomes if outcome.id != failed]
    assert others
    assert all(outcome.price is not None for outcome in others)
    priced = {
        f"#{10 * row['fallbackOutcome']}"
        for row in markets_response["questions"]
        if row["fallbackOutcome"] != 8656
    }
    assert seen.count(failed) == 1
    assert set(seen) == priced
    assert len(seen) == len(priced)
    question_ids = [f"q{row['question']}" for row in markets_response["questions"]]
    assert [market.id for market in markets if market.id.startswith("q")] == question_ids
