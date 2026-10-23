#!/usr/bin/env python3
"""Record Hyperliquid Outcomes fixtures from the live info API.

Writes tests/fixtures/hyperliquid/*.json from real mainnet responses.
Re-run when refreshing fixtures; unit tests do not call the network.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

INFO_URL = "https://api.hyperliquid.xyz/info"
OUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "hyperliquid"


def post(payload: dict[str, Any]) -> Any:
    request = Request(
        INFO_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode())


def write_json(name: str, payload: Any) -> None:
    path = OUT_DIR / name
    path.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {path} ({path.stat().st_size} bytes)")


def write_meta(name: str, body: dict[str, Any], recorded_at: str, **extra: Any) -> None:
    meta = {
        "recorded_at": recorded_at,
        "method": "POST",
        "url": INFO_URL,
        "request": {"url": INFO_URL, "method": "POST", "body": body},
        **extra,
    }
    write_json(name, meta)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    recorded_at = datetime.now(timezone.utc).isoformat()
    print(f"recording at {recorded_at}")

    meta = post({"type": "outcomeMeta"})
    write_json("markets_response.json", meta)
    write_meta(
        "markets_response.meta.json",
        {"type": "outcomeMeta"},
        recorded_at,
        description="Live HIP-4 outcomeMeta snapshot (outcomes, questions, deployers)",
    )

    templates = post({"type": "outcomeTemplates"})
    write_json("outcome_templates.json", templates)
    write_meta(
        "outcome_templates.meta.json",
        {"type": "outcomeTemplates"},
        recorded_at,
        description="Live outcomeTemplates used to render HIP-4 display names",
    )

    mids = post({"type": "allMids"})
    write_json("mids_response.json", mids)
    write_meta(
        "mids_response.meta.json",
        {"type": "allMids"},
        recorded_at,
        description="Live allMids snapshot, including HIP-4 #coins and other markets",
    )

    outcomes = meta["outcomes"]
    btc = next(
        item
        for item in outcomes
        if str(item.get("description", "")).startswith("class:priceBinary")
        and "underlying:BTC|" in str(item.get("description", ""))
    )
    outcome_id = int(btc["outcome"])
    coin = f"#{10 * outcome_id}"
    book = post({"type": "l2Book", "coin": coin})
    write_json("orderbook_response.json", book)
    write_meta(
        "orderbook_response.meta.json",
        {"type": "l2Book", "coin": coin},
        recorded_at,
        token_id=coin,
        market_id=str(outcome_id),
        description=f"l2Book for BTC daily binary outcome {outcome_id} side 0 ({coin})",
    )

    trades = post({"type": "recentTrades", "coin": coin})
    write_json("trades_response.json", trades)
    write_meta(
        "trades_response.meta.json",
        {"type": "recentTrades", "coin": coin},
        recorded_at,
        token_id=coin,
        market_id=str(outcome_id),
        description=f"recentTrades window for {coin}",
    )

    settled = post({"type": "settledOutcome", "outcome": 95})
    write_json("settled_outcome.json", settled)
    write_meta(
        "settled_outcome.meta.json",
        {"type": "settledOutcome", "outcome": 95},
        recorded_at,
        description="settledOutcome for historical BTC binary outcome 95 (settleFraction 0)",
    )

    empty_book = None
    empty_coin = None
    empty_outcome = None
    for index, item in enumerate(outcomes):
        candidate = int(item["outcome"])
        candidate_coin = f"#{10 * candidate}"
        if index % 25 == 0:
            print(f"scanning books {index}/{len(outcomes)}")
        snapshot = post({"type": "l2Book", "coin": candidate_coin})
        levels = snapshot.get("levels") if isinstance(snapshot, dict) else None
        if isinstance(levels, list) and len(levels) >= 2 and levels[0] == [] and levels[1] == []:
            empty_book = snapshot
            empty_coin = candidate_coin
            empty_outcome = candidate
            break
    if empty_book is not None and empty_coin is not None:
        write_json("empty_orderbook.json", empty_book)
        write_meta(
            "empty_orderbook.meta.json",
            {"type": "l2Book", "coin": empty_coin},
            recorded_at,
            token_id=empty_coin,
            market_id=str(empty_outcome),
            description=f"l2Book with no bids or asks for outcome {empty_outcome}",
        )
    else:
        print("no empty order book found; skipped empty_orderbook.json")


if __name__ == "__main__":
    main()
