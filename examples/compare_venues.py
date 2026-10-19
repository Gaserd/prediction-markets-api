#!/usr/bin/env python3
"""Compare the same event across multiple prediction market venues.

This example shows how to search for markets on different venues and compare
their prices for the same event.
"""

import asyncio

from prediction_markets_api import KalshiClient, LimitlessClient, PolymarketClient


async def main():
    """Compare Bitcoin price predictions across Polymarket, Kalshi, and Limitless."""
    print("=" * 80)
    print("Comparing Bitcoin Markets Across Venues")
    print("=" * 80)

    # Search for Bitcoin markets on Polymarket
    print("\n[Polymarket] Searching for Bitcoin markets...")
    async with PolymarketClient(timeout=60.0) as client:
        polymarket_markets = []
        async for market in client.list_markets(closed=False, limit=10):
            if "bitcoin" in market.question.lower() or "btc" in market.question.lower():
                polymarket_markets.append(market)
                if len(polymarket_markets) >= 3:
                    break

        for market in polymarket_markets:
            print(f"\n  Question: {market.question}")
            print(f"  Market ID: {market.id}")
            print(f"  Volume: ${market.volume:,.2f}" if market.volume else "  Volume: N/A")
            print("  Outcomes:")
            for outcome in market.outcomes:
                if outcome.price:
                    print(f"    - {outcome.name}: {outcome.price:.2%}")
                else:
                    print(f"    - {outcome.name}: N/A")

    # Search for Bitcoin markets on Kalshi
    print("\n\n[Kalshi] Searching for Bitcoin markets...")
    async with KalshiClient(timeout=60.0) as client:
        kalshi_markets = []
        async for market in client.list_markets(closed=False, limit=10):
            if "bitcoin" in market.question.lower() or "btc" in market.question.lower():
                kalshi_markets.append(market)
                if len(kalshi_markets) >= 3:
                    break

        for market in kalshi_markets:
            print(f"\n  Question: {market.question}")
            print(f"  Ticker: {market.id}")
            print(f"  Volume: ${market.volume:,.2f}" if market.volume else "  Volume: N/A")
            print("  Outcomes:")
            for outcome in market.outcomes:
                if outcome.price:
                    print(f"    - {outcome.name}: {outcome.price:.2%}")
                else:
                    print(f"    - {outcome.name}: N/A")

    # Search for Bitcoin markets on Limitless
    print("\n\n[Limitless] Searching for Bitcoin markets...")
    async with LimitlessClient(timeout=60.0) as client:
        limitless_markets = []
        async for market in client.list_markets(closed=False, limit=10):
            if "bitcoin" in market.question.lower() or "btc" in market.question.lower():
                limitless_markets.append(market)
                if len(limitless_markets) >= 3:
                    break

        for market in limitless_markets:
            print(f"\n  Question: {market.question}")
            print(f"  Slug: {market.id}")
            print(f"  Volume: ${market.volume:,.2f}" if market.volume else "  Volume: N/A")
            print("  Outcomes:")
            for outcome in market.outcomes:
                if outcome.price:
                    print(f"    - {outcome.name}: {outcome.price:.2%}")
                else:
                    print(f"    - {outcome.name}: N/A")

    print("\n" + "=" * 80)
    print("Analysis:")
    print(f"  Found {len(polymarket_markets)} Bitcoin markets on Polymarket")
    print(f"  Found {len(kalshi_markets)} Bitcoin markets on Kalshi")
    print(f"  Found {len(limitless_markets)} Bitcoin markets on Limitless")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
