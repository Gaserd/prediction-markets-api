#!/usr/bin/env python3
"""Export prediction market data to pandas DataFrames for analysis.

This example requires pandas to be installed:
    pip install pandas

It shows how to convert market data and trades into DataFrames for
statistical analysis and visualization.
"""

import asyncio

from prediction_markets_api import PolymarketClient

try:
    import pandas as pd

    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False
    print("Warning: pandas is not installed. Install with: pip install pandas")


async def main():
    """Export market data to pandas DataFrames."""
    if not PANDAS_AVAILABLE:
        print("\nThis example requires pandas to be installed.")
        print("Run: pip install pandas")
        return

    print("=" * 80)
    print("Export to Pandas Example")
    print("=" * 80)

    async with PolymarketClient(timeout=60.0) as client:
        # Fetch markets
        print("\nFetching markets...")
        markets = []
        async for market in client.list_markets(closed=False, limit=20):
            markets.append(market)

        # Convert markets to DataFrame
        market_data = []
        for market in markets:
            for outcome in market.outcomes:
                market_data.append(
                    {
                        "market_id": market.id,
                        "question": market.question,
                        "venue": market.venue,
                        "outcome_id": outcome.id,
                        "outcome_name": outcome.name,
                        "price": float(outcome.price) if outcome.price else None,
                        "volume": float(market.volume) if market.volume else None,
                        "liquidity": float(market.liquidity) if market.liquidity else None,
                        "open_interest": (
                            float(market.open_interest) if market.open_interest else None
                        ),
                        "created_at": market.created_at,
                        "end_date": market.end_date,
                        "resolved": market.resolved,
                    }
                )

        df_markets = pd.DataFrame(market_data)

        print(f"\nMarkets DataFrame shape: {df_markets.shape}")
        print("\nFirst few rows:")
        print(df_markets[["question", "outcome_name", "price", "volume"]].head(10))

        # Basic statistics
        print("\n" + "=" * 80)
        print("Price Statistics:")
        print(df_markets["price"].describe())

        print("\n" + "=" * 80)
        print("Volume Statistics:")
        print(df_markets["volume"].describe())

        # Find a market with trade history
        print("\n" + "=" * 80)
        print("Fetching trade history for analysis...")

        outcome_id = None
        for market in markets:
            if market.outcomes:
                outcome_id = market.outcomes[0].id
                print(f"\nUsing outcome: {market.outcomes[0].name}")
                print(f"From market: {market.question}")
                break

        if outcome_id:
            trades = []
            async for trade in client.get_trades(outcome_id, limit=100):
                trades.append(
                    {
                        "timestamp": trade.timestamp,
                        "side": trade.side.value,
                        "price": float(trade.price),
                        "size": float(trade.size),
                        "outcome_id": trade.outcome_id,
                    }
                )

            if trades:
                df_trades = pd.DataFrame(trades)

                print(f"\nTrades DataFrame shape: {df_trades.shape}")
                print("\nTrade Statistics:")
                print(df_trades[["price", "size"]].describe())

                # Calculate VWAP (Volume Weighted Average Price)
                df_trades["value"] = df_trades["price"] * df_trades["size"]
                vwap = df_trades["value"].sum() / df_trades["size"].sum()
                print(f"\nVolume Weighted Average Price (VWAP): {vwap:.4f}")

                # Price movement
                df_trades = df_trades.sort_values("timestamp")
                if len(df_trades) >= 2:
                    first_price = df_trades.iloc[0]["price"]
                    last_price = df_trades.iloc[-1]["price"]
                    change_pct = ((last_price - first_price) / first_price) * 100
                    print(f"Price change: {change_pct:+.2f}%")

                print("\nSample data saved. You can now use these DataFrames for:")
                print("  - Time series analysis")
                print("  - Statistical modeling")
                print("  - Visualization with matplotlib/seaborn")
                print("  - Export to CSV: df_markets.to_csv('markets.csv')")
            else:
                print("\nNo trades found for this outcome")

    print("\n" + "=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
