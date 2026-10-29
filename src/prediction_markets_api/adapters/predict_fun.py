"""Read-only Predict.fun adapter (BNB Chain).

Docs: https://dev.predict.fun/ and the live OpenAPI at
https://api.predict.fun/docs. Terms: https://docs.predict.fun/terms-of-service.
Mainnet keys are issued at https://developers.predict.fun.

Hosts:

- Mainnet ``https://api.predict.fun`` (BNB mainnet). Every endpoint requires
  the ``x-api-key`` header. The key is read from ``PREDICTFUN_API_KEY`` only.
  It is never hardcoded, logged, or included in ``repr``. A missing key raises
  ``AuthenticationError`` before the request. HTTP 401 raises the same error.
- Testnet ``https://api-testnet.predict.fun``. No API key. Pass this origin as
  ``base_url``. JWT bearer auth is for order actions and is not used here.

Endpoints used:

- ``GET /v1/markets`` — cursor page (``first``, ``after``). The server default
  for ``first`` is 20. Values above 100 are silently clamped to 100. The docs
  do not publish a maximum. This client sends at most 100. ``status`` is
  ``OPEN`` or ``RESOLVED``.
- ``GET /v1/markets/{id}`` — one market.
- ``GET /v1/markets/{id}/orderbook`` — bids and asks for outcome ``indexSet`` 1.
  The endpoint has no ``indexSet`` parameter. Levels are ``[price, size]``.
  Prices are probabilities in ``[0, 1]``. Outcome names are arbitrary.
- ``GET /v1/orders/matches`` — public match events for ``marketId``, newest
  first, cursor page (``first``, ``after``). The server default for ``first``
  is 50 and values above 150 are clamped to 150. This client sends 100. This
  is the trades read. It does not require a JWT. An unknown market is an empty
  page, not a 404; this client then calls ``get_market`` and raises
  ``NotFoundError`` when that 404s. A full outcome id is checked with
  ``get_market`` before this request.

``decimalPrecision`` is the number of price decimal places. The tick is
``10 ** -decimalPrecision`` (2 → ``0.01``, 3 → ``0.001`` on testnet). The book
payload does not include it, so ``OrderBook.tick_size`` is left unset.
``spreadThreshold`` and ``shareThreshold`` are reward thresholds, not the tick
and not ``min_order_size``.

Match ``price`` / ``priceExecuted`` and ``amount`` / ``amountFilled`` are
18-decimal integer strings (wei). Collateral is USDT. Divide by ``10**18`` to
get a probability or a share count. Book sizes are already share counts.
``stats.volumeTotalUsd`` and ``stats.totalLiquidityUsd`` are USD notionals.
``Market.volume`` uses ``volumeTotalUsd``. ``totalLiquidityUsd`` stays on
``raw_data["stats"]`` only: ``Market.liquidity`` is ``None`` because the
registry maps a top-level raw field and this one is nested.

``Outcome.price`` is the mid of ``bestBid`` and ``bestAsk`` when both exist.
Those two fields on ``outcomes[]`` are deprecated by the venue and are still
the only prices on the market payload. A one-sided or empty book is
``price=None`` (same rule as Pascal). A resolved market uses ``1`` for the
winning outcome and ``0`` for the others.

The order book is denominated in ``indexSet`` 1. ``indexSet`` 2 is the
complement (``1 - price``, sides swapped), including when the names are not
Yes and No. Outcome ids are ``{market_id}:{index_set}:{on_chain_id}``.
``get_order_book`` / ``get_trades`` also accept the bare market id, which
addresses the indexSet-1 book and every match on that market.

Rate limits are per API key. ``/v1/markets`` and ``/v1/markets/{id}`` allow
240 requests per minute. ``/v1/markets/{id}/orderbook``, ``/quote``, and
``/v1/orders/matches`` share a separate bucket of 500 requests per minute
(testnet ``ratelimit-policy`` headers, observed 2026-10-29). HTTP 429 maps to
``RateLimitError``. ``Retry-After`` wins, then ``ratelimit-reset`` (seconds).

Trading, websockets, and account reads are not implemented. ``place_order``
validates a dry-run and does not submit.
"""

import logging
import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from prediction_markets_api.adapters.base import BaseClient, validate_limit
from prediction_markets_api.exceptions import (
    AuthenticationError,
    NoLiquidityError,
    NotFoundError,
    RateLimitError,
    VenueAPIError,
)
from prediction_markets_api.models.base import (
    Currency,
    Market,
    MarketStatus,
    OrderBook,
    OrderLevel,
    OrderSide,
    Outcome,
    Price,
    Trade,
)
from prediction_markets_api.utils import parse_timestamp

logger = logging.getLogger(__name__)

_PAGE_MAX = 100
# A new order cannot be placed in these trading states, so they are CLOSED.
_CLOSED_TRADING = frozenset({"CLOSED", "PAUSED", "CANCEL_ONLY", "MATCHING_NOT_ENABLED"})
_CLOSED_LIFECYCLE = frozenset({"REMOVED", "PAUSED"})
_WEI = Decimal(10) ** 18
_MAINNET_HOST = "api.predict.fun"
_VENUE = "predict_fun"
_API_KEY_HEADER = "x-api-key"
_API_KEY_ENV = "PREDICTFUN_API_KEY"
_PARAM_KEYS = frozenset({"sort", "market_variant", "tag_ids"})


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _in_unit_interval(value: Decimal | None) -> Decimal | None:
    if value is None or value < Decimal("0") or value > Decimal("1"):
        return None
    return value


def _non_negative(value: Any) -> Decimal | None:
    number = _decimal(value)
    if number is None or number < 0:
        return None
    return number


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_outcome_id(outcome_id: str) -> tuple[str, int | None, str | None]:
    """Split an outcome id.

    ``{market_id}`` is the indexSet-1 book and every match on that market.
    ``{market_id}:{index_set}:{on_chain_id}`` selects one outcome. ``index_set``
    1 is the book as published. ``index_set`` 2 is the complement.
    """
    if not outcome_id or not outcome_id.strip():
        raise ValueError("outcome id must not be empty")
    parts = outcome_id.split(":")
    if len(parts) == 1:
        return outcome_id, None, None
    if len(parts) != 3:
        raise ValueError(
            "outcome id must be '{market_id}' or '{market_id}:{index_set}:{on_chain_id}', "
            f"got {outcome_id!r}"
        )
    market_id, index_raw, on_chain = parts
    if not market_id or not index_raw.isdigit() or not on_chain:
        raise ValueError(
            "outcome id must be '{market_id}' or '{market_id}:{index_set}:{on_chain_id}', "
            f"got {outcome_id!r}"
        )
    return market_id, int(index_raw), on_chain


def _level_price(level: Any) -> tuple[Decimal | None, Decimal | None]:
    """Return ``(price, size)`` for a ``{price, size}`` object, or Nones."""
    if not isinstance(level, dict):
        return None, None
    price = _in_unit_interval(_decimal(level.get("price")))
    size = _decimal(level.get("size"))
    if size is not None and size < 0:
        size = None
    return price, size


def _outcome_quote(
    raw: dict[str, Any], *, resolved: bool, winning: str | None
) -> tuple[Decimal | None, str | None]:
    """Probability for one outcome.

    Resolution wins: the winning outcome is ``1`` and every other outcome is
    ``0``. Otherwise the mid of a two-sided top of book. One side, or neither,
    is ``None``.
    """
    name = raw.get("name")
    if resolved:
        if isinstance(winning, str) and isinstance(name, str) and name == winning:
            return Decimal("1"), "1"
        if isinstance(winning, str):
            return Decimal("0"), "0"
        if raw.get("status") == "WON":
            return Decimal("1"), "1"
        if raw.get("status") == "LOST":
            return Decimal("0"), "0"
        return None, None
    # outcomes[].bestBid and bestAsk are deprecated. The market payload still
    # has no other price. A missing field leaves this outcome at price=None.
    bid, _bid_size = _level_price(raw.get("bestBid"))
    ask, _ask_size = _level_price(raw.get("bestAsk"))
    if bid is None or ask is None:
        return None, None
    mid = (bid + ask) / Decimal("2")
    return _in_unit_interval(mid), None if _in_unit_interval(mid) is None else str(mid)


def _accepted_winner(raw: dict[str, Any], outcomes_raw: object) -> str | None:
    """Winning outcome name, or None when it does not match an outcome."""
    winning = _winning_name(raw)
    if winning is None or not isinstance(outcomes_raw, list):
        return None
    for outcome in outcomes_raw:
        if isinstance(outcome, dict) and outcome.get("name") == winning:
            return winning
    return None


def _winning_name(raw: dict[str, Any]) -> str | None:
    resolution = raw.get("resolution")
    if isinstance(resolution, dict):
        name = resolution.get("name")
        if isinstance(name, str) and name:
            return name
    outcomes = raw.get("outcomes")
    if isinstance(outcomes, list):
        for outcome in outcomes:
            if isinstance(outcome, dict) and outcome.get("status") == "WON":
                name = outcome.get("name")
                if isinstance(name, str) and name:
                    return name
    return None


def _status(raw: dict[str, Any]) -> tuple[MarketStatus, bool, str]:
    """Map venue status onto the shared enum.

    ``OPEN`` means a new order can be placed. Lifecycle ``PAUSED`` and
    ``REMOVED``, and ``tradingStatus`` ``PAUSED``, ``CANCEL_ONLY``,
    ``MATCHING_NOT_ENABLED``, and ``CLOSED``, are ``CLOSED``.
    ``PRICE_PROPOSED`` and ``PRICE_DISPUTED`` stay ``OPEN`` when
    ``tradingStatus`` is ``OPEN``. ``venue_status`` keeps the raw lifecycle
    ``status`` when the venue sent one. Both fields stay on ``raw_data``.
    """
    lifecycle = raw.get("status")
    trading = raw.get("tradingStatus")
    if isinstance(lifecycle, str) and lifecycle:
        venue_status = lifecycle
    elif isinstance(trading, str) and trading:
        venue_status = trading
    else:
        venue_status = "unknown"
    if lifecycle == "RESOLVED":
        return MarketStatus.RESOLVED, True, venue_status
    if lifecycle in _CLOSED_LIFECYCLE or trading in _CLOSED_TRADING:
        return MarketStatus.CLOSED, False, venue_status
    return MarketStatus.OPEN, False, venue_status


def _parse_market(raw: dict[str, Any]) -> Market | None:
    market_raw_id = raw.get("id")
    if market_raw_id is None:
        return None
    market_id = str(market_raw_id)
    if not market_id:
        return None
    status, resolved, venue_status = _status(raw)
    outcomes_raw = raw.get("outcomes")
    winning = _accepted_winner(raw, outcomes_raw) if resolved else None
    outcomes: list[Outcome] = []
    primary_bid: Decimal | None = None
    primary_ask: Decimal | None = None
    if isinstance(outcomes_raw, list):
        for outcome in outcomes_raw:
            if not isinstance(outcome, dict):
                continue
            name = outcome.get("name")
            on_chain = outcome.get("onChainId")
            index_set = outcome.get("indexSet")
            if not isinstance(name, str) or not name:
                continue
            if not isinstance(on_chain, str) or not on_chain or not isinstance(index_set, int):
                continue
            price, raw_price = _outcome_quote(outcome, resolved=resolved, winning=winning)
            outcomes.append(
                Outcome(
                    id=f"{market_id}:{index_set}:{on_chain}",
                    market_id=market_id,
                    name=name,
                    price=price,
                    raw_price=raw_price,
                )
            )
            if index_set == 1:
                primary_bid = _level_price(outcome.get("bestBid"))[1]
                primary_ask = _level_price(outcome.get("bestAsk"))[1]
    question = raw.get("question")
    if not isinstance(question, str) or not question.strip():
        title = raw.get("title")
        question = title.strip() if isinstance(title, str) and title.strip() else market_id
    else:
        question = question.strip()
    description = raw.get("description")
    created = _parse_iso(raw.get("createdAt"))
    partial = created is None
    if created is None:
        created = datetime.now(timezone.utc)
    stats = raw.get("stats")
    volume = None
    if isinstance(stats, dict):
        volume = _non_negative(stats.get("volumeTotalUsd"))
    return Market(
        id=market_id,
        venue=_VENUE,
        question=question,
        description=description if isinstance(description, str) and description else None,
        outcomes=outcomes,
        created_at=created,
        end_date=None,
        resolved=resolved,
        venue_status=venue_status,
        status=status,
        winning_outcome=winning,
        volume=volume,
        open_interest=None,
        best_bid_size=primary_bid if primary_bid is not None and primary_bid >= 0 else None,
        best_ask_size=primary_ask if primary_ask is not None and primary_ask >= 0 else None,
        liquidity=None,
        currency=Currency.USD,
        raw_data=raw,
        partial=partial,
    )


def _parse_levels(raw: object) -> list[OrderLevel]:
    if not isinstance(raw, list):
        return []
    levels: list[OrderLevel] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        price = _in_unit_interval(_decimal(item[0]))
        size = _decimal(item[1])
        if price is None or size is None or size <= 0:
            continue
        original = item[0]
        levels.append(
            OrderLevel(
                price=price,
                size=size,
                raw_price=original if isinstance(original, str) else str(price),
            )
        )
    return levels


def _invert_levels(levels: list[OrderLevel]) -> list[OrderLevel]:
    inverted: list[OrderLevel] = []
    for level in levels:
        price = Decimal("1") - level.price
        if price < Decimal("0") or price > Decimal("1"):
            continue
        inverted.append(OrderLevel(price=price, size=level.size, raw_price=str(price)))
    return inverted


def _from_wei(value: Any) -> Decimal | None:
    """Scale an 18-decimal wei string down to a human unit."""
    number = _decimal(value)
    if number is None:
        return None
    return number / _WEI


def _message(body: object, status_code: int) -> str:
    if isinstance(body, dict):
        message = body.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        error = body.get("error")
        if isinstance(error, str) and error.strip():
            return error.strip()
    return f"Predict.fun API request failed (HTTP {status_code})"


def _retry_after(response: httpx.Response) -> int | None:
    for header in ("Retry-After", "ratelimit-reset"):
        raw = response.headers.get(header)
        if raw is not None and raw.isdigit():
            return int(raw)
    return None


def _cursor(body: dict[str, Any]) -> str | None:
    cursor = body.get("cursor")
    if isinstance(cursor, str) and cursor:
        return cursor
    return None


def _data_list(body: object, url: str, what: str) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(body, dict):
        raise VenueAPIError(f"Invalid Predict.fun {what} payload", venue=_VENUE, url=url)
    data = body.get("data")
    if not isinstance(data, list):
        raise VenueAPIError(f"Invalid Predict.fun {what} payload", venue=_VENUE, url=url)
    items = [item for item in data if isinstance(item, dict)]
    return items, _cursor(body)


def _parse_trade(outcome_id: str, market_id: str, data: dict[str, Any]) -> Trade:
    nested = data.get("market")
    trade_market = market_id
    if isinstance(nested, dict) and nested.get("id") is not None:
        trade_market = str(nested["id"])
        if trade_market != market_id:
            raise ValueError("trade is for a different market")
    taker = data.get("taker")
    if not isinstance(taker, dict):
        raise ValueError("trade taker missing")
    quote = taker.get("quoteType")
    if quote == "Bid":
        side = OrderSide.BUY
    elif quote == "Ask":
        side = OrderSide.SELL
    else:
        raise ValueError(f"unknown quote type: {quote!r}")
    price = _in_unit_interval(_from_wei(data.get("priceExecuted", taker.get("price"))))
    size = _from_wei(data.get("amountFilled", taker.get("amount")))
    if price is None or size is None or size <= 0:
        raise ValueError("trade price or size missing")
    timestamp = _parse_iso(data.get("executedAt"))
    if timestamp is None:
        raise ValueError("trade time missing")
    trade_hash = data.get("transactionHash")
    taker_hash = taker.get("hash")
    if isinstance(trade_hash, str) and trade_hash:
        trade_id = trade_hash
    elif isinstance(taker_hash, str) and taker_hash:
        trade_id = taker_hash
    else:
        raise ValueError("trade id missing")
    outcome = taker.get("outcome")
    trade_outcome = outcome_id
    if isinstance(outcome, dict):
        index_set = outcome.get("indexSet")
        on_chain = outcome.get("onChainId")
        if isinstance(index_set, int) and isinstance(on_chain, str) and on_chain:
            trade_outcome = f"{trade_market}:{index_set}:{on_chain}"
    return Trade(
        id=trade_id,
        market_id=trade_market,
        outcome_id=trade_outcome,
        side=side,
        price=price,
        size=size,
        timestamp=timestamp,
        currency=Currency.USD,
        raw_data=data,
    )


class PredictFunClient(BaseClient):
    """Read-only Predict.fun client.

    Mainnet reads require ``PREDICTFUN_API_KEY``. Testnet reads do not: pass
    ``base_url=PredictFunClient.TESTNET_API_BASE``. ``place_order`` is a
    dry-run stub and does not submit.
    """

    API_BASE = "https://api.predict.fun"
    TESTNET_API_BASE = "https://api-testnet.predict.fun"

    def __init__(
        self,
        max_order_size: Decimal | None = None,
        timeout: float = 30.0,
        base_url: str | None = None,
    ):
        """Initialize the Predict.fun client.

        Args:
            max_order_size: Maximum order size accepted by ``place_order``.
            timeout: HTTP timeout in seconds.
            base_url: API origin. Defaults to mainnet. Use
                ``TESTNET_API_BASE`` for the public testnet.
        """
        self._max_order_size = max_order_size
        self._timeout = timeout
        self._base_url = (base_url or self.API_BASE).rstrip("/")
        self._http_client = httpx.AsyncClient(timeout=timeout)
        raw_key = os.getenv(_API_KEY_ENV)
        # Whitespace-only is treated as unset. The value is never logged.
        self._api_key = raw_key.strip() if isinstance(raw_key, str) and raw_key.strip() else None

    def __repr__(self) -> str:
        masked = "***" if self._api_key else None
        return f"PredictFunClient(base_url={self._base_url!r}, api_key={masked!r})"

    async def list_markets(
        self,
        closed: bool | None = None,
        limit: int | None = None,
        page_size: int = 100,
        venue_params: dict[str, Any] | None = None,
    ) -> AsyncIterator[Market]:
        """List markets from ``GET /v1/markets``.

        ``closed=False`` sends ``status=OPEN`` and then drops markets whose
        normalized status is not ``OPEN``. The venue filter also returns
        markets where a new order cannot be placed. ``closed=True`` sends
        ``status=RESOLVED``. ``closed=None`` omits ``status``. Pages use
        ``first`` and ``after``. The server default for ``first`` is 20 and
        values above 100 are silently clamped to 100. This client sends at
        most 100. ``limit`` stops the iterator before the next page. A 5xx
        on a later page raises ``VenueAPIError`` with that page number and
        cursor instead of ending the listing.

        ``venue_params``:

        - ``sort``: a ``MarketSort`` value, sent as ``sort``.
        - ``market_variant``: a ``MarketVariant`` value, sent as ``marketVariant``.
        - ``tag_ids``: a string or a list of strings, sent as ``tagIds``.

        Raises:
            ValueError: If ``limit`` or ``page_size`` is not positive, or
                ``venue_params`` has an unsupported key.
            AuthenticationError: If this is mainnet and ``PREDICTFUN_API_KEY``
                is unset, or the key is rejected.
            RateLimitError: If the venue returns HTTP 429.
            VenueAPIError: If a read request fails for another reason.
        """
        validate_limit(limit)
        if page_size <= 0:
            raise ValueError(f"page_size must be positive, got {page_size}")
        params = self._list_params(closed, venue_params)
        yielded = 0
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_ids: set[str] = set()
        page_number = 0
        while True:
            if limit is not None and yielded >= limit:
                return
            remaining = None if limit is None else limit - yielded
            page = page_size if remaining is None else min(page_size, remaining)
            page = min(page, _PAGE_MAX)
            page_number += 1
            query = dict(params)
            query["first"] = str(page)
            if cursor is not None:
                query["after"] = cursor
            url = f"{self._base_url}/v1/markets"
            try:
                body = await self._request("GET", "/v1/markets", params=query)
            except VenueAPIError as exc:
                if exc.status_code is not None and exc.status_code >= 500:
                    shown = cursor if cursor is not None else "none"
                    raise VenueAPIError(
                        "Predict.fun list_markets failed on "
                        f"page {page_number} at cursor {shown}: {exc}",
                        status_code=exc.status_code,
                        venue=exc.venue,
                        url=exc.url,
                    ) from exc
                raise
            raw_markets, next_cursor = _data_list(body, url, "markets")
            if next_cursor is not None and next_cursor in seen_cursors:
                logger.warning(
                    "Stopping Predict.fun market pagination: cursor %s repeated",
                    next_cursor,
                )
                return
            if not raw_markets:
                return
            for raw in raw_markets:
                if limit is not None and yielded >= limit:
                    return
                market = self._one_market(raw)
                if market is None or market.id in seen_ids:
                    continue
                if closed is False and market.status != MarketStatus.OPEN:
                    continue
                if closed is True and market.status != MarketStatus.RESOLVED:
                    continue
                seen_ids.add(market.id)
                yield market
                yielded += 1
            if limit is not None and yielded >= limit:
                return
            if not next_cursor:
                return
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    async def get_market(self, market_id: str) -> Market:
        """Get one market by id from ``GET /v1/markets/{id}``.

        Raises:
            NotFoundError: If the id is empty or the venue returns 404.
            AuthenticationError: If mainnet credentials are missing or rejected.
            VenueAPIError: If the request fails for another reason.
        """
        if not market_id:
            raise NotFoundError(
                "Market not found: empty id",
                resource_type="market",
                resource_id=market_id,
            )
        path = f"/v1/markets/{quote(market_id, safe='')}"
        url = f"{self._base_url}{path}"
        try:
            body = await self._request("GET", path)
        except NotFoundError as exc:
            raise NotFoundError(
                f"Market not found: {market_id}",
                resource_type="market",
                resource_id=market_id,
            ) from exc
        if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            raise VenueAPIError("Invalid Predict.fun market payload", venue=_VENUE, url=url)
        market = self._one_market(body["data"])
        if market is None or market.id != market_id:
            raise NotFoundError(
                f"Market not found: {market_id}",
                resource_type="market",
                resource_id=market_id,
            )
        return market

    async def get_order_book(self, outcome_id: str) -> OrderBook:
        """Get ``GET /v1/markets/{id}/orderbook`` for one outcome.

        The endpoint has no ``indexSet`` parameter. It always returns the
        indexSet-1 book. A bare market id and ``indexSet`` 1 use that book.
        ``indexSet`` 2 inverts it (``1 - price``, sides swapped). Outcome names
        are arbitrary. Any other index has no book on this endpoint. A full
        outcome id is checked against ``get_market`` before the book request.
        An empty book is returned as-is. A crossed book raises ``ValueError``.

        Raises:
            NotFoundError: If the market or outcome has no book, or
                ``onChainId`` is not on the market.
            ValueError: If ``outcome_id`` is malformed or the book is crossed.
            AuthenticationError: If mainnet credentials are missing or rejected.
            VenueAPIError: If the request fails for another reason.
        """
        market_id, index_set, on_chain = _parse_outcome_id(outcome_id)
        if index_set not in (None, 1, 2):
            raise NotFoundError(
                f"Order book is only published for indexSet 1 and its complement "
                f"(indexSet 2), got indexSet {index_set}",
                resource_type="outcome",
                resource_id=outcome_id,
            )
        if on_chain is not None:
            await self._require_known_outcome(outcome_id)
        path = f"/v1/markets/{quote(market_id, safe='')}/orderbook"
        url = f"{self._base_url}{path}"
        try:
            body = await self._request("GET", path)
        except NotFoundError as exc:
            raise NotFoundError(
                f"Order book not found for outcome: {outcome_id}",
                resource_type="outcome",
                resource_id=outcome_id,
            ) from exc
        if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            raise VenueAPIError("Invalid Predict.fun order book payload", venue=_VENUE, url=url)
        book = _order_book(outcome_id, market_id, index_set == 2, body["data"])
        book.validate_not_crossed()
        return book

    async def get_price(self, outcome_id: str, side: OrderSide) -> Price:
        """Best ask for BUY and best bid for SELL.

        An empty side raises ``NoLiquidityError``. An unknown market raises
        ``NotFoundError`` from the order book request.
        """
        book = await self.get_order_book(outcome_id)
        if side == OrderSide.BUY:
            levels = book.asks
            empty_side = "BUY"
            message = f"No liquidity to buy (no asks) for {outcome_id}"
        else:
            levels = book.bids
            empty_side = "SELL"
            message = f"No liquidity to sell (no bids) for {outcome_id}"
        if not levels:
            raise NoLiquidityError(message, market_id=book.market_id, side=empty_side)
        level = levels[0]
        return Price(
            outcome_id=book.outcome_id,
            market_id=book.market_id,
            side=side,
            price=level.price,
            raw_price=level.raw_price,
            timestamp=book.timestamp,
            currency=book.currency,
        )

    async def get_trades(
        self,
        outcome_id: str,
        limit: int = 100,
    ) -> AsyncIterator[Trade]:
        """Yield up to ``limit`` public matches for one market or outcome.

        ``GET /v1/orders/matches`` is filtered by ``marketId``. A full outcome
        id is checked with ``get_market`` before that request. Unknown
        ``onChainId`` values raise ``NotFoundError`` and no match page is
        fetched. Matching prints are then filtered on the client, so each page
        requests ``first=100`` (the endpoint defaults to 50 and clamps to 150).
        Prices and sizes are converted from 18-decimal wei. ``quoteType``
        ``Bid`` is ``BUY`` and ``Ask`` is ``SELL``.

        Raises:
            ValueError: If ``limit`` is not positive or ``outcome_id`` is malformed.
            NotFoundError: If the market or outcome does not exist.
            AuthenticationError: If mainnet credentials are missing or rejected.
            VenueAPIError: If the request fails for another reason.
        """
        validate_limit(limit)
        async for trade in self._iter_trades(outcome_id, limit):
            yield trade

    async def iter_trades(self, outcome_id: str) -> AsyncIterator[Trade]:
        """Yield every match the matches endpoint will page through.

        A cursor that has already been returned stops the iterator. One warning
        is logged. The same prints are not requested again.
        """
        async for trade in self._iter_trades(outcome_id, None):
            yield trade

    async def place_order(
        self,
        outcome_id: str,
        side: OrderSide,
        size: Decimal,
        price: Decimal,
        dry_run: bool = True,
        **kwargs: object,
    ) -> dict[str, object]:
        """Validate an order without sending it.

        Order placement is not implemented. ``dry_run`` defaults to ``True``
        and performs no HTTP request. ``dry_run=False`` still does not submit
        an order; it requires ``PREDICTFUN_API_KEY`` and returns a
        not-implemented payload. ``kwargs`` is accepted because ``place_order``
        still matches the base signature, which takes ``**kwargs`` until 1.0.
        """
        del kwargs
        if price < Decimal("0") or price > Decimal("1"):
            raise ValueError(f"Price must be between 0 and 1, got {price}")
        if size <= Decimal("0"):
            raise ValueError(f"Size must be positive, got {size}")
        if self._max_order_size is not None and size > self._max_order_size:
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
                "Trading requires API credentials via environment variable: PREDICTFUN_API_KEY"
            )
        return {
            "dry_run": False,
            "status": "not_implemented",
            "message": "Order placement not implemented in this version",
        }

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http_client.aclose()

    def _requires_api_key(self) -> bool:
        host = urlparse(self._base_url).hostname
        return host is not None and host.lower() == _MAINNET_HOST

    def _read_headers(self) -> dict[str, str]:
        """Headers for one read. The key is not logged."""
        if self._requires_api_key() and not self._api_key:
            raise AuthenticationError(
                "Predict.fun mainnet reads require an API key. "
                f"Set the {_API_KEY_ENV} environment variable. "
                f"The key is sent as the {_API_KEY_HEADER} header and is not logged.",
                venue=_VENUE,
                hint=f"Set the {_API_KEY_ENV} environment variable.",
            )
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers[_API_KEY_HEADER] = self._api_key
        return headers

    def _list_params(
        self, closed: bool | None, venue_params: dict[str, Any] | None
    ) -> dict[str, str]:
        options = venue_params or {}
        unknown = set(options) - _PARAM_KEYS
        if unknown:
            raise ValueError(
                f"unsupported venue_params: {', '.join(sorted(unknown))}. "
                "Supported keys: sort, market_variant, tag_ids"
            )
        params: dict[str, str] = {}
        if closed is False:
            params["status"] = "OPEN"
        elif closed is True:
            params["status"] = "RESOLVED"
        sort = options.get("sort")
        if sort is not None:
            if not isinstance(sort, str) or not sort.strip():
                raise ValueError("venue_params['sort'] must be a non-empty string")
            params["sort"] = sort.strip()
        variant = options.get("market_variant")
        if variant is not None:
            if not isinstance(variant, str) or not variant.strip():
                raise ValueError("venue_params['market_variant'] must be a non-empty string")
            params["marketVariant"] = variant.strip()
        if "tag_ids" in options:
            tag_ids = options["tag_ids"]
            if isinstance(tag_ids, str):
                tags = [tag_ids]
            elif isinstance(tag_ids, list) and all(isinstance(tag, str) and tag for tag in tag_ids):
                tags = tag_ids
            else:
                raise ValueError("venue_params['tag_ids'] must be a string or a list of strings")
            if tags:
                params["tagIds"] = ",".join(tags)
        return params

    def _one_market(self, raw: dict[str, Any]) -> Market | None:
        try:
            return _parse_market(raw)
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            logger.warning(
                "Skipping Predict.fun market %s due to parse error: %s", raw.get("id"), exc
            )
            return None

    async def _require_known_outcome(self, outcome_id: str) -> None:
        """Raise ``NotFoundError`` when a full outcome id is not on the market.

        Called before an order book or match request. A bare market id is not
        checked here.
        """
        market_id, _index_set, on_chain = _parse_outcome_id(outcome_id)
        if on_chain is None:
            return
        market = await self.get_market(market_id)
        if any(outcome.id == outcome_id for outcome in market.outcomes):
            return
        raise NotFoundError(
            f"Outcome not found: {outcome_id}",
            resource_type="outcome",
            resource_id=outcome_id,
        )

    async def _iter_trades(self, outcome_id: str, limit: int | None) -> AsyncIterator[Trade]:
        market_id, _index_set, on_chain = _parse_outcome_id(outcome_id)
        if on_chain is not None:
            await self._require_known_outcome(outcome_id)
        cursor: str | None = None
        seen_cursors: set[str] = set()
        yielded = 0
        first_page = True
        while True:
            if limit is not None and yielded >= limit:
                return
            # Outcome filtering is client-side, so a filtered read always asks
            # for a full page instead of shrinking `first` to the remaining limit.
            if on_chain is not None or limit is None:
                page = _PAGE_MAX
            else:
                page = min(_PAGE_MAX, limit - yielded)
            params: dict[str, str] = {"marketId": market_id, "first": str(page)}
            if cursor is not None:
                params["after"] = cursor
            url = f"{self._base_url}/v1/orders/matches"
            body = await self._request("GET", "/v1/orders/matches", params=params)
            items, next_cursor = _data_list(body, url, "trades")
            if next_cursor is not None and next_cursor in seen_cursors:
                logger.warning(
                    "Stopping Predict.fun trade pagination for %s: cursor %s repeated",
                    outcome_id,
                    next_cursor,
                )
                return
            if not items:
                if first_page:
                    await self._ensure_market(market_id)
                return
            first_page = False
            for item in items:
                if limit is not None and yielded >= limit:
                    return
                if not _trade_matches(item, on_chain):
                    continue
                try:
                    trade = _parse_trade(outcome_id, market_id, item)
                except (KeyError, ValueError, TypeError, ArithmeticError):
                    continue
                if on_chain is not None:
                    trade = trade.model_copy(update={"outcome_id": outcome_id})
                yield trade
                yielded += 1
            if limit is not None and yielded >= limit:
                return
            if not next_cursor:
                return
            # A page can contain prints for other outcomes only. Keep paging
            # until the cursor ends or the caller's limit is met.
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    async def _ensure_market(self, market_id: str) -> None:
        """Raise ``NotFoundError`` when an empty match page is an unknown market."""
        await self.get_market(market_id)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> object:
        """Send one read request and map failures onto library errors.

        Request headers are not logged. ``PREDICTFUN_API_KEY`` is not copied
        into the error message.
        """
        url = f"{self._base_url}{path}"
        headers = self._read_headers()
        try:
            response = await self._http_client.request(method, url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise VenueAPIError(
                f"HTTP error calling Predict.fun: {exc}",
                venue=_VENUE,
                url=url,
            ) from exc
        try:
            body: object = response.json()
        except ValueError as exc:
            if response.status_code == 401:
                raise AuthenticationError(
                    _auth_rejected_message(),
                    status_code=response.status_code,
                    venue=_VENUE,
                    hint=f"Check the {_API_KEY_ENV} environment variable.",
                ) from exc
            if response.status_code == 429:
                raise RateLimitError(
                    "Rate limit exceeded",
                    venue=_VENUE,
                    url=str(response.request.url),
                    retry_after=_retry_after(response),
                ) from exc
            raise VenueAPIError(
                f"Invalid JSON from Predict.fun: {exc}",
                status_code=response.status_code,
                venue=_VENUE,
                url=str(response.request.url),
            ) from exc
        code = body.get("code") if isinstance(body, dict) else None
        error = body.get("error") if isinstance(body, dict) else None
        if response.status_code == 401 or code == 401:
            raise AuthenticationError(
                _auth_rejected_message(),
                status_code=401,
                venue=_VENUE,
                hint=f"Check the {_API_KEY_ENV} environment variable.",
            )
        if response.status_code == 404 or code == 404 or error == "not_found":
            raise NotFoundError(
                _message(body, response.status_code),
                resource_type="market",
                resource_id=None,
            )
        if response.status_code == 429 or code == 429:
            raise RateLimitError(
                _message(body, response.status_code),
                venue=_VENUE,
                url=str(response.request.url),
                retry_after=_retry_after(response),
            )
        failed = response.status_code >= 400 or (
            isinstance(body, dict) and body.get("success") is False
        )
        if failed:
            raise VenueAPIError(
                _message(body, response.status_code),
                status_code=response.status_code,
                venue=_VENUE,
                url=str(response.request.url),
            )
        return body


def _auth_rejected_message() -> str:
    return (
        "Predict.fun rejected the API key (HTTP 401). "
        f"Check the {_API_KEY_ENV} environment variable. The key is not logged."
    )


def _trade_matches(item: dict[str, Any], on_chain: str | None) -> bool:
    if on_chain is None:
        return True
    taker = item.get("taker")
    if not isinstance(taker, dict):
        return False
    outcome = taker.get("outcome")
    if not isinstance(outcome, dict):
        return False
    return outcome.get("onChainId") == on_chain


def _order_book(
    outcome_id: str,
    market_id: str,
    invert: bool,
    book: dict[str, Any],
) -> OrderBook:
    bids = _parse_levels(book.get("bids"))
    asks = _parse_levels(book.get("asks"))
    if invert:
        bids, asks = _invert_levels(asks), _invert_levels(bids)
    bids.sort(key=lambda level: level.price, reverse=True)
    asks.sort(key=lambda level: level.price)
    timestamp = None
    raw_ts = book.get("updateTimestampMs")
    if isinstance(raw_ts, (int, str)) and str(raw_ts).isdigit():
        try:
            timestamp = parse_timestamp(str(raw_ts), is_milliseconds=True)
        except ValueError:
            timestamp = None
    partial = timestamp is None
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)
    return OrderBook(
        outcome_id=outcome_id,
        market_id=market_id,
        bids=bids,
        asks=asks,
        timestamp=timestamp,
        last_trade_price=None,
        min_order_size=None,
        tick_size=None,
        currency=Currency.USD,
        partial=partial,
    )
