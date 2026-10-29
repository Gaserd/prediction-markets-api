"""Tests for venue registry."""

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from prediction_markets_api import (
    HyperliquidClient,
    KalshiClient,
    LimitlessClient,
    PolymarketClient,
)
from prediction_markets_api.venues import VENUES, VenueStatus


def test_all_venues_have_new_fields():
    """Test that every venue has the new url, referral_url, and supports_closed_markets fields."""
    for venue_key, venue in VENUES.items():
        assert hasattr(venue, "url"), f"Venue {venue_key} missing url field"
        assert hasattr(venue, "referral_url"), f"Venue {venue_key} missing referral_url field"
        assert hasattr(venue, "supports_closed_markets"), (
            f"Venue {venue_key} missing supports_closed_markets field"
        )

        assert isinstance(venue.url, str), f"Venue {venue_key} url must be string"
        assert venue.referral_url is None or isinstance(venue.referral_url, str), (
            f"Venue {venue_key} referral_url must be string or None"
        )
        assert isinstance(venue.supports_closed_markets, bool), (
            f"Venue {venue_key} supports_closed_markets must be bool"
        )


def test_limitless_fields():
    """Test Limitless has correct values for new fields."""
    venue = VENUES["limitless"]
    assert venue.url == "https://limitless.exchange"
    assert venue.referral_url == "https://limitless.exchange?r=0D2RQUXIT4"
    assert venue.supports_closed_markets is False


def test_polymarket_fields():
    """Test Polymarket has correct values for new fields."""
    venue = VENUES["polymarket"]
    assert venue.url == "https://polymarket.com"
    assert venue.referral_url is None
    assert venue.supports_closed_markets is True


def test_kalshi_fields():
    """Test Kalshi has correct values for new fields."""
    venue = VENUES["kalshi"]
    assert venue.url == "https://kalshi.com"
    assert venue.referral_url is None
    assert venue.supports_closed_markets is True


def test_pascal_fields():
    """Pascal is a read-only implemented Solana venue. The product is in beta."""
    venue = VENUES["pascal"]
    assert venue.url == "https://app.pascal.trade"
    assert venue.chain is not None
    assert venue.chain.value == "Solana"
    assert venue.referral_url is None
    assert venue.supports_closed_markets is True
    assert venue.can_read is True
    assert venue.can_trade is False
    assert venue.status == VenueStatus.IMPLEMENTED
    assert venue.liquidity_source is None
    assert venue.api_key_for_read is False
    assert venue.geo_restrictions == (
        "Restricted by venue ToS: US, UK, Russia (see ToS for the full list)"
    )


def test_hyperliquid_fields():
    """Test Hyperliquid Outcomes is a read-only implemented venue."""
    venue = VENUES["hyperliquid"]
    assert venue.url == "https://hyperliquid.xyz"
    assert venue.referral_url is None
    assert venue.supports_closed_markets is False
    assert venue.can_read is True
    assert venue.can_trade is False
    assert venue.status == VenueStatus.IMPLEMENTED
    assert venue.liquidity_source is None
    assert venue.api_key_for_read is False


@pytest.mark.asyncio
async def test_limitless_raises_not_implemented_for_closed_markets():
    """Test that Limitless raises NotImplementedError when closed=True."""
    client = LimitlessClient()

    with pytest.raises(
        NotImplementedError, match="Limitless API does not provide an endpoint for listing closed"
    ):
        async for _ in client.list_markets(closed=True):
            pass

    await client.close()


@pytest.mark.asyncio
async def test_hyperliquid_raises_not_implemented_for_closed_markets():
    """Settled HIP-4 markets are not listable; closed=True is unsupported."""
    client = HyperliquidClient()

    with pytest.raises(
        NotImplementedError,
        match="does not provide an endpoint for listing closed",
    ):
        async for _ in client.list_markets(closed=True):
            pass

    await client.close()


@pytest.mark.asyncio
async def test_polymarket_supports_closed_markets():
    """Test that Polymarket does not raise NotImplementedError for closed=True."""
    client = PolymarketClient()

    mock_response = MagicMock()
    mock_response.json.return_value = {"markets": [], "nextCursor": None}
    mock_response.raise_for_status = MagicMock()

    with patch.object(client._http_client, "get", new=AsyncMock(return_value=mock_response)):
        markets = []
        async for market in client.list_markets(closed=True, limit=1):
            markets.append(market)

        # Should not raise NotImplementedError
        assert True

    await client.close()


@pytest.mark.asyncio
async def test_kalshi_supports_closed_markets():
    """Test that Kalshi does not raise NotImplementedError for closed=True."""
    client = KalshiClient()

    mock_response = MagicMock()
    mock_response.json.return_value = {"markets": [], "cursor": None}
    mock_response.raise_for_status = MagicMock()

    with patch.object(client._http_client, "get", new=AsyncMock(return_value=mock_response)):
        markets = []
        async for market in client.list_markets(closed=True, limit=1):
            markets.append(market)

        # Should not raise NotImplementedError
        assert True

    await client.close()


def test_readme_table_generated_correctly():
    """Test that the README table matches the output of the generator script."""
    # Read the README
    readme_path = Path(__file__).parent.parent / "README.md"
    readme_content = readme_path.read_text()

    # Extract the table between markers
    start_marker = "<!-- venues-table:start -->"
    end_marker = "<!-- venues-table:end -->"

    assert start_marker in readme_content, "README missing start marker"
    assert end_marker in readme_content, "README missing end marker"

    start_idx = readme_content.index(start_marker) + len(start_marker)
    end_idx = readme_content.index(end_marker)
    readme_table = readme_content[start_idx:end_idx].strip()

    # Generate the table
    script_path = Path(__file__).parent.parent / "scripts" / "gen_venues_table.py"
    result = subprocess.run(
        ["python3", str(script_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    generated_table = result.stdout.strip()

    assert readme_table == generated_table, (
        "README table does not match generated table. Run: python3 scripts/gen_venues_table.py"
    )


def test_referral_links_in_readme():
    """Test that every referral link in README is marked 'yes' and matches registry."""
    readme_path = Path(__file__).parent.parent / "README.md"
    readme_content = readme_path.read_text()

    # Extract the table between markers
    start_marker = "<!-- venues-table:start -->"
    end_marker = "<!-- venues-table:end -->"

    start_idx = readme_content.index(start_marker) + len(start_marker)
    end_idx = readme_content.index(end_marker)
    readme_table = readme_content[start_idx:end_idx].strip()

    # Check each venue
    for venue_key in ["limitless", "polymarket", "kalshi", "hyperliquid"]:
        venue = VENUES[venue_key]

        if venue.referral_url:
            # Should be marked as "yes" in README
            assert "| yes (`" in readme_table, (
                f"Referral link for {venue.name} should be marked 'yes' in README"
            )

            # Referral URL should appear in README
            assert venue.referral_url in readme_table, (
                f"Referral URL for {venue.name} not found in README"
            )

            # Plain link should be present
            assert f"plain link: {venue.url}" in readme_table, (
                f"Plain link for {venue.name} not found in README"
            )
        else:
            # Should be marked as "no" in README
            venue_line = [line for line in readme_table.split("\n") if venue.name in line]
            assert len(venue_line) == 1, f"Venue {venue.name} not found in README"
            assert "| no |" in venue_line[0], (
                f"Non-referral link for {venue.name} should be marked 'no' in README"
            )


def test_referral_disclosure_only_in_designated_section():
    """Test that referral/reward mentions only appear in the 'Venues & referral links' section."""
    readme_path = Path(__file__).parent.parent / "README.md"
    readme_content = readme_path.read_text()

    # Find the designated section
    section_start = readme_content.find("## Venues & referral links")
    assert section_start != -1, "Venues & referral links section not found"

    # Find the end of the section (next ## heading or end of file)
    section_end = readme_content.find("\n## ", section_start + 1)
    if section_end == -1:
        section_end = len(readme_content)

    readme_content[section_start:section_end]
    rest_of_readme = readme_content[:section_start] + readme_content[section_end:]

    # Check that referral/reward keywords only appear in the designated section
    # Exception: pointer sentences that reference the designated section are allowed
    referral_keywords = ["referral", "reward"]
    pointer_pattern = "see [Venues & referral links]"

    for keyword in referral_keywords:
        keyword_lower = keyword.lower()
        # Case-insensitive search in the rest of README
        if keyword_lower in rest_of_readme.lower():
            # Find where it appears
            lines = rest_of_readme.split("\n")
            for i, line in enumerate(lines):
                if keyword_lower in line.lower():
                    # Allow pointer sentences that reference the designated section
                    if pointer_pattern in line:
                        continue
                    raise AssertionError(
                        f"'{keyword}' mentioned outside 'Venues & referral links' section "
                        f"at line ~{i}: {line.strip()}"
                    )


def test_referral_params_only_in_designated_section():
    """Test that links with referral params only appear in 'Venues & referral links' section."""
    readme_path = Path(__file__).parent.parent / "README.md"
    readme_content = readme_path.read_text()

    # Find the designated section
    section_start = readme_content.find("## Venues & referral links")
    section_end = readme_content.find("\n## ", section_start + 1)
    if section_end == -1:
        section_end = len(readme_content)

    designated_section = readme_content[section_start:section_end]
    rest_of_readme = readme_content[:section_start] + readme_content[section_end:]

    # Check that all referral URLs from registry only appear in designated section
    for _venue_key, venue in VENUES.items():
        if venue.referral_url and "?" in venue.referral_url:
            # This is a referral link with parameters
            assert venue.referral_url in designated_section, (
                f"Referral URL for {venue.name} not found in designated section"
            )
            assert venue.referral_url not in rest_of_readme, (
                f"Referral URL for {venue.name} found outside designated section"
            )

    # Also check that no URL with common referral parameters appears outside
    referral_params = ["?r=", "?ref=", "?referral=", "&r=", "&ref=", "&referral="]
    for param in referral_params:
        # Search for markdown links with these params
        import re

        link_pattern = rf"\[.*?\]\((https?://[^\)]*{re.escape(param)}[^\)]*)\)"
        matches = re.finditer(link_pattern, rest_of_readme)
        for match in matches:
            url = match.group(1)
            raise AssertionError(
                f"Link with referral parameter found outside designated section: {url}"
            )
