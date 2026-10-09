"""Base data models for all prediction market venues.

All prices are normalized to 0..1 (probability space).
All timestamps are timezone-aware UTC.
All amounts include explicit currency.
Partial responses are allowed with a `partial` flag.
"""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class OrderSide(str, Enum):
    """Order side: BUY or SELL."""

    BUY = "BUY"
    SELL = "SELL"


class Currency(str, Enum):
    """Supported currencies."""

    USD = "USD"
    USDC = "USDC"


class Outcome(BaseModel):
    """An outcome (token) within a market.

    Example: "Yes" and "No" for a binary market.
    """

    id: str = Field(description="Venue-specific outcome identifier")
    market_id: str = Field(description="Parent market identifier")
    name: str = Field(description="Outcome name (e.g., 'Yes', 'No', or event name)")
    price: Decimal | None = Field(
        None,
        description="Current price normalized to 0..1 probability",
        ge=Decimal("0"),
        le=Decimal("1"),
    )
    raw_price: Any | None = Field(
        None, description="Original price from venue (preserved for reference)"
    )

    @field_validator("price", mode="before")
    @classmethod
    def coerce_price(cls, v: Any) -> Decimal | None:
        if v is None:
            return None
        return Decimal(str(v))


class Market(BaseModel):
    """A prediction market.

    Can have multiple outcomes (binary: Yes/No; categorical: multiple options).
    """

    id: str = Field(description="Venue-specific market identifier")
    venue: str = Field(description="Market venue (e.g., 'polymarket', 'kalshi')")
    question: str = Field(description="Market question")
    description: str | None = Field(None, description="Market description")
    outcomes: list[Outcome] = Field(
        default_factory=list, description="Available outcomes for this market"
    )
    created_at: datetime = Field(description="Market creation timestamp (UTC)")
    end_date: datetime | None = Field(None, description="Market end/close timestamp (UTC)")
    resolved: bool = Field(default=False, description="Whether market is resolved")
    volume: Decimal | None = Field(None, description="Total trading volume", ge=Decimal("0"))
    liquidity: Decimal | None = Field(None, description="Available liquidity", ge=Decimal("0"))
    currency: Currency = Field(default=Currency.USD, description="Market currency")
    raw_data: dict[str, Any] = Field(
        default_factory=dict, description="Original venue response (preserved)"
    )
    partial: bool = Field(
        default=False,
        description="True if venue returned incomplete data (not an error)",
    )

    @field_validator("created_at", "end_date", mode="before")
    @classmethod
    def ensure_utc(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return None
        if v.tzinfo is None:
            raise ValueError("Timestamps must be timezone-aware UTC")
        return v

    @field_validator("volume", "liquidity", mode="before")
    @classmethod
    def coerce_decimal(cls, v: Any) -> Decimal | None:
        if v is None:
            return None
        return Decimal(str(v))


class OrderLevel(BaseModel):
    """A price level in an order book."""

    price: Decimal = Field(
        description="Price normalized to 0..1 probability", ge=Decimal("0"), le=Decimal("1")
    )
    size: Decimal = Field(description="Size/quantity at this level", ge=Decimal("0"))
    raw_price: Any | None = Field(
        None, description="Original price from venue (preserved for reference)"
    )

    @field_validator("price", "size", mode="before")
    @classmethod
    def coerce_decimal(cls, v: Any) -> Decimal:
        return Decimal(str(v))


class OrderBook(BaseModel):
    """Order book for a market outcome.

    Bids are sorted descending by price (best bid first).
    Asks are sorted ascending by price (best ask first).
    Books are validated to never be crossed (best_bid < best_ask).
    """

    outcome_id: str = Field(description="Outcome identifier")
    market_id: str = Field(description="Market identifier")
    bids: list[OrderLevel] = Field(
        default_factory=list, description="Bids sorted descending by price"
    )
    asks: list[OrderLevel] = Field(
        default_factory=list, description="Asks sorted ascending by price"
    )
    timestamp: datetime = Field(description="Order book snapshot timestamp (UTC)")
    last_trade_price: Decimal | None = Field(
        None, description="Last trade price (0..1)", ge=Decimal("0"), le=Decimal("1")
    )
    min_order_size: Decimal | None = Field(None, description="Minimum order size", ge=Decimal("0"))
    tick_size: Decimal | None = Field(None, description="Minimum price increment", ge=Decimal("0"))
    currency: Currency = Field(default=Currency.USD, description="Order book currency")
    partial: bool = Field(default=False, description="True if venue returned incomplete data")

    @field_validator("timestamp", mode="before")
    @classmethod
    def ensure_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("Timestamps must be timezone-aware UTC")
        return v

    @field_validator("last_trade_price", "min_order_size", "tick_size", mode="before")
    @classmethod
    def coerce_decimal(cls, v: Any) -> Decimal | None:
        if v is None:
            return None
        return Decimal(str(v))

    def validate_not_crossed(self) -> None:
        """Validate that the order book is not crossed (best_bid < best_ask)."""
        if self.bids and self.asks:
            best_bid = self.bids[0].price
            best_ask = self.asks[0].price
            if best_bid >= best_ask:
                raise ValueError(
                    f"Order book is crossed: best_bid={best_bid} >= best_ask={best_ask}"
                )


class Price(BaseModel):
    """Best executable price for a market outcome."""

    outcome_id: str = Field(description="Outcome identifier")
    market_id: str = Field(description="Market identifier")
    side: OrderSide = Field(description="Order side (BUY or SELL)")
    price: Decimal = Field(
        description="Best executable price normalized to 0..1",
        ge=Decimal("0"),
        le=Decimal("1"),
    )
    raw_price: Any | None = Field(
        None, description="Original price from venue (preserved for reference)"
    )
    timestamp: datetime = Field(description="Price timestamp (UTC)")
    currency: Currency = Field(default=Currency.USD, description="Price currency")

    @field_validator("timestamp", mode="before")
    @classmethod
    def ensure_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("Timestamps must be timezone-aware UTC")
        return v

    @field_validator("price", mode="before")
    @classmethod
    def coerce_decimal(cls, v: Any) -> Decimal:
        return Decimal(str(v))


class Trade(BaseModel):
    """A historical trade."""

    id: str = Field(description="Trade identifier")
    market_id: str = Field(description="Market identifier")
    outcome_id: str = Field(description="Outcome identifier")
    side: OrderSide = Field(description="Trade side (BUY or SELL)")
    price: Decimal = Field(
        description="Trade price normalized to 0..1", ge=Decimal("0"), le=Decimal("1")
    )
    size: Decimal = Field(description="Trade size/quantity", ge=Decimal("0"))
    timestamp: datetime = Field(description="Trade timestamp (UTC)")
    currency: Currency = Field(default=Currency.USD, description="Trade currency")
    raw_data: dict[str, Any] = Field(default_factory=dict, description="Original venue data")

    @field_validator("timestamp", mode="before")
    @classmethod
    def ensure_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("Timestamps must be timezone-aware UTC")
        return v

    @field_validator("price", "size", mode="before")
    @classmethod
    def coerce_decimal(cls, v: Any) -> Decimal:
        return Decimal(str(v))
