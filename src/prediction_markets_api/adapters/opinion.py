"""Read-only Opinion adapter (Opinion Labs, BNB Chain).

Docs: https://docs.opinion.trade/developer-guide/opinion-open-api/.
Terms: https://docs.opinion.trade/legal/terms-of-use.
Official site: https://opinion.trade.

Host: ``https://openapi.opinion.trade/openapi``. There is no published testnet
host (``openapi-testnet.opinion.trade`` does not resolve). The official
``opinion-clob-sdk`` 0.7.0 speaks to this host on chain id 56 (BNB mainnet)
only.

Public reads do not send an API key. ``OPINION_API_KEY`` is not read and is
not sent. A key in the environment does not change these reads. HTTP 401 from
the venue still raises ``AuthenticationError``. There is no public trade
feed. ``get_trades`` and ``iter_trades`` raise ``NotImplementedError`` before
any request. Own-trade history is planned for trading 1.0. Price history
(``/openapi/token/price-history``) is candles, not trades, and is not used
here.

Endpoints used:

- ``GET /openapi/market`` — page (``page`` starts at 1, ``limit``). The server
  default for ``limit`` is 10. Values above 20 are silently clamped to 20.
  Every request in one listing sends the same ``limit``,
  ``min(page_size, 20)``. The caller's ``limit`` truncates the iterator on
  the client. ``status`` is ``activated`` or ``resolved``. ``marketType`` 0
  is binary, 1 is categorical, 2 is all. This client sends 2 unless
  ``venue_params['market_type']`` says otherwise.
- ``GET /openapi/market/{marketId}`` — one binary market, including a
  categorical child. Unknown ids are HTTP 200 with ``errno`` 10200.
  Binary and categorical ids are independent and overlap, so this path is
  used only for a bare numeric library id.
- ``GET /openapi/market/categorical/{marketId}`` — one categorical parent.
  Library id ``categorical-<id>`` (hyphen, not a colon). An unknown parent
  is errno 0 with ``result.data`` null, or errno 10200, and raises
  ``NotFoundError``. This client does not fall back from one path to the
  other.
- ``GET /openapi/token/orderbook?token_id=`` — bids and asks for one token.
  Prices are decimal probabilities in ``[0, 1]``. Sizes are share counts.
  This client always requests the YES token. The NO book is derived from
  that YES book (``1 - price``, sides swapped) and is not fetched separately.

Envelope: ``{errno, errmsg, result}``. ``errno`` 0 is success. Quote token on
chain 56 is USDT (18 decimals). ``Market.currency`` is ``USD`` for those
notionals. ``volume`` is a human decimal string, not wei. There is no
liquidity field. List payloads have no best bid or ask, so ``Outcome.price``
is ``None`` unless the market (or a categorical child) is resolved.

``OPEN`` means a new order can be placed right now. Only status ``Activated``
(2) is ``OPEN``, and only while ``cutoffAt`` is unset or still in the future.
A positive ``cutoffAt`` that is already in the past maps to ``CLOSED`` even
when the status is ``Activated``. ``0`` and a missing cutoff are unset, not
past. ``Created`` (1), ``Resolving`` (3), ``Failed`` (5), and ``Deleted`` (6)
are ``CLOSED``. ``Resolved`` (4) is ``RESOLVED`` and is not overridden by the
cutoff. The raw status stays on ``venue_status`` and on ``raw_data``. An
unknown code logs one warning and leaves ``status`` unset.

A categorical parent uses the same rule on both the list payload and
``GET /openapi/market/categorical/{id}``. Those two payloads can disagree on
the parent status (list ``Activated``, detail ``Created``). ``Resolved`` on
the parent stays ``RESOLVED``. A parent whose children are all ``Resolved``
is ``RESOLVED`` as well, including when the parent's own status is still
``Activated``, and ``list_markets(closed=True)`` returns it. ``venue_status``
stays the raw parent status. Otherwise the parent is ``OPEN`` when at least
one child is ``Activated`` with a cutoff that is unset or still in the future,
and the parent's own ``cutoffAt`` is unset or still in the future. Every
other parent is ``CLOSED``.

The library id of a categorical parent is ``categorical-<venue id>``. The
venue number stays on ``raw_data['marketId']``. A colon would collide with
outcome ids. Binary markets, including categorical children fetched on their
own, keep the bare numeric id.

Outcome names are the venue labels (team names, ``YES`` / ``NO``, or anything
else). Outcome ids are ``{library market id}:{tokenId}``. A categorical
child outcome is ``categorical-<parent id>:<tokenId>``. A bare market id
addresses the YES-token book of a binary market via ``GET /openapi/market/{id}``
only. A categorical parent has no single book. A full outcome id is checked
with ``get_market`` before the book request, so the book uses the same
endpoint as that market id.

Anonymous rate limit observed 2026-10-29: ``ratelimit-limit: 5`` and
``ratelimit-reset: 1`` (5 requests per second per IP). The docs also describe
15 requests per second for an authenticated consumer. HTTP 429 maps to
``RateLimitError``. ``Retry-After`` wins, then ``ratelimit-reset`` (seconds).

Trading and websockets are not implemented. ``place_order`` validates a
dry-run and does not submit.
"""

import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import quote

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

_PAGE_MAX = 20
_VENUE = "opinion"
_NOT_FOUND_ERRNO = 10200
# Hyphen, not a colon: outcome ids are ``{market id}:{token id}``.
_CATEGORICAL_PREFIX = "categorical-"
_NO_PUBLIC_TRADES = (
    "Opinion does not publish a public trade feed. Own-trade history is planned for trading 1.0."
)
_PARAM_KEYS = frozenset({"market_type", "sort_by", "chain_id", "collection_type"})
_STATUS_BY_CODE: dict[int, tuple[MarketStatus, bool]] = {
    1: (MarketStatus.CLOSED, False),  # Created
    2: (MarketStatus.OPEN, False),  # Activated: a new order can be placed
    3: (MarketStatus.CLOSED, False),  # Resolving: not matching a new order
    4: (MarketStatus.RESOLVED, True),
    5: (MarketStatus.CLOSED, False),  # Failed
    6: (MarketStatus.CLOSED, False),  # Deleted
}
_STATUS_BY_NAME: dict[str, tuple[MarketStatus, bool]] = {
    "created": _STATUS_BY_CODE[1],
    "activated": _STATUS_BY_CODE[2],
    "resolving": _STATUS_BY_CODE[3],
    "resolved": _STATUS_BY_CODE[4],
    "failed": _STATUS_BY_CODE[5],
    "deleted": _STATUS_BY_CODE[6],
}


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


def _unix(value: Any) -> datetime | None:
    """Parse a unix timestamp. Values of ``10**12`` or more are milliseconds."""
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    try:
        return parse_timestamp(number, is_milliseconds=number >= 10**12)
    except (ValueError, OverflowError, OSError):
        return None


def _library_id(venue_id: str, *, categorical: bool) -> str:
    """Map a venue market number onto the library id.

    Categorical parents are ``categorical-<id>``. Binary markets stay bare.
    The venue number itself is left on ``raw_data``.
    """
    if categorical:
        return f"{_CATEGORICAL_PREFIX}{venue_id}"
    return venue_id


def _split_library_id(market_id: str) -> tuple[str, bool]:
    """Return ``(venue id, is_categorical_parent)`` for a library market id."""
    if market_id.startswith(_CATEGORICAL_PREFIX):
        venue_id = market_id[len(_CATEGORICAL_PREFIX) :]
        if not venue_id:
            raise NotFoundError(
                f"Market not found: {market_id}",
                resource_type="market",
                resource_id=market_id,
            )
        return venue_id, True
    return market_id, False


def _parse_outcome_id(outcome_id: str) -> tuple[str, str | None]:
    """Split ``{market_id}`` or ``{market_id}:{token_id}``.

    The market id may be ``categorical-<id>``. The separator is the first
    colon. Token ids are decimal strings and do not contain colons, so the
    prefix hyphen does not split the id. A bare numeric id addresses the
    YES-token book of a binary market. ``categorical-<id>`` addresses the
    parent and has no single book.
    """
    if not outcome_id or not outcome_id.strip():
        raise ValueError("outcome id must not be empty")
    if ":" not in outcome_id:
        return outcome_id, None
    market_id, token_id = outcome_id.split(":", 1)
    if not market_id or not token_id:
        raise ValueError(
            "outcome id must be '{market_id}' or '{market_id}:{token_id}', "
            f"got {outcome_id!r}"
        )
    return market_id, token_id


def _status(raw: dict[str, Any]) -> tuple[MarketStatus | None, bool, str]:
    """Map Opinion status onto the shared enum.

    Only ``Activated`` (2) is ``OPEN``. ``Resolving`` is ``CLOSED`` because a
    new order cannot be placed. Unknown codes are not mapped.
    """
    code = raw.get("status")
    enum = raw.get("statusEnum")
    if isinstance(enum, str) and enum.strip():
        venue_status = enum.strip()
    elif code is not None and code != "":
        venue_status = str(code)
    else:
        venue_status = ""
    mapped: tuple[MarketStatus, bool] | None = None
    if isinstance(code, int) or (isinstance(code, str) and str(code).lstrip("-").isdigit()):
        mapped = _STATUS_BY_CODE.get(int(code))
    if mapped is None and isinstance(enum, str):
        mapped = _STATUS_BY_NAME.get(enum.strip().lower())
    if mapped is None:
        logger.warning(
            "Unknown Opinion market status for %s: status=%s statusEnum=%s",
            raw.get("marketId"),
            code,
            enum,
        )
        return None, False, venue_status
    status, resolved = mapped
    return status, resolved, venue_status


def _cutoff_in_past(value: Any) -> bool:
    """True when ``cutoffAt`` is a positive unix time already before now.

    ``0`` and a missing value are unset, not in the past.
    """
    moment = _unix(value)
    if moment is None:
        return False
    return moment < datetime.now(timezone.utc)


def _apply_open_cutoff(
    raw: dict[str, Any], status: MarketStatus | None, resolved: bool
) -> tuple[MarketStatus | None, bool]:
    """Map an otherwise-open market to ``CLOSED`` once ``cutoffAt`` has passed.

    ``RESOLVED`` and every non-open status stay as ``_status`` mapped them.
    ``venue_status`` is left as the raw status, typically ``Activated``.
    """
    if status == MarketStatus.OPEN and _cutoff_in_past(raw.get("cutoffAt")):
        return MarketStatus.CLOSED, False
    return status, resolved


def _child_tradable(child: dict[str, Any]) -> bool:
    """A child can still take an order when it is ``Activated`` and not past cutoff."""
    status, _resolved, _venue_status = _status(child)
    if status != MarketStatus.OPEN:
        return False
    return not _cutoff_in_past(child.get("cutoffAt"))


def _categorical_flags(raw: dict[str, Any]) -> tuple[MarketStatus | None, bool, str]:
    """Parent status shared by the list row and the categorical detail.

    ``Resolved`` on the parent stays ``RESOLVED``. A parent whose children are
    all ``Resolved`` is ``RESOLVED`` too, even when the parent's own status is
    ``Activated``, so ``closed=True`` returns it. ``venue_status`` stays the
    raw parent status. Otherwise the parent is ``OPEN`` when at least one
    child is tradable and the parent's own ``cutoffAt`` is unset or still in
    the future. List ``Activated`` and detail ``Created`` can differ; the
    normalized status does not.
    """
    status, resolved, venue_status = _status(raw)
    if status == MarketStatus.RESOLVED:
        return status, resolved, venue_status
    children = raw.get("childMarkets")
    child_rows: list[dict[str, Any]] = []
    if isinstance(children, list):
        child_rows = [child for child in children if isinstance(child, dict)]
    if child_rows and all(_child_is_resolved(child) for child in child_rows):
        return MarketStatus.RESOLVED, True, venue_status
    tradable = any(_child_tradable(child) for child in child_rows)
    if tradable and not _cutoff_in_past(raw.get("cutoffAt")):
        return MarketStatus.OPEN, False, venue_status
    return MarketStatus.CLOSED, False, venue_status


def _child_is_resolved(child: dict[str, Any]) -> bool:
    status, resolved, _venue_status = _status(child)
    return resolved and status == MarketStatus.RESOLVED


def _resolved_price(
    token_id: str, result_token_id: str, resolved: bool
) -> tuple[Decimal | None, str | None]:
    if not resolved or not result_token_id:
        return None, None
    if token_id == result_token_id:
        return Decimal("1"), "1"
    return Decimal("0"), "0"


def _winner(outcomes: list[Outcome], resolved: bool) -> str | None:
    if not resolved:
        return None
    names = [outcome.name for outcome in outcomes if outcome.price == Decimal("1")]
    if len(names) == 1:
        return names[0]
    return None


def _is_categorical(raw: dict[str, Any]) -> bool:
    if raw.get("marketType") == 1:
        return True
    children = raw.get("childMarkets")
    yes = raw.get("yesTokenId")
    return isinstance(children, list) and len(children) > 0 and not yes


def _one_outcome(
    market_id: str,
    token_id: str,
    name: str,
    result_token_id: str,
    resolved: bool,
) -> Outcome:
    price, raw_price = _resolved_price(token_id, result_token_id, resolved)
    return Outcome(
        id=f"{market_id}:{token_id}",
        market_id=market_id,
        name=name,
        price=price,
        raw_price=raw_price,
    )


def _binary_outcomes(market_id: str, raw: dict[str, Any], resolved: bool) -> list[Outcome]:
    result = str(raw.get("resultTokenId") or "")
    outcomes: list[Outcome] = []
    for token_key, label_key in (("yesTokenId", "yesLabel"), ("noTokenId", "noLabel")):
        token = str(raw.get(token_key) or "")
        if not token:
            continue
        label = raw.get(label_key)
        name = label.strip() if isinstance(label, str) and label.strip() else token
        outcomes.append(_one_outcome(market_id, token, name, result, resolved))
    return outcomes


def _categorical_outcomes(market_id: str, raw: dict[str, Any]) -> list[Outcome]:
    children = raw.get("childMarkets")
    if not isinstance(children, list):
        return []
    outcomes: list[Outcome] = []
    for child in children:
        if not isinstance(child, dict):
            continue
        title = child.get("marketTitle")
        title_text = title.strip() if isinstance(title, str) else ""
        _child_status, child_resolved, _venue = _status(child)
        result = str(child.get("resultTokenId") or "")
        for token_key, label_key in (("yesTokenId", "yesLabel"), ("noTokenId", "noLabel")):
            token = str(child.get(token_key) or "")
            if not token:
                continue
            label = child.get(label_key)
            label_text = label.strip() if isinstance(label, str) and label.strip() else token
            name = f"{title_text}: {label_text}" if title_text else label_text
            outcomes.append(_one_outcome(market_id, token, name, result, child_resolved))
    return outcomes


def _parse_market(raw: dict[str, Any]) -> Market:
    market_id_raw = raw.get("marketId")
    if market_id_raw is None or market_id_raw == "":
        raise ValueError("market id missing")
    venue_id = str(market_id_raw)
    categorical = _is_categorical(raw)
    market_id = _library_id(venue_id, categorical=categorical)
    if categorical:
        status, resolved, venue_status = _categorical_flags(raw)
        outcomes = _categorical_outcomes(market_id, raw)
    else:
        status, resolved, venue_status = _status(raw)
        status, resolved = _apply_open_cutoff(raw, status, resolved)
        outcomes = _binary_outcomes(market_id, raw, resolved)
    created = _unix(raw.get("createdAt"))
    partial = created is None
    if created is None:
        created = datetime.now(timezone.utc)
    title = raw.get("marketTitle")
    question = title if isinstance(title, str) else ""
    rules = raw.get("rules")
    description = rules if isinstance(rules, str) and rules.strip() else None
    volume = _decimal(raw.get("volume"))
    if volume is not None and volume < 0:
        volume = None
    return Market(
        id=market_id,
        venue=_VENUE,
        question=question,
        description=description,
        outcomes=outcomes,
        created_at=created,
        end_date=_unix(raw.get("cutoffAt")),
        resolved=resolved,
        venue_status=venue_status,
        status=status,
        winning_outcome=_winner(outcomes, resolved),
        volume=volume,
        open_interest=None,
        best_bid_size=None,
        best_ask_size=None,
        liquidity=None,
        currency=Currency.USD,
        raw_data=raw,
        partial=partial,
    )


def _token_role(raw: dict[str, Any], token_id: str) -> tuple[str, bool] | None:
    """Return ``(yes_token_id, complement)`` when ``token_id`` is on this market.

    The book request always uses the YES token. A NO token is the complement
    (``1 - price``, sides swapped).
    """
    yes = str(raw.get("yesTokenId") or "")
    no = str(raw.get("noTokenId") or "")
    if token_id == yes and yes:
        return yes, False
    if token_id == no and no and yes:
        return yes, True
    children = raw.get("childMarkets")
    if isinstance(children, list):
        for child in children:
            if not isinstance(child, dict):
                continue
            child_yes = str(child.get("yesTokenId") or "")
            child_no = str(child.get("noTokenId") or "")
            if token_id == child_yes and child_yes:
                return child_yes, False
            if token_id == child_no and child_no and child_yes:
                return child_yes, True
    return None


def _levels(raw: object, *, invert: bool) -> list[OrderLevel]:
    if not isinstance(raw, list):
        return []
    levels: list[OrderLevel] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        price = _in_unit_interval(_decimal(item.get("price")))
        size = _decimal(item.get("size"))
        if price is None or size is None or size <= 0:
            continue
        if invert:
            # The NO book is derived. raw_price is the computed probability,
            # not the venue string from the YES level.
            price = Decimal("1") - price
            raw_price: Any = str(price)
        else:
            raw_price = item.get("price")
        levels.append(OrderLevel(price=price, size=size, raw_price=raw_price))
    return levels


def _order_book(
    outcome_id: str,
    market_id: str,
    invert: bool,
    book: dict[str, Any],
) -> OrderBook:
    if invert:
        bids = _levels(book.get("asks"), invert=True)
        asks = _levels(book.get("bids"), invert=True)
    else:
        bids = _levels(book.get("bids"), invert=False)
        asks = _levels(book.get("asks"), invert=False)
    bids.sort(key=lambda level: level.price, reverse=True)
    asks.sort(key=lambda level: level.price)
    timestamp = _unix(book.get("timestamp"))
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


def _retry_after(response: httpx.Response) -> int | None:
    for header in ("Retry-After", "ratelimit-reset"):
        raw = response.headers.get(header)
        if raw is not None and raw.isdigit():
            return int(raw)
    return None


def _errno(body: dict[str, Any]) -> int | None:
    raw = body.get("errno")
    if raw is None:
        raw = body.get("code")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.lstrip("-").isdigit():
        return int(raw)
    return None


def _message(body: object, status_code: int) -> str:
    if isinstance(body, dict):
        text = body.get("errmsg")
        if not isinstance(text, str) or not text:
            text = body.get("msg")
        if isinstance(text, str) and text:
            return text
    return f"Opinion request failed with HTTP {status_code}"


def _result_list(body: object, url: str, what: str) -> list[dict[str, Any]]:
    if not isinstance(body, dict):
        raise VenueAPIError(f"Invalid Opinion {what} payload", venue=_VENUE, url=url)
    result = body.get("result")
    if result is None:
        return []
    if not isinstance(result, dict):
        raise VenueAPIError(f"Invalid Opinion {what} payload", venue=_VENUE, url=url)
    items = result.get("list")
    if items is None:
        items = result.get("data")
    if items is None:
        return []
    if not isinstance(items, list):
        raise VenueAPIError(f"Invalid Opinion {what} payload", venue=_VENUE, url=url)
    return [item for item in items if isinstance(item, dict)]


def _market_dict(body: object, url: str) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise VenueAPIError("Invalid Opinion market payload", venue=_VENUE, url=url)
    result = body.get("result")
    if isinstance(result, dict):
        # Unknown categorical ids are errno 0 with result.data null.
        if "data" in result and result.get("data") is None:
            raise NotFoundError(
                "Market not found",
                resource_type="market",
                resource_id=None,
            )
        data = result.get("data")
        if isinstance(data, dict) and "marketId" in data:
            return data
        if "marketId" in result:
            return result
    raise VenueAPIError("Invalid Opinion market payload", venue=_VENUE, url=url)


def _book_dict(body: object, url: str) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise VenueAPIError("Invalid Opinion order book payload", venue=_VENUE, url=url)
    result = body.get("result")
    if isinstance(result, dict):
        return result
    raise VenueAPIError("Invalid Opinion order book payload", venue=_VENUE, url=url)


class OpinionClient(BaseClient):
    """Read-only Opinion client.

    Public market, order book, and price reads do not send an API key.
    There is no public trade feed. ``place_order`` is a dry-run stub and
    does not submit.
    """

    API_BASE = "https://openapi.opinion.trade/openapi"

    def __init__(
        self,
        max_order_size: Decimal | None = None,
        timeout: float = 30.0,
        base_url: str | None = None,
    ):
        """Initialize the Opinion client.

        Args:
            max_order_size: Maximum order size accepted by ``place_order``.
            timeout: HTTP timeout in seconds.
            base_url: API origin including the ``/openapi`` prefix. Defaults
                to mainnet. No testnet host is published.
        """
        self._max_order_size = max_order_size
        self._timeout = timeout
        self._base_url = (base_url or self.API_BASE).rstrip("/")
        self._http_client = httpx.AsyncClient(timeout=timeout)

    def __repr__(self) -> str:
        return f"OpinionClient(base_url={self._base_url!r})"

    async def list_markets(
        self,
        closed: bool | None = None,
        limit: int | None = None,
        page_size: int = 100,
        venue_params: dict[str, Any] | None = None,
    ) -> AsyncIterator[Market]:
        """List markets from ``GET /openapi/market``.

        ``closed=False`` sends ``status=activated`` and then drops markets
        whose normalized status is not ``OPEN``. ``closed=True`` sends
        ``status=resolved``. ``closed=None`` omits ``status``. Pages use
        ``page`` (starting at 1) and a fixed ``limit`` of
        ``min(page_size, 20)``. The server default for ``limit`` is 10 and
        values above 20 are silently clamped to 20, so a shrinking page size
        would re-read earlier rows. The caller's ``limit`` truncates after
        parse. A 5xx on a later page raises ``VenueAPIError`` with that page
        number and cursor instead of ending the listing. There is no cursor
        token; the cursor text is the page number.

        ``venue_params``:

        - ``market_type``: ``0`` binary, ``1`` categorical, ``2`` all. Sent as
          ``marketType``. The default is ``2``.
        - ``sort_by``: ``0`` through ``8``, sent as ``sortBy`` only when set.
          The Market OpenAPI enum lists ``1`` through ``8``. The server also
          accepts ``0`` (``opinion_api`` ``NoSort``).
        - ``chain_id``: sent as ``chainId`` only when set.
        - ``collection_type``: ``0``, ``1``, or ``2``, sent as ``collectionType``
          only when set. This field is not in the Market OpenAPI. Live accepts
          ``0``, ``1``, and ``2``, and the reported total changes.

        Raises:
            ValueError: If ``limit`` or ``page_size`` is not positive, or
                ``venue_params`` has an unsupported key.
            AuthenticationError: If the venue rejects the request with HTTP 401.
            RateLimitError: If the venue returns HTTP 429.
            VenueAPIError: If a read request fails for another reason.
        """
        validate_limit(limit)
        if page_size <= 0:
            raise ValueError(f"page_size must be positive, got {page_size}")
        params = self._list_params(closed, venue_params)
        # Fixed server page size. Shrinking `limit` to the remainder keeps the
        # page number and re-reads rows the previous pages already returned.
        page_limit = min(page_size, _PAGE_MAX)
        yielded = 0
        seen_ids: set[str] = set()
        page_number = 0
        while True:
            if limit is not None and yielded >= limit:
                return
            page_number += 1
            query = dict(params)
            query["page"] = str(page_number)
            query["limit"] = str(page_limit)
            url = f"{self._base_url}/market"
            try:
                body = await self._request("GET", "/market", params=query)
            except VenueAPIError as exc:
                if exc.status_code is not None and exc.status_code >= 500:
                    raise VenueAPIError(
                        "Opinion list_markets failed on "
                        f"page {page_number} at cursor {page_number}: {exc}",
                        status_code=exc.status_code,
                        venue=exc.venue,
                        url=exc.url,
                    ) from exc
                raise
            raw_markets = _result_list(body, url, "markets")
            if not raw_markets:
                return
            novel = False
            for raw in raw_markets:
                if limit is not None and yielded >= limit:
                    return
                market = self._one_market(raw)
                if market is None or market.id in seen_ids:
                    continue
                novel = True
                seen_ids.add(market.id)
                if closed is False and market.status != MarketStatus.OPEN:
                    continue
                if closed is True and market.status != MarketStatus.RESOLVED:
                    continue
                yield market
                yielded += 1
            if not novel:
                logger.warning(
                    "Stopping Opinion market pagination: page %s repeated",
                    page_number,
                )
                return
            if limit is not None and yielded >= limit:
                return
            if len(raw_markets) < page_limit:
                return

    async def get_market(self, market_id: str) -> Market:
        """Get one market by library id.

        A bare id calls only ``GET /openapi/market/{id}`` (binary market or
        categorical child). ``categorical-<id>`` calls only
        ``GET /openapi/market/categorical/{id}``. Binary and categorical ids
        overlap, so this client does not choose the endpoint by errno 10200.
        A 10200 on a bare id raises ``NotFoundError`` and names
        ``categorical-<id>``. The venue number stays on ``raw_data``.

        Raises:
            NotFoundError: If the id is empty, the venue reports errno 10200,
                or the categorical payload has ``result.data`` null.
            AuthenticationError: If the venue rejects the request with HTTP 401.
            VenueAPIError: If the request fails for another reason.
        """
        if not market_id:
            raise NotFoundError(
                "Market not found: empty id",
                resource_type="market",
                resource_id=market_id,
            )
        venue_id, categorical = _split_library_id(market_id)
        if categorical:
            path = f"/market/categorical/{quote(venue_id, safe='')}"
        else:
            path = f"/market/{quote(venue_id, safe='')}"
        url = f"{self._base_url}{path}"
        try:
            body = await self._request("GET", path)
        except NotFoundError as exc:
            if categorical:
                message = f"Market not found: {market_id}"
            else:
                message = (
                    f"Market not found: {market_id}. "
                    f"Categorical parents use {_CATEGORICAL_PREFIX}{market_id}."
                )
            raise NotFoundError(
                message,
                resource_type="market",
                resource_id=market_id,
            ) from exc
        return self._require_market(body, url, market_id)

    async def get_order_book(self, outcome_id: str) -> OrderBook:
        """Get the order book for one outcome.

        ``GET /openapi/token/orderbook`` is per token. This client always
        requests the YES token. The NO book is derived from that YES book
        (``1 - price``, sides swapped) and is not fetched separately.
        ``raw_price`` on an inverted level is the computed probability string,
        not the venue string from the YES level. Outcome names are arbitrary.
        A bare market id addresses the YES book of a binary market and calls
        only ``GET /openapi/market/{id}``. A categorical parent id
        ``categorical-<id>`` has no single book and is loaded only from
        ``GET /openapi/market/categorical/{id}``. Child outcome ids are
        ``categorical-<parent id>:<tokenId>``; the first colon separates the
        token, so the book request uses the parent endpoint. ``get_price``
        uses this same lookup. An empty book is returned as-is. A crossed
        book raises ``ValueError``.

        Raises:
            NotFoundError: If the market or outcome has no book.
            ValueError: If ``outcome_id`` is malformed or the book is crossed.
            AuthenticationError: If the venue rejects the request with HTTP 401.
            VenueAPIError: If the request fails for another reason.
        """
        market_id, token_id = _parse_outcome_id(outcome_id)
        market = await self.get_market(market_id)
        raw = market.raw_data
        if token_id is None:
            if _is_categorical(raw):
                raise NotFoundError(
                    f"Order book for categorical market {market_id} needs an outcome id",
                    resource_type="outcome",
                    resource_id=outcome_id,
                )
            yes = str(raw.get("yesTokenId") or "")
            if not yes:
                raise NotFoundError(
                    f"Order book not found for outcome: {outcome_id}",
                    resource_type="outcome",
                    resource_id=outcome_id,
                )
            book_token, invert = yes, False
        else:
            if not any(outcome.id == outcome_id for outcome in market.outcomes):
                raise NotFoundError(
                    f"Outcome not found: {outcome_id}",
                    resource_type="outcome",
                    resource_id=outcome_id,
                )
            role = _token_role(raw, token_id)
            if role is None:
                raise NotFoundError(
                    f"Outcome not found: {outcome_id}",
                    resource_type="outcome",
                    resource_id=outcome_id,
                )
            book_token, invert = role
        path = "/token/orderbook"
        url = f"{self._base_url}{path}"
        body = await self._request("GET", path, params={"token_id": book_token})
        book = _order_book(outcome_id, market_id, invert, _book_dict(body, url))
        book.validate_not_crossed()
        return book

    async def get_price(self, outcome_id: str, side: OrderSide) -> Price:
        """Best ask for BUY and best bid for SELL.

        An empty side raises ``NoLiquidityError``. An unknown market or outcome
        raises ``NotFoundError`` from the order book request.
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
        """Opinion does not publish a public trade feed.

        This method does not call the venue and does not read an API key.
        Own-trade history is planned for trading 1.0.

        Raises:
            ValueError: If ``limit`` is not positive.
            NotImplementedError: Always. There is no public trade feed.
        """
        del outcome_id
        validate_limit(limit)
        raise NotImplementedError(_NO_PUBLIC_TRADES)
        yield  # pragma: no cover

    async def iter_trades(self, outcome_id: str) -> AsyncIterator[Trade]:
        """Opinion does not publish a public trade feed.

        This method does not call the venue. Own-trade history is planned
        for trading 1.0.
        """
        del outcome_id
        raise NotImplementedError(_NO_PUBLIC_TRADES)
        yield  # pragma: no cover

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
        an order and does not read an API key. ``kwargs`` is accepted because
        ``place_order`` still matches the base signature, which takes
        ``**kwargs`` until 1.0.
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
        raise PermissionError("Order placement is not implemented")

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http_client.aclose()

    def _read_headers(self) -> dict[str, str]:
        """Headers for one public read. No API key is sent."""
        return {"Accept": "application/json"}

    def _list_params(
        self, closed: bool | None, venue_params: dict[str, Any] | None
    ) -> dict[str, str]:
        options = venue_params or {}
        unknown = set(options) - _PARAM_KEYS
        if unknown:
            raise ValueError(
                f"unsupported venue_params: {', '.join(sorted(unknown))}. "
                "Supported keys: market_type, sort_by, chain_id, collection_type"
            )
        market_type = options.get("market_type", 2)
        if market_type not in (0, 1, 2) or isinstance(market_type, bool):
            raise ValueError("venue_params['market_type'] must be 0, 1, or 2")
        params: dict[str, str] = {"marketType": str(market_type)}
        if closed is False:
            params["status"] = "activated"
        elif closed is True:
            params["status"] = "resolved"
        if "sort_by" in options:
            sort_by = options["sort_by"]
            # OpenAPI lists 1..8. The server accepts 0 (opinion_api NoSort).
            if isinstance(sort_by, bool) or not isinstance(sort_by, int) or not 0 <= sort_by <= 8:
                raise ValueError("venue_params['sort_by'] must be an integer from 0 to 8")
            params["sortBy"] = str(sort_by)
        if "chain_id" in options:
            chain_id = options["chain_id"]
            if not isinstance(chain_id, str) or not chain_id.strip():
                raise ValueError("venue_params['chain_id'] must be a non-empty string")
            params["chainId"] = chain_id.strip()
        if "collection_type" in options:
            # Not in the Market OpenAPI. Live accepts 0, 1, and 2 and the
            # reported total changes.
            collection = options["collection_type"]
            if (
                isinstance(collection, bool)
                or not isinstance(collection, int)
                or collection
                not in (
                    0,
                    1,
                    2,
                )
            ):
                raise ValueError("venue_params['collection_type'] must be 0, 1, or 2")
            params["collectionType"] = str(collection)
        return params

    def _one_market(self, raw: dict[str, Any]) -> Market | None:
        try:
            return _parse_market(raw)
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            logger.warning(
                "Skipping Opinion market %s due to parse error: %s", raw.get("marketId"), exc
            )
            return None

    def _require_market(self, body: object, url: str, market_id: str) -> Market:
        try:
            raw = _market_dict(body, url)
        except NotFoundError as exc:
            raise NotFoundError(
                f"Market not found: {market_id}",
                resource_type="market",
                resource_id=market_id,
            ) from exc
        market = self._one_market(raw)
        if market is None or market.id != market_id:
            hint = ""
            if (
                market is not None
                and not market_id.startswith(_CATEGORICAL_PREFIX)
                and market.id == f"{_CATEGORICAL_PREFIX}{market_id}"
            ):
                hint = f" Categorical parents use {market.id}."
            raise NotFoundError(
                f"Market not found: {market_id}.{hint}",
                resource_type="market",
                resource_id=market_id,
            )
        return market

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> object:
        """Send one read request and map failures onto library errors.

        No API key is sent. The error text does not include environment values.
        """
        url = f"{self._base_url}{path}"
        headers = self._read_headers()
        try:
            response = await self._http_client.request(method, url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise VenueAPIError(
                f"HTTP error calling Opinion: {exc}",
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
                    hint="Public reads do not send an API key.",
                ) from exc
            if response.status_code == 429:
                raise RateLimitError(
                    "Rate limit exceeded",
                    venue=_VENUE,
                    url=str(response.request.url),
                    retry_after=_retry_after(response),
                ) from exc
            if response.status_code >= 500:
                raise VenueAPIError(
                    f"Opinion request failed with HTTP {response.status_code}: non-JSON body",
                    status_code=response.status_code,
                    venue=_VENUE,
                    url=str(response.request.url),
                ) from exc
            raise VenueAPIError(
                f"Invalid JSON from Opinion: {exc}",
                status_code=response.status_code,
                venue=_VENUE,
                url=str(response.request.url),
            ) from exc
        errno = _errno(body) if isinstance(body, dict) else None
        if response.status_code == 401 or errno in (11001, 11002):
            raise AuthenticationError(
                _auth_rejected_message(),
                status_code=response.status_code if response.status_code else 401,
                venue=_VENUE,
                hint="Public reads do not send an API key.",
            )
        if response.status_code == 404 or errno == _NOT_FOUND_ERRNO:
            raise NotFoundError(
                _message(body, response.status_code),
                resource_type="market",
                resource_id=None,
            )
        if response.status_code == 429:
            raise RateLimitError(
                _message(body, response.status_code),
                venue=_VENUE,
                url=str(response.request.url),
                retry_after=_retry_after(response),
            )
        failed = response.status_code >= 400 or (errno is not None and errno != 0)
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
        "Opinion rejected the request (HTTP 401). "
        "Public market, order book, and price reads do not send an API key."
    )
