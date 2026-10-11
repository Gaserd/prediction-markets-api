# Examples

This directory contains runnable examples demonstrating the prediction-markets-api library.

## Running Examples

All examples can be run directly:

```bash
python examples/compare_venues.py
python examples/price_history.py
python examples/export_to_pandas.py
```

## Examples

### compare_venues.py

Compare the same event (e.g., Bitcoin predictions) across multiple venues (Polymarket and Kalshi). Shows how to search for markets and compare prices.

**Features:**
- Search markets by keyword
- Compare prices across venues
- Display volume and outcomes

### price_history.py

Fetch and analyze price history for a prediction market outcome using historical trade data.

**Features:**
- Get recent trades
- Calculate price statistics (min, max, average)
- Analyze price movements
- Display best bid/ask spreads

### export_to_pandas.py

Export prediction market data to pandas DataFrames for statistical analysis and visualization.

**Requires:** `pip install prediction-markets-api[pandas]`

**Features:**
- Convert market data to DataFrame
- Calculate statistics (VWAP, price changes)
- Export to CSV for further analysis
- Ready for matplotlib/seaborn visualization

## Notes

- All examples use read-only API access (no authentication required)
- Examples may be subject to API rate limits
- Some venues have geographic restrictions (see main README)
