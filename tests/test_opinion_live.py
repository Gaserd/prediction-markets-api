"""Live smoke tests for Opinion (opt-in with -m live).

Public market, order book, and price reads do not send an API key.
There is no public trade feed, so these tests do not call ``get_trades``.
Normal CI deselects this module with ``-m "not live"``.
"""

from decimal import Decimal

import pytest

from prediction_markets_api import OpinionClient
from prediction_markets_api.models.base import MarketStatus


@pytest.mark.live
@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_list_and_get_market_live() -> None:
    """List a few open markets and fetch one by id. No API key."""
    async with OpinionClient() as client:
        markets = [market async for market in client.list_markets(closed=False, limit=3)]
        assert markets
        assert len(markets) <= 3
        assert all(market.venue == "opinion" for market in markets)
        assert all(market.status == MarketStatus.OPEN for market in markets)
        fetched = await client.get_market(markets[0].id)
        assert fetched.id == markets[0].id
        assert fetched.question == markets[0].question
        assert fetched.status == markets[0].status
        assert fetched.outcomes


@pytest.mark.live
@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_overlapping_id_2592_live() -> None:
    """Binary 2592 and categorical parent 2592 are different markets."""
    async with OpinionClient() as client:
        parent = await client.get_market("categorical-2592")
        binary = await client.get_market("2592")
        assert parent.id == "categorical-2592"
        assert parent.raw_data["marketId"] == 2592
        assert binary.id == "2592"
        assert binary.raw_data["marketId"] == 2592
        assert parent.question != binary.question


@pytest.mark.live
@pytest.mark.timeout(90)
@pytest.mark.asyncio
async def test_order_book_invariants_live() -> None:
    """One snapshot of a two-sided binary book. Skip when none is in the window."""
    async with OpinionClient() as client:
        book = None
        async for market in client.list_markets(
            closed=False, limit=12, venue_params={"market_type": 0}
        ):
            if len(market.outcomes) != 2:
                continue
            candidate = await client.get_order_book(market.id)
            if candidate.bids and candidate.asks:
                book = candidate
                break
        if book is None:
            pytest.skip("No two-sided Opinion binary book in the first 12 markets")
        assert book.bids == sorted(book.bids, key=lambda level: level.price, reverse=True)
        assert book.asks == sorted(book.asks, key=lambda level: level.price)
        assert book.bids[0].price < book.asks[0].price
        for level in [*book.bids, *book.asks]:
            assert Decimal("0") <= level.price <= Decimal("1")
            assert level.size > 0
