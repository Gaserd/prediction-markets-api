#!/usr/bin/env python3
"""Record fixtures from live Kalshi API."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
FIXTURES_DIR = Path(__file__).parent.parent / "tests" / "fixtures" / "kalshi"


async def record_fixtures():
    """Record fixtures from Kalshi API."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. Record markets response - fetch more to find good ones
        print("Fetching markets...")
        response = await client.get(f"{API_BASE}/markets", params={"limit": 100})
        response.raise_for_status()
        all_markets_data = response.json()

        # Filter for simple binary markets (not multivariate)
        markets = [
            m
            for m in all_markets_data.get("markets", [])
            if "MVE" not in m["ticker"] and "CROSS" not in m.get("event_ticker", "")
        ]

        if not markets:
            # Fallback to all markets
            markets = all_markets_data.get("markets", [])[:3]

        # Take first 3 simple markets
        markets = markets[:3]

        markets_data = {"cursor": all_markets_data.get("cursor"), "markets": markets}

        # Save markets response
        with open(FIXTURES_DIR / "markets_response.json", "w") as f:
            json.dump(markets_data, f, indent=2)

        # Save markets metadata
        with open(FIXTURES_DIR / "markets_response.meta.json", "w") as f:
            json.dump(
                {
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "method": "GET",
                    "url": f"{API_BASE}/markets",
                    "params": {"limit": 100},
                    "note": "Filtered for simple binary markets (non-multivariate)",
                    "description": "Sample markets from Kalshi",
                },
                f,
                indent=2,
            )

        print(f"✓ Recorded {len(markets_data.get('markets', []))} markets")

        # Get first market ticker
        markets = markets_data.get("markets", [])
        if not markets:
            print("No markets found!")
            return

        ticker = markets[0]["ticker"]
        print(f"Using ticker: {ticker}")

        # 2. Record order book (back-to-back with price check)
        print("Fetching order book...")
        orderbook_response = await client.get(f"{API_BASE}/markets/{ticker}/orderbook")
        orderbook_response.raise_for_status()
        orderbook_data = orderbook_response.json()

        with open(FIXTURES_DIR / "orderbook_response.json", "w") as f:
            json.dump(orderbook_data, f, indent=2)

        with open(FIXTURES_DIR / "orderbook_response.meta.json", "w") as f:
            json.dump(
                {
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "ticker": ticker,
                    "method": "GET",
                    "url": f"{API_BASE}/markets/{ticker}/orderbook",
                    "description": f"Order book for {ticker}",
                },
                f,
                indent=2,
            )

        print(f"✓ Recorded order book for {ticker}")

        # 3. Record prices (should match order book top levels)
        # Note: Kalshi doesn't have a separate price endpoint, we derive from orderbook
        # Store the book data again as "prices" for consistency with polymarket structure
        with open(FIXTURES_DIR / "prices_response.json", "w") as f:
            # Extract best prices from orderbook
            yes_bids = orderbook_data.get("orderbook", {}).get("yes", [])
            no_bids = orderbook_data.get("orderbook", {}).get("no", [])

            buy_price = None
            sell_price = None

            if yes_bids:
                # Best bid for YES
                sell_price = yes_bids[0][0] / 100  # cents to dollars

            if no_bids:
                # Best ask for YES (derived from NO bid)
                buy_price = (100 - no_bids[0][0]) / 100

            json.dump(
                {
                    "buy": {"price": buy_price} if buy_price else None,
                    "sell": {"price": sell_price} if sell_price else None,
                    "source": "derived_from_orderbook",
                },
                f,
                indent=2,
            )

        with open(FIXTURES_DIR / "prices_response.meta.json", "w") as f:
            json.dump(
                {
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "ticker": ticker,
                    "method": "DERIVED",
                    "url": f"{API_BASE}/markets/{ticker}/orderbook",
                    "note": "Prices derived from order book (Kalshi has no separate price endpoint)",
                    "description": f"Prices for {ticker}",
                },
                f,
                indent=2,
            )

        print(f"✓ Recorded prices for {ticker}")

        # 4. Record trades
        print("Fetching trades...")
        try:
            trades_response = await client.get(
                f"{API_BASE}/markets/{ticker}/trades", params={"limit": 10}
            )
            trades_response.raise_for_status()
            trades_data = trades_response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                print(f"No trades endpoint for {ticker}, trying to find another market with trades...")
                # Try a few more markets
                found_trades = False
                for market in markets[:5]:  # Try first 5 markets
                    alt_ticker = market["ticker"]
                    try:
                        trades_response = await client.get(
                            f"{API_BASE}/markets/{alt_ticker}/trades", params={"limit": 10}
                        )
                        trades_response.raise_for_status()
                        trades_data = trades_response.json()
                        if trades_data.get("trades"):
                            ticker = alt_ticker
                            found_trades = True
                            print(f"Found trades for {ticker}")
                            break
                    except httpx.HTTPStatusError:
                        continue

                if not found_trades:
                    # Create an empty trades response
                    trades_data = {"trades": [], "cursor": None}
                    print("No trades found for any market, using empty response")
            else:
                raise

        with open(FIXTURES_DIR / "trades_response.json", "w") as f:
            json.dump(trades_data, f, indent=2)

        with open(FIXTURES_DIR / "trades_response.meta.json", "w") as f:
            json.dump(
                {
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "ticker": ticker,
                    "method": "GET",
                    "url": f"{API_BASE}/markets/{ticker}/trades",
                    "params": {"limit": 10},
                    "description": f"Recent trades for {ticker}",
                },
                f,
                indent=2,
            )

        print(f"✓ Recorded {len(trades_data.get('trades', []))} trades")

    print("\n✓ All fixtures recorded successfully!")


if __name__ == "__main__":
    asyncio.run(record_fixtures())
