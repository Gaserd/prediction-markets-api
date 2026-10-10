#!/usr/bin/env python3
"""Update README with generated venue table."""

import re
import sys
from pathlib import Path

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent))

from generate_venue_table import generate_venue_table


def update_readme() -> bool:
    """Update README with generated venue table.

    Returns:
        True if README was modified, False if already up to date
    """
    readme_path = Path(__file__).parent.parent / "README.md"
    readme_content = readme_path.read_text()

    # Generate new table
    new_table = generate_venue_table()

    # Find the venue section using a pattern that matches just the table
    venue_section_pattern = re.compile(
        r"(## Supported Venues\n\n.*?\n\n\| Venue.*?\| BetDEX.*?\|)\n",
        re.DOTALL | re.MULTILINE
    )

    # Check if the section exists
    match = venue_section_pattern.search(readme_content)

    if match:
        old_table = match.group(1)
        if old_table == new_table:
            print("✓ README venue table is already up to date")
            return False

        # Replace the old table with the new one
        updated_content = venue_section_pattern.sub(new_table + "\n", readme_content, count=1)
        readme_path.write_text(updated_content)
        print("✓ README venue table updated")
        return True
    else:
        print("ERROR: Could not find '## Supported Venues' section in README", file=sys.stderr)
        return False


if __name__ == "__main__":
    modified = update_readme()
    sys.exit(0 if not modified else 1)  # Exit 0 if no changes, 1 if modified
