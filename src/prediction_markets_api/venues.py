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
    liquidity_source: str | None  # Venue field that maps to Market.liquidity, or None
    url: str
    referral_url: str | None
    supports_closed_markets: bool


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
        liquidity_source="liquidityNum",  # Polymarket provides liquidity metric
        url="https://polymarket.com",
        referral_url=None,
        supports_closed_markets=True,
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
        liquidity_source=None,  # Kalshi does not provide liquidity metric
        url="https://kalshi.com",
        referral_url=None,
        supports_closed_markets=True,
    ),
    "limitless": VenueInfo(
        name="Limitless",
        chain=Chain.BASE,
        geo_restrictions=None,
        api_key_for_read=False,
        api_key_for_trade=True,
        can_read=True,
        can_trade=False,
        status=VenueStatus.IMPLEMENTED,
        signup_link_key="limitless",
        liquidity_source=None,  # Limitless does not provide liquidity (absent on all ~75 checked live markets)
        url="https://limitless.exchange",
        referral_url="https://limitless.exchange?r=0D2RQUXIT4",
        supports_closed_markets=False,
    ),
    # Wave 2
    "hyperliquid": VenueInfo(
        name="Hyperliquid Outcomes (HIP-4)",
        chain=Chain.HYPERLIQUID,
        geo_restrictions=None,
        api_key_for_read=False,
        api_key_for_trade=True,
        can_read=True,
        can_trade=False,  # Stub only; place_order does not submit
        status=VenueStatus.IMPLEMENTED,
        signup_link_key="hyperliquid",
        liquidity_source=None,  # outcomeMeta has no liquidity field
        url="https://hyperliquid.xyz",
        referral_url=None,
        supports_closed_markets=False,  # settledOutcome is by id; there is no closed listing
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
        liquidity_source=None,  # Unknown until implemented
        url="https://predict.fun",
        referral_url=None,
        supports_closed_markets=False,
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
        liquidity_source=None,  # Unknown until implemented
        url="https://opinion.finance",
        referral_url=None,
        supports_closed_markets=False,
    ),
    "pascal": VenueInfo(
        name="Pascal",
        chain=Chain.SOLANA,
        # Named here: US, UK, and Russia. The Terms list more jurisdictions:
        # https://app.pascal.trade/terms.pdf
        geo_restrictions=("Restricted by venue ToS: US, UK, Russia (see ToS for the full list)"),
        api_key_for_read=False,
        api_key_for_trade=True,
        can_read=True,
        can_trade=False,  # Stub only; place_order does not submit
        status=VenueStatus.IMPLEMENTED,
        signup_link_key="pascal",
        liquidity_source=None,  # Market state has no liquidity field
        url="https://app.pascal.trade",
        referral_url=None,
        supports_closed_markets=True,  # resolved listing is lookback-limited (4h/24h/3d)
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
        liquidity_source=None,  # Unknown until implemented
        url="https://sx.bet",
        referral_url=None,
        supports_closed_markets=False,
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
        liquidity_source=None,  # Unknown until implemented
        url="https://azuro.org",
        referral_url=None,
        supports_closed_markets=False,
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
        liquidity_source=None,  # Unknown until implemented
        url="https://jup.ag",
        referral_url=None,
        supports_closed_markets=False,
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
        liquidity_source=None,  # Unknown until implemented
        url="https://polymarket.com",
        referral_url=None,
        supports_closed_markets=False,
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
        liquidity_source=None,  # Unknown until implemented
        url="https://novig.com",
        referral_url=None,
        supports_closed_markets=False,
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
        liquidity_source=None,  # Unknown until implemented
        url="https://world.org",
        referral_url=None,
        supports_closed_markets=False,
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
        liquidity_source=None,  # Unknown until implemented
        url="https://betdex.com",
        referral_url=None,
        supports_closed_markets=False,
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
