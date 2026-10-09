"""Base client interface for all prediction market venues."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from decimal import Decimal

from prediction_markets_api.models.base import Market, OrderBook, OrderSide, Price, Trade


class BaseClient(ABC):
    """Base client interface that all venue adapters implement.

    Provides unified methods for reading market data and placing orders.
    """

    @abstractmethod
    async def list_markets(
        self,
        closed: bool | None = None,
        limit: int = 100,
        **kwargs: object,
    ) -> AsyncIterator[Market]:
        """List markets from the venue.

        Args:
            closed: Filter by closed status (None = all)
            limit: Maximum results per page
            **kwargs: Venue-specific filters

        Yields:
            Market objects
        """
        raise NotImplementedError
        yield  # pragma: no cover

    @abstractmethod
    async def get_market(self, market_id: str) -> Market:
        """Get a specific market by ID.

        Args:
            market_id: Venue-specific market identifier

        Returns:
            Market object

        Raises:
            ValueError: If market not found or invalid data
        """
        raise NotImplementedError

    @abstractmethod
    async def get_order_book(self, outcome_id: str) -> OrderBook:
        """Get order book for a specific outcome.

        Args:
            outcome_id: Venue-specific outcome identifier

        Returns:
            OrderBook with sorted bids/asks

        Raises:
            ValueError: If outcome not found or data is invalid
        """
        raise NotImplementedError

    @abstractmethod
    async def get_price(self, outcome_id: str, side: OrderSide) -> Price:
        """Get best executable price for an outcome and side.

        Args:
            outcome_id: Venue-specific outcome identifier
            side: OrderSide.BUY or OrderSide.SELL

        Returns:
            Price object with best executable price

        Raises:
            ValueError: If outcome not found or no liquidity
        """
        raise NotImplementedError

    @abstractmethod
    async def get_trades(
        self,
        outcome_id: str,
        limit: int = 100,
        **kwargs: object,
    ) -> AsyncIterator[Trade]:
        """Get historical trades for an outcome.

        Args:
            outcome_id: Venue-specific outcome identifier
            limit: Maximum results per page
            **kwargs: Venue-specific filters

        Yields:
            Trade objects
        """
        raise NotImplementedError
        yield  # pragma: no cover

    @abstractmethod
    async def place_order(
        self,
        outcome_id: str,
        side: OrderSide,
        size: Decimal,
        price: Decimal,
        dry_run: bool = True,
        **kwargs: object,
    ) -> dict[str, object]:
        """Place an order (trading interface).

        IMPORTANT: dry_run is ON by default.
        - dry_run=True: Build and sign order without sending (safe)
        - dry_run=False: Actually send order (requires explicit opt-in)

        Args:
            outcome_id: Venue-specific outcome identifier
            side: OrderSide.BUY or OrderSide.SELL
            size: Order size/quantity
            price: Order price (normalized 0..1)
            dry_run: If True (default), build order without sending
            **kwargs: Venue-specific order parameters

        Returns:
            Dict with order details or dry-run preview

        Raises:
            ValueError: If order validation fails
            RuntimeError: If max_order_size exceeded
            PermissionError: If API keys not configured
        """
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        """Close the client and cleanup resources."""
        raise NotImplementedError

    async def __aenter__(self) -> "BaseClient":
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """Async context manager exit."""
        await self.close()
