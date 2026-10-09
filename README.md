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
from prediction_markets_api import PolymarketClient
from prediction_markets_api.models.base import OrderSide

async with PolymarketClient() as client:
    # Get best executable prices
    buy_price = await client.get_price(outcome_id="token_id", side=OrderSide.BUY)
    sell_price = await client.get_price(outcome_id="token_id", side=OrderSide.SELL)
    
    print(f"Best ask (to buy): {buy_price.price:.4f}")
    print(f"Best bid (to sell): {sell_price.price:.4f}")
```

### Placing Orders (Dry-Run by Default)

```python
from decimal import Decimal
from prediction_markets_api import PolymarketClient
from prediction_markets_api.models.base import OrderSide

async with PolymarketClient(max_order_size=Decimal("1000")) as client:
    # Dry-run mode (default): validates but doesn't send
    result = await client.place_order(
        outcome_id="token_id",
        side=OrderSide.BUY,
        size=Decimal("100"),
        price=Decimal("0.65"),
        dry_run=True,  # Default: safe simulation
    )
    print(result)  # {'dry_run': True, 'status': 'simulated', ...}
    
    # Real orders require explicit opt-in + API keys
    # result = await client.place_order(..., dry_run=False)
```

### Getting Historical Trades

```python
async with PolymarketClient() as client:
    async for trade in client.get_trades(outcome_id="token_id", limit=10):
        print(f"{trade.timestamp}: {trade.side.value} {trade.size} @ {trade.price}")
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
Market(
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
```

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

| Venue      | Status       | Read | Trade |
|------------|--------------|------|-------|
| Polymarket | ✅ Available | ✅   | 🚧    |
| Kalshi     | 🚧 Planned   | —    | —     |
| Limitless  | 🚧 Planned   | —    | —     |

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
