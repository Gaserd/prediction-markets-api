"""Pytest configuration and fixtures."""

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_SCRIPTS = str(Path(__file__).resolve().parents[1] / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from nightly_smoke import kalshi_smoke_api_base  # noqa: E402


@pytest.fixture
def fixtures_dir() -> Path:
    """Return path to fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def polymarket_fixtures_dir(fixtures_dir: Path) -> Path:
    """Return path to Polymarket fixtures."""
    return fixtures_dir / "polymarket"


@pytest.fixture
def markets_response(polymarket_fixtures_dir: Path) -> dict:
    """Load markets API response fixture."""
    with open(polymarket_fixtures_dir / "markets_response.json") as f:
        return json.load(f)


@pytest.fixture
def orderbook_response(polymarket_fixtures_dir: Path) -> dict:
    """Load order book API response fixture."""
    with open(polymarket_fixtures_dir / "orderbook_response.json") as f:
        return json.load(f)


@pytest.fixture
def prices_response(polymarket_fixtures_dir: Path) -> dict:
    """Load prices API response fixture."""
    with open(polymarket_fixtures_dir / "prices_response.json") as f:
        return json.load(f)


@pytest.fixture
def trades_response(polymarket_fixtures_dir: Path) -> dict:
    """Load trades API response fixture."""
    with open(polymarket_fixtures_dir / "trades_response.json") as f:
        return json.load(f)


@pytest.fixture(autouse=True)
def _kalshi_demo_base_for_live_tests(request: pytest.FixtureRequest) -> Iterator[None]:
    """Point Kalshi live reads at the demo Trade API when KALSHI_API_BASE is set.

    KalshiClient hardcodes the production host and has no base-url argument.
    The nightly smoke job sets KALSHI_API_BASE to the demo host and never to
    production. When the variable is unset, live tests keep the adapter default.
    Non-live tests are left alone so recorded-fixture mocks still match.
    """
    if request.node.get_closest_marker("live") is None:
        yield
        return
    base = kalshi_smoke_api_base()
    if base is None:
        yield
        return
    from prediction_markets_api.adapters.kalshi import KalshiClient

    original = KalshiClient.API_BASE
    KalshiClient.API_BASE = base
    try:
        yield
    finally:
        KalshiClient.API_BASE = original
