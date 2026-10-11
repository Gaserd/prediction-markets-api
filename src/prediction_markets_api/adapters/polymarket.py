"""Polymarket adapter for prediction markets API."""

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


class PolymarketClient(BaseClient):
    """Polymarket client adapter.

    Uses Polymarket's public APIs:
    - Gamma API: Market discovery and metadata
    - CLOB API: Order books and prices
    - Data API v2: Historical trades and price history

    Read-only operations require no authentication.
    Trading operations require API keys via environment variables:
    - POLYMARKET_API_KEY
    - POLYMARKET_API_SECRET
    - POLYMARKET_WALLET_ADDRESS
    """

    GAMMA_API_BASE = "https://gamma-api.polymarket.com"
    CLOB_API_BASE = "https://clob.polymarket.com"
    DATA_API_BASE = "https://data-api.polymarket.com/v2"

    def __init__(
        self,
        max_order_size: Decimal | None = None,
        timeout: float = 30.0,
    ):
        """Initialize Polymarket client.

        Args:
            max_order_size: Maximum order size limit (for safety)
            timeout: HTTP request timeout in seconds
        """
        self._max_order_size = max_order_size
        self._timeout = timeout
        self._http_client = httpx.AsyncClient(timeout=timeout)

        self._api_key: str | None = os.getenv("POLYMARKET_API_KEY")
        self._api_secret: str | None = os.getenv("POLYMARKET_API_SECRET")
        self._wallet_address: str | None = os.getenv("POLYMARKET_WALLET_ADDRESS")

    async def list_markets(
        self,
        closed: bool | None = None,
        limit: int | None = None,
        page_size: int = 100,
        venue_params: dict[str, Any] | None = None,
    ) -> AsyncIterator[Market]:
        """List markets using Gamma API keyset pagination.

        Args:
            closed: Filter by closed status (None = all)
            limit: Maximum total markets to return (None = unlimited)
            page_size: Results per API request (1-100)
            venue_params: Venue-specific parameters as a dict (optional, unused by Polymarket)

        Yields:
            Market objects (up to `limit` total)

        Raises:
            ValueError: If API returns invalid data
        """
        url = f"{self.GAMMA_API_BASE}/markets/keyset"
        params: dict[str, Any] = {"limit": min(page_size, 100)}

        if closed is not None:
            params["closed"] = str(closed).lower()

        cursor: str | None = None
        yielded = 0

        while True:
            if cursor:
                params["after_cursor"] = cursor

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
                except (KeyError, ValueError, TypeError):
                    try:
                        partial_market = self._parse_partial_market(market_data)
                        yield partial_market
                        yielded += 1
                    except Exception:
                        import logging

                        logging.warning(
                            f"Skipping market due to parse error: {market_data.get('id', 'unknown')}"
                        )
                        continue

            cursor = data.get("nextCursor")
            if not cursor:
                break

    async def get_market(self, market_id: str) -> Market:
        """Get a specific market by condition_id.

        Args:
            market_id: Polymarket condition_id

        Returns:
            Market object

        Raises:
            ValueError: If market not found or invalid data
        """
        url = f"{self.GAMMA_API_BASE}/markets"
        params = {"condition_ids": market_id, "limit": 1}

        try:
            response = await self._http_client.get(url, params=params)  # type: ignore[arg-type]
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ValueError(f"Failed to fetch market {market_id}: {e}") from e

        markets = data.get("markets", [])
        if not markets:
            raise ValueError(f"Market {market_id} not found")

        return self._parse_market(markets[0])

    async def get_order_book(self, outcome_id: str) -> OrderBook:
        """Get order book for an outcome (token_id).

        Args:
            outcome_id: Polymarket token_id (asset_id)

        Returns:
            OrderBook with sorted and validated levels

        Raises:
            ValueError: If outcome not found or book is crossed
        """
        url = f"{self.CLOB_API_BASE}/book"
        params = {"token_id": outcome_id}

        try:
            response = await self._http_client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ValueError(f"Failed to fetch order book for {outcome_id}: {e}") from e

        order_book = self._parse_order_book(outcome_id, data)
        order_book.validate_not_crossed()

        return order_book

    async def get_price(self, outcome_id: str, side: OrderSide) -> Price:
        """Get best executable price for an outcome and side.

        Contract:
        - get_price(BUY) returns the best ask (what a buyer pays to buy)
        - get_price(SELL) returns the best bid (what a seller receives when selling)

        Important: Polymarket's CLOB API uses 'side' to mean the book side (bid/ask),
        not the user action. So we invert:
        - To get the price a user pays to BUY, query side=SELL (the ask side)
        - To get the price a user receives when SELLing, query side=BUY (the bid side)

        Args:
            outcome_id: Polymarket token_id
            side: OrderSide.BUY (user buying) or OrderSide.SELL (user selling)

        Returns:
            Price object with best executable price

        Raises:
            ValueError: If outcome not found or no liquidity
        """
        url = f"{self.CLOB_API_BASE}/price"

        # Invert the side: user BUY -> query SELL side (ask), user SELL -> query BUY side (bid)
        clob_side = "SELL" if side == OrderSide.BUY else "BUY"

        params = {
            "token_id": outcome_id,
            "side": clob_side,
        }

        try:
            response = await self._http_client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ValueError(f"Failed to fetch price for {outcome_id}: {e}") from e

        raw_price = data.get("price")
        if raw_price is None:
            raise ValueError(f"No liquidity for {side.value} on outcome {outcome_id}")

        normalized_price = Decimal(str(raw_price))

        return Price(
            outcome_id=outcome_id,
            market_id="",
            side=side,
            price=normalized_price,
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

        Uses Data API v2 /trades endpoint.

        Args:
            outcome_id: Polymarket token_id
            limit: Results per page
            **kwargs: Additional filters (start_ts, end_ts, etc.)

        Yields:
            Trade objects
        """
        url = f"{self.DATA_API_BASE}/trades"
        params: dict[str, Any] = {
            "asset_id": outcome_id,
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

            trades = data.get("data", [])
            if not trades:
                break

            for trade_data in trades:
                try:
                    trade = self._parse_trade(outcome_id, trade_data)
                    yield trade
                except (KeyError, ValueError):
                    continue

            cursor = data.get("next_cursor")
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
            outcome_id: Polymarket token_id
            side: OrderSide.BUY or OrderSide.SELL
            size: Order size
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

        if not all([self._api_key, self._api_secret, self._wallet_address]):
            raise PermissionError(
                "Trading requires API credentials via environment variables: "
                "POLYMARKET_API_KEY, POLYMARKET_API_SECRET, POLYMARKET_WALLET_ADDRESS"
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
        """Parse Gamma API market data into Market object."""
        import json

        condition_id = data.get("conditionId", "")
        question = data.get("question", "")
        description = data.get("description")

        created_at_str = data.get("createdAt") or data.get("startDate")
        if created_at_str:
            created_at = parser.isoparse(created_at_str)
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
        else:
            created_at = datetime.now(timezone.utc)

        end_date = None
        end_date_str = data.get("endDate")
        if end_date_str:
            end_date = parser.isoparse(end_date_str)
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)

        resolved = data.get("closed", False)

        volume = None
        if "volumeNum" in data:
            volume = Decimal(str(data["volumeNum"]))
        elif "volume" in data:
            try:
                volume = Decimal(str(data["volume"]))
            except (ValueError, TypeError):
                pass

        # Polymarket provides liquidityNum as an actual liquidity metric
        liquidity = None
        if "liquidityNum" in data:
            liquidity = Decimal(str(data["liquidityNum"]))
        elif "liquidity" in data:
            try:
                liquidity = Decimal(str(data["liquidity"]))
            except (ValueError, TypeError):
                pass

        # Polymarket doesn't provide open_interest, best_bid_size, or best_ask_size
        # at the market level (available per-token via CLOB API)
        open_interest = None
        best_bid_size = None
        best_ask_size = None

        outcomes: list[Outcome] = []

        outcomes_str = data.get("outcomes", "[]")
        outcome_prices_str = data.get("outcomePrices", "[]")
        token_ids_str = data.get("clobTokenIds", "[]")

        try:
            outcome_names = (
                json.loads(outcomes_str) if isinstance(outcomes_str, str) else outcomes_str
            )
            outcome_prices = (
                json.loads(outcome_prices_str)
                if isinstance(outcome_prices_str, str)
                else outcome_prices_str
            )
            token_ids = (
                json.loads(token_ids_str) if isinstance(token_ids_str, str) else token_ids_str
            )
        except (json.JSONDecodeError, ValueError):
            outcome_names = []
            outcome_prices = []
            token_ids = []

        for i, outcome_name in enumerate(outcome_names):
            token_id = token_ids[i] if i < len(token_ids) else ""
            raw_price = outcome_prices[i] if i < len(outcome_prices) else None

            normalized_price = None
            if raw_price is not None:
                try:
                    normalized_price = Decimal(str(raw_price))
                except (ValueError, TypeError):
                    pass

            outcomes.append(
                Outcome(
                    id=token_id,
                    market_id=condition_id,
                    name=outcome_name,
                    price=normalized_price,
                    raw_price=raw_price,
                )
            )

        return Market(
            id=condition_id,
            venue="polymarket",
            question=question,
            description=description,
            outcomes=outcomes,
            created_at=created_at,
            end_date=end_date,
            resolved=resolved,
            volume=volume,
            open_interest=open_interest,
            best_bid_size=best_bid_size,
            best_ask_size=best_ask_size,
            liquidity=liquidity,
            currency=Currency.USDC,
            raw_data=data,
            partial=False,
        )

    def _parse_partial_market(self, data: dict[str, Any]) -> Market:
        """Parse partial market data when full parsing fails."""
        condition_id = data.get("conditionId", data.get("id", "unknown"))
        question = data.get("question", "Unknown market")

        try:
            created_at_str = data.get("createdAt") or data.get("startDate")
            if created_at_str:
                created_at = parser.isoparse(created_at_str)
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
            else:
                created_at = datetime.now(timezone.utc)
        except (ValueError, TypeError):
            created_at = datetime.now(timezone.utc)

        return Market(
            id=str(condition_id),
            venue="polymarket",
            question=question,
            description=data.get("description"),
            outcomes=[],
            created_at=created_at,
            end_date=None,
            resolved=False,
            volume=None,
            open_interest=None,
            best_bid_size=None,
            best_ask_size=None,
            liquidity=None,
            currency=Currency.USDC,
            raw_data=data,
            partial=True,
        )

    def _parse_order_book(self, outcome_id: str, data: dict[str, Any]) -> OrderBook:
        """Parse CLOB API order book data."""
        market_id = data.get("market", "")

        timestamp_str = data.get("timestamp")
        if timestamp_str:
            try:
                timestamp = datetime.fromtimestamp(float(timestamp_str), tz=timezone.utc)
            except (ValueError, TypeError):
                timestamp = datetime.now(timezone.utc)
        else:
            timestamp = datetime.now(timezone.utc)

        bids = []
        for bid_data in data.get("bids", []):
            price_str = bid_data.get("price")
            size_str = bid_data.get("size")
            if price_str and size_str:
                bids.append(
                    OrderLevel(
                        price=Decimal(price_str),
                        size=Decimal(size_str),
                        raw_price=price_str,
                    )
                )

        bids.sort(key=lambda x: x.price, reverse=True)

        asks = []
        for ask_data in data.get("asks", []):
            price_str = ask_data.get("price")
            size_str = ask_data.get("size")
            if price_str and size_str:
                asks.append(
                    OrderLevel(
                        price=Decimal(price_str),
                        size=Decimal(size_str),
                        raw_price=price_str,
                    )
                )

        asks.sort(key=lambda x: x.price)

        last_trade_price = None
        last_trade_str = data.get("last_trade_price")
        if last_trade_str:
            try:
                last_trade_price = Decimal(last_trade_str)
            except (ValueError, TypeError):
                pass

        min_order_size = None
        min_order_str = data.get("min_order_size")
        if min_order_str:
            try:
                min_order_size = Decimal(min_order_str)
            except (ValueError, TypeError):
                pass

        tick_size = None
        tick_str = data.get("tick_size")
        if tick_str:
            try:
                tick_size = Decimal(tick_str)
            except (ValueError, TypeError):
                pass

        return OrderBook(
            outcome_id=outcome_id,
            market_id=market_id,
            bids=bids,
            asks=asks,
            timestamp=timestamp,
            last_trade_price=last_trade_price,
            min_order_size=min_order_size,
            tick_size=tick_size,
            currency=Currency.USDC,
            partial=False,
        )

    def _parse_trade(self, outcome_id: str, data: dict[str, Any]) -> Trade:
        """Parse Data API v2 trade data."""
        trade_id = str(data.get("proxy_wallet", ""))
        market_id = data.get("condition_id", "")

        side_str = data.get("side", "BUY")
        side = OrderSide.BUY if side_str.upper() == "BUY" else OrderSide.SELL

        price_val = data.get("price", 0)
        price = Decimal(str(price_val))

        size_val = data.get("size", 0)
        size = Decimal(str(size_val))

        timestamp_val = data.get("timestamp")
        if timestamp_val:
            try:
                if isinstance(timestamp_val, (int, float)):
                    timestamp = datetime.fromtimestamp(timestamp_val, tz=timezone.utc)
                else:
                    timestamp = parser.isoparse(str(timestamp_val))
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                timestamp = datetime.now(timezone.utc)
        else:
            timestamp = datetime.now(timezone.utc)

        return Trade(
            id=trade_id,
            market_id=market_id,
            outcome_id=outcome_id,
            side=side,
            price=price,
            size=size,
            timestamp=timestamp,
            currency=Currency.USDC,
            raw_data=data,
        )
