"""Read-only Pascal adapter (Solana prediction market, beta).

Public read API at ``https://data.pascal.trade``. No API key.

Docs: https://docs.pascal.trade/llms/api.txt (primary reference). The product is
in beta and the read API has no public changelog. Endpoints used:

- ``GET /api/v1/markets`` — every unresolved market in one snapshot, ordered by
  symbol. There is no cursor. ``limit`` is applied locally. ``page_size`` is
  ignored.
- ``GET /api/v1/markets/resolved`` — markets resolved inside a lookback
  (``4h``, ``24h``, or ``3d``). ``order_by`` is always sent. The API accepts
  only ``limit`` 50, 100, or 200 and does not page, so this is not a full
  archive.
- ``POST /api/v1/markets/search`` — exact symbol lookup for ``get_market``
  (resolved markets included). An empty list is not found.
- ``POST /api/v1/markets/text-search`` — fuzzy token search when
  ``venue_params["query"]`` is set. The trimmed query must be 1–32 characters;
  longer or blank queries raise ``ValueError`` before the request (the API
  returns ``INVALID_REQUEST``). ``order_by`` is always sent. On 2026-10-27 the
  API default became ``volume_all_time``. Limited to 60 requests per minute
  per IP, and at most 100 markets.
- ``GET /api/v1/books`` — one order book for the symbol (the long contract).
  Up to 50 symbols per call; this client requests one. A single-symbol book
  may be up to one second old. An unknown symbol is HTTP 422
  ``SYMBOL_NOT_FOUND``.
- ``GET /api/v1/trades`` — public prints, paged with ``before_cursor``. Page
  size is 1–500.

``price`` is ``None`` unless both ``best_bid`` and ``best_ask`` are present.
A one-sided book is not a price, and ``mark_price`` is not a substitute: it
can be a last trade (including a value near 0.5) with an empty book. A
resolved market uses ``resolution.price``.

``is_mece`` (optional on market payloads since 2026-10-28) means the event's
outcomes are mutually exclusive and collectively exhaustive. It stays on
``Market.raw_data`` when present. The shared model has no such field, and a
missing flag is normal. WebSocket ``mece_designation`` updates are not read.

Positions are not implemented (they belong to 1.0). ``GET
/api/v1/accounts/{owner}/position-resolutions`` accepts an optional ``symbols``
filter of up to 100 market symbols (added 2026-10-27). This client does not
call that endpoint. Trading, candles, and websockets are not implemented.
``place_order`` validates a dry-run and does not submit.

The docs do not publish a request weight for reads. Clients must handle HTTP
429 (``RATE_LIMITED``). Limits may change.
"""

import logging
import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, NoReturn

import httpx

from prediction_markets_api.adapters.base import BaseClient, validate_limit
from prediction_markets_api.exceptions import (
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

_SHORT_SUFFIX = ":short"
_QUERY_MIN = 1
_QUERY_MAX = 32
_TEXT_LIMIT_MAX = 100
_TRADE_PAGE_MAX = 500
_TEXT_ORDERS = frozenset({"volume_24h", "volume_all_time"})
_RESOLVED_ORDERS = frozenset({"resolution_time", "volume_24h", "volume_all_time"})
_LOOKBACKS = frozenset({"4h", "24h", "3d"})
_TEXT_STATUSES = frozenset({"live", "resolved", "all"})
_DEFAULT_TEXT_ORDER = "volume_all_time"
_DEFAULT_RESOLVED_ORDER = "resolution_time"
_DEFAULT_LOOKBACK = "24h"
_NOT_FOUND_CODES = frozenset(
    {
        "SYMBOL_NOT_FOUND",
        "ACCOUNT_NOT_FOUND",
        "ORDER_NOT_FOUND",
        "TRADING_KEY_NOT_FOUND",
    }
)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _split_outcome(outcome_id: str) -> tuple[str, bool]:
    """Return ``(symbol, is_short)``.

    The long outcome id is the market symbol. The short outcome id is
    ``{symbol}:short``. Symbols are ``EVENT.MARKET`` and do not contain ``:``.
    """
    if outcome_id.endswith(_SHORT_SUFFIX):
        symbol = outcome_id[: -len(_SHORT_SUFFIX)]
        if not symbol:
            raise ValueError(f"outcome id {outcome_id!r} has no market symbol")
        return symbol, True
    if not outcome_id:
        raise ValueError("outcome id must not be empty")
    return outcome_id, False


def _validated_query(query: str) -> str:
    """Trim a text-search query and require 1–32 characters.

    The API rejects a blank or longer trimmed query with ``INVALID_REQUEST``.
    Length is Unicode scalar values, which is ``len`` on a Python str.
    """
    trimmed = query.strip()
    length = len(trimmed)
    if length < _QUERY_MIN or length > _QUERY_MAX:
        raise ValueError(
            f"Pascal text-search query must be 1-32 characters after trimming, got {length}"
        )
    return trimmed


def _resolved_api_limit(limit: int | None) -> int:
    """Map a client limit onto the only sizes the resolved endpoint accepts."""
    if limit is None or limit > 100:
        return 200
    if limit > 50:
        return 100
    return 50


def _error_fields(body: object) -> tuple[str | None, str | None]:
    if not isinstance(body, dict):
        return None, None
    data = body.get("data")
    if not isinstance(data, dict):
        return None, None
    code = data.get("code")
    message = data.get("message")
    return (
        code if isinstance(code, str) else None,
        message if isinstance(message, str) else None,
    )


def _retry_after(response: httpx.Response) -> int | None:
    raw = response.headers.get("Retry-After")
    if raw is not None and raw.isdigit():
        return int(raw)
    return None


def _raise_api_error(response: httpx.Response, body: object) -> NoReturn:
    code, message = _error_fields(body)
    url = str(response.request.url)
    detail = message or code or f"Pascal API request failed (HTTP {response.status_code})"
    if response.status_code == 429 or code == "RATE_LIMITED":
        raise RateLimitError(detail, venue="pascal", url=url, retry_after=_retry_after(response))
    if response.status_code == 404 or code in _NOT_FOUND_CODES:
        raise NotFoundError(detail, resource_type="market", resource_id=None)
    raise VenueAPIError(detail, status_code=response.status_code, venue="pascal", url=url)


def _envelope_time(body: object) -> datetime:
    if isinstance(body, dict):
        for key in ("server_time_ms", "state_time_ms"):
            raw = body.get(key)
            if isinstance(raw, (int, str)) and str(raw).isdigit():
                try:
                    return parse_timestamp(str(raw), is_milliseconds=True)
                except ValueError:
                    continue
    return datetime.now(timezone.utc)


def _markets_from_body(body: object, url: str) -> list[dict[str, Any]]:
    """Accept the success envelope or a bare array (the documented payload)."""
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    if isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    raise VenueAPIError("Invalid Pascal markets payload", venue="pascal", url=url)


def _ms_datetime(raw: object) -> datetime | None:
    if not isinstance(raw, (int, str)):
        return None
    if isinstance(raw, str) and not raw.isdigit():
        return None
    try:
        return parse_timestamp(str(raw), is_milliseconds=True)
    except ValueError:
        return None


def _label(attrs: dict[str, Any], side: str, default: str) -> str:
    labels = attrs.get("outcome_labels")
    if isinstance(labels, dict):
        node = labels.get(side)
        if isinstance(node, dict):
            extended = node.get("extended")
            if isinstance(extended, str) and extended.strip():
                return extended.strip()
    return default


def _question(symbol: str, attrs: dict[str, Any]) -> str:
    event = attrs.get("event_description")
    market = attrs.get("market_description")
    event_text = event.strip() if isinstance(event, str) else ""
    market_text = market.strip() if isinstance(market, str) else ""
    if event_text and market_text:
        return f"{event_text}: {market_text}"
    if event_text:
        return event_text
    if market_text:
        return market_text
    return symbol


def _volume(raw: dict[str, Any]) -> Decimal | None:
    stats = raw.get("stats")
    if not isinstance(stats, dict):
        return None
    window = stats.get("all_time")
    if not isinstance(window, dict):
        return None
    buy = _decimal(window.get("taker_buy_volume_notional"))
    sell = _decimal(window.get("taker_sell_volume_notional"))
    if buy is None and sell is None:
        return None
    total = (buy if buy is not None else Decimal("0")) + (
        sell if sell is not None else Decimal("0")
    )
    if total < 0:
        return None
    return total


def _open_interest(raw: dict[str, Any]) -> Decimal | None:
    value = raw.get("long_interest", raw.get("open_interest"))
    interest = _decimal(value)
    if interest is None or interest < 0:
        return None
    return interest


def _in_unit_interval(value: Decimal | None) -> Decimal | None:
    if value is None or value < Decimal("0") or value > Decimal("1"):
        return None
    return value


def _book_side(raw: dict[str, Any], key: str) -> tuple[Decimal | None, str | None]:
    """A quoted side, or ``(None, None)`` when the venue omitted it."""
    if key not in raw or raw.get(key) is None:
        return None, None
    price = _in_unit_interval(_decimal(raw.get(key)))
    if price is None:
        return None, None
    original = raw.get(key)
    return price, original if isinstance(original, str) else str(price)


def _long_price(raw: dict[str, Any]) -> tuple[Decimal | None, str | None]:
    """Long-contract price.

    Resolution wins. Otherwise the mid of a two-sided book. Only a bid, only
    an ask, or neither side is ``price=None``. ``mark_price`` is not a
    substitute.
    """
    resolution = raw.get("resolution")
    if isinstance(resolution, dict) and "price" in resolution:
        price = _in_unit_interval(_decimal(resolution.get("price")))
        if price is not None:
            original = resolution.get("price")
            return price, original if isinstance(original, str) else str(price)
    bid = _book_side(raw, "best_bid")[0]
    ask = _book_side(raw, "best_ask")[0]
    if bid is None or ask is None:
        return None, None
    mid = (bid + ask) / Decimal("2")
    mark_raw = raw.get("mark_price")
    mark = _decimal(mark_raw) if "mark_price" in raw else None
    if mark is not None and mark == mid and isinstance(mark_raw, str):
        return mid, mark_raw
    return mid, str(mid)


def _complement(price: Decimal | None) -> tuple[Decimal | None, str | None]:
    if price is None:
        return None, None
    other = Decimal("1") - price
    if other < Decimal("0") or other > Decimal("1"):
        return None, None
    return other, str(other)


def _parse_market(raw: dict[str, Any], *, require_resolution: bool) -> Market | None:
    symbol = raw.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        return None
    resolution_raw = raw.get("resolution")
    resolution = resolution_raw if isinstance(resolution_raw, dict) else None
    resolved = resolution is not None and "price" in resolution
    if require_resolution and not resolved:
        return None
    attrs_raw = raw.get("display_attributes")
    attrs: dict[str, Any] = attrs_raw if isinstance(attrs_raw, dict) else {}
    long_name = _label(attrs, "long", "Yes")
    short_name = _label(attrs, "short", "No")
    long_price, long_raw = _long_price(raw)
    short_price, short_raw = _complement(long_price)
    # is_mece is optional and is not copied onto the shared model. raw_data
    # keeps the venue object, including the flag when the payload has it.
    created = _ms_datetime(raw.get("listing_ts_ms"))
    partial = created is None
    if created is None:
        created = datetime.now(timezone.utc)
    end = (
        _ms_datetime(resolution.get("market_resolution_ts_ms")) if resolution is not None else None
    )
    if end is None:
        end = _ms_datetime(attrs.get("expected_resolution_time_ms"))
    rules = raw.get("rules_url")
    winning: str | None = None
    if resolved and long_price == Decimal("1"):
        winning = long_name
    elif resolved and long_price == Decimal("0"):
        winning = short_name
    status = MarketStatus.RESOLVED if resolved else MarketStatus.OPEN
    return Market(
        id=symbol,
        venue="pascal",
        question=_question(symbol, attrs),
        description=rules if isinstance(rules, str) and rules else None,
        outcomes=[
            Outcome(
                id=symbol,
                market_id=symbol,
                name=long_name,
                price=long_price,
                raw_price=long_raw,
            ),
            Outcome(
                id=f"{symbol}{_SHORT_SUFFIX}",
                market_id=symbol,
                name=short_name,
                price=short_price,
                raw_price=short_raw,
            ),
        ],
        created_at=created,
        end_date=end,
        resolved=resolved,
        venue_status="resolved" if resolved else "live",
        status=status,
        winning_outcome=winning,
        volume=_volume(raw),
        open_interest=_open_interest(raw),
        best_bid_size=None,
        best_ask_size=None,
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


def _parse_trade(
    outcome_id: str,
    symbol: str,
    data: dict[str, Any],
    *,
    is_short: bool,
) -> Trade:
    side_raw = data.get("taker_side")
    if side_raw == "BID":
        side = OrderSide.BUY
    elif side_raw == "ASK":
        side = OrderSide.SELL
    else:
        raise ValueError(f"unknown taker side: {side_raw!r}")
    price = _in_unit_interval(_decimal(data.get("price")))
    size = _decimal(data.get("size"))
    if price is None or size is None or size <= 0:
        raise ValueError("trade price or size missing")
    if is_short:
        # The print is on the long contract. Short price is the complement
        # and the aggressor side flips (long SELL 0.90 is short BUY 0.10).
        price = Decimal("1") - price
        if price < Decimal("0") or price > Decimal("1"):
            raise ValueError("inverted trade price out of range")
        side = OrderSide.BUY if side == OrderSide.SELL else OrderSide.SELL
    trade_id = data.get("trade_id")
    if trade_id is None:
        raise ValueError("trade id missing")
    timestamp = _ms_datetime(data.get("trade_ts_ms"))
    if timestamp is None:
        raise ValueError("trade time missing")
    trade_symbol = data.get("symbol")
    market_id = trade_symbol if isinstance(trade_symbol, str) and trade_symbol else symbol
    return Trade(
        id=str(trade_id),
        market_id=market_id,
        outcome_id=outcome_id,
        side=side,
        price=price,
        size=size,
        timestamp=timestamp,
        currency=Currency.USD,
        raw_data=data,
    )


class PascalClient(BaseClient):
    """Read-only Pascal client.

    Read methods use the public data API. ``place_order`` is a dry-run stub.
    Trading is not implemented; ``dry_run=False`` requires ``PASCAL_API_KEY``
    and still does not submit an order.
    """

    API_BASE = "https://data.pascal.trade"

    def __init__(
        self,
        max_order_size: Decimal | None = None,
        timeout: float = 30.0,
        base_url: str | None = None,
    ):
        """Initialize the Pascal client.

        Args:
            max_order_size: Maximum order size accepted by ``place_order``.
            timeout: HTTP timeout in seconds.
            base_url: Read API origin. Defaults to production.
        """
        self._max_order_size = max_order_size
        self._timeout = timeout
        self._base_url = (base_url or self.API_BASE).rstrip("/")
        self._http_client = httpx.AsyncClient(timeout=timeout)
        self._api_key: str | None = os.getenv("PASCAL_API_KEY")

    async def list_markets(
        self,
        closed: bool | None = None,
        limit: int | None = None,
        page_size: int = 100,
        venue_params: dict[str, Any] | None = None,
    ) -> AsyncIterator[Market]:
        """List Pascal markets.

        ``closed=False`` reads ``GET /api/v1/markets`` (unresolved only).
        ``closed=True`` reads ``GET /api/v1/markets/resolved`` for one lookback
        window (default ``24h``). ``closed=None`` reads unresolved markets,
        then that resolved window, skipping symbols already yielded.
        ``page_size`` is accepted for signature compatibility. The live
        snapshot is not paged, so it is ignored.

        ``venue_params["query"]`` switches the call to ``POST
        /api/v1/markets/text-search``. The query is validated locally (1–32
        characters after trimming). ``order_by`` is always sent and defaults
        to ``volume_all_time``.

        Other ``venue_params`` keys:

        - ``order_by``: ``volume_24h`` or ``volume_all_time`` for text search;
          ``resolution_time``, ``volume_24h``, or ``volume_all_time`` for the
          resolved listing. The live snapshot has no sort parameter.
        - ``tags``: exact tags for text search (at most 5). Requires ``query``.
        - ``status``: ``live``, ``resolved``, or ``all`` for text search.
          Requires ``query``. When omitted, ``closed`` chooses it.
        - ``lookback``: ``4h``, ``24h``, or ``3d`` for the resolved listing.

        Raises:
            ValueError: If ``limit`` is not positive, or a text-search query is
                empty or longer than 32 characters.
            VenueAPIError: If a read request fails.
        """
        validate_limit(limit)
        # page_size matches BaseClient. Pascal's live listing is one snapshot.
        del page_size
        options = venue_params or {}
        query = options.get("query")
        if query is not None:
            if not isinstance(query, str):
                raise ValueError(
                    f"venue_params['query'] must be a string, got {type(query).__name__}"
                )
            async for market in self._text_search(
                query, closed=closed, limit=limit, options=options
            ):
                yield market
            return
        self._reject_text_only_params(options, closed=closed)
        if closed is True:
            async for market in self._list_resolved(limit, options, seen=set()):
                yield market
            return

        seen: set[str] = set()
        yielded = 0
        url = f"{self._base_url}/api/v1/markets"
        body = await self._request("GET", "/api/v1/markets")
        for market in self._markets(body, url, require_resolution=False):
            if limit is not None and yielded >= limit:
                return
            if market.id in seen:
                continue
            if closed is False and market.status != MarketStatus.OPEN:
                continue
            seen.add(market.id)
            yield market
            yielded += 1
        if closed is None and (limit is None or yielded < limit):
            remaining = None if limit is None else limit - yielded
            async for market in self._list_resolved(remaining, options, seen=seen):
                yield market

    async def get_market(self, market_id: str) -> Market:
        """Get one market by symbol, including a resolved market.

        Uses ``POST /api/v1/markets/search`` with that exact symbol. An empty
        result raises ``NotFoundError``. There is no ``GET /markets/{symbol}``.

        Raises:
            NotFoundError: If the symbol is unknown.
            VenueAPIError: If the search request fails.
        """
        if not market_id:
            raise NotFoundError(
                "Market not found: empty id",
                resource_type="market",
                resource_id=market_id,
            )
        url = f"{self._base_url}/api/v1/markets/search"
        body = await self._request(
            "POST",
            "/api/v1/markets/search",
            json_body={"symbols": [market_id]},
        )
        markets = self._markets(body, url, require_resolution=False)
        for market in markets:
            if market.id == market_id:
                return market
        raise NotFoundError(
            f"Market not found: {market_id}",
            resource_type="market",
            resource_id=market_id,
        )

    async def get_order_book(self, outcome_id: str) -> OrderBook:
        """Get the long-contract book, or the complementary short book.

        ``GET /api/v1/books`` is keyed by symbol. A ``:short`` outcome id
        inverts that book (short bid = 1 - long ask). An empty book is
        returned as-is. An unknown symbol raises ``NotFoundError``.

        Raises:
            NotFoundError: If the symbol does not exist.
            ValueError: If the book is crossed.
            VenueAPIError: If the request fails for another reason.
        """
        symbol, is_short = _split_outcome(outcome_id)
        url = f"{self._base_url}/api/v1/books"
        body = await self._request("GET", "/api/v1/books", params={"symbols": symbol})
        book_raw = _one_book(body, symbol, url)
        book = _order_book(outcome_id, symbol, is_short, book_raw, _envelope_time(body))
        book.validate_not_crossed()
        return book

    async def get_price(self, outcome_id: str, side: OrderSide) -> Price:
        """Best ask for BUY and best bid for SELL.

        An empty side on a symbol that exists raises ``NoLiquidityError``.
        An unknown symbol raises ``NotFoundError``.
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
            symbol, _is_short = _split_outcome(outcome_id)
            await self.get_market(symbol)
            raise NoLiquidityError(message, market_id=symbol, side=empty_side)
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
        """Yield up to ``limit`` public trades for one symbol.

        ``GET /api/v1/trades`` pages with ``before_cursor``. Each request asks
        for at most the remaining trades, capped at 500. ``taker_side`` ``BID``
        is ``BUY`` and ``ASK`` is ``SELL`` on the long contract. A ``:short``
        outcome id inverts each print: price is ``1 - long price`` and the
        side is flipped.

        Raises:
            ValueError: If ``limit`` is not positive.
            NotFoundError: If the symbol does not exist.
            VenueAPIError: If the request fails for another reason.
        """
        validate_limit(limit)
        async for trade in self._iter_trades(outcome_id, limit):
            yield trade

    async def iter_trades(self, outcome_id: str) -> AsyncIterator[Trade]:
        """Yield every public trade the trades endpoint will page through.

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
        an order; it requires ``PASCAL_API_KEY`` and returns a not-implemented
        payload. ``kwargs`` is accepted because ``place_order`` still matches
        the base signature, which takes ``**kwargs`` until 1.0.
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
                "Trading requires API credentials via environment variable: PASCAL_API_KEY"
            )
        return {
            "dry_run": False,
            "status": "not_implemented",
            "message": "Order placement not implemented in this version",
        }

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http_client.aclose()

    async def _text_search(
        self,
        query: str,
        *,
        closed: bool | None,
        limit: int | None,
        options: dict[str, Any],
    ) -> AsyncIterator[Market]:
        trimmed = _validated_query(query)
        order_by = options.get("order_by", _DEFAULT_TEXT_ORDER)
        if not isinstance(order_by, str) or order_by not in _TEXT_ORDERS:
            raise ValueError(
                "venue_params['order_by'] for text search must be "
                "'volume_24h' or 'volume_all_time', "
                f"got {order_by!r}"
            )
        status = options.get("status")
        if status is None:
            if closed is True:
                status = "resolved"
            elif closed is False:
                status = "live"
            else:
                status = "all"
        elif not isinstance(status, str) or status not in _TEXT_STATUSES:
            raise ValueError(
                f"venue_params['status'] must be 'live', 'resolved', or 'all', got {status!r}"
            )
        body: dict[str, Any] = {
            "query": trimmed,
            "order_by": order_by,
            "status": status,
            "limit": _TEXT_LIMIT_MAX if limit is None else min(limit, _TEXT_LIMIT_MAX),
        }
        if "tags" in options:
            tags = options["tags"]
            if (
                not isinstance(tags, list)
                or len(tags) > 5
                or any(not isinstance(tag, str) for tag in tags)
            ):
                raise ValueError("venue_params['tags'] must be a list of at most 5 strings")
            body["tags"] = tags
        if "lookback" in options:
            raise ValueError("venue_params['lookback'] is only valid for the resolved listing")
        url = f"{self._base_url}/api/v1/markets/text-search"
        payload = await self._request("POST", "/api/v1/markets/text-search", json_body=body)
        yielded = 0
        for market in self._markets(payload, url, require_resolution=False):
            if limit is not None and yielded >= limit:
                return
            if status == "live" and market.status != MarketStatus.OPEN:
                continue
            if status == "resolved" and market.status != MarketStatus.RESOLVED:
                continue
            yield market
            yielded += 1

    async def _list_resolved(
        self,
        limit: int | None,
        options: dict[str, Any],
        *,
        seen: set[str],
    ) -> AsyncIterator[Market]:
        lookback = options.get("lookback", _DEFAULT_LOOKBACK)
        if not isinstance(lookback, str) or lookback not in _LOOKBACKS:
            raise ValueError(
                f"venue_params['lookback'] must be '4h', '24h', or '3d', got {lookback!r}"
            )
        order_by = options.get("order_by", _DEFAULT_RESOLVED_ORDER)
        if not isinstance(order_by, str) or order_by not in _RESOLVED_ORDERS:
            raise ValueError(
                "venue_params['order_by'] for resolved markets must be "
                "'resolution_time', 'volume_24h', or 'volume_all_time', "
                f"got {order_by!r}"
            )
        params: dict[str, Any] = {
            "lookback": lookback,
            "order_by": order_by,
            "limit": _resolved_api_limit(limit),
        }
        url = f"{self._base_url}/api/v1/markets/resolved"
        body = await self._request("GET", "/api/v1/markets/resolved", params=params)
        yielded = 0
        for market in self._markets(body, url, require_resolution=True):
            if limit is not None and yielded >= limit:
                return
            if market.id in seen:
                continue
            seen.add(market.id)
            yield market
            yielded += 1

    def _reject_text_only_params(self, options: dict[str, Any], *, closed: bool | None) -> None:
        if "tags" in options or "status" in options:
            raise ValueError("venue_params tags and status require query")
        if "order_by" in options and closed is False:
            raise ValueError(
                "order_by is not supported on the live market snapshot; pass query or closed=True"
            )
        if "lookback" in options and closed is False:
            raise ValueError("lookback is only valid when listing resolved markets")

    def _markets(self, body: object, url: str, *, require_resolution: bool) -> list[Market]:
        parsed: list[Market] = []
        for raw in _markets_from_body(body, url):
            try:
                market = _parse_market(raw, require_resolution=require_resolution)
            except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
                logger.warning(
                    "Skipping Pascal market %s due to parse error: %s",
                    raw.get("symbol"),
                    exc,
                )
                continue
            if market is not None:
                parsed.append(market)
        return parsed

    async def _iter_trades(self, outcome_id: str, limit: int | None) -> AsyncIterator[Trade]:
        symbol, is_short = _split_outcome(outcome_id)
        cursor: str | None = None
        seen_cursors: set[str] = set()
        yielded = 0
        while True:
            if limit is not None and yielded >= limit:
                return
            page_limit = _TRADE_PAGE_MAX if limit is None else min(_TRADE_PAGE_MAX, limit - yielded)
            params: dict[str, Any] = {"symbols": symbol, "limit": page_limit}
            if cursor is not None:
                params["before_cursor"] = cursor
            url = f"{self._base_url}/api/v1/trades"
            body = await self._request("GET", "/api/v1/trades", params=params)
            items, next_cursor = _trade_page(body, url)
            if next_cursor is not None and next_cursor in seen_cursors:
                logger.warning(
                    "Stopping Pascal trade pagination for %s: cursor %s repeated",
                    outcome_id,
                    next_cursor,
                )
                return
            if not items:
                return
            for item in items:
                if limit is not None and yielded >= limit:
                    return
                try:
                    trade = _parse_trade(outcome_id, symbol, item, is_short=is_short)
                except (KeyError, ValueError, TypeError):
                    continue
                yield trade
                yielded += 1
            if not next_cursor:
                return
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> object:
        """Send one read request and map failures onto library errors."""
        url = f"{self._base_url}{path}"
        try:
            response = await self._http_client.request(method, url, params=params, json=json_body)
        except httpx.HTTPError as exc:
            raise VenueAPIError(
                f"HTTP error calling Pascal: {exc}",
                venue="pascal",
                url=url,
            ) from exc
        try:
            body: object = response.json()
        except ValueError as exc:
            if response.status_code == 429:
                raise RateLimitError(
                    "Rate limit exceeded",
                    venue="pascal",
                    url=str(response.request.url),
                    retry_after=_retry_after(response),
                ) from exc
            raise VenueAPIError(
                f"Invalid JSON from Pascal: {exc}",
                status_code=response.status_code,
                venue="pascal",
                url=str(response.request.url),
            ) from exc
        if response.status_code >= 400 or (
            isinstance(body, dict) and body.get("status") == "error"
        ):
            _raise_api_error(response, body)
        return body


def _one_book(body: object, symbol: str, url: str) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise VenueAPIError("Invalid Pascal order book payload", venue="pascal", url=url)
    data = body.get("data")
    books = data.get("books") if isinstance(data, dict) else None
    if not isinstance(books, dict):
        raise VenueAPIError("Invalid Pascal order book payload", venue="pascal", url=url)
    book = books.get(symbol)
    if not isinstance(book, dict):
        raise NotFoundError(
            f"Order book not found for outcome: {symbol}",
            resource_type="outcome",
            resource_id=symbol,
        )
    return book


def _order_book(
    outcome_id: str,
    symbol: str,
    is_short: bool,
    book: dict[str, Any],
    timestamp: datetime,
) -> OrderBook:
    bids = _parse_levels(book.get("bids"))
    asks = _parse_levels(book.get("asks"))
    if is_short:
        short_bids = _invert_levels(asks)
        short_asks = _invert_levels(bids)
        bids, asks = short_bids, short_asks
    bids.sort(key=lambda level: level.price, reverse=True)
    asks.sort(key=lambda level: level.price)
    spec = book.get("spec")
    tick_size = None
    if isinstance(spec, dict):
        tick_size = _decimal(spec.get("tick_size_min"))
        if tick_size is not None and tick_size < 0:
            tick_size = None
    return OrderBook(
        outcome_id=outcome_id,
        market_id=symbol,
        bids=bids,
        asks=asks,
        timestamp=timestamp,
        last_trade_price=None,
        min_order_size=None,
        tick_size=tick_size,
        currency=Currency.USD,
        partial=False,
    )


def _trade_page(body: object, url: str) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(body, dict):
        raise VenueAPIError("Invalid Pascal trades payload", venue="pascal", url=url)
    data = body.get("data")
    if not isinstance(data, dict):
        raise VenueAPIError("Invalid Pascal trades payload", venue="pascal", url=url)
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        raise VenueAPIError("Invalid Pascal trades payload", venue="pascal", url=url)
    items = [item for item in raw_items if isinstance(item, dict)]
    cursor = data.get("next_cursor")
    next_cursor = cursor if isinstance(cursor, str) and cursor else None
    return items, next_cursor
