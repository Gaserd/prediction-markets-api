"""Pytest configuration and fixtures."""

import json
from pathlib import Path

import pytest


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
