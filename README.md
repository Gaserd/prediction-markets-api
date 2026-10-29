# prediction-markets-api

[![CI](https://github.com/prediction-markets-api/prediction-markets-api/workflows/CI/badge.svg)](https://github.com/prediction-markets-api/prediction-markets-api/actions)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A unified Python client for prediction markets. Currently supports **Polymarket**, **Kalshi**, **Limitless**, **Hyperliquid Outcomes (HIP-4)**, and **Pascal** (Solana, beta).

## Features

- **Unified API** across multiple prediction market venues
- **Type-safe** data models with Pydantic validation
- **Normalized data**: Prices in 0..1 probability space, UTC timestamps, explicit currencies
- **Safe trading**: `dry_run` mode ON by default
- **Async-first** design with `httpx`
- **Well-tested** with recorded API fixtures

## Installation

```bash
pip install prediction-markets-api
```

For development:

```bash
pip install prediction-markets-api[dev]
```

## Quick Start

### Reading Market Data

```python
import asyncio
from prediction_markets_api import PolymarketClient


async def main():
    async with PolymarketClient() as client:
        # List active markets
        async for market in client.list_markets(closed=False, limit=5):
            print(f"\n{market.question}")
            print(f"  Volume: ${market.volume:,.2f}")

            for outcome in market.outcomes:
                print(f"  - {outcome.name}: {outcome.price:.2%}")

            # Get order book for first outcome
            if market.outcomes:
                outcome = market.outcomes[0]
                book = await client.get_order_book(outcome.id)

                if book.bids and book.asks:
                    print(f"  Spread: {book.bids[0].price:.4f} / {book.asks[0].price:.4f}")


asyncio.run(main())
```

### Getting Prices

```python
import asyncio
from prediction_markets_api import PolymarketClient
from prediction_markets_api.models.base import OrderSide


async def main():
    async with PolymarketClient() as client:
        # Get first market with outcomes
        async for market in client.list_markets(closed=False, limit=10):
            if market.outcomes:
                outcome = market.outcomes[0]
                break

        # Get best executable prices
        buy_price = await client.get_price(outcome_id=outcome.id, side=OrderSide.BUY)
        sell_price = await client.get_price(outcome_id=outcome.id, side=OrderSide.SELL)

        print(f"Best ask (to buy): {buy_price.price:.4f}")
        print(f"Best bid (to sell): {sell_price.price:.4f}")


asyncio.run(main())
```

### Getting Historical Trades

```python
import asyncio
from prediction_markets_api import PolymarketClient


async def main():
    async with PolymarketClient() as client:
        # Get first market with outcomes
        async for market in client.list_markets(closed=False, limit=10):
            if market.outcomes:
                outcome = market.outcomes[0]
                break

        # Get historical trades
        async for trade in client.get_trades(outcome_id=outcome.id, limit=10):
            print(f"{trade.timestamp}: {trade.side.value} {trade.size} @ {trade.price}")


asyncio.run(main())
```

## How It Works

### Data Model Contract

All venues return data through a unified model with strict guarantees:

**Price Normalization:**
- Prices are normalized to 0..1 probability space (e.g., 0.65 = 65% probability)
- price is None when the venue has no order book for that outcome. An empty book is None for Hyperliquid fallback outcomes (after an `l2Book` check). A one-sided book (only a bid or only an ask) is None for Pascal. Pascal does not substitute `mark_price`.
- Original venue prices preserved in `raw_price` field for reference
- `OrderSide.BUY` means buying the outcome (get_price returns best ask)
- `OrderSide.SELL` means selling the outcome (get_price returns best bid)

**Timestamps:**
- All timestamps are timezone-aware UTC (never naive datetimes)
- Created dates, end dates, and trade timestamps use `datetime` with `tzinfo=timezone.utc`

**Currency:**
- All amounts include explicit currency (`Currency.USD`, `Currency.USDC`)
- No ambiguous currency-free numbers

**Partial Responses:**
- Markets with incomplete data are flagged with `partial=True`
- Only raise errors for invalid data, not incomplete data
- Allows graceful degradation when APIs return partial information

**Order Book Semantics:**
- Bids sorted descending (best bid = highest price first)
- Asks sorted ascending (best ask = lowest price first)
- Books validated to never be crossed (best_bid < best_ask)
- Sizes: `best_bid_size`, `best_ask_size`, `open_interest`, `liquidity`

**Market Status:**
- Each market includes `venue_status` (raw venue string) and `status` (normalized enum)
- Normalized statuses: `MarketStatus.OPEN`, `MarketStatus.CLOSED`, `MarketStatus.RESOLVED`
- `resolved=True` means the outcome is known (may still be settling)
- `winning_outcome: str | None` contains the name of the winning outcome when resolved (e.g., "Yes", "No")
- Unknown venue statuses log a warning and return `status=None` (not silently mapped)

**Status Mapping Table:**

| Venue | Raw Status | Normalized Status | Resolved | Notes |
|-------|------------|------------------|----------|-------|
| **Kalshi** | `active` | `OPEN` | False | Market is open for trading (filter: `open`) |
| | `initialized` | `CLOSED` | False | Not yet opened, excluded by `closed=None` (filter: `unopened`) |
| | `determined` | `RESOLVED` | True | Outcome known, settling (filter: `closed`) |
| | `finalized` | `RESOLVED` | True | Fully settled and paid out (filter: `settled`) |
| **Polymarket** | `open` | `OPEN` | False | Market is open for trading (`closed=false`) |
| | `closed` | `CLOSED` | False | Market closed but outcome not yet determined |
| | `resolved` | `RESOLVED` | True | Market resolved, outcome prices settled to 0/1 |
| **Limitless** | `FUNDED` | `OPEN` | False | Market is active and funded |
| **Hyperliquid Outcomes** | `open` | `OPEN` | False | Listed by `outcomeMeta` and the expiry is still in the future |
| | `expired` | `CLOSED` | False | Expiry has passed but the outcome is still listed (not settled) |
| | `settled` | `RESOLVED` | True | Loaded by id via `settledOutcome`. `settleFraction` `1` / `0` sets the winning side |
| **Pascal** | `live` | `OPEN` | False | Unresolved market from `GET /api/v1/markets`. `price` is `None` unless both best bid and best ask are present |
| | `resolved` | `RESOLVED` | True | `resolution.price` of `1` / `0` names the long / short outcome. Listed via `GET /api/v1/markets/resolved` |

**Notes:**
- Kalshi filter parameter names differ from response status values. The table shows response values with filter parameters in parentheses.
- Kalshi resolved markets with `result='scalar'` (e.g., cancelled events): These are marked as `resolved=True` with `status=RESOLVED` but `winning_outcome=None` since no yes/no outcome won. This is acceptable and documented.
- Polymarket resolution detection:
  - No explicit resolution status fields in API response (umaResolutionStatuses always empty, resolvedBy/resolutionSource not status indicators)
  - Resolution determined by strict price settlement check: >0.999 and <0.001 thresholds
  - This minimizes false positives from markets trading near certainty but not yet formally resolved
  - Winning outcome automatically detected from settled prices
- Limitless only exposes `FUNDED` markets via `/markets/active` endpoint. Calling `list_markets(closed=True)` raises `NotImplementedError` with a clear explanation, since the API does not provide an endpoint for closed/resolved markets.
- Hyperliquid Outcomes lists live markets from `outcomeMeta` only. `list_markets(closed=True)` raises `NotImplementedError`. A single settled outcome can still be loaded with `get_market` when its numeric id is known (`settledOutcome`). `recentTrades` is one short window with no cursor, and `l2Book` returns at most 20 levels per side. `price` is `None` for a fallback outcome only after this adapter checks that outcome's book.
- Pascal (beta, Solana) lists unresolved markets from `GET /api/v1/markets` and resolved markets from `GET /api/v1/markets/resolved` (lookback `4h`, `24h`, or `3d` only). See [Pascal](#pascal).

### Adapter Architecture

Each venue implements the `BaseClient` interface:

```python
import asyncio
from prediction_markets_api import PolymarketClient, KalshiClient


async def main():
    # All clients provide the same methods:
    async with PolymarketClient() as client:
        async for market in client.list_markets(limit=10):  # Standard method
            print(market.question)


asyncio.run(main())
```

**Unified Methods (Read-Only for 0.9):**
- `list_markets(closed, limit, page_size, *, venue_params)` - List markets with pagination
- `iter_markets(closed)` - Iterate all markets (no limit)
- `get_market(market_id)` - Get single market by ID
- `get_order_book(outcome_id)` - Get sorted, validated order book
- `get_price(outcome_id, side)` - Get best executable price
- `get_trades(outcome_id, limit, page_size)` - Historical trades iterator (up to `limit` total, default 100)
- `iter_trades(outcome_id)` - Iterate all historical trades (unbounded)

> **Note:** Trading functionality with `place_order()` is planned for version 1.0. Version 0.9 is read-only.

**Venue-Specific Parameters:**
- Venue-specific filters are keyword-only arguments (no `**kwargs`)
- Example: `client.list_markets(category="politics")` for Kalshi
- Prevents accidental parameter mismatches between venues

### API Keys and Configuration

**Version 0.9 (Read-Only):** No authentication required. All market data, prices, order books, and trades are accessible without API keys.

**Trading (Planned for 1.0):** Will require API keys via environment variables.

### Geographic Restrictions

Be aware of venue restrictions:
- **Polymarket**: US users blocked
- **Kalshi**: US only (CFTC regulated)
- **Limitless**: No restrictions (planned)
- **Pascal**: Restricted by venue ToS: US, UK, Russia (see ToS for the full list). Terms: https://app.pascal.trade/terms.pdf. The product is in beta.

### Exception Hierarchy

The library provides a consistent exception hierarchy:

```python
from prediction_markets_api import (
    PredictionMarketsError,  # Base exception
    NotFoundError,  # Market/outcome/token not found
    NoLiquidityError,  # Empty order book for existing market
    VenueAPIError,  # HTTP errors, invalid responses
    RateLimitError,  # HTTP 429 rate limit
)
```

**Exception Types:**

- **`PredictionMarketsError`**: Base exception for all library errors
- **`NotFoundError`**: Resource not found (markets, outcomes, tokens)
  - Attributes: `resource_type`, `resource_id`
  - Raised on HTTP 404 or empty results
- **`NoLiquidityError`**: No liquidity available (empty order book for an existing market)
  - Attributes: `market_id`, `side` (BUY/SELL)
  - Raised by `get_price()` when the order book has no bids/asks on the requested side
  - Different from `NotFoundError` (market doesn't exist) vs. market exists but has no liquidity
- **`VenueAPIError`**: HTTP errors, timeouts, invalid JSON responses
  - Attributes: `status_code`, `venue`, `url`
  - Wraps all `httpx` errors for consistent handling
- **`RateLimitError`**: Rate limit exceeded (HTTP 429)
  - Subclass of `VenueAPIError`
  - Attributes: `retry_after` (seconds, from Retry-After header if available)

**User Input Errors:**
- `ValueError`: Invalid parameters (e.g., price not in 0..1, negative size)
- `NotImplementedError`: Unsupported operations (e.g., `LimitlessClient.list_markets(closed=True)`, `HyperliquidClient.list_markets(closed=True)`)

**Example:**

```python
import asyncio
from prediction_markets_api import PolymarketClient, NotFoundError, VenueAPIError


async def main():
    async with PolymarketClient() as client:
        try:
            market = await client.get_market("nonexistent")
        except NotFoundError as e:
            print(f"Market not found: {e.resource_id}")
        except VenueAPIError as e:
            print(f"API error: {e.status_code} from {e.venue}")


asyncio.run(main())
```

## Data Model

All venues return data through a unified model:

### Core Types

- **Market**: Question, outcomes, volume, liquidity, timestamps
- **Outcome**: Individual tradeable token (e.g., "Yes"/"No"). price is None when the venue has no order book for that outcome. An empty book is None for Hyperliquid fallback outcomes. A one-sided book is None for Pascal.
- **OrderBook**: Sorted bids/asks, never crossed
- **Price**: Best executable price for a side
- **Trade**: Historical trade record

### Key Guarantees

1. **Prices normalized to 0..1** (probability space)
   - Original venue prices preserved in `raw_price` field
2. **All timestamps are timezone-aware UTC**
   - Never naive datetimes
3. **Amounts include explicit currency** (`USD`, `USDC`)
4. **Partial responses allowed** (flagged, not an error)
   - Only raise on invalid data
5. **Order books are sorted and validated**
   - Bids descending, asks ascending
   - Never crossed (best_bid < best_ask)

### Example: Market Object

```python
from datetime import datetime, timezone
from decimal import Decimal
from prediction_markets_api.models.base import Market, Outcome, Currency

market_id = "0x1234..."
market = Market(
    id=market_id,
    venue="polymarket",
    question="Will Bitcoin reach $100k by Dec 31?",
    outcomes=[
        Outcome(id="token_yes", market_id=market_id, name="Yes", price=Decimal("0.65")),
        Outcome(id="token_no", market_id=market_id, name="No", price=Decimal("0.35")),
    ],
    created_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
    end_date=datetime(2026, 12, 31, tzinfo=timezone.utc),
    volume=Decimal("1250000.50"),
    currency=Currency.USDC,
    partial=False,
)
print(market.question)
```

## Examples

The `examples/` directory contains runnable examples:

### Compare Same Event Across Venues

```bash
python examples/compare_venues.py
```

Search for the same event (e.g., Bitcoin predictions) across Polymarket and Kalshi, comparing prices and volumes.

### Price History Analysis

```bash
python examples/price_history.py
```

Fetch historical trades for an outcome and calculate price statistics, movements, and spreads.

### Export to Pandas

```bash
pip install prediction-markets-api[pandas]
python examples/export_to_pandas.py
```

Export market data and trades to pandas DataFrames for statistical analysis, visualization, or CSV export.

See [examples/README.md](examples/README.md) for details.

## Trading Configuration

### API Keys (Environment Variables Only)

```bash
# Polymarket
export POLYMARKET_API_KEY="your_key"
export POLYMARKET_API_SECRET="your_secret"
export POLYMARKET_WALLET_ADDRESS="0x..."
```

**Never hardcode API keys in your code.**

### Safety Features

- **`dry_run=True` by default**: Orders are validated and signed but not sent
- **`max_order_size` limit**: Configurable per-client safety limit
- **Explicit opt-in for real trading**: Must pass `dry_run=False`

## Venues & referral links

This library is free and open source (MIT). Some links below are **referral links**: if you sign up through them, the venue may pay the project a share of fees or a bonus. It costs you nothing extra, and using the library never requires them. Plain links are given next to each one.

<!-- venues-table:start -->
| Venue | Sign up | Referral? | Closed markets | Notes |
|---|---|---|---|---|
| Limitless | [limitless.exchange](https://limitless.exchange?r=0D2RQUXIT4) | yes (`r=0D2RQUXIT4`) | no | plain link: https://limitless.exchange |
| Polymarket | [polymarket.com](https://polymarket.com) | no | yes |  |
| Kalshi | [kalshi.com](https://kalshi.com) | no | yes |  |
| Hyperliquid Outcomes (HIP-4) | [hyperliquid.xyz](https://hyperliquid.xyz) | no | no |  |
| Pascal | [app.pascal.trade](https://app.pascal.trade) | no | yes |  |
<!-- venues-table:end -->

Hyperliquid Outcomes limits: trades have no pagination (`recentTrades` only), the order book has at most 20 levels per side, and closed markets cannot be listed. Each `list_markets` or `get_market` call also sends one side-0 `l2Book` per priced question fallback (info weight 2). Those checks run at most 8 at a time and are not cached on the client. A full live listing on 2026-10-28 was 34 `l2Book` calls (weight 68). With `outcomeMeta` (weight 20), `outcomeTemplates` (weight 20), and `allMids` (weight 2) that is weight 110 of the 1200 per minute per IP budget, about 10 full listings a minute. Wall time is around 6 s (about 6.2 s with these checks, about 2.8 s on main). The recorded fixture asks for 25, because fallback 8656 is omitted from its question. An empty book sets that leg's `price` to `None` instead of the `0.5` `allMids` mid. If a check fails (404, 429, 5xx, or timeout), that leg's `price` is `None`, one warning is emitted, and the listing continues.

Referral links are defined in `src/prediction_markets_api/venues.py` (the same registry that generates this table), so you can see exactly what is linked and change it in a fork.

Trading and builder-code attribution are not part of 0.9 (read-only). How builder codes will work, what they cost and how to turn them off will be documented here before 1.0.

## Supported Venues

Sign-up links and referral disclosure: see [Venues & referral links](#venues--referral-links).

| Venue | Chain | Status | Read | Trade | API Key (Read) | API Key (Trade) | Geo Restrictions | Link |
|-------|-------|--------|------|-------|----------------|-----------------|------------------|------|
| Polymarket | Polygon | ✅ Implemented | ✅ | — | No | Yes | US blocked | [Link](https://polymarket.com) |
| Kalshi | None | ✅ Implemented | ✅ | — | No | Yes | US only | [Link](https://kalshi.com) |
| Limitless | Base | ✅ Implemented | ✅ | — | No | Yes | None | [Link](https://limitless.exchange) |
| Hyperliquid Outcomes (HIP-4) | Hyperliquid | ✅ Implemented | ✅ | — | No | Yes | None | [Link](https://hyperliquid.xyz) |
| Predict.fun | Base | 🚧 Planned | — | — | No | Yes | None | [Link](https://predict.fun) |
| Opinion | Unknown | 🚧 Planned | — | — | No | Yes | None | [Link](https://opinion.finance) |
| Pascal | Solana | ✅ Implemented | ✅ | — | No | Yes | Restricted by venue ToS: US, UK, Russia (see ToS for the full list) | [Link](https://app.pascal.trade) |
| SX Bet | Arbitrum | 🚧 Planned | — | — | No | Yes | None | [Link](https://sx.bet) |
| Azuro/Bookmaker.xyz | Polygon | 🚧 Planned | — | — | No | Yes | None | [Link](https://azuro.org) |
| Jupiter Prediction | Solana | 🚧 Planned | — | — | No | Yes | None | [Link](https://jup.ag) |
| Polymarket US | Unknown | 🚧 Planned | — | — | No | Yes | US only | [Link](https://polymarket.com) |
| Novig | None | 🚧 Planned | — | — | No | Yes | None | [Link](https://novig.com) |
| World | Unknown | 🚧 Planned | — | — | No | Yes | None | [Link](https://world.org) |
| BetDEX | Solana | 🚧 Planned | — | — | No | Yes | None | [Link](https://betdex.com) |


## Pascal

Pascal is a Solana prediction market in **beta**. The read client talks to the public data API at `https://data.pascal.trade`. Reads are unauthenticated. The web app is [app.pascal.trade](https://app.pascal.trade). The reference used here is [docs.pascal.trade/llms/api.txt](https://docs.pascal.trade/llms/api.txt). There is no public changelog; the notes below follow that document as of 2026-10-29, including the text-search and `is_mece` changes from 27–28 October 2026.

### Example

```python
import asyncio
from prediction_markets_api import PascalClient
from prediction_markets_api.models.base import OrderSide


async def main():
    async with PascalClient() as client:
        markets = [market async for market in client.list_markets(closed=False, limit=5)]
        for market in markets:
            price = market.outcomes[0].price
            shown = "n/a" if price is None else f"{price:.2%}"
            print(f"{market.question}: {shown}")

        matches = [
            market async for market in client.list_markets(limit=1, venue_params={"query": "house"})
        ]
        if matches:
            print(matches[0].id)

        for market in markets:
            outcome = market.outcomes[0]
            book = await client.get_order_book(outcome.id)
            if book.bids and book.asks:
                buy = await client.get_price(outcome.id, OrderSide.BUY)
                print(f"{book.bids[0].price:.4f} / {buy.price:.4f}")
                break


asyncio.run(main())
```

**Supported**

- `list_markets` — `GET /api/v1/markets` returns every unresolved market in one snapshot, ordered by symbol. There is no cursor, so `limit` is applied locally and `page_size` is ignored. `closed=False` is that snapshot. `closed=True` is `GET /api/v1/markets/resolved` (lookback `4h`, `24h`, or `3d`, default `24h`). `order_by` is always sent (default `resolution_time`). The API accepts only `limit` 50, 100, or 200 and does not page, so this is not a full archive. `closed=None` reads the unresolved snapshot, then that resolved window.
- Text search — `venue_params={"query": "..."}` calls `POST /api/v1/markets/text-search`. The trimmed query must be 1–32 characters; a blank or longer query raises `ValueError` before any request (the API would return `INVALID_REQUEST`). `order_by` is always sent and defaults to `volume_all_time`, which became the API default on 2026-10-27. At most 100 markets. 60 requests per minute per IP.
- `get_market` — `POST /api/v1/markets/search` with the exact symbol, including resolved markets. An empty result is `NotFoundError`. There is no `GET /markets/{symbol}`.
- `get_order_book` / `get_price` — `GET /api/v1/books` for one symbol (the long contract). A `:short` outcome id inverts that book. A single-symbol book may be up to one second old. Up to 50 symbols are allowed per call; this client requests one. An empty book on a listed market makes `get_price` raise `NoLiquidityError`. An unknown symbol is HTTP 422 `SYMBOL_NOT_FOUND`, mapped to `NotFoundError`.
- `get_trades` / `iter_trades` — `GET /api/v1/trades`, paged with `before_cursor`. Each page asks for at most 500 prints. `taker_side` `BID` / `ASK` is `BUY` / `SELL` on the long contract. A `:short` outcome id uses price `1 - long price` and flips the side (long SELL 0.90 is short BUY 0.10). A cursor that repeats stops the iterator and logs one warning.

**Prices**

The long outcome price is the mid when both `best_bid` and `best_ask` exist. When only one side exists, or neither does, `price` is `None` and `raw_price` is `None`. `mark_price` is not used: it can be a last trade, including a value near 0.5, on an empty or one-sided book. A resolved market uses `resolution.price` (`1` names the long outcome, `0` names the short outcome). The short outcome price is `1 - long` when the long price is set.

**`is_mece`**

Market payloads may include optional `is_mece` (outcomes are mutually exclusive and collectively exhaustive), added 2026-10-28. It is kept on `Market.raw_data` when the venue sends it. The shared model has no field for it, and a missing flag is normal. WebSocket `update_type` `mece_designation` is not read. This client does not open the market-data websocket.

**Rate limits**

The docs say limits may change and that clients must handle HTTP 429 (`RATE_LIMITED` → `RateLimitError`). They do not publish a request weight for reads. Documented caps used here: text search 60 requests per minute per IP; books up to 50 symbols (this client sends one); trades up to 120 symbols and a page size of 1–500 (default 100). Write endpoints, which this client does not call, accept up to 100 requests per second.

**Not in this adapter**

Trading (`place_order` validates a dry-run and does not submit), websockets, candles, and account or position reads. `GET /api/v1/accounts/{owner}/position-resolutions` gained an optional `symbols` filter of up to 100 symbols on 2026-10-27. Positions are planned for 1.0 and are not implemented.

## Development

### Setup

```bash
git clone https://github.com/prediction-markets-api/prediction-markets-api.git
cd prediction-markets-api

python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows

pip install -e ".[dev]"
```

### Running Tests

```bash
# Run all tests (uses recorded fixtures, no network)
pytest

# Run with coverage
pytest --cov=src/prediction_markets_api --cov-report=html

# Run only live tests (requires network, opt-in)
pytest -m live

# Skip live tests (default)
pytest -m "not live"
```

### Linting and Type Checking

```bash
# Format code
ruff format .

# Lint
ruff check .

# Type check
mypy src/prediction_markets_api
```

### CI

GitHub Actions runs on pull requests and pushes to `main`:
- **Lint** (ruff)
- **Type check** (mypy)
- **Tests** on Python 3.10, 3.11, 3.12, and 3.13 (Linux, macOS, Windows)

Those checks use recorded API fixtures (no live network calls).

### Nightly live smoke

[`.github/workflows/nightly-smoke.yml`](.github/workflows/nightly-smoke.yml) runs the opt-in live tests against real venues. It is a separate workflow: it does not run on pull requests and it is not a required check on `main`. One venue failing does not hide the others (`fail-fast: false`).

- **Schedule:** every day at 00:00 UTC (03:00 Europe/Moscow).
- **Manual run:** Actions → Nightly live smoke → Run workflow. The `venues` input takes a comma- or space-separated list (`polymarket, kalshi`). Leave it empty to run every venue.
- **Jobs:** one matrix job per venue (`polymarket`, `kalshi`, `limitless`, `hyperliquid`, `pascal`, `predict_fun`) on Python 3.13. Each job runs `pytest -m live tests/test_<venue>_live.py`.
- **Kalshi** uses the demo Trade API only (`https://external-api.demo.kalshi.co/trade-api/v2`). Public demo reads do not need a key. `KALSHI_API_KEY` and `KALSHI_API_SECRET`, if configured, must be demo credentials; the current live tests do not read them, and a missing pair does not skip the job. The production host is not used.
- **Secrets:** `PREDICTFUN_API_KEY` for Predict.fun mainnet. When that secret is missing, the job does not call the venue and the summary says `skipped: no key`. Until `tests/test_predict_fun_live.py` exists and collects tests, the `predict_fun` job says `skipped: no live tests` instead.
- **Failures:** the job opens or comments on one open issue labeled `nightly-smoke` and `venue:<name>`, with the run link, the UTC time, and the failing test names. A later run that actually passes comments and closes that issue.

To add a venue, add one object to the list in the workflow. If it needs a key, set `required_secrets` and pass that secret in the smoke job `env`.

## Architecture

```
prediction-markets-api/
├── src/prediction_markets_api/
│   ├── models/
│   │   └── base.py          # Unified data models
│   ├── adapters/
│   │   ├── base.py          # Common client interface
│   │   └── polymarket.py    # Polymarket adapter
│   └── __init__.py
├── tests/
│   ├── fixtures/            # Recorded API responses
│   ├── test_models.py       # Model validation tests
│   ├── test_polymarket.py   # Adapter tests (mocked)
│   └── test_polymarket_live.py  # Live smoke tests (opt-in)
└── pyproject.toml
```

## Contributing

Contributions welcome! Please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Add tests for new functionality
4. Ensure CI passes (`pytest`, `ruff`, `mypy`)
5. Submit a Pull Request

## License

MIT License - see [LICENSE](LICENSE) for details.

## Roadmap

- [ ] Complete Polymarket trading implementation
- [ ] Add Kalshi adapter
- [ ] Add Limitless adapter
- [ ] WebSocket support for real-time data
- [ ] Portfolio management utilities
- [ ] Order history and fills tracking

## Support

- **Issues**: [GitHub Issues](https://github.com/prediction-markets-api/prediction-markets-api/issues)
- **Discussions**: [GitHub Discussions](https://github.com/prediction-markets-api/prediction-markets-api/discussions)

---

**Disclaimer**: This is an independent open-source project and is not affiliated with or endorsed by any prediction market platform.
