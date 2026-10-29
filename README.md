# prediction-markets-api

[![CI](https://github.com/prediction-markets-api/prediction-markets-api/workflows/CI/badge.svg)](https://github.com/prediction-markets-api/prediction-markets-api/actions)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A unified Python client for prediction markets. Currently supports **Polymarket**, **Kalshi**, **Limitless**, **Hyperliquid Outcomes (HIP-4)**, **Pascal** (Solana, beta), **Predict.fun** (BNB Chain), and **Opinion** (BNB Chain).

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
| **Predict.fun** | `REGISTERED`, `PRICE_PROPOSED`, `PRICE_DISPUTED` with trading `OPEN` | `OPEN` | False | `OPEN` means a new order can be placed. `price` is `None` unless both best bid and best ask are present |
| | `REMOVED`, lifecycle `PAUSED`, or trading `CLOSED`, `PAUSED`, `CANCEL_ONLY`, `MATCHING_NOT_ENABLED` | `CLOSED` | False | Raw lifecycle `status` stays on `venue_status`. Both `status` and `tradingStatus` stay on `raw_data`. `status=OPEN` pages can include these; `list_markets(closed=False)` drops them |
| | `RESOLVED` | `RESOLVED` | True | Winning outcome price is `1` and the others are `0`. Listed with `status=RESOLVED` |
| **Opinion** | `Activated` (2) | `OPEN` | False | `OPEN` means a new order can be placed right now. `price` is `None` on a listing (the payload has no best bid or ask) |
| | `Created` (1), `Resolving` (3), `Failed` (5), `Deleted` (6) | `CLOSED` | False | Raw `statusEnum` stays on `venue_status`. `status` and `statusEnum` stay on `raw_data`. `list_markets(closed=False)` drops these |
| | `Resolved` (4) | `RESOLVED` | True | Winning outcome price is `1` and the other outcome is `0`. Listed with `status=resolved` |
| | anything else | unset | False | One warning is logged. The raw status stays on `venue_status` |

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
- Predict.fun (BNB Chain) lists open markets with `GET /v1/markets?status=OPEN` and resolved markets with `status=RESOLVED`. Mainnet reads require `PREDICTFUN_API_KEY`. See [Predict.fun](#predictfun).
- Opinion (BNB Chain) lists activated markets with `GET /openapi/market?status=activated` and resolved markets with `status=resolved`. Public market, order book, and price reads do not need a key. See [Opinion](#opinion).

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

**Version 0.9 (Read-Only):** Polymarket, Kalshi, Limitless, Hyperliquid Outcomes, and Pascal serve market data, prices, order books, and trades without an API key. Predict.fun mainnet reads require `PREDICTFUN_API_KEY` (the `x-api-key` header). The public testnet does not. Opinion market, order book, and price reads do not need a key. Opinion user trade history (`GET /openapi/trade`) requires `OPINION_API_KEY` (the `apikey` header).

**Trading (Planned for 1.0):** Will require API keys via environment variables.

### Geographic Restrictions

Be aware of venue restrictions:
- **Polymarket**: US users blocked
- **Kalshi**: US only (CFTC regulated)
- **Limitless**: No restrictions (planned)
- **Pascal**: Restricted by venue ToS: US, UK, Russia (see ToS for the full list). Terms: https://app.pascal.trade/terms.pdf. The product is in beta.
- **Predict.fun**: Restricted by venue ToS: US, UK, France, Ontario, Singapore, Poland, Thailand, Australia, Belgium, Taiwan (see ToS for the full list). Terms: https://docs.predict.fun/terms-of-service. Mainnet reads require an API key.
- **Opinion**: Restricted by venue ToS: US, UK, France, Ontario, Singapore, Poland, Thailand, South Korea, China, Taiwan, Hong Kong, Iran, Syria, Cuba, North Korea, Crimea, Donetsk, Luhansk (see ToS for the full list). Terms: https://docs.opinion.trade/legal/terms-of-use. Public reads do not need an API key.

### Exception Hierarchy

The library provides a consistent exception hierarchy:

```python
from prediction_markets_api import (
    PredictionMarketsError,  # Base exception
    NotFoundError,  # Market/outcome/token not found
    NoLiquidityError,  # Empty order book for existing market
    VenueAPIError,  # HTTP errors, invalid responses
    RateLimitError,  # HTTP 429 rate limit
    AuthenticationError,  # Missing or rejected API key
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
- **`AuthenticationError`**: API key missing or rejected (HTTP 401)
  - Subclass of `VenueAPIError`
  - Predict.fun mainnet raises this when `PREDICTFUN_API_KEY` is unset or the venue rejects the key. The message does not include the key.
  - Opinion raises this for `GET /openapi/trade` when `OPINION_API_KEY` is unset, and when a configured key is rejected. Public market, order book, and price reads do not need a key. The message does not include the key.

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

# Predict.fun mainnet reads. Testnet does not need this. Never commit the key.
export PREDICTFUN_API_KEY="your_key"

# Opinion user trade history only. Market, book, and price reads do not need this.
export OPINION_API_KEY="your_key"
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
| Predict.fun | [predict.fun](https://predict.fun) | no | yes |  |
| Opinion | [opinion.trade](https://opinion.trade) | no | yes |  |
<!-- venues-table:end -->

Hyperliquid Outcomes limits: trades have no pagination (`recentTrades` only), the order book has at most 20 levels per side, and closed markets cannot be listed. Each `list_markets` or `get_market` call also sends one side-0 `l2Book` per priced question fallback (info weight 2). Those checks run at most 8 at a time and are not cached on the client. A full live listing on 2026-10-28 was 34 `l2Book` calls (weight 68). With `outcomeMeta` (weight 20), `outcomeTemplates` (weight 20), and `allMids` (weight 2) that is weight 110 of the 1200 per minute per IP budget, about 10 full listings a minute. Wall time is around 6 s (about 6.2 s with these checks, about 2.8 s on main). The recorded fixture asks for 25, because fallback 8656 is omitted from its question. An empty book sets that leg's `price` to `None` instead of the `0.5` `allMids` mid. If a check fails (404, 429, 5xx, or timeout), that leg's `price` is `None`, one warning is emitted, and the listing continues.

Referral links are defined in `src/prediction_markets_api/venues.py` (the same registry that generates this table), so you can see exactly what is linked and change it in a fork.

Predict.fun rate limits are per API key. `/v1/markets` and `/v1/markets/{id}` allow 240 requests per minute. `/v1/markets/{id}/orderbook`, `/quote`, and `/v1/orders/matches` share a separate bucket of 500 requests per minute. `GET /v1/markets` pages with `first` (server default 20, silently clamped to 100, no documented maximum) and `after`. `GET /v1/orders/matches` defaults `first` to 50 and clamps it to 150; this client sends 100. The public testnet does not require a key.

Trading and builder-code attribution are not part of 0.9 (read-only). How builder codes will work, what they cost and how to turn them off will be documented here before 1.0.

## Supported Venues

Sign-up links and referral disclosure: see [Venues & referral links](#venues--referral-links).

| Venue | Chain | Status | Read | Trade | API Key (Read) | API Key (Trade) | Geo Restrictions | Link |
|-------|-------|--------|------|-------|----------------|-----------------|------------------|------|
| Polymarket | Polygon | ✅ Implemented | ✅ | — | No | Yes | US blocked | [Link](https://polymarket.com) |
| Kalshi | None | ✅ Implemented | ✅ | — | No | Yes | US only | [Link](https://kalshi.com) |
| Limitless | Base | ✅ Implemented | ✅ | — | No | Yes | None | [Link](https://limitless.exchange) |
| Hyperliquid Outcomes (HIP-4) | Hyperliquid | ✅ Implemented | ✅ | — | No | Yes | None | [Link](https://hyperliquid.xyz) |
| Predict.fun | BNB Chain | ✅ Implemented | ✅ | — | Yes | Yes | Restricted by venue ToS: US, UK, France, Ontario, Singapore, Poland, Thailand, Australia, Belgium, Taiwan (see ToS for the full list) | [Link](https://predict.fun) |
| Opinion | BNB Chain | ✅ Implemented | ✅ | — | No | Yes | Restricted by venue ToS: US, UK, France, Ontario, Singapore, Poland, Thailand, South Korea, China, Taiwan, Hong Kong, Iran, Syria, Cuba, North Korea, Crimea, Donetsk, Luhansk (see ToS for the full list) | [Link](https://opinion.trade) |
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

## Predict.fun

Predict.fun is a prediction market on **BNB Chain**. The read client talks to `https://api.predict.fun`. Docs: [dev.predict.fun](https://dev.predict.fun/). Live OpenAPI: [api.predict.fun/docs](https://api.predict.fun/docs). Terms: [docs.predict.fun/terms-of-service](https://docs.predict.fun/terms-of-service).

Mainnet requires an API key from https://developers.predict.fun

Mainnet requires an API key in `PREDICTFUN_API_KEY`, sent as the `x-api-key` header. The key is read only from that environment variable. It is not logged, and `repr` shows `***` when it is set. A missing key raises `AuthenticationError` before the request. HTTP 401 raises the same error. The public testnet (`https://api-testnet.predict.fun`) does not require a key. Pass it as `base_url`. JWT bearer auth is only for order actions and is not used.

### Example

```python
import asyncio
import os

from prediction_markets_api import PredictFunClient


async def main():
    # Mainnet reads require PREDICTFUN_API_KEY. Testnet does not.
    base_url = None if os.getenv("PREDICTFUN_API_KEY") else PredictFunClient.TESTNET_API_BASE
    async with PredictFunClient(base_url=base_url) as client:
        markets = [market async for market in client.list_markets(closed=False, limit=2)]
        for market in markets:
            price = market.outcomes[0].price if market.outcomes else None
            shown = "n/a" if price is None else f"{price:.2%}"
            print(f"{market.question}: {shown}")


asyncio.run(main())
```

**Supported**

- `list_markets` — `GET /v1/markets`, paged with `first` and `after`. The server default for `first` is 20. Values above 100 are silently clamped to 100, and the docs do not publish a maximum. This client sends at most 100. `closed=False` sends `status=OPEN` and then drops markets that are not tradable (`PAUSED`, `CANCEL_ONLY`, `MATCHING_NOT_ENABLED`, lifecycle `REMOVED` or `PAUSED`). `closed=True` sends `status=RESOLVED`. `closed=None` omits `status`. `limit` stops before the next page. A repeated cursor stops the iterator and logs one warning.
- `venue_params` — `sort`, `market_variant` (`marketVariant`), and `tag_ids` (`tagIds`). Any other key raises `ValueError` before the request.
- `get_market` — `GET /v1/markets/{id}`. HTTP 404 is `NotFoundError`.
- `get_order_book` / `get_price` — `GET /v1/markets/{id}/orderbook`. The endpoint has no `indexSet` parameter. It always returns the indexSet 1 book. Outcome names are arbitrary. `indexSet` 2 is the complement (`1 - price`, sides swapped). Outcome ids are `{market_id}:{index_set}:{on_chain_id}`. A bare market id addresses the indexSet-1 book. A full id whose `onChainId` is not on the market raises `NotFoundError` before the book request. An empty side makes `get_price` raise `NoLiquidityError`. An unknown market is HTTP 404.
- `get_trades` / `iter_trades` — `GET /v1/orders/matches?marketId=...`, newest first, paged with `first` and `after`. The server default for `first` is 50 and values above 150 are clamped to 150. This client sends 100. No JWT. `priceExecuted` and `amountFilled` are 18-decimal wei strings (USDT collateral); this client divides by `10**18`. Taker `quoteType` `Bid` is `BUY` and `Ask` is `SELL`. A full outcome id is checked with `get_market` before any match request, then prints for other outcomes are dropped on the client. An empty page for an unknown market is `NotFoundError`.

**Prices and units**

Outcome prices and book levels are probabilities in `[0, 1]`. `decimalPrecision` is the number of decimal places. The tick is `10 ** -decimalPrecision` (`2` → `0.01`, `3` → `0.001` on the testnet snapshot recorded 2026-10-29). The book payload does not include it, so `OrderBook.tick_size` is unset. `spreadThreshold` and `shareThreshold` are venue liquidity-program thresholds, not the tick and not `min_order_size`.

`Outcome.price` is the mid when both `bestBid` and `bestAsk` exist. Those outcome fields are deprecated by the venue. When either field is missing, `price` is `None` and `raw_price` is `None`. A resolved market sets the winning outcome to `1` and the others to `0`.

Book sizes are share counts. Match amounts are wei. `stats.volumeTotalUsd` maps to `Market.volume`. `stats.totalLiquidityUsd` stays on `raw_data` only (`Market.liquidity` is `None`). Reported stats are USD notionals. The settlement token is USDT.

**Rate limits**

Per API key: 240 requests per minute for `/v1/markets` and `/v1/markets/{id}`. `/v1/markets/{id}/orderbook`, `/quote`, and `/v1/orders/matches` share a separate bucket of 500 requests per minute (testnet `ratelimit-policy` headers, observed 2026-10-29). HTTP 429 maps to `RateLimitError`. `Retry-After` is used when present, otherwise `ratelimit-reset` (seconds).

**Known issues**

A long testnet walk of `GET /v1/markets` can hit a venue-side HTTP 500 on a deep page (observed after the cursor for id 3882684 when `first` is 90 or more). There is no client workaround. `list_markets` raises `VenueAPIError`. The message includes the page number and the cursor of the failed request, and the listing does not finish as a successful partial result.

**Not in this adapter**

Trading (`place_order` validates a dry-run and does not submit), websockets, order placement, and account or position reads.

## Opinion

Opinion (Opinion Labs) is a prediction market on **BNB Chain** (chain id 56). The read client talks to `https://openapi.opinion.trade/openapi`. Docs: [Opinion OpenAPI](https://docs.opinion.trade/developer-guide/opinion-open-api/). Terms: [Terms of Use](https://docs.opinion.trade/legal/terms-of-use). The site is [opinion.trade](https://opinion.trade).

Public market, order book, and price reads do not need an API key. When `OPINION_API_KEY` is set it is sent as the `apikey` header. The key is read only from that environment variable. It is not logged, and `repr` shows `***` when it is set. An invalid key is HTTP 401 and raises `AuthenticationError` with no anonymous fallback. There is no published testnet host.

`GET /openapi/trade` is the API-key owner's filled trades, not a public tape. No public per-market trade history is published (price history is candles and is not used as trades). A missing key raises `AuthenticationError` before that request. The hint names `OPINION_API_KEY`.

### Example

```python
import asyncio

from prediction_markets_api import OpinionClient


async def main():
    # Market, book, and price reads do not need OPINION_API_KEY.
    async with OpinionClient() as client:
        markets = [market async for market in client.list_markets(closed=False, limit=2)]
        for market in markets:
            price = market.outcomes[0].price if market.outcomes else None
            shown = "n/a" if price is None else f"{price:.2%}"
            print(f"{market.question}: {shown}")


asyncio.run(main())
```

**Supported**

- `list_markets` — `GET /openapi/market`, paged with `page` (starting at 1) and `limit`. The server default for `limit` is 10. Values above 20 are silently clamped to 20. This client sends at most 20. `closed=False` sends `status=activated` and then drops markets that are not `OPEN`. `closed=True` sends `status=resolved`. `closed=None` omits `status`. `marketType` defaults to `2` (all). `limit` stops before the next page. A repeated page stops the iterator and logs one warning. A 5xx raises `VenueAPIError`. The message includes the page number and the cursor, which is that page number, and the listing does not finish as a successful partial result.
- `venue_params` — `market_type` (`0` binary, `1` categorical, `2` all), `sort_by` (`0`–`8`, sent as `sortBy` only when set), `chain_id` (`chainId`, only when set), and `collection_type` (`0`–`2`, sent as `collectionType` only when set). Any other key raises `ValueError` before the request.
- `get_market` — `GET /openapi/market/{id}` for a binary market or a categorical child. A categorical parent id returns errno 10200 on that path; this client then calls `GET /openapi/market/categorical/{id}`. Unknown ids raise `NotFoundError`.
- `get_order_book` / `get_price` — `GET /openapi/token/orderbook?token_id=`. The client requests the YES token. A NO token uses that book with `1 - price` and the sides swapped. Outcome names are the venue labels and are not assumed to be Yes and No. Outcome ids are `{marketId}:{tokenId}`. A bare market id addresses the YES book of a binary market. A categorical parent has no single book. A full id whose token is not on the market raises `NotFoundError` before the book request. An empty side makes `get_price` raise `NoLiquidityError`.
- `get_trades` / `iter_trades` — `GET /openapi/trade` with `page`, `limit` (max 20), `marketId`, and `chainId=56`. This is the caller's fills. `outcomeSide` `1` is the YES token and `2` is the NO token. Only filled rows (status `2`) are yielded. A full outcome id is checked with `get_market` before any trade request.

**Prices and units**

Book prices are probabilities in `[0, 1]`. Sizes are share counts. `volume` is a decimal string of the quote notional, not an 18-decimal integer. The quote token on chain 56 is USDT. `Market.currency` is `USD` for those notionals. There is no liquidity field (`Market.liquidity` is `None`). List payloads have no best bid or ask, so `Outcome.price` is `None` unless that outcome is resolved (`1` or `0` from `resultTokenId`). `OrderBook.tick_size`, `min_order_size`, and `last_trade_price` are left unset. Live books include prices such as `0.002` and `0.285`, so this client does not invent a tick.

A categorical parent is one `Market`. Each child contributes a YES outcome and a NO outcome, named `{child title}: {label}`.

**Rate limits**

Anonymous reads are 5 requests per second per IP (`ratelimit-limit: 5`, `ratelimit-reset: 1`, observed 2026-10-29). The docs also describe 15 requests per second for an authenticated API consumer. HTTP 429 maps to `RateLimitError`. `Retry-After` is used when present, otherwise `ratelimit-reset` (seconds).

**Known issues**

No testnet host is published. `GET /openapi/market/categorical/3825` reported status `Created` while the market list reported the same id as `Activated` (recorded 2026-10-29). This client maps the payload it received. `GET /openapi/trade` cannot be recorded without an API key; unit tests use a schema-shaped payload, and the success body was not verified live. Whether `chainId` is required on that call was not verified live (the official client sends `56`). Whether status `Resolving` still accepts a new order was not checked with a live order. The OpenAPI document at the host returned HTTP 403 or 404, so wire names follow the official `opinion_api` 0.4.0 client and live responses. An older `opinion_api` docstring swaps `marketType` `0` and `2`; live responses and `opinion-clob-sdk` 0.7.0 use `0` binary, `1` categorical, `2` all.

**Not in this adapter**

Trading (`place_order` validates a dry-run and does not submit), websockets, candles, and account or position reads.

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
- **Jobs:** one matrix job per venue (`polymarket`, `kalshi`, `limitless`, `hyperliquid`, `pascal`, `predict_fun`, `opinion`) on Python 3.13. Each job runs `pytest -m live tests/test_<venue>_live.py`.
- **Kalshi** uses the demo Trade API only (`https://external-api.demo.kalshi.co/trade-api/v2`). Public demo reads do not need a key. `KALSHI_API_KEY` and `KALSHI_API_SECRET`, if configured, must be demo credentials; the current live tests do not read them, and a missing pair does not skip the job. The production host is not used.
- **Secrets:** `PREDICTFUN_API_KEY` for Predict.fun mainnet. When that secret is missing, the job does not call the venue and the summary says `skipped: no key`. Until `tests/test_predict_fun_live.py` exists and collects tests, the `predict_fun` job says `skipped: no live tests` instead.
- **Opinion** public reads do not need a key, so `required_secrets` is empty and the job runs `tests/test_opinion_live.py`. User trade history needs `OPINION_API_KEY`. That secret is not passed into the smoke job, so the trade-history live test skips.
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
