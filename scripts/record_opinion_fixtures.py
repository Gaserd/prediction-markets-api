#!/usr/bin/env python3
"""Record Opinion read fixtures from the live API.

Writes tests/fixtures/opinion/*_2026-10-29.json from real responses.
Public market and order book reads do not need a key. This script never
sends or writes an API key. There is no public trade feed to record.

Unit tests do not call the network. Re-run this when refreshing fixtures.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = "https://openapi.opinion.trade/openapi"
OUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "opinion"
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


def write(stem: str, status: int, url: str, payload: Any, description: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{stem}_{DATE}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    meta = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "method": "GET",
        "url": url,
        "status": status,
        "request": {"url": url, "method": "GET"},
        "environment": "mainnet",
        "description": description,
    }
    if stem == "orderbook_response":
        # Contract tests address the book with this bare market id.
        meta["token_id"] = str(payload.get("_market_id", ""))
    path.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path.name} status={status}")


def pause() -> None:
    time.sleep(0.25)


def main() -> None:
    list_url = f"{BASE}/market?page=1&limit=2&status=activated&marketType=0"
    status, _headers, payload = fetch(list_url)
    write(
        "markets_response",
        status,
        list_url,
        payload,
        "Live mainnet GET /openapi/market binary page (no API key)",
    )
    pause()

    book_market: dict[str, Any] | None = None
    book: Any = None
    book_url = ""
    scan_url = f"{BASE}/market?page=1&limit=8&status=activated&marketType=0&sortBy=5"
    status, _headers, scan = fetch(scan_url)
    candidates = ((scan or {}).get("result") or {}).get("list") or []
    pause()
    for market in candidates:
        token = market.get("yesTokenId")
        if not token:
            continue
        url = f"{BASE}/token/orderbook?token_id={token}"
        status, _headers, body = fetch(url)
        result = (body or {}).get("result") or {}
        bids = result.get("bids") or []
        asks = result.get("asks") or []
        pause()
        if bids and asks:
            book_market = market
            book = body
            book_url = url
            break
        if book_market is None and (bids or asks):
            book_market = market
            book = body
            book_url = url
    if book_market is None or book is None:
        raise SystemExit("no order book recorded")
    market_id = str(book_market["marketId"])
    # token_id for the contract suite is the bare market id, not the token.
    book_to_save = dict(book)
    book_to_save["_market_id"] = market_id
    write(
        "orderbook_response",
        200,
        book_url,
        book_to_save,
        f"Live mainnet YES-token order book for market {market_id} (no API key)",
    )
    # The contract parser reads the venue payload. Drop the recorder-only key
    # from the JSON body and keep the market id in the meta file only.
    body_path = OUT_DIR / f"orderbook_response_{DATE}.json"
    saved = json.loads(body_path.read_text(encoding="utf-8"))
    saved.pop("_market_id", None)
    body_path.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
    meta_path = body_path.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["token_id"] = market_id
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    detail_url = f"{BASE}/market/{market_id}"
    status, _headers, detail = fetch(detail_url)
    write(
        "market_response",
        status,
        detail_url,
        detail,
        f"Live mainnet GET /openapi/market/{market_id} (no API key)",
    )
    pause()

    no_token = book_market.get("noTokenId")
    if no_token:
        no_url = f"{BASE}/token/orderbook?token_id={no_token}"
        status, _headers, no_book = fetch(no_url)
        write(
            "complement_orderbook",
            status,
            no_url,
            no_book,
            f"Live mainnet NO-token order book for market {market_id} (no API key)",
        )
        pause()

    cat_url = f"{BASE}/market?page=1&limit=1&status=activated&marketType=1"
    status, _headers, categorical = fetch(cat_url)
    write(
        "categorical_market",
        status,
        cat_url,
        categorical,
        "Live mainnet categorical parent page (no API key)",
    )
    parents = ((categorical or {}).get("result") or {}).get("list") or []
    pause()
    if parents:
        parent_id = parents[0].get("marketId")
        parent_url = f"{BASE}/market/categorical/{parent_id}"
        status, _headers, parent = fetch(parent_url)
        write(
            "categorical_detail",
            status,
            parent_url,
            parent,
            f"Live mainnet GET /openapi/market/categorical/{parent_id} (no API key)",
        )
        pause()

    resolved_url = f"{BASE}/market?page=1&limit=1&status=resolved&marketType=0"
    status, _headers, resolved = fetch(resolved_url)
    write(
        "resolved_market",
        status,
        resolved_url,
        resolved,
        "Live mainnet resolved binary page (no API key)",
    )
    pause()

    missing_url = f"{BASE}/market/999999999"
    status, _headers, missing = fetch(missing_url)
    write(
        "error_not_found",
        status,
        missing_url,
        missing,
        "Live mainnet unknown market id (HTTP 200, errno 10200, no API key)",
    )
    pause()

    for stem, path, description in (
        (
            "overlap_binary",
            "/market/2592",
            "Live mainnet GET /openapi/market/2592 (binary sharing id 2592, no API key)",
        ),
        (
            "overlap_categorical",
            "/market/categorical/2592",
            "Live mainnet GET /openapi/market/categorical/2592 (parent sharing id 2592, no API key)",
        ),
    ):
        overlap_url = f"{BASE}{path}"
        status, _headers, overlap = fetch(overlap_url)
        write(stem, status, overlap_url, overlap, description)
        pause()

    categorical_missing_url = f"{BASE}/market/categorical/999999999"
    status, _headers, categorical_missing = fetch(categorical_missing_url)
    write(
        "categorical_not_found",
        status,
        categorical_missing_url,
        categorical_missing,
        "Live mainnet unknown categorical id (HTTP 200, errno 0, result.data null, no API key)",
    )


if __name__ == "__main__":
    main()
