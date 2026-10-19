#!/usr/bin/env python3
"""Get price history and trade data for a prediction market outcome.

This example shows how to fetch historical trades and analyze price movements.
"""

import asyncio

from prediction_markets_api import PolymarketClient
from prediction_markets_api.models.base import OrderSide


async def main():
    """Fetch and analyze price history for a Polymarket outcome."""
    print("=" * 80)
    print("Price History Analysis")
    print("=" * 80)

    async with PolymarketClient(timeout=60.0) as client:
        # Find an active market
        print("\nFinding an active market...")
        market = None
        async for m in client.list_markets(closed=False, limit=10):
            if len(m.outcomes) >= 2 and m.outcomes[0].price:
                market = m
                break

        if not market:
            print("No suitable market found")
            return

        print(f"\nMarket: {market.question}")
        print(f"ID: {market.id}")

        outcome = market.outcomes[0]
        print(f"\nAnalyzing outcome: {outcome.name}")
        print(f"Current price: {outcome.price:.2%}" if outcome.price else "Current price: N/A")

        # Get current best prices
        try:
            buy_price = await client.get_price(outcome.id, OrderSide.BUY)
            sell_price = await client.get_price(outcome.id, OrderSide.SELL)

            print("\nCurrent Order Book:")
            print(f"  Best ask (buy price): {buy_price.price:.4f}")
            print(f"  Best bid (sell price): {sell_price.price:.4f}")
            print(f"  Spread: {(buy_price.price - sell_price.price):.4f}")
        except ValueError as e:
            print(f"\nCould not fetch order book: {e}")

        # Get recent trades
        print("\nRecent Trades (last 20):")
        print("-" * 80)

        trades = []
        async for trade in client.get_trades(outcome.id, limit=20):
            trades.append(trade)

        if trades:
            # Calculate statistics
            prices = [float(trade.price) for trade in trades]
            volumes = [float(trade.size) for trade in trades]

            print("\nTrade Statistics:")
            print(f"  Number of trades: {len(trades)}")
            print(f"  Average price: {sum(prices) / len(prices):.4f}")
            print(f"  Min price: {min(prices):.4f}")
            print(f"  Max price: {max(prices):.4f}")
            print(f"  Total volume: {sum(volumes):,.2f}")

            print("\nMost Recent Trades:")
            for i, trade in enumerate(trades[:5], 1):
                time_str = trade.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
                print(
                    f"  {i}. {time_str} | {trade.side.value:4} | "
                    f"Price: {trade.price:.4f} | Size: {trade.size:,.2f}"
                )

            # Price movement
            if len(trades) >= 2:
                first_price = prices[-1]  # Oldest trade
                last_price = prices[0]  # Most recent trade
                change = ((last_price - first_price) / first_price) * 100

                print("\nPrice Movement (oldest to newest):")
                print(f"  Start: {first_price:.4f}")
                print(f"  End: {last_price:.4f}")
                print(f"  Change: {change:+.2f}%")
        else:
            print("  No recent trades found")

    print("\n" + "=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
