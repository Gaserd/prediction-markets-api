#!/usr/bin/env python3
"""Record Polymarket /v2/tokens endpoint fixture from live API."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx


async def main():
    """Record tokens endpoint response."""
    fixtures_dir = Path(__file__).parent.parent / "tests" / "fixtures" / "polymarket"
    fixtures_dir.mkdir(parents=True, exist_ok=True)

    # Use some real token IDs from a known market
    token_ids = [
        "54533043819946592547517511176940999955633860128497669742211153063842200957669",
        "32338220190071351435772801779725302244575775216413325951443816017994629993401",
    ]

    url = "https://data-api.polymarket.com/v2/tokens"
    params = {"token_ids": ",".join(token_ids)}

    async with httpx.AsyncClient(timeout=30.0) as client:
        print(f"Recording GET {url}")
        print(f"Params: {params}")

        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()

        print(f"Response: {len(data.get('data', []))} tokens")

        # Save response
        response_file = fixtures_dir / "tokens_response.json"
        with open(response_file, "w") as f:
            json.dump(data, f, indent=2)
        print(f"Saved to {response_file}")

        # Save metadata
        meta = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "request": {
                "method": "GET",
                "url": url,
                "params": params,
            },
            "description": "Response from Polymarket Data API v2 /tokens endpoint (added Oct 7, 2026). Shows batch token data retrieval.",
        }

        meta_file = fixtures_dir / "tokens_response.meta.json"
        with open(meta_file, "w") as f:
            json.dump(meta, f, indent=2)
        print(f"Saved metadata to {meta_file}")


if __name__ == "__main__":
    asyncio.run(main())
