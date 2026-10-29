# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Read-only **Opinion** adapter (`OpinionClient`, BNB Chain, chain id 56).
  Public market, order book, and price reads use `https://openapi.opinion.trade/openapi`
  and do not send an API key. `OPINION_API_KEY` is not read. There is no
  public trade feed: `get_trades` and `iter_trades` raise `NotImplementedError`
  before any request. Own-trade history is planned for trading 1.0. No testnet
  host is published. Docs: https://docs.opinion.trade/developer-guide/opinion-open-api/.
  Terms: https://docs.opinion.trade/legal/terms-of-use. `list_markets` uses
  `GET /openapi/market` with a fixed page size `min(page_size, 20)` (`page`
  starts at 1; server default 10, silently clamped to 20; `marketType` defaults
  to `2`). The caller's `limit` truncates on the client. `closed=False` sends
  `status=activated` and drops markets that are not `OPEN`. Only `Activated`
  (2) is `OPEN`, and only while `cutoffAt` is unset or still in the future. A
  past cutoff is `CLOSED` and `venue_status` stays `Activated`. `Created`,
  `Resolving`, `Failed`, and `Deleted` are `CLOSED`. `Resolved` (4) is
  `RESOLVED`.   A categorical parent is `OPEN` when at least one child is still
  tradable and the parent's own cutoff has not passed. A parent whose children
  are all `Resolved` is `RESOLVED` (raw status stays on `venue_status`) and
  `list_markets(closed=True)` returns it. List and get use that same rule.
  Categorical parent ids are `categorical-<id>` because binary and categorical
  numbers overlap; the venue number stays on `raw_data`. A bare id calls only
  `GET /openapi/market/{id}`, and `categorical-<id>` calls only
  `GET /openapi/market/categorical/{id}`. Child outcome ids are
  `categorical-<parent id>:<tokenId>`. An unknown categorical id
  (`result.data` null) is `NotFoundError`. A 5xx on a later page raises
  `VenueAPIError` naming that page and cursor. A non-JSON 5xx body names the
  HTTP status and `non-JSON body`. `get_order_book`
  requests the YES token and derives the NO book from it (`1 - price`, sides
  swapped); the NO book is not fetched separately. Outcome names are the venue
  labels. A full outcome id is checked with `get_market` before the book
  request. Quote notionals are USDT and are reported as `USD`.
  `Market.liquidity` is `None`. `Outcome.price` stays `None` on an open listing.
- Read-only **Predict.fun** adapter (`PredictFunClient`, BNB Chain). Mainnet
  `https://api.predict.fun` requires `PREDICTFUN_API_KEY` (`x-api-key`); the key
  is read only from that environment variable, is not logged, and is masked as
  `***` in `repr`. A missing mainnet key raises `AuthenticationError` before
  the request. HTTP 401 raises the same error. Testnet
  `https://api-testnet.predict.fun` needs no key (`base_url`). Mainnet keys
  come from https://developers.predict.fun. Docs: https://dev.predict.fun/
  and the live OpenAPI at https://api.predict.fun/docs. `list_markets`
  uses `GET /v1/markets` (`first` / `after`; server default 20, silently
  clamped to 100, no documented maximum; this client sends at most 100).
  `closed=False` sends `status=OPEN` and drops markets that are not tradable.
  Lifecycle `PAUSED` and `REMOVED`, and `tradingStatus` `CLOSED`, `PAUSED`,
  `CANCEL_ONLY`, and `MATCHING_NOT_ENABLED`, map to `CLOSED` (`OPEN` means a
  new order can be placed). `PRICE_PROPOSED` and `PRICE_DISPUTED` stay `OPEN`
  when `tradingStatus` is `OPEN`. Raw `status` stays on `venue_status`; both
  `status` and `tradingStatus` stay on `raw_data`. `closed=True` sends
  `status=RESOLVED`, `closed=None` omits `status`. A 5xx on a later page
  raises `VenueAPIError` naming that page and cursor. `get_market` uses
  `GET /v1/markets/{id}`. `get_order_book` / `get_price` use
  `GET /v1/markets/{id}/orderbook` (always the indexSet 1 book; no indexSet
  parameter; outcome names are arbitrary; indexSet 2 is the complement).
  `get_trades` / `iter_trades` use `GET /v1/orders/matches` (no JWT; server
  default `first` 50, clamped to 150; this client sends 100). A full outcome
  id is checked with `get_market` before any match request. Match
  `priceExecuted` and `amountFilled`
  are 18-decimal wei (USDT collateral) and are scaled by `10**-18`. `price` is
  `None` unless both deprecated `outcomes[].bestBid` and `bestAsk` are present.
  A resolved market
  uses `1` / `0`. `decimalPrecision` is the tick (`10 ** -decimalPrecision`);
  the book payload does not include it, so `tick_size` is unset.
  `stats.totalLiquidityUsd` stays on `raw_data` (`Market.liquidity` is `None`).
  Rate limits per key: 240 requests per minute for `/v1/markets` and
  `/v1/markets/{id}`; 500 requests per minute for
  `/v1/markets/{id}/orderbook`, `/quote`, and `/v1/orders/matches` (testnet
  `ratelimit-policy` headers, 2026-10-29). HTTP 429 maps to `RateLimitError`.
  Registry `geo_restrictions` is `Restricted by venue ToS: US, UK, France,
  Ontario, Singapore, Poland, Thailand, Australia, Belgium, Taiwan (see ToS
  for the full list)` (https://docs.predict.fun/terms-of-service). The Terms
  also name US-sanctioned territories and are not an exhaustive list. Trading
  is not implemented. Fixtures were recorded from testnet on 2026-10-29 because
  mainnet returns HTTP 401 without a key; the 401 body itself was recorded
  from mainnet.

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
