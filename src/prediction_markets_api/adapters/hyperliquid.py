"""Hyperliquid Outcomes (HIP-4) adapter.

Read-only client for the public info API (``POST /info``). No API key is required.

Endpoints used:

- ``outcomeMeta`` — live outcomes and questions (one payload, no cursor)
- ``outcomeTemplates`` — display-name patterns for template markets
- ``allMids`` — mid prices for outcome coins (``#<10 * outcome + side>``).
  An empty book still has a mid (often ``0.5``); that mid is not used as the
  outcome price. See ``l2Book`` below.
- ``l2Book`` — order book for one outcome coin (at most 20 levels per side).
  ``list_markets`` and ``get_market`` call this once per priced question
  fallback (side 0 only; Yes and No share one book), up to 8 calls in flight.
  Each call has info weight 2. The result is not cached on the client. No
  levels means ``price`` is ``None``. A failed check (404, 429, 5xx, timeout)
  also sets that leg's price to ``None`` and does not abort the call.
- ``recentTrades`` — latest trades for one coin (no cursor; a short window)
- ``settledOutcome`` — one settled outcome by id

Coin encoding follows the Asset IDs docs: ``encoding = 10 * outcome + side``
with side ``0`` or ``1``, and the trade coin is ``#<encoding>``.

Gaps (see the changelog and the client docstrings):

- There is no list endpoint for settled markets. ``list_markets(closed=True)``
  raises ``NotImplementedError``. ``get_market`` can still load one known
  settled outcome id via ``settledOutcome``.
- ``recentTrades`` is not paginated.
- ``outcomeMeta`` does not include creation time, volume, open interest, or a
  liquidity figure. ``created_at`` is the time the record was parsed.
"""

import asyncio
import logging
import os
import re
import warnings
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

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

# Info API: l2Book weight is 2. Cap in-flight checks so a full listing stays
# near a handful of round trips instead of one request per fallback.
_L2_IN_FLIGHT = 8
_HL_TIME = re.compile(r"^\d{8}-\d{4}$")
_TEMPLATE_PREFIX = "template:"
# outcomeMeta / l2Book docs: books return at most 20 levels per side.
_L2_LEVEL_CAP = 20
_END_KEYS = (
    "expiry",
    "time",
    "resolutionDeadline",
    "listingDeadline",
    "dateTime",
    "decisionDeadline",
)


def _parse_kv(description: str | None) -> dict[str, str]:
    """Parse ``key:value|key:value`` descriptions.

    Values may contain colons (``perp:xyz:LLY``), so each segment is split on
    the first colon only.
    """
    fields: dict[str, str] = {}
    if not description:
        return fields
    for part in description.split("|"):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        if key:
            fields[key] = value
    return fields


def _subst(pattern: str, fields: dict[str, str]) -> str:
    """Replace ``{keyword}`` placeholders from a description map."""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return fields.get(key, match.group(0))

    return re.sub(r"\{([^{}]+)\}", replace, pattern)


def _parse_hl_time(value: str) -> datetime | None:
    """Parse a HIP-4 ``YYYYMMDD-HHMM`` timestamp as UTC."""
    if not _HL_TIME.match(value):
        return None
    try:
        parsed = datetime.strptime(value, "%Y%m%d-%H%M")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _end_date(fields: dict[str, str]) -> datetime | None:
    for key in _END_KEYS:
        raw = fields.get(key)
        if raw is None:
            continue
        parsed = _parse_hl_time(raw)
        if parsed is not None:
            return parsed
    return None


def _currency(quote_token: Any) -> Currency:
    if quote_token is None or str(quote_token).upper() == "USDC":
        return Currency.USDC
    return Currency.USD


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _coin(outcome: int, side: int) -> str:
    if side not in (0, 1):
        raise ValueError(f"outcome side must be 0 or 1, got {side}")
    return f"#{10 * outcome + side}"


def coin_from_outcome_id(outcome_id: str) -> str:
    """Map a public outcome id to an info-API coin.

    Accepted forms:

    - ``#160490`` — already a trade coin
    - ``16049`` — outcome id, side 0
    - ``16049:1`` — outcome id and side
    - any other string — passed through as the coin (used by contract fixtures)
    """
    if outcome_id.startswith("#"):
        return outcome_id
    if outcome_id.startswith("q") and outcome_id[1:].isdigit():
        raise ValueError(
            f"{outcome_id} is a question id and has no single order book; "
            "pass an outcome id or a #coin"
        )
    if ":" in outcome_id:
        outcome_part, side_part = outcome_id.split(":", 1)
        if outcome_part.isdigit() and side_part.isdigit():
            return _coin(int(outcome_part), int(side_part))
    if outcome_id.isdigit():
        return _coin(int(outcome_id), 0)
    return outcome_id


def _outcome_number(market_id: str) -> int | None:
    """Return the numeric outcome id from ``16049``, ``#160490``, or ``16049:1``."""
    if market_id.isdigit():
        return int(market_id)
    if market_id.startswith("#") and market_id[1:].isdigit():
        encoding = int(market_id[1:])
        side = encoding % 10
        if side not in (0, 1):
            return None
        return encoding // 10
    if ":" in market_id:
        outcome_part, side_part = market_id.split(":", 1)
        if outcome_part.isdigit() and side_part.isdigit() and int(side_part) in (0, 1):
            return int(outcome_part)
    return None


def _template_id(name: str) -> str | None:
    if name.startswith(_TEMPLATE_PREFIX):
        return name[len(_TEMPLATE_PREFIX) :]
    return None


def _render_title(name: str, fields: dict[str, str], templates: dict[str, dict[str, Any]]) -> str:
    """Human-readable title for an outcome or question name."""
    template_key = _template_id(name)
    if template_key is not None:
        template = templates.get(template_key)
        if template is not None:
            pattern = template.get("name")
            if isinstance(pattern, str) and pattern:
                rendered = _subst(pattern, fields).strip()
                if rendered and "{" not in rendered:
                    return rendered
        rendered_key = _subst(template_key, fields).strip()
        if rendered_key and "{" not in rendered_key:
            return rendered_key
    if name == "Recurring" and fields.get("class") == "priceBinary":
        underlying = fields.get("underlying", "Asset")
        target = fields.get("targetPrice", "?")
        expiry = fields.get("expiry", "?")
        return f"{underlying} above {target} at {expiry}?"
    if name == "Recurring" and fields.get("class") == "priceBucket":
        underlying = fields.get("underlying", "Asset")
        expiry = fields.get("expiry", "?")
        return f"{underlying} price bucket at {expiry}"
    return name


def _side_label(spec_name: str, fields: dict[str, str]) -> str:
    label = spec_name
    if label.startswith(_TEMPLATE_PREFIX):
        label = label[len(_TEMPLATE_PREFIX) :]
    rendered = _subst(label, fields).strip()
    return rendered or spec_name


def _bucket_label(fields: dict[str, str], parent_fields: dict[str, str]) -> str | None:
    """Label a recurring price-bucket leg using the parent thresholds."""
    if "index" not in fields:
        return None
    try:
        index = int(fields["index"])
    except ValueError:
        return None
    parts = [part for part in parent_fields.get("priceThresholds", "").split(",") if part]
    if len(parts) >= 2:
        lower, upper = parts[0], parts[1]
        if index == 0:
            return f"Below {lower}"
        if index == 1:
            return f"{lower} to {upper}"
        if index == 2:
            return f"{upper} or above"
    return f"Bucket {index}"


def _is_other(name: str, description: str | None) -> bool:
    if (description or "").strip() == "other":
        return True
    return name in {"template fallback", "Recurring Fallback", "template:awardOther"}


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _mids_map(payload: object) -> dict[str, str]:
    """Return coin -> mid from an ``allMids`` payload.

    Other info responses (notably ``outcomeMeta``) are dicts too. Ignore those
    so a single mocked ``/info`` body cannot be misread as mids.
    """
    if not isinstance(payload, dict) or "outcomes" in payload or "questions" in payload:
        return {}
    mids: dict[str, str] = {}
    for key, value in payload.items():
        if isinstance(key, str) and key.startswith("#") and isinstance(value, str):
            mids[key] = value
    return mids


def _templates_map(payload: object) -> dict[str, dict[str, Any]]:
    templates: dict[str, dict[str, Any]] = {}
    if not isinstance(payload, list):
        return templates
    for item in payload:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            templates[item["id"]] = item
    return templates


class HyperliquidClient(BaseClient):
    """Read-only Hyperliquid Outcomes (HIP-4) client.

    Read methods use the public info API. ``place_order`` is a dry-run stub.
    Trading is not implemented; ``dry_run=False`` requires ``HYPERLIQUID_API_KEY``
    and still does not submit an order.
    """

    API_BASE = "https://api.hyperliquid.xyz"

    def __init__(
        self,
        max_order_size: Decimal | None = None,
        timeout: float = 30.0,
        base_url: str | None = None,
    ):
        """Initialize the Hyperliquid client.

        Args:
            max_order_size: Maximum order size accepted by ``place_order``.
            timeout: HTTP timeout in seconds.
            base_url: Info API origin. Defaults to mainnet. Testnet is
                ``https://api.hyperliquid-testnet.xyz``.
        """
        self._max_order_size = max_order_size
        self._timeout = timeout
        self._base_url = (base_url or self.API_BASE).rstrip("/")
        self._http_client = httpx.AsyncClient(timeout=timeout)
        self._api_key: str | None = os.getenv("HYPERLIQUID_API_KEY")
        self._templates: dict[str, dict[str, Any]] | None = None

    @property
    def _info_url(self) -> str:
        return f"{self._base_url}/info"

    async def list_markets(
        self,
        closed: bool | None = None,
        limit: int | None = None,
        page_size: int = 100,
        venue_params: dict[str, Any] | None = None,
    ) -> AsyncIterator[Market]:
        """List live HIP-4 markets.

        ``outcomeMeta`` returns every live outcome and question in one response,
        so ``page_size`` is accepted for signature compatibility and ignored.
        ``limit`` stops the iterator, including further fallback book lookups.

        Question fallbacks are priced from ``allMids`` only when ``l2Book`` has
        at least one level. An empty book sets that leg's price to ``None``
        (``allMids`` often reports ``0.5`` with no orders). One side-0
        ``l2Book`` is issued per priced fallback this call yields, at most 8
        in flight. The check belongs to this call only and is not stored on
        the client. The duplicate-Other fallback that is left out of a question
        is not requested. Standalone outcomes are not checked here.

        If a book cannot be checked (HTTP 404, 429, 5xx, or a timeout), that
        leg's price is ``None``. One warning summarises how many legs failed,
        and the rest of the listing is still returned. A question that cannot
        be parsed does not use up a ``limit`` slot.

        Args:
            closed: ``False`` keeps markets that are still open. ``None`` returns
                every market in ``outcomeMeta`` (open, plus listed markets whose
                expiry has passed but that are not settled). ``True`` is not
                supported.
            limit: Maximum markets to yield. ``None`` yields the full snapshot.
            page_size: Unused. The info API does not page this listing.
            venue_params: Optional filters. ``venue`` (str) keeps outcomes whose
                ``venue`` field equals that deployer venue (for example ``out``).
                A question is kept when any of its legs match.

        Raises:
            ValueError: If ``limit`` is not positive.
            NotImplementedError: If ``closed`` is ``True``.
            VenueAPIError: If ``outcomeMeta``, ``outcomeTemplates``, or
                ``allMids`` fails. A failed fallback ``l2Book`` does not raise.
        """
        validate_limit(limit)
        # page_size is part of the shared signature. outcomeMeta is one snapshot.
        if closed is True:
            raise NotImplementedError(
                "Hyperliquid info API does not provide an endpoint for listing closed "
                "or settled outcome markets. outcomeMeta returns live outcomes only. "
                "Use closed=False or closed=None, or get_market() with a known outcome "
                "id to read one settled market via settledOutcome."
            )

        venue_filter = _venue_filter(venue_params)
        meta = await self._outcome_meta()
        outcomes = _dict_list(meta.get("outcomes"))
        questions = _dict_list(meta.get("questions"))
        if not outcomes and not questions:
            return

        templates = await self._load_templates()
        mids = await self._load_mids()
        by_id = _outcomes_by_id(outcomes)
        selected, grouped, fallback_ids, scan_standalones = _plan_listing(
            questions,
            by_id,
            templates,
            mids,
            venue_filter,
            closed=closed,
            limit=limit,
        )
        states = await self._classify_books(fallback_ids)
        _warn_unverified_books(_unverified_count(states), stacklevel=3)
        bookless = _bookless_coins(states)
        yielded = 0
        for question in selected:
            if limit is not None and yielded >= limit:
                return
            try:
                market = _question_market(question, by_id, templates, mids, bookless=bookless)
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning(
                    "Skipping Hyperliquid question %s due to parse error: %s",
                    question.get("question"),
                    exc,
                )
                continue
            if closed is False and market.status != MarketStatus.OPEN:
                continue
            yield market
            yielded += 1

        if not scan_standalones:
            return
        for outcome in outcomes:
            if limit is not None and yielded >= limit:
                return
            outcome_id = outcome.get("outcome")
            if not isinstance(outcome_id, int) or outcome_id in grouped:
                continue
            if not _outcome_matches_venue(outcome, venue_filter):
                continue
            try:
                market = _binary_market(outcome, templates, mids)
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning(
                    "Skipping Hyperliquid outcome %s due to parse error: %s",
                    outcome_id,
                    exc,
                )
                continue
            if closed is False and market.status != MarketStatus.OPEN:
                continue
            yield market
            yielded += 1

    async def get_market(self, market_id: str) -> Market:
        """Get one market.

        Ids:

        - ``16049`` — one outcome (a binary contract)
        - ``#160490`` or ``16049:0`` — same outcome, addressed by coin or side
        - ``q505`` — one categorical question

        Settled outcomes are not in ``outcomeMeta``. A numeric outcome id that
        is missing there is loaded from ``settledOutcome``. A null body means
        the id does not exist.

        A question fallback, or a fallback fetched by its own outcome id, has
        ``price=None`` when its side-0 book has no levels or that check fails.
        The check is one ``l2Book`` for this call and is not cached. A failed
        check warns once and does not raise.

        Raises:
            NotFoundError: If the id is unknown.
            VenueAPIError: If ``outcomeMeta``, ``outcomeTemplates``, ``allMids``,
                or ``settledOutcome`` fails. A failed fallback ``l2Book`` does
                not raise.
        """
        meta = await self._outcome_meta()
        templates = await self._load_templates()
        outcomes = _dict_list(meta.get("outcomes"))
        questions = _dict_list(meta.get("questions"))
        by_id = {int(item["outcome"]): item for item in outcomes if "outcome" in item}

        if market_id.startswith("q") and market_id[1:].isdigit():
            question_id = int(market_id[1:])
            question = next(
                (item for item in questions if item.get("question") == question_id), None
            )
            if question is None:
                raise NotFoundError(
                    f"Market not found: {market_id}",
                    resource_type="market",
                    resource_id=market_id,
                )
            mids = await self._load_mids()
            fallback_id = _priced_fallback_id(question, by_id, templates)
            question_ids = [fallback_id] if fallback_id is not None else []
            question_states = await self._classify_books(question_ids)
            _warn_unverified_books(_unverified_count(question_states), stacklevel=3)
            return _question_market(
                question,
                by_id,
                templates,
                mids,
                bookless=_bookless_coins(question_states),
            )

        outcome_id = _outcome_number(market_id)
        if outcome_id is None:
            raise NotFoundError(
                f"Market not found: {market_id}",
                resource_type="market",
                resource_id=market_id,
            )
        raw = by_id.get(outcome_id)
        if raw is not None:
            mids = await self._load_mids()
            side_bookless: set[str] = set()
            if outcome_id in _fallback_ids(questions):
                side_states = await self._classify_books([outcome_id])
                _warn_unverified_books(_unverified_count(side_states), stacklevel=3)
                side_bookless = _bookless_coins(side_states)
            return _binary_market(raw, templates, mids, bookless=side_bookless)
        settled = await self._info({"type": "settledOutcome", "outcome": outcome_id})
        if isinstance(settled, dict) and isinstance(settled.get("spec"), dict):
            return _settled_market(settled, templates)
        raise NotFoundError(
            f"Market not found: {market_id}",
            resource_type="market",
            resource_id=market_id,
        )

    async def get_order_book(self, outcome_id: str) -> OrderBook:
        """Get the L2 book for one outcome coin.

        ``l2Book`` returns at most 20 levels per side. A book that hits that cap
        is marked ``partial=True``. A null body means the coin does not exist.

        Raises:
            NotFoundError: If the info API returns null for the coin.
            ValueError: If the book is crossed, or ``outcome_id`` is a question id.
            VenueAPIError: If the info API request fails.
        """
        coin = coin_from_outcome_id(outcome_id)
        data = await self._info({"type": "l2Book", "coin": coin})
        if data is None:
            raise NotFoundError(
                f"Order book not found for outcome: {outcome_id}",
                resource_type="outcome",
                resource_id=outcome_id,
            )
        if not isinstance(data, dict):
            raise VenueAPIError(
                f"Invalid order book payload for {outcome_id}",
                venue="hyperliquid",
                url=self._info_url,
            )
        book = _parse_order_book(outcome_id, coin, data)
        book.validate_not_crossed()
        return book

    async def get_price(self, outcome_id: str, side: OrderSide) -> Price:
        """Best ask for BUY and best bid for SELL, from ``l2Book``.

        An empty side on a coin that exists raises ``NoLiquidityError``. An
        unknown coin raises ``NotFoundError`` (null book, or the follow-up
        market lookup misses).
        """
        book = await self.get_order_book(outcome_id)
        if side == OrderSide.BUY:
            if not book.asks:
                await self._require_market(outcome_id)
                raise NoLiquidityError(
                    f"No liquidity to buy (no asks) for {outcome_id}",
                    market_id=outcome_id,
                    side="BUY",
                )
            level = book.asks[0]
        else:
            if not book.bids:
                await self._require_market(outcome_id)
                raise NoLiquidityError(
                    f"No liquidity to sell (no bids) for {outcome_id}",
                    market_id=outcome_id,
                    side="SELL",
                )
            level = book.bids[0]
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
        """Yield up to ``limit`` recent trades for one outcome coin.

        ``recentTrades`` returns a single window (10 trades on mainnet as of
        the recorded fixtures) and has no cursor. Fewer than ``limit`` trades
        are returned when the window is shorter. There is no way to request
        older pages.

        Raises:
            ValueError: If ``limit`` is not positive, or ``outcome_id`` is a question id.
            VenueAPIError: If the info API request fails. Unknown coins are
                reported by the venue as HTTP 500, which surfaces as
                ``VenueAPIError`` rather than ``NotFoundError``.
        """
        validate_limit(limit)
        async for trade in self._iter_trades(outcome_id, limit):
            yield trade

    async def iter_trades(self, outcome_id: str) -> AsyncIterator[Trade]:
        """Yield the full ``recentTrades`` window for one coin.

        The public API does not paginate trade history, so this is the same
        window ``get_trades`` reads, without the default limit of 100.
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

        Order placement is not implemented. ``dry_run`` defaults to ``True`` and
        performs no HTTP request. ``dry_run=False`` still does not submit an
        order; it requires ``HYPERLIQUID_API_KEY`` and returns a not-implemented
        payload. ``kwargs`` is accepted because ``place_order`` still matches the
        base signature, which takes ``**kwargs`` until 1.0.
        """
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
                "Trading requires API credentials via environment variable: HYPERLIQUID_API_KEY"
            )
        return {
            "dry_run": False,
            "status": "not_implemented",
            "message": "Order placement not implemented in this version",
        }

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http_client.aclose()

    async def _require_market(self, outcome_id: str) -> None:
        """Raise ``NotFoundError`` when ``outcome_id`` is not a real market."""
        number = _outcome_number(outcome_id)
        lookup = str(number) if number is not None else outcome_id
        await self.get_market(lookup)

    async def _iter_trades(self, outcome_id: str, limit: int | None) -> AsyncIterator[Trade]:
        coin = coin_from_outcome_id(outcome_id)
        data = await self._info({"type": "recentTrades", "coin": coin})
        if data is None:
            return
        if not isinstance(data, list):
            raise VenueAPIError(
                f"Unexpected recentTrades payload for {outcome_id}",
                venue="hyperliquid",
                url=self._info_url,
            )
        yielded = 0
        for item in data:
            if limit is not None and yielded >= limit:
                return
            if not isinstance(item, dict):
                continue
            try:
                trade = _parse_trade(outcome_id, coin, item)
            except (KeyError, ValueError, TypeError):
                continue
            yield trade
            yielded += 1

    async def _info(self, payload: dict[str, Any]) -> object:
        """POST one info request and map HTTP failures onto library errors."""
        url = self._info_url
        try:
            response = await self._http_client.post(url, json=payload)
            response.raise_for_status()
            body: object = response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 404:
                raise NotFoundError(
                    f"Hyperliquid info request failed: {payload.get('type')}",
                    resource_type="info",
                    resource_id=str(payload.get("type")),
                ) from exc
            if status == 429:
                raise RateLimitError(
                    "Rate limit exceeded",
                    venue="hyperliquid",
                    url=str(exc.request.url),
                ) from exc
            raise VenueAPIError(
                f"Hyperliquid info request failed: {exc}",
                status_code=status,
                venue="hyperliquid",
                url=str(exc.request.url),
            ) from exc
        except httpx.HTTPError as exc:
            raise VenueAPIError(
                f"HTTP error calling Hyperliquid info API: {exc}",
                venue="hyperliquid",
                url=url,
            ) from exc
        except ValueError as exc:
            raise VenueAPIError(
                f"Invalid JSON from Hyperliquid info API: {exc}",
                venue="hyperliquid",
                url=url,
            ) from exc
        return body

    async def _outcome_meta(self) -> dict[str, Any]:
        data = await self._info({"type": "outcomeMeta"})
        if not isinstance(data, dict):
            raise VenueAPIError(
                "Invalid outcomeMeta payload",
                venue="hyperliquid",
                url=self._info_url,
            )
        return data

    async def _load_templates(self) -> dict[str, dict[str, Any]]:
        if self._templates is not None:
            return self._templates
        data = await self._info({"type": "outcomeTemplates"})
        self._templates = _templates_map(data)
        return self._templates

    async def _load_mids(self) -> dict[str, str]:
        data = await self._info({"type": "allMids"})
        return _mids_map(data)

    async def _classify_books(self, outcome_ids: list[int]) -> dict[int, str]:
        """Classify side-0 books for this call only.

        Values are ``empty``, ``live``, ``unknown``, or ``unverified``. At most
        ``_L2_IN_FLIGHT`` requests run at once. Ids are deduplicated and the
        result keeps the first-seen order. Nothing is stored on the client.
        ``VenueAPIError`` (429, 5xx, timeout) and ``NotFoundError`` (404) become
        ``unverified``. Other books in the same call still run. A body that is
        not an order book is ``unknown`` and does not clear the mid.
        """
        ordered: list[int] = []
        seen: set[int] = set()
        for outcome_id in outcome_ids:
            if outcome_id in seen:
                continue
            seen.add(outcome_id)
            ordered.append(outcome_id)
        if not ordered:
            return {}
        semaphore = asyncio.Semaphore(_L2_IN_FLIGHT)

        async def one(outcome_id: int) -> tuple[int, str]:
            async with semaphore:
                try:
                    payload = await self._info({"type": "l2Book", "coin": _coin(outcome_id, 0)})
                except (VenueAPIError, NotFoundError):
                    return outcome_id, "unverified"
                return outcome_id, _l2_book_state(payload)

        pairs = await asyncio.gather(*(one(outcome_id) for outcome_id in ordered))
        return dict(pairs)


def _bookless_coins(states: dict[int, str]) -> set[str]:
    """Side-0 coins whose book is empty or could not be checked."""
    coins: set[str] = set()
    for outcome_id, state in states.items():
        if state in {"empty", "unverified"}:
            coins.add(_coin(outcome_id, 0))
    return coins


def _unverified_count(states: dict[int, str]) -> int:
    """How many books this call failed to check."""
    return sum(1 for state in states.values() if state == "unverified")


def _warn_unverified_books(count: int, *, stacklevel: int) -> None:
    """One warning per call when some fallback books could not be checked.

    ``stacklevel`` is forwarded to ``warnings.warn``. Callers pass 3 so the
    warning points at the user of ``list_markets`` / ``get_market``.
    """
    if count <= 0:
        return
    noun = "leg" if count == 1 else "legs"
    message = f"Hyperliquid l2Book check failed for {count} fallback {noun}; those prices are None"
    logger.warning(message)
    warnings.warn(message, UserWarning, stacklevel=stacklevel)


def _outcomes_by_id(outcomes: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    by_id: dict[int, dict[str, Any]] = {}
    for outcome in outcomes:
        outcome_id = outcome.get("outcome")
        if isinstance(outcome_id, int):
            by_id[outcome_id] = outcome
    return by_id


def _venue_filter(venue_params: dict[str, Any] | None) -> str | None:
    if not venue_params:
        return None
    raw = venue_params.get("venue")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"venue_params['venue'] must be a string, got {type(raw).__name__}")
    return raw


def _outcome_matches_venue(outcome: dict[str, Any], venue_filter: str | None) -> bool:
    if venue_filter is None:
        return True
    return outcome.get("venue") == venue_filter


def _question_leg_ids(question: dict[str, Any]) -> list[int]:
    legs: list[int] = []
    named = question.get("namedOutcomes")
    if isinstance(named, list):
        legs.extend(item for item in named if isinstance(item, int))
    fallback = question.get("fallbackOutcome")
    if isinstance(fallback, int):
        legs.append(fallback)
    return legs


def _priced_fallback_id(
    question: dict[str, Any],
    by_id: dict[int, dict[str, Any]],
    templates: dict[str, dict[str, Any]],
) -> int | None:
    """Fallback this question prices, or None when that leg is omitted.

    The duplicate-Other fallback is left out of the question, so its book is
    not requested from a question listing or ``get_market`` of the question.
    """
    description = question.get("description")
    description_text = description if isinstance(description, str) else None
    fields = _parse_kv(description_text)
    fallback_id = question.get("fallbackOutcome")
    if not isinstance(fallback_id, int) or fallback_id not in by_id:
        return None
    if _duplicate_other_fallback(question, by_id, fields, templates) == fallback_id:
        return None
    return fallback_id


def _plan_listing(
    questions: list[dict[str, Any]],
    by_id: dict[int, dict[str, Any]],
    templates: dict[str, dict[str, Any]],
    mids: dict[str, str],
    venue_filter: str | None,
    *,
    closed: bool | None,
    limit: int | None,
) -> tuple[list[dict[str, Any]], set[int], list[int], bool]:
    """Questions this call will try to yield, plus whether to scan standalones.

    Hitting ``limit`` stops the question loop. Only a question that parses
    counts toward that limit, so a broken question does not shrink the page.
    Legs of questions that were not visited must not come back as binaries,
    so the standalone scan is skipped in that case. Venue-filtered and
    already-expired questions are grouped and do not spend an ``l2Book`` call.
    """
    grouped: set[int] = set()
    selected: list[dict[str, Any]] = []
    fallback_ids: list[int] = []
    stopped_early = False
    for question in questions:
        if limit is not None and len(selected) >= limit:
            stopped_early = True
            break
        leg_ids = _question_leg_ids(question)
        legs = [by_id[leg_id] for leg_id in leg_ids if leg_id in by_id]
        if venue_filter is not None and not any(
            _outcome_matches_venue(leg, venue_filter) for leg in legs
        ):
            grouped.update(leg_ids)
            continue
        grouped.update(leg_ids)
        if closed is False:
            description = question.get("description")
            description_text = description if isinstance(description, str) else None
            if _status_for_end(_end_date(_parse_kv(description_text))) != MarketStatus.OPEN:
                continue
        try:
            _question_market(question, by_id, templates, mids)
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning(
                "Skipping Hyperliquid question %s due to parse error: %s",
                question.get("question"),
                exc,
            )
            continue
        selected.append(question)
        fallback_id = _priced_fallback_id(question, by_id, templates)
        if fallback_id is not None:
            fallback_ids.append(fallback_id)
    return selected, grouped, fallback_ids, not stopped_early


def _status_for_end(end_date: datetime | None, *, now: datetime | None = None) -> MarketStatus:
    current = now if now is not None else datetime.now(timezone.utc)
    if end_date is not None and end_date <= current:
        return MarketStatus.CLOSED
    return MarketStatus.OPEN


def _binary_market(
    outcome: dict[str, Any],
    templates: dict[str, dict[str, Any]],
    mids: dict[str, str],
    *,
    now: datetime | None = None,
    bookless: set[str] | None = None,
) -> Market:
    outcome_id = outcome.get("outcome")
    if not isinstance(outcome_id, int):
        raise ValueError("outcome id missing")
    name = str(outcome.get("name") or "")
    description = outcome.get("description")
    description_text = description if isinstance(description, str) else None
    fields = _parse_kv(description_text)
    question = _market_question(name, description_text, fields, templates)
    end = _end_date(fields)
    status = _status_for_end(end, now=now)
    sides = _dict_list(outcome.get("sideSpecs"))
    outcomes = _side_outcomes(str(outcome_id), outcome_id, sides, fields, mids, bookless=bookless)
    return Market(
        id=str(outcome_id),
        venue="hyperliquid",
        question=question,
        description=description_text,
        outcomes=outcomes,
        created_at=now if now is not None else datetime.now(timezone.utc),
        end_date=end,
        resolved=False,
        venue_status="expired" if status == MarketStatus.CLOSED else "open",
        status=status,
        winning_outcome=None,
        volume=None,
        open_interest=None,
        best_bid_size=None,
        best_ask_size=None,
        liquidity=None,
        currency=_currency(outcome.get("quoteToken")),
        raw_data=outcome,
        partial=False,
    )


def _question_market(
    question: dict[str, Any],
    by_id: dict[int, dict[str, Any]],
    templates: dict[str, dict[str, Any]],
    mids: dict[str, str],
    *,
    now: datetime | None = None,
    bookless: set[str] | None = None,
) -> Market:
    question_id = question.get("question")
    if not isinstance(question_id, int):
        raise ValueError("question id missing")
    name = str(question.get("name") or "")
    description = question.get("description")
    description_text = description if isinstance(description, str) else None
    fields = _parse_kv(description_text)
    title = _market_question(name, description_text, fields, templates)
    end = _end_date(fields)
    status = _status_for_end(end, now=now)
    quote: Any = None
    legs: list[Outcome] = []
    # A named "Other" (awardOther) plus the protocol fallback (also labeled
    # Other, and usually no book) would count the same leg twice. Drop the fallback.
    omit_fallback = _duplicate_other_fallback(question, by_id, fields, templates)
    for leg_id in _question_leg_ids(question):
        if leg_id == omit_fallback:
            continue
        raw = by_id.get(leg_id)
        if raw is None:
            continue
        if quote is None:
            quote = raw.get("quoteToken")
        legs.append(_leg_outcome(str(question_id), raw, fields, templates, mids, bookless=bookless))
    return Market(
        id=f"q{question_id}",
        venue="hyperliquid",
        question=title,
        description=description_text,
        outcomes=legs,
        created_at=now if now is not None else datetime.now(timezone.utc),
        end_date=end,
        resolved=False,
        venue_status="expired" if status == MarketStatus.CLOSED else "open",
        status=status,
        winning_outcome=None,
        volume=None,
        open_interest=None,
        best_bid_size=None,
        best_ask_size=None,
        liquidity=None,
        currency=_currency(quote),
        raw_data=question,
        partial=not legs,
    )


def _settled_market(payload: dict[str, Any], templates: dict[str, dict[str, Any]]) -> Market:
    spec = payload.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("settled outcome spec missing")
    fraction = _decimal(payload.get("settleFraction"))
    if fraction is None or fraction < Decimal("0") or fraction > Decimal("1"):
        raise ValueError(f"invalid settleFraction: {payload.get('settleFraction')!r}")
    market = _binary_market(spec, templates, {})
    side_names = [outcome.name for outcome in market.outcomes]
    winning: str | None = None
    if fraction == Decimal("1") and side_names:
        winning = side_names[0]
    elif fraction == Decimal("0") and len(side_names) > 1:
        winning = side_names[1]
    priced: list[Outcome] = []
    for index, outcome in enumerate(market.outcomes):
        price = fraction if index == 0 else Decimal("1") - fraction
        if price < Decimal("0") or price > Decimal("1"):
            price = min(Decimal("1"), max(Decimal("0"), price))
        raw_price = str(payload.get("settleFraction")) if index == 0 else str(price)
        priced.append(outcome.model_copy(update={"price": price, "raw_price": raw_price}))
    return market.model_copy(
        update={
            "outcomes": priced,
            "resolved": True,
            "status": MarketStatus.RESOLVED,
            "venue_status": "settled",
            "winning_outcome": winning,
            "raw_data": payload,
            "partial": False,
        }
    )


def _market_question(
    name: str,
    description: str | None,
    fields: dict[str, str],
    templates: dict[str, dict[str, Any]],
) -> str:
    rendered = _render_title(name, fields, templates)
    if rendered and rendered != name and not rendered.startswith(_TEMPLATE_PREFIX):
        return rendered
    if fields.get("class") == "priceBinary":
        return _render_title("Recurring", fields, templates)
    if description and description not in {"other", ""}:
        return description
    if rendered:
        return rendered
    return name or description or "Hyperliquid outcome"


def _side_outcomes(
    market_id: str,
    outcome_id: int,
    sides: list[dict[str, Any]],
    fields: dict[str, str],
    mids: dict[str, str],
    *,
    bookless: set[str] | None = None,
) -> list[Outcome]:
    parsed: list[Outcome] = []
    for index, side in enumerate(sides[:2]):
        spec_name = side.get("name")
        label = _side_label(spec_name if isinstance(spec_name, str) else f"Side {index}", fields)
        coin = _coin(outcome_id, index)
        price, raw_price = _mid_for_coin(coin, mids, bookless)
        parsed.append(
            Outcome(
                id=coin,
                market_id=market_id,
                name=label,
                price=price,
                raw_price=raw_price,
            )
        )
    return parsed


def _duplicate_other_fallback(
    question: dict[str, Any],
    by_id: dict[int, dict[str, Any]],
    parent_fields: dict[str, str],
    templates: dict[str, dict[str, Any]],
) -> int | None:
    """Return a fallback id to omit when a named leg is already ``Other``.

    Award markets (Ballon d'Or) register a tradable ``awardOther`` and a
    protocol fallback (``template fallback``, description ``other``). Both
    render as Other. Listing both would count the same leg twice. Contest and
    price-bucket fallbacks are the only Other leg, so they stay. An empty
    book on a leg that stays is priced as ``None`` by the caller, not as the
    ``allMids`` value.
    """
    fallback_id = question.get("fallbackOutcome")
    if not isinstance(fallback_id, int):
        return None
    fallback = by_id.get(fallback_id)
    if fallback is None or _leg_label(fallback, parent_fields, templates) != "Other":
        return None
    named = question.get("namedOutcomes")
    if not isinstance(named, list):
        return None
    for leg_id in named:
        if not isinstance(leg_id, int) or leg_id == fallback_id:
            continue
        raw = by_id.get(leg_id)
        if raw is not None and _leg_label(raw, parent_fields, templates) == "Other":
            return fallback_id
    return None


def _leg_label(
    outcome: dict[str, Any],
    parent_fields: dict[str, str],
    templates: dict[str, dict[str, Any]],
) -> str:
    """Display name for one question leg."""
    outcome_id = outcome.get("outcome")
    name = str(outcome.get("name") or "")
    description = outcome.get("description")
    description_text = description if isinstance(description, str) else None
    fields = _parse_kv(description_text)
    if _is_other(name, description_text):
        return "Other"
    bucket = _bucket_label(fields, parent_fields)
    label = bucket or _render_title(name, fields, templates)
    if not label or label.startswith(_TEMPLATE_PREFIX):
        return description_text or name or str(outcome_id)
    return label


def _leg_outcome(
    market_id: str,
    outcome: dict[str, Any],
    parent_fields: dict[str, str],
    templates: dict[str, dict[str, Any]],
    mids: dict[str, str],
    *,
    bookless: set[str] | None = None,
) -> Outcome:
    outcome_id = outcome.get("outcome")
    if not isinstance(outcome_id, int):
        raise ValueError("leg outcome id missing")
    label = _leg_label(outcome, parent_fields, templates)
    coin = _coin(outcome_id, 0)
    price, raw_price = _mid_for_coin(coin, mids, bookless)
    return Outcome(
        id=coin,
        market_id=market_id,
        name=label,
        price=price,
        raw_price=raw_price,
    )


def _fallback_ids(questions: list[dict[str, Any]]) -> set[int]:
    """Outcome ids registered as a question's ``fallbackOutcome``."""
    ids: set[int] = set()
    for question in questions:
        fallback_id = question.get("fallbackOutcome")
        if isinstance(fallback_id, int):
            ids.add(fallback_id)
    return ids


def _l2_book_state(payload: object) -> str:
    """Classify an ``l2Book`` body as ``empty``, ``live``, or ``unknown``.

    ``empty`` is a null body or a book whose bid and ask lists are both empty.
    ``live`` has at least one level. ``unknown`` is some other info payload
    (for example ``outcomeMeta``); callers keep the ``allMids`` price then.
    """
    if payload is None:
        return "empty"
    if not isinstance(payload, dict):
        return "unknown"
    levels = payload.get("levels")
    if not isinstance(levels, list) or len(levels) < 2:
        return "unknown"
    bids = levels[0]
    asks = levels[1]
    if not isinstance(bids, list) or not isinstance(asks, list):
        return "unknown"
    if not bids and not asks:
        return "empty"
    return "live"


def _coin_lacks_book(coin: str, bookless: set[str]) -> bool:
    """True when ``coin`` or its side-0 pair is in ``bookless``.

    Yes and No share one merged book, so an empty side-0 book clears both sides.
    """
    if coin in bookless:
        return True
    if not coin.startswith("#") or not coin[1:].isdigit():
        return False
    encoding = int(coin[1:])
    side = encoding % 10
    if side not in (0, 1):
        return False
    return f"#{encoding - side}" in bookless


def _mid_for_coin(
    coin: str,
    mids: dict[str, str],
    bookless: set[str] | None,
) -> tuple[Decimal | None, str | None]:
    """Return ``(price, raw_mid)`` for one coin.

    An empty book suppresses the ``allMids`` value. A mid outside 0..1 is dropped.
    price is None when the venue has no order book for that outcome.
    """
    if bookless is not None and _coin_lacks_book(coin, bookless):
        return None, None
    mid = mids.get(coin)
    price = _decimal(mid)
    if price is None or price < Decimal("0") or price > Decimal("1"):
        return None, None
    return price, mid


def _parse_order_book(outcome_id: str, coin: str, data: dict[str, Any]) -> OrderBook:
    levels = data.get("levels")
    bids_raw: list[Any] = []
    asks_raw: list[Any] = []
    if isinstance(levels, list):
        if levels and isinstance(levels[0], list):
            bids_raw = levels[0]
        if len(levels) > 1 and isinstance(levels[1], list):
            asks_raw = levels[1]
    bids = _parse_levels(bids_raw)
    asks = _parse_levels(asks_raw)
    bids.sort(key=lambda level: level.price, reverse=True)
    asks.sort(key=lambda level: level.price)
    timestamp = datetime.now(timezone.utc)
    raw_time = data.get("time")
    if isinstance(raw_time, (int, float, str)):
        try:
            timestamp = parse_timestamp(raw_time, is_milliseconds=True)
        except ValueError:
            timestamp = datetime.now(timezone.utc)
    number = _outcome_number(coin) or _outcome_number(outcome_id)
    market_id = str(number) if number is not None else outcome_id
    partial = len(bids_raw) >= _L2_LEVEL_CAP or len(asks_raw) >= _L2_LEVEL_CAP
    return OrderBook(
        outcome_id=outcome_id,
        market_id=market_id,
        bids=bids,
        asks=asks,
        timestamp=timestamp,
        last_trade_price=None,
        min_order_size=None,
        tick_size=None,
        currency=Currency.USDC,
        partial=partial,
    )


def _parse_levels(raw_levels: list[Any]) -> list[OrderLevel]:
    parsed: list[OrderLevel] = []
    for level in raw_levels:
        if not isinstance(level, dict):
            continue
        price = _decimal(level.get("px"))
        size = _decimal(level.get("sz"))
        if price is None or size is None:
            continue
        if price < Decimal("0") or price > Decimal("1") or size <= Decimal("0"):
            continue
        parsed.append(OrderLevel(price=price, size=size, raw_price=str(level.get("px"))))
    return parsed


def _parse_trade(outcome_id: str, coin: str, data: dict[str, Any]) -> Trade:
    side_raw = data.get("side")
    if side_raw == "B":
        side = OrderSide.BUY
    elif side_raw == "A":
        side = OrderSide.SELL
    else:
        raise ValueError(f"unknown trade side: {side_raw!r}")
    price = _decimal(data.get("px"))
    size = _decimal(data.get("sz"))
    if price is None or size is None:
        raise ValueError("trade price or size missing")
    if price < Decimal("0") or price > Decimal("1"):
        raise ValueError(f"trade price out of range: {price}")
    if size <= Decimal("0"):
        raise ValueError(f"trade size out of range: {size}")
    trade_id = data.get("tid")
    if trade_id is None:
        raise ValueError("trade id missing")
    raw_time = data.get("time")
    if not isinstance(raw_time, (int, float, str)):
        raise ValueError("trade time missing")
    number = _outcome_number(coin) or _outcome_number(outcome_id)
    market_id = str(number) if number is not None else outcome_id
    return Trade(
        id=str(trade_id),
        market_id=market_id,
        outcome_id=outcome_id,
        side=side,
        price=price,
        size=size,
        timestamp=parse_timestamp(raw_time, is_milliseconds=True),
        currency=Currency.USDC,
        raw_data=data,
    )
