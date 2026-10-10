#!/usr/bin/env python3
"""Re-record order book and price fixtures from a market with orders."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
FIXTURES_DIR = Path(__file__).parent.parent / "tests" / "fixtures" / "kalshi"
TICKER = "KXNEXTNATOSECGEN-99-KIOH"


async def record_book_and_price():
    """Record order book and derive prices back-to-back."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        print(f"Recording fixtures for {TICKER}...")

        # Record order book
        recorded_at = datetime.now(timezone.utc).isoformat()
        print("\nFetching order book...")
        ob_response = await client.get(f"{API_BASE}/markets/{TICKER}/orderbook")
        ob_response.raise_for_status()
        orderbook_data = ob_response.json()

        # Save order book
        with open(FIXTURES_DIR / "orderbook_response.json", "w") as f:
            json.dump(orderbook_data, f, indent=2)

        with open(FIXTURES_DIR / "orderbook_response.meta.json", "w") as f:
            json.dump(
                {
                    "recorded_at": recorded_at,
                    "ticker": TICKER,
                    "method": "GET",
                    "url": f"{API_BASE}/markets/{TICKER}/orderbook",
                    "description": f"Order book for {TICKER} (market with orders on both sides)",
                },
                f,
                indent=2,
            )

        print("✓ Recorded order book")

        # Derive prices from order book
        ob = orderbook_data.get("orderbook_fp", {})
        yes_bids = ob.get("yes_dollars", [])
        no_bids = ob.get("no_dollars", [])

        buy_price = None
        sell_price = None
        buy_raw = None
        sell_raw = None

        if yes_bids:
            # Best YES bid (for selling YES) - first element (sorted descending)
            sell_raw = yes_bids[0][0]
            sell_price = float(sell_raw)

        if no_bids:
            # Best NO bid (for buying YES) - LAST element (sorted ascending)
            no_bid_raw = no_bids[-1][0]
            buy_raw = no_bid_raw
            buy_price = 1.0 - float(no_bid_raw)

        # Save prices
        with open(FIXTURES_DIR / "prices_response.json", "w") as f:
            json.dump(
                {
                    "buy": {
                        "price": buy_price,
                        "raw_price": buy_raw,
                        "note": "Derived from best NO bid (ask_yes = 1 - bid_no)",
                    }
                    if buy_price is not None
                    else None,
                    "sell": {
                        "price": sell_price,
                        "raw_price": sell_raw,
                        "note": "Best YES bid",
                    }
                    if sell_price is not None
                    else None,
                    "source": "derived_from_orderbook",
                },
                f,
                indent=2,
            )

        with open(FIXTURES_DIR / "prices_response.meta.json", "w") as f:
            json.dump(
                {
                    "recorded_at": recorded_at,
                    "ticker": TICKER,
                    "method": "DERIVED",
                    "url": f"{API_BASE}/markets/{TICKER}/orderbook",
                    "note": "Prices derived from order book recorded back-to-back. "
                    "BUY price = 1 - best_no_bid, SELL price = best_yes_bid",
                    "description": f"Prices for {TICKER}",
                    "yes_bids_count": len(yes_bids),
                    "no_bids_count": len(no_bids),
                },
                f,
                indent=2,
            )

        print("✓ Recorded prices")
        print("\nSummary:")
        print(f"  YES bids: {len(yes_bids)}")
        print(f"  NO bids: {len(no_bids)}")
        if sell_price:
            print(f"  SELL price (best YES bid): {sell_price}")
        if buy_price:
            print(f"  BUY price (1 - best NO bid): {buy_price}")


if __name__ == "__main__":
    asyncio.run(record_book_and_price())
