"""Venue registry for prediction market platforms.

This registry maintains metadata about supported and planned prediction market venues,
including their capabilities, requirements, and restrictions.
"""

from dataclasses import dataclass
from enum import Enum


class VenueStatus(str, Enum):
    """Implementation status of a venue adapter."""

    IMPLEMENTED = "implemented"
    PLANNED = "planned"


class Chain(str, Enum):
    """Blockchain network."""

    POLYGON = "Polygon"
    ARBITRUM = "Arbitrum"
    BASE = "Base"
    SOLANA = "Solana"
    HYPERLIQUID = "Hyperliquid"
    NONE = "None"  # Centralized / no blockchain


@dataclass(frozen=True)
class VenueInfo:
    """Information about a prediction market venue."""

    name: str
    chain: Chain | None
    geo_restrictions: str | None
    api_key_for_read: bool
    api_key_for_trade: bool
    can_read: bool
    can_trade: bool
    status: VenueStatus
    signup_link_key: str


# Venue registry
# NOTE: Signup links are referral links; the project receives a reward.
VENUES: dict[str, VenueInfo] = {
    # Wave 1
    "polymarket": VenueInfo(
        name="Polymarket",
        chain=Chain.POLYGON,
        geo_restrictions="US blocked",
        api_key_for_read=False,
        api_key_for_trade=True,
        can_read=True,
        can_trade=False,  # Stub only in current version
        status=VenueStatus.IMPLEMENTED,
        signup_link_key="polymarket",
    ),
    "kalshi": VenueInfo(
        name="Kalshi",
        chain=Chain.NONE,
        geo_restrictions="US only",
        api_key_for_read=False,
        api_key_for_trade=True,
        can_read=True,
        can_trade=False,
        status=VenueStatus.IMPLEMENTED,
        signup_link_key="kalshi",
    ),
    "limitless": VenueInfo(
        name="Limitless",
        chain=Chain.BASE,
        geo_restrictions=None,
        api_key_for_read=False,
        api_key_for_trade=True,
        can_read=False,
        can_trade=False,
        status=VenueStatus.PLANNED,
        signup_link_key="limitless",
    ),
    # Wave 2
    "hyperliquid": VenueInfo(
        name="Hyperliquid Outcomes (HIP-4)",
        chain=Chain.HYPERLIQUID,
        geo_restrictions=None,
        api_key_for_read=False,
        api_key_for_trade=True,
        can_read=False,
        can_trade=False,
        status=VenueStatus.PLANNED,
        signup_link_key="hyperliquid",
    ),
    "predict_fun": VenueInfo(
        name="Predict.fun",
        chain=Chain.BASE,
        geo_restrictions=None,
        api_key_for_read=False,
        api_key_for_trade=True,
        can_read=False,
        can_trade=False,
        status=VenueStatus.PLANNED,
        signup_link_key="predict_fun",
    ),
    "opinion": VenueInfo(
        name="Opinion",
        chain=None,  # Unknown
        geo_restrictions=None,
        api_key_for_read=False,
        api_key_for_trade=True,
        can_read=False,
        can_trade=False,
        status=VenueStatus.PLANNED,
        signup_link_key="opinion",
    ),
    "pascal": VenueInfo(
        name="Pascal",
        chain=Chain.BASE,
        geo_restrictions=None,
        api_key_for_read=False,
        api_key_for_trade=True,
        can_read=False,
        can_trade=False,
        status=VenueStatus.PLANNED,
        signup_link_key="pascal",
    ),
    # Wave 3
    "sx_bet": VenueInfo(
        name="SX Bet",
        chain=Chain.ARBITRUM,
        geo_restrictions=None,
        api_key_for_read=False,
        api_key_for_trade=True,
        can_read=False,
        can_trade=False,
        status=VenueStatus.PLANNED,
        signup_link_key="sx_bet",
    ),
    "azuro": VenueInfo(
        name="Azuro/Bookmaker.xyz",
        chain=Chain.POLYGON,
        geo_restrictions=None,
        api_key_for_read=False,
        api_key_for_trade=True,
        can_read=False,
        can_trade=False,
        status=VenueStatus.PLANNED,
        signup_link_key="azuro",
    ),
    "jupiter": VenueInfo(
        name="Jupiter Prediction",
        chain=Chain.SOLANA,
        geo_restrictions=None,
        api_key_for_read=False,
        api_key_for_trade=True,
        can_read=False,
        can_trade=False,
        status=VenueStatus.PLANNED,
        signup_link_key="jupiter",
    ),
    # Wave 4
    "polymarket_us": VenueInfo(
        name="Polymarket US",
        chain=None,  # Unknown
        geo_restrictions="US only",
        api_key_for_read=False,
        api_key_for_trade=True,
        can_read=False,
        can_trade=False,
        status=VenueStatus.PLANNED,
        signup_link_key="polymarket_us",
    ),
    "novig": VenueInfo(
        name="Novig",
        chain=Chain.NONE,
        geo_restrictions=None,
        api_key_for_read=False,
        api_key_for_trade=True,
        can_read=False,
        can_trade=False,
        status=VenueStatus.PLANNED,
        signup_link_key="novig",
    ),
    "world": VenueInfo(
        name="World",
        chain=None,  # Unknown
        geo_restrictions=None,
        api_key_for_read=False,
        api_key_for_trade=True,
        can_read=False,
        can_trade=False,
        status=VenueStatus.PLANNED,
        signup_link_key="world",
    ),
    "betdex": VenueInfo(
        name="BetDEX",
        chain=Chain.SOLANA,
        geo_restrictions=None,
        api_key_for_read=False,
        api_key_for_trade=True,
        can_read=False,
        can_trade=False,
        status=VenueStatus.PLANNED,
        signup_link_key="betdex",
    ),
}


def get_venue(venue_key: str) -> VenueInfo | None:
    """Get venue information by key.

    Args:
        venue_key: Venue identifier key

    Returns:
        VenueInfo object or None if not found
    """
    return VENUES.get(venue_key)


def list_venues(status: VenueStatus | None = None) -> list[VenueInfo]:
    """List all venues, optionally filtered by status.

    Args:
        status: Filter by VenueStatus (None = all venues)

    Returns:
        List of VenueInfo objects
    """
    venues = list(VENUES.values())
    if status is not None:
        venues = [v for v in venues if v.status == status]
    return venues
