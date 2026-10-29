#!/usr/bin/env python3
"""Record Predict.fun read fixtures from the live API.

Writes tests/fixtures/predict_fun/*_2026-10-29.json from real responses.
Mainnet list/book/trade reads require PREDICTFUN_API_KEY and return HTTP 401
without it. This script records the public testnet (no key) plus the mainnet
401 body. It never sends or writes an API key.

Unit tests do not call the network. Re-run this when refreshing fixtures.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

TESTNET = "https://api-testnet.predict.fun"
MAINNET = "https://api.predict.fun"
OUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "predict_fun"
DATE = "2026-10-29"


def fetch(url: str) -> tuple[int, dict[str, str], Any]:
    request = Request(
        url, headers={"Accept": "application/json", "User-Agent": "prediction-markets-api"}
    )
    try:
        with urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode())
            headers = {key.lower(): value for key, value in response.headers.items()}
            return response.status, headers, payload
    except HTTPError as exc:
        raw = exc.read().decode()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        headers = {key.lower(): value for key, value in exc.headers.items()} if exc.headers else {}
        return exc.code, headers, payload


def write_json(name: str, payload: Any) -> None:
    path = OUT_DIR / name
    path.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {path} ({path.stat().st_size} bytes)")


def write_meta(stem: str, *, url: str, recorded_at: str, status: int, **extra: Any) -> None:
    meta: dict[str, Any] = {
        "recorded_at": recorded_at,
        "method": "GET",
        "url": url,
        "status": status,
        "request": {"url": url, "method": "GET"},
        "environment": "testnet" if url.startswith(TESTNET) else "mainnet",
        "note": (
            "Recorded from the public testnet because mainnet reads require "
            "PREDICTFUN_API_KEY and return HTTP 401 without it. The testnet "
            "payload uses the same read schema."
            if url.startswith(TESTNET)
            else "Mainnet response without an API key."
        ),
    }
    meta.update(extra)
    write_json(f"{stem}.meta.json", meta)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    recorded_at = datetime.now(timezone.utc).isoformat()
    print(f"recording at {recorded_at}")

    markets_url = f"{TESTNET}/v1/markets?first=2&status=OPEN"
    status, headers, markets = fetch(markets_url)
    if status != 200:
        raise SystemExit(f"markets failed: {status} {markets}")
    write_json(f"markets_response_{DATE}.json", markets)
    write_meta(
        f"markets_response_{DATE}",
        url=markets_url,
        recorded_at=recorded_at,
        status=status,
        description="Live testnet GET /v1/markets?first=2&status=OPEN",
        rate_limit={
            "limit": headers.get("ratelimit-limit"),
            "policy": headers.get("ratelimit-policy"),
        },
    )

    data = markets.get("data") or []
    if not data:
        raise SystemExit("no open markets on testnet")
    market_id = data[0]["id"]
    primary = next(outcome for outcome in data[0]["outcomes"] if outcome.get("indexSet") == 1)
    outcome_id = f"{market_id}:{primary['indexSet']}:{primary['onChainId']}"

    market_url = f"{TESTNET}/v1/markets/{market_id}"
    status, _, market = fetch(market_url)
    write_json(f"market_response_{DATE}.json", market)
    write_meta(
        f"market_response_{DATE}",
        url=market_url,
        recorded_at=recorded_at,
        status=status,
        description=f"Live testnet GET /v1/markets/{market_id}",
        token_id=outcome_id,
    )

    book_url = f"{TESTNET}/v1/markets/{market_id}/orderbook"
    status, _, book = fetch(book_url)
    write_json(f"orderbook_response_{DATE}.json", book)
    write_meta(
        f"orderbook_response_{DATE}",
        url=book_url,
        recorded_at=recorded_at,
        status=status,
        description=f"Live testnet order book for market {market_id} (indexSet 1 prices)",
        token_id=str(market_id),
    )

    trades_url = f"{TESTNET}/v1/orders/matches?marketId={market_id}&first=2"
    status, _, trades = fetch(trades_url)
    write_json(f"trades_response_{DATE}.json", trades)
    write_meta(
        f"trades_response_{DATE}",
        url=trades_url,
        recorded_at=recorded_at,
        status=status,
        description=f"Live testnet match events for market {market_id}",
        token_id=str(market_id),
    )

    scan_url = f"{TESTNET}/v1/markets?first=50&status=OPEN"
    status, _, scan = fetch(scan_url)
    onesided = None
    precise = None
    for item in scan.get("data") or []:
        outcomes = item.get("outcomes") or []
        if onesided is None and any(
            isinstance(outcome, dict)
            and (outcome.get("bestBid") is None) != (outcome.get("bestAsk") is None)
            for outcome in outcomes
        ):
            onesided = item
        if precise is None and item.get("decimalPrecision") not in (None, 2):
            precise = item
    if onesided is None:
        raise SystemExit("no one-sided market in the first 50 open markets")
    side_id = onesided["id"]
    side_url = f"{TESTNET}/v1/markets/{side_id}"
    status, _, side_market = fetch(side_url)
    write_json(f"onesided_market_{DATE}.json", side_market)
    write_meta(
        f"onesided_market_{DATE}",
        url=side_url,
        recorded_at=recorded_at,
        status=status,
        description=f"Live testnet market {side_id} with a one-sided book",
    )
    side_book_url = f"{TESTNET}/v1/markets/{side_id}/orderbook"
    status, _, side_book = fetch(side_book_url)
    write_json(f"onesided_orderbook_{DATE}.json", side_book)
    write_meta(
        f"onesided_orderbook_{DATE}",
        url=side_book_url,
        recorded_at=recorded_at,
        status=status,
        description=f"Live testnet order book for one-sided market {side_id}",
        token_id=str(side_id),
    )
    if precise is not None:
        precise_url = f"{TESTNET}/v1/markets/{precise['id']}"
        status, _, precise_market = fetch(precise_url)
        write_json(f"precision_market_{DATE}.json", precise_market)
        write_meta(
            f"precision_market_{DATE}",
            url=precise_url,
            recorded_at=recorded_at,
            status=status,
            description=(
                f"Live testnet market {precise['id']} with decimalPrecision "
                f"{precise.get('decimalPrecision')}"
            ),
        )

    resolved_url = f"{TESTNET}/v1/markets?first=1&status=RESOLVED"
    status, _, resolved = fetch(resolved_url)
    write_json(f"resolved_markets_{DATE}.json", resolved)
    write_meta(
        f"resolved_markets_{DATE}",
        url=resolved_url,
        recorded_at=recorded_at,
        status=status,
        description="Live testnet GET /v1/markets?first=1&status=RESOLVED",
    )

    missing_url = f"{TESTNET}/v1/markets/999999999"
    status, _, missing = fetch(missing_url)
    write_json(f"error_not_found_{DATE}.json", missing)
    write_meta(
        f"error_not_found_{DATE}",
        url=missing_url,
        recorded_at=recorded_at,
        status=status,
        description="Live testnet 404 for an unknown market id",
    )

    denied_url = f"{MAINNET}/v1/markets?first=1"
    status, _, denied = fetch(denied_url)
    write_json(f"error_unauthorized_{DATE}.json", denied)
    write_meta(
        f"error_unauthorized_{DATE}",
        url=denied_url,
        recorded_at=recorded_at,
        status=status,
        description="Live mainnet 401 when PREDICTFUN_API_KEY is not sent",
    )


if __name__ == "__main__":
    main()
