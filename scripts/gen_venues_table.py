#!/usr/bin/env python3
"""Generate the venues table for README from the venue registry."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from prediction_markets_api.venues import VENUES


def generate_table() -> str:
    """Generate markdown table from venue registry."""
    lines = [
        "| Venue | Sign up | Referral? | Closed markets | Notes |",
        "|---|---|---|---|---|",
    ]

    for venue_key in ["limitless", "polymarket", "kalshi"]:
        venue = VENUES[venue_key]

        # Sign up column - use referral link if available, otherwise plain URL
        if venue.referral_url:
            referral_param = venue.referral_url.split("?")[-1] if "?" in venue.referral_url else ""
            signup_link = f"[{venue.url.replace('https://', '')}]({venue.referral_url})"
        else:
            signup_link = f"[{venue.url.replace('https://', '')}]({venue.url})"

        # Referral column
        if venue.referral_url:
            referral_param = venue.referral_url.split("?")[-1] if "?" in venue.referral_url else ""
            referral = f"yes (`{referral_param}`)"
        else:
            referral = "no"

        # Closed markets column
        closed_markets = "yes" if venue.supports_closed_markets else "no"

        # Notes column
        notes = []
        if venue.referral_url:
            notes.append(f"plain link: {venue.url}")
        notes_str = " ".join(notes) if notes else ""

        lines.append(
            f"| {venue.name} | {signup_link} | {referral} | {closed_markets} | {notes_str} |"
        )

    return "\n".join(lines)


if __name__ == "__main__":
    print(generate_table())
