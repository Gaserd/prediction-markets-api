"""Kalshi adapter for prediction markets API.

Kalshi-specific semantics:
- Prices are in cents (0-100) and normalized to 0..1 probability
- Order books show bids for YES outcome; asks are derived from complementary NO side
- Markets identified by ticker (e.g., KXBTC-24DEC31-T100K)
- US-only, regulated by CFTC
"""

import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx
from dateutil import parser

from prediction_markets_api.adapters.base import BaseClient
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


class KalshiClient(BaseClient):
    """Kalshi client adapter.

    Uses Kalshi's public Trade API v2:
    - /markets: Market discovery
    - /markets/{ticker}: Single market details
    - /markets/{ticker}/orderbook: Order book snapshot
    - /markets/{ticker}/trades: Historical trades

    Read-only operations require no authentication.
    Trading operations would require API keys via environment variables:
    - KALSHI_API_KEY
    - KALSHI_API_SECRET
    """

    API_BASE = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(
        self,
        max_order_size: Decimal | None = None,
        timeout: float = 30.0,
    ):
        """Initialize Kalshi client.

        Args:
            max_order_size: Maximum order size limit (for safety)
            timeout: HTTP request timeout in seconds
        """
        self._max_order_size = max_order_size
        self._timeout = timeout
        self._http_client = httpx.AsyncClient(timeout=timeout)

        self._api_key: str | None = os.getenv("KALSHI_API_KEY")
        self._api_secret: str | None = os.getenv("KALSHI_API_SECRET")

    async def list_markets(
        self,
        closed: bool | None = None,
        limit: int | None = None,
        page_size: int = 100,
        venue_params: dict[str, Any] | None = None,
        **kwargs: object,
    ) -> AsyncIterator[Market]:
        """List markets using Kalshi API cursor pagination.

        Args:
            closed: Filter by closed status (None = all)
            limit: Maximum total markets to return (None = unlimited)
            page_size: Results per API request (max 200)
            venue_params: Venue-specific parameters as a dict (optional, unused by Kalshi)
            **kwargs: Additional Kalshi API filters. Supported kwargs:
                - include_multivariate (bool): Include multivariate combo markets (KXMVE*).
                  Default False to exclude them via server-side mve_filter=exclude, as they
                  flood listings and often 404 on direct GET. Kalshi-specific.
                - Other filters: status, series_ticker, etc.

        Yields:
            Market objects (up to `limit` total)

        Raises:
            ValueError: If API returns invalid data
        """
        # Extract Kalshi-specific parameter from kwargs
        include_multivariate = bool(kwargs.pop("include_multivariate", False))

        url = f"{self.API_BASE}/markets"
        params: dict[str, Any] = {"limit": min(page_size, 200)}

        if closed is not None:
            # Kalshi uses status filter: "active", "closed", "settled"
            params["status"] = "closed" if closed else "active"

        # Exclude multivariate combo markets by default (server-side filter)
        if not include_multivariate:
            params["mve_filter"] = "exclude"

        for key, value in kwargs.items():
            if value is not None:
                params[key] = value

        cursor: str | None = None
        yielded = 0

        while True:
            if cursor:
                params["cursor"] = cursor

            try:
                response = await self._http_client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError) as e:
                raise ValueError(f"Failed to fetch markets: {e}") from e

            markets = data.get("markets", [])
            if not markets:
                break

            for market_data in markets:
                if limit is not None and yielded >= limit:
                    return

                try:
                    market = self._parse_market(market_data)
                    yield market
                    yielded += 1
                except (KeyError, ValueError, TypeError) as e:
                    # Log and skip markets we can't parse
                    import logging

                    logging.warning(
                        f"Skipping market due to parse error: {market_data.get('ticker', 'unknown')} - {e}"
                    )
                    continue

            cursor = data.get("cursor")
            if not cursor:
                break

    async def get_market(self, market_id: str) -> Market:
        """Get a specific market by ticker.

        Args:
            market_id: Kalshi ticker (e.g., KXBTC-24DEC31-T100K)

        Returns:
            Market object

        Raises:
            ValueError: If market not found or invalid data
        """
        url = f"{self.API_BASE}/markets/{market_id}"

        try:
            response = await self._http_client.get(url)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ValueError(f"Failed to fetch market {market_id}: {e}") from e

        market_data = data.get("market")
        if not market_data:
            raise ValueError(f"Market {market_id} not found")

        return self._parse_market(market_data)

    async def get_order_book(self, outcome_id: str) -> OrderBook:
        """Get order book for an outcome (ticker).

        Kalshi-specific: The API returns YES bids and NO asks. To get the full
        order book for the YES outcome, we derive YES asks from NO bids using
        the complementary probability (ask_yes = 1 - bid_no).

        Args:
            outcome_id: Kalshi ticker (market identifier)

        Returns:
            OrderBook with sorted and validated levels

        Raises:
            ValueError: If market not found or book is crossed
        """
        url = f"{self.API_BASE}/markets/{outcome_id}/orderbook"

        try:
            response = await self._http_client.get(url)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ValueError(f"Failed to fetch order book for {outcome_id}: {e}") from e

        order_book = self._parse_order_book(outcome_id, data)
        order_book.validate_not_crossed()

        return order_book

    async def get_price(self, outcome_id: str, side: OrderSide) -> Price:
        """Get best executable price for an outcome and side.

        Contract (same as Polymarket):
        - get_price(BUY) returns the best ask (what a buyer pays to buy YES)
        - get_price(SELL) returns the best bid (what a seller receives selling YES)

        Note: For the derived YES ask price, raw_price holds the original raw NO bid string
        from the Kalshi API (before the 1 - p transformation).

        Args:
            outcome_id: Kalshi ticker
            side: OrderSide.BUY (user buying YES) or OrderSide.SELL (user selling YES)

        Returns:
            Price object with best executable price

        Raises:
            ValueError: If market not found or no liquidity
        """
        # Get the full order book
        order_book = await self.get_order_book(outcome_id)

        if side == OrderSide.BUY:
            # User wants to BUY YES -> needs best ask
            if not order_book.asks:
                raise ValueError(f"No liquidity to buy (no asks) for {outcome_id}")
            best_level = order_book.asks[0]
            best_price = best_level.price
            raw_price = best_level.raw_price
        else:
            # User wants to SELL YES -> needs best bid
            if not order_book.bids:
                raise ValueError(f"No liquidity to sell (no bids) for {outcome_id}")
            best_level = order_book.bids[0]
            best_price = best_level.price
            raw_price = best_level.raw_price

        return Price(
            outcome_id=outcome_id,
            market_id=outcome_id,
            side=side,
            price=best_price,
            raw_price=raw_price,
            timestamp=datetime.now(timezone.utc),
            currency=Currency.USD,
        )

    async def get_trades(
        self,
        outcome_id: str,
        limit: int = 100,
        **kwargs: object,
    ) -> AsyncIterator[Trade]:
        """Get historical trades for an outcome.

        Args:
            outcome_id: Kalshi ticker
            limit: Results per page
            **kwargs: Additional filters (min_ts, max_ts, etc.)

        Yields:
            Trade objects
        """
        url = f"{self.API_BASE}/markets/{outcome_id}/trades"
        params: dict[str, Any] = {
            "limit": limit,
        }

        for key, value in kwargs.items():
            if value is not None:
                params[key] = value

        cursor: str | None = None

        while True:
            if cursor:
                params["cursor"] = cursor

            try:
                response = await self._http_client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError) as e:
                raise ValueError(f"Failed to fetch trades for {outcome_id}: {e}") from e

            trades = data.get("trades", [])
            if not trades:
                break

            for trade_data in trades:
                try:
                    trade = self._parse_trade(outcome_id, trade_data)
                    yield trade
                except (KeyError, ValueError):
                    continue

            cursor = data.get("cursor")
            if not cursor:
                break

    async def place_order(
        self,
        outcome_id: str,
        side: OrderSide,
        size: Decimal,
        price: Decimal,
        dry_run: bool = True,
        **kwargs: object,
    ) -> dict[str, object]:
        """Place an order (stub implementation).

        Args:
            outcome_id: Kalshi ticker
            side: OrderSide.BUY or OrderSide.SELL
            size: Order size (contracts)
            price: Order price (0..1)
            dry_run: If True (default), simulate only
            **kwargs: Additional order parameters

        Returns:
            Dict with order details or dry-run preview

        Raises:
            ValueError: If order validation fails
            RuntimeError: If max_order_size exceeded
            PermissionError: If API keys not configured and dry_run=False
        """
        if price < Decimal("0") or price > Decimal("1"):
            raise ValueError(f"Price must be between 0 and 1, got {price}")

        if size <= Decimal("0"):
            raise ValueError(f"Size must be positive, got {size}")

        if self._max_order_size and size > self._max_order_size:
            raise RuntimeError(f"Order size {size} exceeds max_order_size {self._max_order_size}")

        if dry_run:
            return {
                "dry_run": True,
                "outcome_id": outcome_id,
                "side": side.value,
                "size": str(size),
                "price": str(price),
                "status": "simulated",
                "message": "Order validated but not sent (dry_run=True)",
            }

        if not all([self._api_key, self._api_secret]):
            raise PermissionError(
                "Trading requires API credentials via environment variables: "
                "KALSHI_API_KEY, KALSHI_API_SECRET"
            )

        return {
            "dry_run": False,
            "status": "not_implemented",
            "message": "Order placement not implemented in this version",
        }

    async def close(self) -> None:
        """Close HTTP client."""
        await self._http_client.aclose()

    def _parse_market(self, data: dict[str, Any]) -> Market:
        """Parse Kalshi API market data into Market object."""
        ticker = data.get("ticker", "")
        title = data.get("title", "")
        subtitle = data.get("subtitle")

        # Kalshi timestamps are ISO 8601 strings
        created_at_str = data.get("open_time")
        if created_at_str:
            created_at = parser.isoparse(created_at_str)
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
        else:
            created_at = datetime.now(timezone.utc)

        end_date = None
        end_date_str = data.get("close_time") or data.get("expiration_time")
        if end_date_str:
            end_date = parser.isoparse(end_date_str)
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)

        status = data.get("status", "")
        resolved = status in ("closed", "settled")

        # Volume in USD (already in dollars in new API)
        volume = None
        volume_raw = data.get("volume")
        if volume_raw is not None:
            try:
                # volume is already in dollars in fractional pricing API
                volume = Decimal(str(volume_raw))
            except (ValueError, TypeError):
                pass

        # Open interest - use open_interest_fp (in contracts)
        # This is the total number of contracts outstanding
        open_interest = None
        if "open_interest_fp" in data and data["open_interest_fp"] is not None:
            try:
                open_interest = Decimal(str(data["open_interest_fp"]))
            except (ValueError, TypeError):
                pass
        elif "open_interest" in data and data["open_interest"] is not None:
            try:
                # Old field was also in contracts
                open_interest = Decimal(str(data["open_interest"]))
            except (ValueError, TypeError):
                pass

        # Best bid/ask sizes from top-of-book
        best_bid_size = None
        if "yes_bid_size_fp" in data and data["yes_bid_size_fp"] is not None:
            try:
                best_bid_size = Decimal(str(data["yes_bid_size_fp"]))
            except (ValueError, TypeError):
                pass

        best_ask_size = None
        if "yes_ask_size_fp" in data and data["yes_ask_size_fp"] is not None:
            try:
                best_ask_size = Decimal(str(data["yes_ask_size_fp"]))
            except (ValueError, TypeError):
                pass

        # Liquidity: Kalshi does not provide a liquidity metric
        # (they removed liquidity_dollars; open_interest is not liquidity)
        liquidity = None

        # Kalshi has yes/no outcomes
        # Use top-of-book prices: yes_bid_dollars, no_bid_dollars, etc.
        outcomes: list[Outcome] = []

        # YES outcome - use yes_bid_dollars (price to sell YES)
        yes_price = None
        yes_raw = None
        if "yes_bid_dollars" in data and data["yes_bid_dollars"] is not None:
            yes_raw = data["yes_bid_dollars"]
            try:
                yes_price = Decimal(str(yes_raw))
            except (ValueError, TypeError):
                pass

        outcomes.append(
            Outcome(
                id=ticker,
                market_id=ticker,
                name="Yes",
                price=yes_price,
                raw_price=yes_raw,
            )
        )

        # NO outcome - use no_bid_dollars (price to sell NO)
        no_price = None
        no_raw = None
        if "no_bid_dollars" in data and data["no_bid_dollars"] is not None:
            no_raw = data["no_bid_dollars"]
            try:
                no_price = Decimal(str(no_raw))
            except (ValueError, TypeError):
                pass

        outcomes.append(
            Outcome(
                id=f"{ticker}_no",
                market_id=ticker,
                name="No",
                price=no_price,
                raw_price=no_raw,
            )
        )

        return Market(
            id=ticker,
            venue="kalshi",
            question=title,
            description=subtitle,
            outcomes=outcomes,
            created_at=created_at,
            end_date=end_date,
            resolved=resolved,
            volume=volume,
            open_interest=open_interest,
            best_bid_size=best_bid_size,
            best_ask_size=best_ask_size,
            liquidity=liquidity,  # None for Kalshi
            currency=Currency.USD,
            raw_data=data,
            partial=False,
        )

    def _parse_order_book(self, ticker: str, data: dict[str, Any]) -> OrderBook:
        """Parse Kalshi API order book data.

        Kalshi returns YES bids and NO bids in the orderbook_fp field.
        Arrays are [[price_dollars, size], ...].

        Important: YES bids are sorted descending (best first),
        but NO bids are sorted ASCENDING (worst first, best last).

        We derive YES asks from NO bids using: ask_yes = 1 - bid_no.
        The best NO bid (highest price, last in array) gives the best YES ask.
        """
        orderbook = data.get("orderbook_fp", data.get("orderbook", {}))

        # YES bids (people buying YES) - sorted descending (best first)
        yes_bids = []
        for level in orderbook.get("yes_dollars", orderbook.get("yes", [])):
            if not level or len(level) < 2:
                continue
            # Prices come as strings like '0.1600'
            price_dollars = Decimal(str(level[0]))
            size_contracts = level[1]

            yes_bids.append(
                OrderLevel(
                    price=price_dollars,
                    size=Decimal(str(size_contracts)),
                    raw_price=str(level[0]),
                )
            )

        # YES bids should already be sorted descending, but ensure it
        yes_bids.sort(key=lambda x: x.price, reverse=True)

        # NO bids -> convert to YES asks
        # NO bids are sorted ASCENDING (worst first), so we need to reverse
        # to get best bids first, then convert to YES asks
        no_bids_raw = orderbook.get("no_dollars", orderbook.get("no", []))

        # Reverse to get best NO bids first (highest prices)
        yes_asks = []
        for level in reversed(no_bids_raw):
            if not level or len(level) < 2:
                continue
            # Prices come as strings like '0.1600'
            price_dollars_no = Decimal(str(level[0]))
            size_contracts = level[1]

            # Convert NO bid to YES ask: if someone bids $X for NO,
            # that's equivalent to asking $(1-X) for YES
            price_dollars_yes = Decimal("1.0") - price_dollars_no

            yes_asks.append(
                OrderLevel(
                    price=price_dollars_yes,
                    size=Decimal(str(size_contracts)),
                    raw_price=str(level[0]),  # Store original NO price
                )
            )

        # Sort asks ascending (best ask = lowest price first)
        yes_asks.sort(key=lambda x: x.price)

        return OrderBook(
            outcome_id=ticker,
            market_id=ticker,
            bids=yes_bids,
            asks=yes_asks,
            timestamp=datetime.now(timezone.utc),
            last_trade_price=None,
            min_order_size=None,
            tick_size=Decimal("0.01"),  # Kalshi uses 1 cent increments
            currency=Currency.USD,
            partial=False,
        )

    def _parse_trade(self, ticker: str, data: dict[str, Any]) -> Trade:
        """Parse Kalshi API trade data."""
        trade_id = str(data.get("trade_id", ""))

        # Kalshi trades specify yes_price; side is implicit
        yes_price_cents = data.get("yes_price", 0)
        price = Decimal(str(yes_price_cents)) / Decimal("100")

        count = data.get("count", 0)
        size = Decimal(str(count))

        # Kalshi doesn't explicitly return trade side; we infer BUY as default
        side = OrderSide.BUY

        timestamp_str = data.get("created_time")
        if timestamp_str:
            timestamp = parser.isoparse(timestamp_str)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
        else:
            timestamp = datetime.now(timezone.utc)

        return Trade(
            id=trade_id,
            market_id=ticker,
            outcome_id=ticker,
            side=side,
            price=price,
            size=size,
            timestamp=timestamp,
            currency=Currency.USD,
            raw_data=data,
        )
