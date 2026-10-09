#!/usr/bin/env python3
"""Check if README venue table is up to date with the registry."""

import re
import sys
from pathlib import Path

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent))

from generate_venue_table import generate_venue_table


def check_readme() -> bool:
    """Check if README venue table matches the generated table.

    Returns:
        True if up to date, False otherwise
    """
    readme_path = Path(__file__).parent.parent / "README.md"
    readme_content = readme_path.read_text()

    # Generate expected table
    expected_table = generate_venue_table()

    # Find the venue section
    venue_section_pattern = re.compile(
        r"## Supported Venues\n\n.*?\n\n\| Venue.*?\| BetDEX.*?\|\n",
        re.DOTALL | re.MULTILINE
    )

    match = venue_section_pattern.search(readme_content)

    if not match:
        print("❌ ERROR: Could not find '## Supported Venues' section in README", file=sys.stderr)
        return False

    current_table = match.group(0).rstrip()
    expected_table_with_newline = expected_table + "\n"

    if current_table != expected_table_with_newline.rstrip():
        print("❌ ERROR: README venue table is out of date", file=sys.stderr)
        print("\nRun the following command to update it:", file=sys.stderr)
        print("  python scripts/update_readme_table.py", file=sys.stderr)
        return False

    print("✓ README venue table is up to date")
    return True


if __name__ == "__main__":
    is_valid = check_readme()
    sys.exit(0 if is_valid else 1)
