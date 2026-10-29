"""Base data models for all prediction market venues.

All prices are normalized to 0..1 (probability space).
All timestamps are timezone-aware UTC.
All amounts include explicit currency.
Partial responses are allowed with a `partial` flag.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class OrderSide(str, Enum):
    """Order side: BUY or SELL."""

    BUY = "BUY"
    SELL = "SELL"


class Currency(str, Enum):
    """Supported currencies."""

    USD = "USD"
    USDC = "USDC"


class MarketStatus(str, Enum):
    """Normalized market status across all venues."""

    OPEN = "open"
    CLOSED = "closed"
    RESOLVED = "resolved"


class Outcome(BaseModel):
    """An outcome (token) within a market.

    Example: "Yes" and "No" for a binary market.

    price is None when the venue has no order book for that outcome. An empty
    book is None for Hyperliquid fallback outcomes. A one-sided book (only a
    bid or only an ask) is None for Pascal.
    """

    id: str = Field(description="Venue-specific outcome identifier")
    market_id: str = Field(description="Parent market identifier")
    name: str = Field(description="Outcome name (e.g., 'Yes', 'No', or event name)")
    price: Decimal | None = Field(
        None,
        description=(
            "Current price normalized to 0..1 probability. "
            "price is None when the venue has no order book for that outcome. "
            "An empty book is None for Hyperliquid fallback outcomes. "
            "A one-sided book (only a bid or only an ask) is None for Pascal."
        ),
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
    venue_status: str | None = Field(None, description="Raw venue-specific status string")
    status: MarketStatus | None = Field(
        None,
        description="Normalized market status: open, closed, or resolved. None if unknown.",
    )
    winning_outcome: str | None = Field(
        None,
        description="Name of the winning outcome when market is resolved (e.g., 'Yes', 'No')",
    )
    volume: Decimal | None = Field(None, description="Total trading volume", ge=Decimal("0"))
    open_interest: Decimal | None = Field(
        None,
        description="Open interest in venue-native units (contracts, shares, etc.)",
        ge=Decimal("0"),
    )
    best_bid_size: Decimal | None = Field(
        None, description="Size at top of book (bid side)", ge=Decimal("0")
    )
    best_ask_size: Decimal | None = Field(
        None, description="Size at top of book (ask side)", ge=Decimal("0")
    )
    liquidity: Decimal | None = Field(
        None,
        description="Venue-provided liquidity metric (None if venue doesn't provide one)",
        ge=Decimal("0"),
    )
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

    @model_validator(mode="after")
    def validate_resolution_invariants(self) -> "Market":
        """Enforce resolution invariants across all adapters.

        Invariant 1: resolved=True if and only if status==RESOLVED
        Invariant 2: winning_outcome is only set when resolved=True
        """
        # Invariant 1: resolved and status must agree
        if self.status == MarketStatus.RESOLVED and not self.resolved:
            raise ValueError(
                f"Market {self.id}: status=RESOLVED but resolved=False. "
                "Invariant violation: resolved must be True when status is RESOLVED."
            )
        if self.resolved and self.status != MarketStatus.RESOLVED:
            raise ValueError(
                f"Market {self.id}: resolved=True but status={self.status}. "
                "Invariant violation: resolved=True if and only if status=RESOLVED."
            )

        # Invariant 2: winning_outcome only when resolved
        if self.winning_outcome is not None and not self.resolved:
            raise ValueError(
                f"Market {self.id}: winning_outcome='{self.winning_outcome}' but resolved=False. "
                "Invariant violation: winning_outcome should only be set when resolved=True."
            )

        return self

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


class OrderType(str, Enum):
    """Time in force for an order.

    ``IOC`` is the normalized name for Polymarket and Limitless "FAK"
    (fill and kill): fill what you can, cancel the rest.
    """

    GTC = "GTC"  # rests until filled or cancelled
    GTD = "GTD"  # rests until expires_at
    IOC = "IOC"  # fill what you can, cancel the rest
    FOK = "FOK"  # all or nothing


class OrderStatus(str, Enum):
    """Normalized order status across venues."""

    DRY_RUN = "dry_run"
    OPEN = "open"  # resting, possibly partially filled
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"


class Order(BaseModel):
    """A trading order, including a dry-run preview that was not sent.

    Frozen: assigning to a field raises ``ValidationError``.

    ``price`` and ``avg_fill_price`` are probabilities in the closed range 0..1.
    Timestamps are timezone-aware UTC. Unless ``partial`` is true,
    ``filled_size + remaining_size`` must equal ``size``.
    """

    model_config = ConfigDict(frozen=True)

    id: str | None = Field(
        default=None,
        description="Venue order id. None for a dry run that was not sent.",
    )
    client_order_id: str | None = Field(
        default=None,
        description="Client-generated id. Stored locally even when the venue ignores it.",
    )
    venue: str = Field(description="Venue name (e.g., 'polymarket', 'kalshi', 'limitless')")
    market_id: str = Field(description="Venue-specific market identifier")
    outcome_id: str = Field(description="Venue-specific outcome identifier")
    side: OrderSide = Field(description="Order side (BUY or SELL)")
    order_type: OrderType = Field(description="Time in force (GTC, GTD, IOC, or FOK)")
    post_only: bool = Field(description="True if the order must rest and must not take liquidity")
    price: Decimal = Field(
        description="Limit price normalized to 0..1 probability",
        ge=Decimal("0"),
        le=Decimal("1"),
    )
    raw_price: Any | None = Field(
        default=None,
        description="Original price from the venue, or the price before venue conversion",
    )
    size: Decimal = Field(
        description="Original order size in outcome contracts/shares",
        ge=Decimal("0"),
    )
    filled_size: Decimal = Field(
        default=Decimal("0"),
        description="Size filled so far",
        ge=Decimal("0"),
    )
    remaining_size: Decimal = Field(
        description="Size still open. With filled_size, equals size unless partial.",
        ge=Decimal("0"),
    )
    avg_fill_price: Decimal | None = Field(
        default=None,
        description="Average fill price normalized to 0..1, if the venue reports it",
        ge=Decimal("0"),
        le=Decimal("1"),
    )
    status: OrderStatus = Field(description="Normalized order status")
    venue_status: str | None = Field(default=None, description="Raw venue-specific status string")
    created_at: datetime | None = Field(default=None, description="Order creation timestamp (UTC)")
    expires_at: datetime | None = Field(
        default=None, description="Expiry timestamp for GTD orders (UTC)"
    )
    builder_code: str | None = Field(
        default=None,
        description="Attached builder code. None if disabled or unsupported by the venue.",
    )
    fees_paid: Decimal | None = Field(
        default=None, description="Fees paid, if the venue reports them"
    )
    currency: Currency = Field(description="Order currency")
    raw_request: dict[str, Any] | None = Field(
        default=None,
        description="Body that was or would be sent to the venue. Must not contain secrets.",
    )
    raw_data: dict[str, Any] = Field(description="Original venue response (preserved)")
    partial: bool = Field(
        default=False,
        description="True if the venue returned incomplete data (fill invariant is not enforced)",
    )

    @field_validator(
        "price",
        "size",
        "filled_size",
        "remaining_size",
        "avg_fill_price",
        "fees_paid",
        mode="before",
    )
    @classmethod
    def coerce_decimal(cls, v: Any) -> Decimal | None:
        if v is None:
            return None
        return Decimal(str(v))

    @field_validator("created_at", "expires_at")
    @classmethod
    def ensure_utc(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return None
        offset = v.utcoffset()
        if offset is None or offset != timedelta(0):
            raise ValueError("Timestamps must be timezone-aware UTC")
        return v

    @model_validator(mode="after")
    def validate_fill_invariant(self) -> "Order":
        """Require filled_size + remaining_size == size unless the payload is partial."""
        if self.partial:
            return self
        if self.filled_size + self.remaining_size != self.size:
            raise ValueError(
                "filled_size + remaining_size must equal size when partial is False "
                f"(filled_size={self.filled_size}, remaining_size={self.remaining_size}, "
                f"size={self.size})"
            )
        return self


class Position(BaseModel):
    """Shares held in one outcome.

    Frozen: assigning to a field raises ``ValidationError``.

    A NO holding is its own outcome with ``size >= 0``, not a negative YES size.
    ``avg_price`` and ``current_price`` are probabilities in the closed range 0..1
    when the venue provides them.
    """

    model_config = ConfigDict(frozen=True)

    venue: str = Field(description="Venue name (e.g., 'polymarket', 'kalshi', 'limitless')")
    market_id: str = Field(description="Venue-specific market identifier")
    outcome_id: str = Field(description="Venue-specific outcome identifier")
    outcome_name: str | None = Field(
        default=None, description="Outcome name (e.g., 'Yes', 'No'), if known"
    )
    size: Decimal = Field(
        description="Shares held. Always >= 0; a NO holding is its own outcome.",
        ge=Decimal("0"),
    )
    avg_price: Decimal | None = Field(
        default=None,
        description="Average cost basis normalized to 0..1, if the venue provides it",
        ge=Decimal("0"),
        le=Decimal("1"),
    )
    current_price: Decimal | None = Field(
        default=None,
        description="Current price normalized to 0..1, if the venue provides it",
        ge=Decimal("0"),
        le=Decimal("1"),
    )
    realized_pnl: Decimal | None = Field(
        default=None, description="Realized PnL in currency units, if the venue provides it"
    )
    unrealized_pnl: Decimal | None = Field(
        default=None, description="Unrealized PnL in currency units, if the venue provides it"
    )
    currency: Currency = Field(description="Position currency")
    raw_data: dict[str, Any] = Field(description="Original venue response (preserved)")
    partial: bool = Field(
        default=False,
        description="True if the venue returned incomplete data",
    )

    @field_validator(
        "size",
        "avg_price",
        "current_price",
        "realized_pnl",
        "unrealized_pnl",
        mode="before",
    )
    @classmethod
    def coerce_decimal(cls, v: Any) -> Decimal | None:
        if v is None:
            return None
        return Decimal(str(v))
