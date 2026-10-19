# prediction-markets-api

[![CI](https://github.com/prediction-markets-api/prediction-markets-api/workflows/CI/badge.svg)](https://github.com/prediction-markets-api/prediction-markets-api/actions)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A unified Python client for prediction markets. Currently supports **Polymarket**, with **Kalshi** and **Limitless** adapters planned.

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

### Placing Orders (Dry-Run by Default)

```python
import asyncio
from decimal import Decimal
from prediction_markets_api import PolymarketClient
from prediction_markets_api.models.base import OrderSide


async def main():
    async with PolymarketClient(max_order_size=Decimal("1000")) as client:
        # Get first market with outcomes
        async for market in client.list_markets(closed=False, limit=10):
            if market.outcomes:
                outcome = market.outcomes[0]
                break

        # Dry-run mode (default): validates but doesn't send
        result = await client.place_order(
            outcome_id=outcome.id,
            side=OrderSide.BUY,
            size=Decimal("100"),
            price=Decimal("0.65"),
            dry_run=True,  # Default: safe simulation
        )
        print(result)  # {'dry_run': True, 'status': 'simulated', ...}

        # Real orders require explicit opt-in + API keys
        # result = await client.place_order(..., dry_run=False)


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

**Notes:**
- Kalshi filter parameter names differ from response status values. The table shows response values with filter parameters in parentheses.
- Kalshi resolved markets with `result='scalar'` (e.g., cancelled events): These are marked as `resolved=True` with `status=RESOLVED` but `winning_outcome=None` since no yes/no outcome won. This is acceptable and documented.
- Polymarket resolution detection:
  - No explicit resolution status fields in API response (umaResolutionStatuses always empty, resolvedBy/resolutionSource not status indicators)
  - Resolution determined by strict price settlement check: >0.999 and <0.001 thresholds
  - This minimizes false positives from markets trading near certainty but not yet formally resolved
  - Winning outcome automatically detected from settled prices
- Limitless only exposes `FUNDED` markets via `/markets/active` endpoint. Calling `list_markets(closed=True)` raises `NotImplementedError` with a clear explanation, since the API does not provide an endpoint for closed/resolved markets.

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

**Unified Methods:**
- `list_markets(closed, limit, page_size, *, venue_params)` - List markets with pagination
- `iter_markets(closed)` - Iterate all markets (no limit)
- `get_market(market_id)` - Get single market by ID
- `get_order_book(outcome_id)` - Get sorted, validated order book
- `get_price(outcome_id, side)` - Get best executable price
- `get_trades(outcome_id, limit)` - Historical trades iterator
- `place_order(...)` - Place order (dry_run=True by default)

**Venue-Specific Parameters:**
- Venue-specific filters are keyword-only arguments (no `**kwargs`)
- Example: `client.list_markets(category="politics")` for Kalshi
- Prevents accidental parameter mismatches between venues

### API Keys and Configuration

**Read Operations:** No authentication required for market data, prices, and order books.

**Trading Operations:** Require API keys via environment variables:

```bash
# Polymarket
export POLYMARKET_API_KEY="your_key"
export POLYMARKET_API_SECRET="your_secret"
export POLYMARKET_WALLET_ADDRESS="0x..."

# Kalshi
export KALSHI_API_KEY="your_key"
export KALSHI_API_SECRET="your_secret"
```

**Never hardcode API keys in source code.**

### Dry-Run Safety

Trading has safe defaults:

```python
import asyncio
from decimal import Decimal
from prediction_markets_api import PolymarketClient
from prediction_markets_api.models.base import OrderSide


async def main():
    async with PolymarketClient() as client:
        # Get first market with outcomes
        async for market in client.list_markets(closed=False, limit=10):
            if market.outcomes:
                outcome_id = market.outcomes[0].id
                break

        # Default: dry_run=True (safe simulation)
        result = await client.place_order(
            outcome_id, OrderSide.BUY, Decimal("100"), Decimal("0.65")
        )
        print(result)  # {'dry_run': True, 'status': 'simulated', ...}

        # Real orders require explicit opt-in
        # result = await client.place_order(..., dry_run=False)


asyncio.run(main())
```

### Geographic Restrictions

Be aware of venue restrictions:
- **Polymarket**: US users blocked
- **Kalshi**: US only (CFTC regulated)
- **Limitless**: No restrictions (planned)

### Exception Hierarchy

The library provides a consistent exception hierarchy:

```python
from prediction_markets_api import (
    PredictionMarketsError,  # Base exception
    NotFoundError,  # Market/outcome/token not found
    VenueAPIError,  # HTTP errors, invalid responses
    RateLimitError,  # HTTP 429 rate limit
)
```

**Exception Types:**

- **`PredictionMarketsError`**: Base exception for all library errors
- **`NotFoundError`**: Resource not found (markets, outcomes, tokens)
  - Attributes: `resource_type`, `resource_id`
  - Raised on HTTP 404 or empty results
- **`VenueAPIError`**: HTTP errors, timeouts, invalid JSON responses
  - Attributes: `status_code`, `venue`, `url`
  - Wraps all `httpx` errors for consistent handling
- **`RateLimitError`**: Rate limit exceeded (HTTP 429)
  - Subclass of `VenueAPIError`
  - Attributes: `retry_after` (seconds, from Retry-After header if available)

**User Input Errors:**
- `ValueError`: Invalid parameters (e.g., price not in 0..1, negative size)
- `NotImplementedError`: Unsupported operations (e.g., `LimitlessClient.list_markets(closed=True)`)

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
- **Outcome**: Individual tradeable token (e.g., "Yes"/"No")
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

market = Market(
    id="0x1234...",
    venue="polymarket",
    question="Will Bitcoin reach $100k by Dec 31?",
    outcomes=[
        Outcome(id="token_yes", name="Yes", price=Decimal("0.65")),
        Outcome(id="token_no", name="No", price=Decimal("0.35")),
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

## Supported Venues

**Note**: Signup links are referral links; the project receives a reward when you sign up.

| Venue | Chain | Status | Read | Trade | API Key (Read) | API Key (Trade) | Geo Restrictions | Signup Link |
|-------|-------|--------|------|-------|----------------|-----------------|------------------|-------------|
| Polymarket | Polygon | ✅ Implemented | ✅ | — | No | Yes | US blocked | [Link](https://polymarket.com) |
| Kalshi | None | ✅ Implemented | ✅ | — | No | Yes | US only | [Link](https://kalshi.com) |
| Limitless | Base | ✅ Implemented | ✅ | — | No | Yes | None | [Link](https://limitless.exchange) |
| Hyperliquid Outcomes (HIP-4) | Hyperliquid | 🚧 Planned | — | — | No | Yes | None | [Link](https://hyperliquid.xyz) |
| Predict.fun | Base | 🚧 Planned | — | — | No | Yes | None | [Link](https://predict.fun) |
| Opinion | Unknown | 🚧 Planned | — | — | No | Yes | None | [Link](https://opinion.finance) |
| Pascal | Base | 🚧 Planned | — | — | No | Yes | None | [Link](https://pascal.money) |
| SX Bet | Arbitrum | 🚧 Planned | — | — | No | Yes | None | [Link](https://sx.bet) |
| Azuro/Bookmaker.xyz | Polygon | 🚧 Planned | — | — | No | Yes | None | [Link](https://azuro.org) |
| Jupiter Prediction | Solana | 🚧 Planned | — | — | No | Yes | None | [Link](https://jup.ag) |
| Polymarket US | Unknown | 🚧 Planned | — | — | No | Yes | US only | [Link](https://polymarket.com) |
| Novig | None | 🚧 Planned | — | — | No | Yes | None | [Link](https://novig.com) |
| World | Unknown | 🚧 Planned | — | — | No | Yes | None | [Link](https://world.org) |
| BetDEX | Solana | 🚧 Planned | — | — | No | Yes | None | [Link](https://betdex.com) |

> **Trading Interface**: This release includes a designed trading interface with `place_order()`, but actual order execution is a stub. Full trading support coming in the next release.

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

GitHub Actions runs:
- **Lint** (ruff)
- **Type check** (mypy)
- **Tests** on Python 3.10, 3.11, 3.12 (Linux, macOS, Windows)

All tests use recorded API fixtures (no live network calls in CI).

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
