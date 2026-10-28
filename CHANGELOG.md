# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
  no order book. One `l2Book` request per priced fallback (side 0) decides
  this; a fallback that has levels keeps its mid. `raw_price` is `None` when
  `price` is `None`.

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
