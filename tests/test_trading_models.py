"""Unit tests for trading models. No network."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

import prediction_markets_api as api
from prediction_markets_api import models as models_pkg
from prediction_markets_api.models.base import (
    Currency,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)

UTC = timezone.utc


def make_order(**overrides: Any) -> Order:
    """Build a valid resting order, applying overrides."""
    created_at = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    payload: dict[str, Any] = {
        "id": "ord_1",
        "client_order_id": "cid_1",
        "venue": "kalshi",
        "market_id": "MKT",
        "outcome_id": "YES",
        "side": OrderSide.BUY,
        "order_type": OrderType.GTC,
        "post_only": False,
        "price": Decimal("0.42"),
        "raw_price": "0.42",
        "size": Decimal("10"),
        "filled_size": Decimal("4"),
        "remaining_size": Decimal("6"),
        "avg_fill_price": Decimal("0.40"),
        "status": OrderStatus.OPEN,
        "venue_status": "resting",
        "created_at": created_at,
        "expires_at": None,
        "builder_code": None,
        "fees_paid": Decimal("0.01"),
        "currency": Currency.USD,
        "raw_request": {"price": "0.42"},
        "raw_data": {"id": "ord_1"},
        "partial": False,
    }
    payload.update(overrides)
    return Order(**payload)


def make_position(**overrides: Any) -> Position:
    """Build a valid position, applying overrides."""
    payload: dict[str, Any] = {
        "venue": "polymarket",
        "market_id": "MKT",
        "outcome_id": "NO",
        "outcome_name": "No",
        "size": Decimal("5"),
        "avg_price": Decimal("0.30"),
        "current_price": Decimal("0.35"),
        "realized_pnl": Decimal("-1.25"),
        "unrealized_pnl": Decimal("0.25"),
        "currency": Currency.USDC,
        "raw_data": {"size": "5"},
        "partial": False,
    }
    payload.update(overrides)
    return Position(**payload)


def test_order_type_and_status_values():
    """Enum values match the trading spec, including IOC as the FAK alias."""
    assert {member.name: member.value for member in OrderType} == {
        "GTC": "GTC",
        "GTD": "GTD",
        "IOC": "IOC",
        "FOK": "FOK",
    }
    assert {member.name: member.value for member in OrderStatus} == {
        "DRY_RUN": "dry_run",
        "OPEN": "open",
        "FILLED": "filled",
        "CANCELLED": "cancelled",
        "EXPIRED": "expired",
        "REJECTED": "rejected",
    }
    assert OrderType.IOC == "IOC"
    assert OrderStatus.DRY_RUN == "dry_run"
    with pytest.raises(ValueError):
        OrderType("FAK")
    with pytest.raises(ValueError):
        OrderStatus("OPEN")


def test_order_construction_and_decimal_coercion():
    """Order accepts the spec fields and coerces numeric inputs to Decimal."""
    order = make_order(
        price="0.42",
        size=10,
        filled_size="4",
        remaining_size=6,
        fees_paid="0.01",
    )

    assert order.id == "ord_1"
    assert order.client_order_id == "cid_1"
    assert order.venue == "kalshi"
    assert order.side is OrderSide.BUY
    assert order.order_type is OrderType.GTC
    assert order.post_only is False
    assert order.price == Decimal("0.42")
    assert order.size == Decimal("10")
    assert order.filled_size == Decimal("4")
    assert order.remaining_size == Decimal("6")
    assert order.avg_fill_price == Decimal("0.40")
    assert order.fees_paid == Decimal("0.01")
    assert order.status is OrderStatus.OPEN
    assert order.currency is Currency.USD
    assert order.raw_request == {"price": "0.42"}
    assert order.raw_data == {"id": "ord_1"}
    assert order.partial is False
    assert order.created_at is not None
    assert order.created_at.tzinfo == UTC


def test_order_defaults_for_optional_fields():
    """Optional fields default as specified: filled size 0, partial false, ids unset."""
    order = Order(
        venue="limitless",
        market_id="slug",
        outcome_id="yes",
        side=OrderSide.SELL,
        order_type=OrderType.FOK,
        post_only=False,
        price=Decimal("0.5"),
        size=Decimal("2"),
        remaining_size=Decimal("2"),
        status=OrderStatus.DRY_RUN,
        currency=Currency.USDC,
        raw_data={},
    )

    assert order.id is None
    assert order.client_order_id is None
    assert order.raw_price is None
    assert order.filled_size == Decimal("0")
    assert order.avg_fill_price is None
    assert order.venue_status is None
    assert order.created_at is None
    assert order.expires_at is None
    assert order.builder_code is None
    assert order.fees_paid is None
    assert order.raw_request is None
    assert order.partial is False
    assert order.status is OrderStatus.DRY_RUN


def test_order_accepts_boundary_prices_and_zero_size():
    """Prices on the closed 0..1 interval are valid, including a flat fill."""
    order = make_order(
        price=Decimal("0"),
        avg_fill_price=Decimal("1"),
        size=Decimal("0"),
        filled_size=Decimal("0"),
        remaining_size=Decimal("0"),
        status=OrderStatus.FILLED,
    )
    assert order.price == Decimal("0")
    assert order.avg_fill_price == Decimal("1")


@pytest.mark.parametrize(
    "field,value",
    [
        ("price", Decimal("-0.01")),
        ("price", Decimal("1.01")),
        ("avg_fill_price", Decimal("-0.01")),
        ("avg_fill_price", Decimal("1.01")),
        ("size", Decimal("-1")),
        ("filled_size", Decimal("-1")),
        ("remaining_size", Decimal("-1")),
    ],
)
def test_order_rejects_out_of_range_numbers(field, value):
    """Price must stay in 0..1 and sizes must be >= 0."""
    with pytest.raises(ValueError):
        make_order(**{field: value})


def test_order_fill_invariant():
    """filled_size + remaining_size equals size unless the payload is partial."""
    with pytest.raises(ValueError, match="filled_size \\+ remaining_size"):
        make_order(size=Decimal("10"), filled_size=Decimal("3"), remaining_size=Decimal("6"))

    partial = make_order(
        size=Decimal("10"),
        filled_size=Decimal("3"),
        remaining_size=Decimal("6"),
        partial=True,
    )
    assert partial.partial is True

    exact = make_order(
        size=Decimal("0.30"),
        filled_size=Decimal("0.10"),
        remaining_size=Decimal("0.20"),
    )
    assert exact.filled_size + exact.remaining_size == exact.size


def test_order_timestamps_must_be_utc():
    """Naive and non-UTC timestamps are rejected; UTC is kept."""
    naive = datetime(2026, 10, 29, 12, 0, 0)
    plus_three = datetime(2026, 10, 29, 12, 0, tzinfo=timezone(timedelta(hours=3)))
    expires = datetime(2026, 10, 29, 13, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="timezone-aware UTC"):
        make_order(created_at=naive)
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        make_order(expires_at=plus_three)

    order = make_order(order_type=OrderType.GTD, expires_at=expires)
    assert order.expires_at == expires
    assert order.created_at is not None
    assert order.created_at.utcoffset() == timedelta(0)


def test_order_is_frozen():
    """Field assignment raises, and model_copy leaves the original unchanged."""
    order = make_order()
    with pytest.raises(ValidationError) as exc_info:
        order.status = OrderStatus.CANCELLED
    assert any(err["type"] == "frozen_instance" for err in exc_info.value.errors())

    updated = order.model_copy(update={"status": OrderStatus.CANCELLED})
    assert updated.status is OrderStatus.CANCELLED
    assert order.status is OrderStatus.OPEN
    assert updated == make_order(status=OrderStatus.CANCELLED)


def test_order_rejects_unknown_enum_and_missing_required_fields():
    """Unknown status strings and a missing remaining size are validation errors."""
    with pytest.raises(ValueError):
        make_order(status="resting")
    with pytest.raises(ValueError):
        Order(
            venue="kalshi",
            market_id="MKT",
            outcome_id="YES",
            side=OrderSide.BUY,
            order_type=OrderType.GTC,
            post_only=False,
            price=Decimal("0.4"),
            size=Decimal("1"),
            status=OrderStatus.OPEN,
            currency=Currency.USD,
            raw_data={},
        )


def test_position_construction():
    """Position stores a non-negative NO holding and allows negative PnL."""
    position = make_position(size="5", avg_price="0.30", current_price=0)

    assert position.venue == "polymarket"
    assert position.outcome_id == "NO"
    assert position.outcome_name == "No"
    assert position.size == Decimal("5")
    assert position.avg_price == Decimal("0.30")
    assert position.current_price == Decimal("0")
    assert position.realized_pnl == Decimal("-1.25")
    assert position.unrealized_pnl == Decimal("0.25")
    assert position.currency is Currency.USDC
    assert position.raw_data == {"size": "5"}
    assert position.partial is False


def test_position_defaults():
    """Unset prices, names, and PnL stay None. partial defaults to false."""
    position = Position(
        venue="kalshi",
        market_id="MKT",
        outcome_id="YES",
        size=Decimal("0"),
        currency=Currency.USD,
        raw_data={},
    )

    assert position.outcome_name is None
    assert position.avg_price is None
    assert position.current_price is None
    assert position.realized_pnl is None
    assert position.unrealized_pnl is None
    assert position.size == Decimal("0")
    assert position.partial is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("size", Decimal("-0.01")),
        ("avg_price", Decimal("-0.01")),
        ("avg_price", Decimal("1.01")),
        ("current_price", Decimal("1.5")),
    ],
)
def test_position_rejects_out_of_range_numbers(field, value):
    """Held size is >= 0 and prices stay in 0..1."""
    with pytest.raises(ValueError):
        make_position(**{field: value})


def test_position_accepts_boundary_prices():
    """Both price fields accept the closed 0..1 interval."""
    position = make_position(avg_price=Decimal("0"), current_price=Decimal("1"))
    assert position.avg_price == Decimal("0")
    assert position.current_price == Decimal("1")


def test_position_is_frozen():
    """Field assignment raises, and model_copy leaves the original unchanged."""
    position = make_position()
    with pytest.raises(ValidationError) as exc_info:
        position.size = Decimal("9")
    assert any(err["type"] == "frozen_instance" for err in exc_info.value.errors())

    updated = position.model_copy(update={"size": Decimal("9")})
    assert updated.size == Decimal("9")
    assert position.size == Decimal("5")


def test_trading_models_are_exported():
    """Order types are part of the package and models public API."""
    assert api.Order is Order is models_pkg.Order
    assert api.Position is Position is models_pkg.Position
    assert api.OrderType is OrderType is models_pkg.OrderType
    assert api.OrderStatus is OrderStatus is models_pkg.OrderStatus
    for name in ("Order", "Position", "OrderType", "OrderStatus"):
        assert name in api.__all__
        assert name in models_pkg.__all__
