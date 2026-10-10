"""Adapter discovery and configuration registry for contract tests."""

import importlib
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prediction_markets_api.adapters.base import BaseClient


@dataclass
class AdapterConfig:
    """Configuration for a venue adapter."""

    name: str
    client_class: type[BaseClient]
    fixtures_dir: Path
    liquidity_source: str | None


def discover_adapters(fixtures_base: Path) -> list[AdapterConfig]:
    """Discover all venue adapters by inspecting the adapters module.

    Args:
        fixtures_base: Base path to fixtures directory (tests/fixtures)

    Returns:
        List of AdapterConfig for each discovered adapter

    Design:
        - Auto-discovers BaseClient subclasses from adapters module
        - Matches each adapter to its fixture directory
        - New adapters are automatically included (e.g., Kalshi, Limitless)
    """
    adapters: list[AdapterConfig] = []

    # Import the adapters module
    adapters_module = importlib.import_module("prediction_markets_api.adapters")

    # Find all BaseClient subclasses
    for name, obj in inspect.getmembers(adapters_module, inspect.isclass):
        if (
            obj is not BaseClient
            and issubclass(obj, BaseClient)
            and obj.__module__.startswith("prediction_markets_api.adapters")
        ):
            # Infer venue name from class name (e.g., PolymarketClient -> polymarket)
            venue_name = name.replace("Client", "").lower()

            # Check if fixtures directory exists
            fixtures_dir = fixtures_base / venue_name
            if not fixtures_dir.exists():
                continue

            # Determine liquidity source field from adapter's venue implementation
            # This maps to the raw fixture field that contains liquidity data
            liquidity_source = _infer_liquidity_source(venue_name)

            adapters.append(
                AdapterConfig(
                    name=venue_name,
                    client_class=obj,
                    fixtures_dir=fixtures_dir,
                    liquidity_source=liquidity_source,
                )
            )

    return adapters


def _infer_liquidity_source(venue_name: str) -> str | None:
    """Map venue name to the raw data field name for liquidity.

    Args:
        venue_name: Venue identifier (e.g., 'polymarket', 'kalshi')

    Returns:
        Field name in raw fixture data, or None if liquidity is not available
    """
    # Registry of known liquidity source fields per venue
    LIQUIDITY_FIELDS: dict[str, str | None] = {
        "polymarket": "liquidityNum",  # Polymarket uses liquidityNum in raw data
        "kalshi": None,  # Kalshi may not expose liquidity
        "limitless": "liquidity",  # Hypothetical - adjust when added
    }

    return LIQUIDITY_FIELDS.get(venue_name, None)


def load_fixtures(adapter: AdapterConfig) -> dict[str, dict[str, Any]]:
    """Load all fixtures for an adapter.

    Args:
        adapter: AdapterConfig to load fixtures for

    Returns:
        Dict mapping fixture name to {data, meta} dicts

    Example:
        {
            'markets_response': {
                'data': {...},
                'meta': {'recorded_at': '...', 'request': {...}}
            },
            ...
        }
    """
    import json

    fixtures: dict[str, dict[str, Any]] = {}

    for fixture_file in adapter.fixtures_dir.glob("*.json"):
        # Skip .meta.json files
        if fixture_file.stem.endswith(".meta"):
            continue

        fixture_name = fixture_file.stem
        meta_file = fixture_file.with_suffix(".meta.json")

        # Load fixture data
        with open(fixture_file) as f:
            data = json.load(f)

        # Load metadata if available
        meta = None
        if meta_file.exists():
            with open(meta_file) as f:
                meta = json.load(f)

        fixtures[fixture_name] = {"data": data, "meta": meta}

    return fixtures
