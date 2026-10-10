"""Limitless adapter for prediction markets API.

Limitless-specific semantics:
- Prices are in 0..1 probability (already normalized)
- Order book sizes in raw 6-decimal units (1 share = 1,000,000)
- Order books show YES side (bids descending, asks ascending)
- Markets identified by slug (e.g., btc-hourly-price)
- Built on Base (L2)
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


class LimitlessClient(BaseClient):
    """Limitless client adapter.

    Uses Limitless's public Trade API:
    - /markets/active: Market discovery
    - /markets/{slug}: Single market details
    - /markets/{slug}/orderbook: Order book snapshot (YES side)
    - /markets/{slug}/events: Historical trades

    Read-only operations require no authentication.
    Trading operations would require API keys via environment variables:
    - LIMITLESS_API_KEY
    """

    API_BASE = "https://api.limitless.exchange"

    def __init__(
        self,
        max_order_size: Decimal | None = None,
        timeout: float = 30.0,
    ):
        """Initialize Limitless client.

        Args:
            max_order_size: Maximum order size limit (for safety)
            timeout: HTTP request timeout in seconds
        """
        self._max_order_size = max_order_size
        self._timeout = timeout
        self._http_client = httpx.AsyncClient(timeout=timeout)

        self._api_key: str | None = os.getenv("LIMITLESS_API_KEY")

    async def list_markets(
        self,
        closed: bool | None = None,
        limit: int | None = None,
        page_size: int = 25,
        **kwargs: object,
    ) -> AsyncIterator[Market]:
        """List markets using Limitless API page pagination.

        Args:
            closed: Filter by closed status (None = all, False = active only)
            limit: Maximum total markets to return (None = unlimited)
            page_size: Results per API request (max 25)
            **kwargs: Additional Limitless API filters (tradeType, automationType, etc.)

        Yields:
            Market objects (up to `limit` total)

        Raises:
            ValueError: If API returns invalid data
        """
        url = f"{self.API_BASE}/markets/active"
        params: dict[str, Any] = {"limit": min(page_size, 25)}

        for key, value in kwargs.items():
            if value is not None:
                params[key] = value

        page = 1
        yielded = 0

        while True:
            params["page"] = page

            try:
                response = await self._http_client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError) as e:
                raise ValueError(f"Failed to fetch markets: {e}") from e

            markets = data.get("data", [])
            if not markets:
                break

            for market_data in markets:
                if limit is not None and yielded >= limit:
                    return

                if closed is False and market_data.get("expired", False):
                    continue

                try:
                    market = self._parse_market(market_data)
                    yield market
                    yielded += 1
                except (KeyError, ValueError, TypeError) as e:
                    import logging

                    logging.warning(
                        f"Skipping market due to parse error: {market_data.get('slug', 'unknown')} - {e}"
                    )
                    continue

            total_count = data.get("totalMarketsCount", 0)
            if yielded >= total_count or len(markets) < params["limit"]:
                break

            page += 1

    async def get_market(self, market_id: str) -> Market:
        """Get a specific market by slug.

        Args:
            market_id: Limitless slug (e.g., btc-hourly-price)

        Returns:
            Market object

        Raises:
            ValueError: If market not found or invalid data
        """
        url = f"{self.API_BASE}/markets/{market_id}"

        try:
            response = await self._http_client.get(url)
            response.raise_for_status()
            market_data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ValueError(f"Failed to fetch market {market_id}: {e}") from e

        return self._parse_market(market_data)

    async def get_order_book(self, outcome_id: str) -> OrderBook:
        """Get order book for an outcome (slug).

        Limitless returns YES-side order book with prices in 0..1 and sizes in
        raw 6-decimal units (1 share = 1,000,000). Bids are sorted descending
        (best first), asks ascending (best first).

        Args:
            outcome_id: Limitless slug (market identifier)

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

        Contract (same as Polymarket and Kalshi):
        - get_price(BUY) returns the best ask (what a buyer pays to buy YES)
        - get_price(SELL) returns the best bid (what a seller receives selling YES)

        Args:
            outcome_id: Limitless slug
            side: OrderSide.BUY (user buying YES) or OrderSide.SELL (user selling YES)

        Returns:
            Price object with best executable price

        Raises:
            ValueError: If market not found or no liquidity
        """
        order_book = await self.get_order_book(outcome_id)

        if side == OrderSide.BUY:
            if not order_book.asks:
                raise ValueError(f"No liquidity to buy (no asks) for {outcome_id}")
            best_level = order_book.asks[0]
            best_price = best_level.price
            raw_price = best_level.raw_price
        else:
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
            currency=Currency.USDC,
        )

    async def get_trades(
        self,
        outcome_id: str,
        limit: int = 100,
        **kwargs: object,
    ) -> AsyncIterator[Trade]:
        """Get historical trades for an outcome.

        Args:
            outcome_id: Limitless slug
            limit: Results per page
            **kwargs: Additional filters (page, etc.)

        Yields:
            Trade objects
        """
        url = f"{self.API_BASE}/markets/{outcome_id}/events"
        params: dict[str, Any] = {
            "limit": min(limit, 100),
            "page": kwargs.get("page", 1),
        }

        try:
            response = await self._http_client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ValueError(f"Failed to fetch trades for {outcome_id}: {e}") from e

        events = data.get("events", [])
        for event_data in events:
            try:
                trade = self._parse_trade(outcome_id, event_data)
                yield trade
            except (KeyError, ValueError):
                continue

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

        TODO: EIP-712 signing implementation will need:
        - Fetch effectiveFeeRateBps from GET /profiles/{account} (authenticated)
        - Sign order with feeRateBps matching effectiveFeeRateBps
        - Handle 400 FEE_RATE_MISMATCH: extract expectedFeeRateBps, rebuild order, re-sign
        - See changelog Sep 23, 2026: "Sign orders with your effective fee rate"

        Args:
            outcome_id: Limitless slug
            side: OrderSide.BUY or OrderSide.SELL
            size: Order size (shares)
            price: Order price (0..1)
            dry_run: If True (default), simulate only
            **kwargs: Additional order parameters (feeRateBps for future implementation)

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

        if not self._api_key:
            raise PermissionError(
                "Trading requires API credentials via environment variable: LIMITLESS_API_KEY"
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
        """Parse Limitless API market data into Market object."""
        slug = data.get("slug", "")
        title = data.get("title", "")
        description = data.get("description")

        created_at_str = data.get("createdAt") or data.get("expirationDate")
        if created_at_str:
            try:
                created_at = parser.isoparse(created_at_str)
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                created_at = datetime.now(timezone.utc)
        else:
            created_at = datetime.now(timezone.utc)

        end_date = None
        end_date_ts = data.get("expirationTimestamp")
        if end_date_ts:
            try:
                end_date = datetime.fromtimestamp(end_date_ts / 1000, tz=timezone.utc)
            except (ValueError, TypeError):
                pass

        status = data.get("status", "")
        expired = data.get("expired", False)
        resolved = status == "RESOLVED" or expired

        volume = None
        volume_str = data.get("volumeFormatted")
        if volume_str:
            try:
                volume = Decimal(str(volume_str))
            except (ValueError, TypeError):
                pass

        open_interest = None
        oi_str = data.get("openInterestFormatted")
        if oi_str:
            try:
                open_interest = Decimal(str(oi_str))
            except (ValueError, TypeError):
                pass

        liquidity = None
        liq_str = data.get("liquidityFormatted")
        if liq_str:
            try:
                liquidity = Decimal(str(liq_str))
            except (ValueError, TypeError):
                pass

        outcomes: list[Outcome] = []
        prices = data.get("prices", [])

        data.get("address", "")

        if prices and len(prices) >= 2:
            yes_price = None
            if prices[0] is not None:
                try:
                    yes_price = Decimal(str(prices[0])) / Decimal("100")
                except (ValueError, TypeError):
                    pass

            outcomes.append(
                Outcome(
                    id=slug,
                    market_id=slug,
                    name="Yes",
                    price=yes_price,
                    raw_price=str(prices[0]) if prices[0] is not None else None,
                )
            )

            no_price = None
            if prices[1] is not None:
                try:
                    no_price = Decimal(str(prices[1])) / Decimal("100")
                except (ValueError, TypeError):
                    pass

            outcomes.append(
                Outcome(
                    id=f"{slug}_no",
                    market_id=slug,
                    name="No",
                    price=no_price,
                    raw_price=str(prices[1]) if prices[1] is not None else None,
                )
            )

        return Market(
            id=slug,
            venue="limitless",
            question=title,
            description=description,
            outcomes=outcomes,
            created_at=created_at,
            end_date=end_date,
            resolved=resolved,
            volume=volume,
            open_interest=open_interest,
            best_bid_size=None,
            best_ask_size=None,
            liquidity=liquidity,
            currency=Currency.USDC,
            raw_data=data,
            partial=False,
        )

    def _parse_order_book(self, slug: str, data: dict[str, Any]) -> OrderBook:
        """Parse Limitless API order book data.

        Limitless returns YES-side book with:
        - Bids sorted descending (best first)
        - Asks sorted ascending (best first)
        - Prices already normalized 0..1
        - Sizes in raw 6-decimal units (divide by 1e6 for shares)
        """
        data.get("tokenId", "")

        yes_bids = []
        for level in data.get("bids", []):
            price_val = level.get("price")
            size_val = level.get("size")
            if price_val is not None and size_val is not None:
                price_decimal = Decimal(str(price_val))
                size_decimal = Decimal(str(size_val)) / Decimal("1000000")

                yes_bids.append(
                    OrderLevel(
                        price=price_decimal,
                        size=size_decimal,
                        raw_price=str(price_val),
                    )
                )

        yes_bids.sort(key=lambda x: x.price, reverse=True)

        yes_asks = []
        for level in data.get("asks", []):
            price_val = level.get("price")
            size_val = level.get("size")
            if price_val is not None and size_val is not None:
                price_decimal = Decimal(str(price_val))
                size_decimal = Decimal(str(size_val)) / Decimal("1000000")

                yes_asks.append(
                    OrderLevel(
                        price=price_decimal,
                        size=size_decimal,
                        raw_price=str(price_val),
                    )
                )

        yes_asks.sort(key=lambda x: x.price)

        last_trade_price = None
        ltp = data.get("lastTradePrice")
        if ltp is not None:
            try:
                last_trade_price = Decimal(str(ltp))
            except (ValueError, TypeError):
                pass

        min_size_str = data.get("minSize")
        min_order_size = None
        if min_size_str:
            try:
                min_order_size = Decimal(str(min_size_str)) / Decimal("1000000")
            except (ValueError, TypeError):
                pass

        return OrderBook(
            outcome_id=slug,
            market_id=slug,
            bids=yes_bids,
            asks=yes_asks,
            timestamp=datetime.now(timezone.utc),
            last_trade_price=last_trade_price,
            min_order_size=min_order_size,
            tick_size=Decimal("0.01"),
            currency=Currency.USDC,
            partial=False,
        )

    def _parse_trade(self, slug: str, data: dict[str, Any]) -> Trade:
        """Parse Limitless API trade data."""
        tx_hash = data.get("txHash", "")
        trade_id = tx_hash if tx_hash else str(data.get("createdAt", ""))

        side_val = data.get("side")
        side = OrderSide.BUY if side_val == 0 else OrderSide.SELL

        price_val = data.get("price", 0)
        price = Decimal(str(price_val))

        matched_size_str = data.get("matchedSize", "0")
        size = Decimal(str(matched_size_str)) / Decimal("1000000")

        timestamp_str = data.get("createdAt")
        if timestamp_str:
            try:
                timestamp = parser.isoparse(timestamp_str)
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                timestamp = datetime.now(timezone.utc)
        else:
            timestamp = datetime.now(timezone.utc)

        return Trade(
            id=trade_id,
            market_id=slug,
            outcome_id=slug,
            side=side,
            price=price,
            size=size,
            timestamp=timestamp,
            currency=Currency.USDC,
            raw_data=data,
        )
