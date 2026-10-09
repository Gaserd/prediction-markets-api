"""Tests for data models."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from prediction_markets_api.models.base import (
    Currency,
    Market,
    OrderBook,
    OrderLevel,
    OrderSide,
    Outcome,
    Price,
    Trade,
)


def test_outcome_creation():
    """Test Outcome model creation."""
    outcome = Outcome(
        id="token_123",
        market_id="market_456",
        name="Yes",
        price=Decimal("0.65"),
        raw_price="0.65",
    )

    assert outcome.id == "token_123"
    assert outcome.market_id == "market_456"
    assert outcome.name == "Yes"
    assert outcome.price == Decimal("0.65")
    assert outcome.raw_price == "0.65"


def test_outcome_price_normalization():
    """Test that outcome prices are normalized to 0..1."""
    with pytest.raises(ValueError):
        Outcome(
            id="token_123",
            market_id="market_456",
            name="Yes",
            price=Decimal("1.5"),
        )

    with pytest.raises(ValueError):
        Outcome(
            id="token_123",
            market_id="market_456",
            name="Yes",
            price=Decimal("-0.1"),
        )


def test_market_creation():
    """Test Market model creation."""
    created_at = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
    end_date = datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

    market = Market(
        id="market_123",
        venue="polymarket",
        question="Will Bitcoin reach $100k?",
        description="Test market",
        outcomes=[],
        created_at=created_at,
        end_date=end_date,
        resolved=False,
        volume=Decimal("1000000"),
        liquidity=Decimal("50000"),
        currency=Currency.USD,
    )

    assert market.id == "market_123"
    assert market.venue == "polymarket"
    assert market.question == "Will Bitcoin reach $100k?"
    assert market.volume == Decimal("1000000")
    assert market.liquidity == Decimal("50000")


def test_market_requires_utc_timezone():
    """Test that Market requires timezone-aware UTC timestamps."""
    naive_dt = datetime(2026, 1, 15, 10, 0, 0)

    with pytest.raises(ValueError, match="timezone-aware"):
        Market(
            id="market_123",
            venue="polymarket",
            question="Test?",
            outcomes=[],
            created_at=naive_dt,
        )


def test_order_level_creation():
    """Test OrderLevel model creation."""
    level = OrderLevel(
        price=Decimal("0.65"),
        size=Decimal("1000"),
        raw_price="0.65",
    )

    assert level.price == Decimal("0.65")
    assert level.size == Decimal("1000")
    assert level.raw_price == "0.65"


def test_order_book_sorting_and_validation():
    """Test that OrderBook validates and sorts correctly."""
    timestamp = datetime.now(timezone.utc)

    bids = [
        OrderLevel(price=Decimal("0.60"), size=Decimal("100")),
        OrderLevel(price=Decimal("0.62"), size=Decimal("200")),
        OrderLevel(price=Decimal("0.61"), size=Decimal("150")),
    ]

    asks = [
        OrderLevel(price=Decimal("0.66"), size=Decimal("100")),
        OrderLevel(price=Decimal("0.64"), size=Decimal("200")),
        OrderLevel(price=Decimal("0.65"), size=Decimal("150")),
    ]

    bids_sorted = sorted(bids, key=lambda x: x.price, reverse=True)
    asks_sorted = sorted(asks, key=lambda x: x.price)

    order_book = OrderBook(
        outcome_id="token_123",
        market_id="market_456",
        bids=bids_sorted,
        asks=asks_sorted,
        timestamp=timestamp,
        currency=Currency.USD,
    )

    assert order_book.bids[0].price == Decimal("0.62")
    assert order_book.asks[0].price == Decimal("0.64")

    order_book.validate_not_crossed()


def test_order_book_crossed_validation():
    """Test that OrderBook rejects crossed books."""
    timestamp = datetime.now(timezone.utc)

    bids = [OrderLevel(price=Decimal("0.66"), size=Decimal("100"))]
    asks = [OrderLevel(price=Decimal("0.64"), size=Decimal("100"))]

    order_book = OrderBook(
        outcome_id="token_123",
        market_id="market_456",
        bids=bids,
        asks=asks,
        timestamp=timestamp,
    )

    with pytest.raises(ValueError, match="crossed"):
        order_book.validate_not_crossed()


def test_price_creation():
    """Test Price model creation."""
    timestamp = datetime.now(timezone.utc)

    price = Price(
        outcome_id="token_123",
        market_id="market_456",
        side=OrderSide.BUY,
        price=Decimal("0.66"),
        raw_price="0.66",
        timestamp=timestamp,
        currency=Currency.USD,
    )

    assert price.outcome_id == "token_123"
    assert price.side == OrderSide.BUY
    assert price.price == Decimal("0.66")


def test_trade_creation():
    """Test Trade model creation."""
    timestamp = datetime.now(timezone.utc)

    trade = Trade(
        id="trade_123",
        market_id="market_456",
        outcome_id="token_789",
        side=OrderSide.BUY,
        price=Decimal("0.65"),
        size=Decimal("100"),
        timestamp=timestamp,
        currency=Currency.USD,
    )

    assert trade.id == "trade_123"
    assert trade.side == OrderSide.BUY
    assert trade.price == Decimal("0.65")
    assert trade.size == Decimal("100")
