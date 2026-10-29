# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Trading models `OrderType`, `OrderStatus`, `Order`, and `Position`. Adapters do
  not return them yet. `Order` and `Position` are frozen. `price`,
  `avg_fill_price`, `avg_price`, and `current_price` are `Decimal` values in
  0..1. Timestamps on `Order` must be timezone-aware UTC. Unless `partial` is
  true, `filled_size + remaining_size` must equal `size`. `Position.size` is
  zero or positive: a NO holding is its own outcome, not a negative YES size.
- `AuthenticationError` (`PredictionMarketsError` and the builtin
  `PermissionError`) for missing credentials and HTTP 401/403, with `venue`,
  `status_code`, and `hint`. `except PermissionError` still catches it, so no
  deprecated alias is added. `OrderRejectedError` (`VenueAPIError`) for
  business-rule rejections, with `venue_code`. Adapters do not raise these yet.

- Read-only **Pascal** adapter (`PascalClient`, Solana, beta) on the public data API
  `https://data.pascal.trade` (no API key). `list_markets` reads `GET /api/v1/markets`
  (one unresolved snapshot; `limit` is local) and `GET /api/v1/markets/resolved`
  (lookback `4h` / `24h` / `3d`, `order_by` always sent, API `limit` only 50 / 100 / 200).
  `venue_params["query"]` calls `POST /api/v1/markets/text-search`. The trimmed query
  must be 1–32 characters or `list_markets` raises `ValueError` before the request.
  `order_by` is always sent and defaults to `volume_all_time` (the API default since
  2026-10-27). Text search is limited to 60 requests per minute per IP and 100 markets.
  `get_market` uses `POST /api/v1/markets/search`. `get_order_book` / `get_price` use
  `GET /api/v1/books` (one book per symbol; a single-symbol book may be up to one
  second old). `get_trades` / `iter_trades` use `GET /api/v1/trades` (`before_cursor`,
  page size up to 500). A `:short` outcome inverts each print (`1 - long price`,
  side flipped). A repeated trade cursor stops pagination and logs one warning.
  `price` is `None` unless both `best_bid` and `best_ask` are present, including
  a one-sided book; `mark_price` is not used as a stand-in. Optional `is_mece`
  (added 2026-10-28) is kept on `Market.raw_data` only. WebSockets, candles,
  positions, and trading are not implemented. Registry `geo_restrictions` is
  `Restricted by venue ToS: US, UK, Russia (see ToS for the full list)`
  (https://app.pascal.trade/terms.pdf).
  `GET /api/v1/accounts/{owner}/position-resolutions`
  gained a `symbols` filter of up to 100 symbols on 2026-10-27; positions stay out
  of this release. The docs do not publish a read-request weight; HTTP 429
  (`RATE_LIMITED`) maps to `RateLimitError`, and `SYMBOL_NOT_FOUND` maps to
  `NotFoundError`.

- Read-only **Hyperliquid Outcomes (HIP-4)** adapter (`HyperliquidClient`):
  `list_markets`, `get_market`, `get_order_book`, `get_price`, `get_trades`, and
  `iter_trades` on the public `POST https://api.hyperliquid.xyz/info` API.
  Listing uses `outcomeMeta` (plus `outcomeTemplates` for display names and
  `allMids` for prices). Order books use `l2Book` and trades use `recentTrades`,
  addressed by the `#<10 * outcome + side>` coin. `get_market` loads one settled
  outcome by id via `settledOutcome`. `list_markets(closed=True)` raises
  `NotImplementedError` (`supports_closed_markets` is false): settled markets are
  not listable. `recentTrades` returns a single non-paginated window.
  `outcomeMeta` does not include creation time, volume, open interest, or liquidity.

### Fixed

- Hyperliquid question fallbacks with an empty order book no longer take the
  `allMids` mid (often `0.5`). `Outcome.price` is `None` when that outcome has
  no order book. This is guaranteed only for outcomes whose adapter checks the
  book, which is currently Hyperliquid fallback outcomes. Each `list_markets`
  or `get_market` call checks those books with side-0 `l2Book` requests (info
  weight 2), at most 8 in flight, and does not keep the result on the client.
  A failed check (404, 429, 5xx, timeout) sets that leg's price to `None`,
  emits one warning, and does not abort the listing. A fallback that has levels
  keeps its mid. `raw_price` is `None` when `price` is `None`.
  `list_markets(limit=N)` still returns N markets when a question in that
  window cannot be parsed.
- Hyperliquid unit tests pin `datetime.now` to the fixture snapshot
  (2026-10-23) so the recorded BTC daily, which expires 2026-10-24 06:00 UTC,
  stays open.

## [0.9.0] - Unreleased

Read-only pre-release. Market data can be listed, fetched, and normalized across
venues. `place_order` remains a dry-run stub and still accepts `**kwargs`; unknown
keywords on `place_order` are not rejected until 1.0.

### Added

- Unified async read client for three venues: **Polymarket**, **Kalshi**, and **Limitless**
  (`list_markets`, `get_market`, `get_order_book`, `get_price`, `get_trades`, `iter_trades`).
- Venue registry in `prediction_markets_api.venues` (`VENUES`), including plain URLs and
  referral links. Limitless signs up through
  `https://limitless.exchange?r=0D2RQUXIT4` (referral code `r=0D2RQUXIT4`). Polymarket and
  Kalshi are registered with plain links and no referral URL. The README venues table is
  generated from this registry. Additional venues are listed as planned and are not implemented.
- Publish workflow (`.github/workflows/publish.yml`) that builds an sdist and a wheel and
  uploads them to TestPyPI with Trusted Publishing (OIDC, no API tokens). It runs only from
  `workflow_dispatch`. A PyPI job is included for a manual dispatch or a published GitHub
  Release, and uses the `pypi` environment. Nothing is published on push.

### Fixed

- Every public read method that accepts `limit` (`list_markets` and `get_trades` on each
  adapter) raises `ValueError` when `limit <= 0`, before any HTTP request. `None` still
  means unlimited on `list_markets`.
- `get_trades` matches `BaseClient` under strict mypy. The `type: ignore[override]` comments
  are removed. The signature has no `**kwargs`, so an unknown keyword argument raises `TypeError`.
  Venue-specific filters stay keyword-only (`page_size`, `start_ts` / `end_ts` on Polymarket,
  `min_ts` / `max_ts` on Kalshi).
