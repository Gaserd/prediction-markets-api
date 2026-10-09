#!/usr/bin/env python3
"""Generate venue table for README from the venue registry."""

import sys
from pathlib import Path

# Add src to path to import the module
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]

from prediction_markets_api.venues import VENUES, VenueStatus


def load_signup_links() -> dict[str, str]:
    """Load signup links from venue_config.toml."""
    config_path = Path(__file__).parent.parent / "venue_config.toml"
    with open(config_path, "rb") as f:
        config = tomllib.load(f)
    return config.get("signup_links", {})


def generate_venue_table() -> str:
    """Generate markdown table of venues."""
    signup_links = load_signup_links()

    lines = [
        "## Supported Venues",
        "",
        "**Note**: Signup links are referral links; the project receives a reward when you sign up.",
        "",
        "| Venue | Chain | Status | Read | Trade | API Key (Read) | API Key (Trade) | Geo Restrictions | Signup Link |",
        "|-------|-------|--------|------|-------|----------------|-----------------|------------------|-------------|",
    ]

    # Sort venues by wave (defined by their order in the registry)
    venue_items = list(VENUES.items())

    for _key, venue in venue_items:
        # Status emoji
        status_emoji = "✅" if venue.status == VenueStatus.IMPLEMENTED else "🚧"
        status_text = f"{status_emoji} {venue.status.value.capitalize()}"

        # Read/Trade support
        read_icon = "✅" if venue.can_read else "—"
        trade_icon = "✅" if venue.can_trade else "—"

        # API key requirements
        api_read = "Yes" if venue.api_key_for_read else "No"
        api_trade = "Yes" if venue.api_key_for_trade else "No"

        # Geo restrictions
        geo = venue.geo_restrictions or "None"

        # Chain
        chain = venue.chain.value if venue.chain else "Unknown"

        # Signup link (with note about referral)
        signup_url = signup_links.get(venue.signup_link_key, "#")
        signup_link = f"[Link]({signup_url})"

        lines.append(
            f"| {venue.name} | {chain} | {status_text} | {read_icon} | {trade_icon} | "
            f"{api_read} | {api_trade} | {geo} | {signup_link} |"
        )

    return "\n".join(lines)


if __name__ == "__main__":
    print(generate_venue_table())
