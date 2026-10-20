#!/usr/bin/env python3
"""Check if README venue tables are up to date with the registry."""

import re
import subprocess
import sys
from pathlib import Path

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent))

from generate_venue_table import generate_venue_table


def check_readme() -> bool:
    """Check if README venue tables match the generated tables.

    Returns:
        True if up to date, False otherwise
    """
    readme_path = Path(__file__).parent.parent / "README.md"
    readme_content = readme_path.read_text()

    all_valid = True

    # Check 1: Supported Venues table (existing table)
    expected_table = generate_venue_table()
    venue_section_pattern = re.compile(
        r"## Supported Venues\n\n.*?\n\n\| Venue.*?\| BetDEX.*?\|\n", re.DOTALL | re.MULTILINE
    )

    match = venue_section_pattern.search(readme_content)

    if not match:
        print("❌ ERROR: Could not find '## Supported Venues' section in README", file=sys.stderr)
        all_valid = False
    else:
        current_table = match.group(0).rstrip()
        expected_table_with_newline = expected_table + "\n"

        if current_table != expected_table_with_newline.rstrip():
            print("❌ ERROR: README 'Supported Venues' table is out of date", file=sys.stderr)
            print("\nRun the following command to update it:", file=sys.stderr)
            print("  python scripts/update_readme_table.py", file=sys.stderr)
            all_valid = False
        else:
            print("✓ README 'Supported Venues' table is up to date")

    # Check 2: Venues & referral links table (new table)
    # Extract the table between markers
    start_marker = "<!-- venues-table:start -->"
    end_marker = "<!-- venues-table:end -->"

    if start_marker not in readme_content or end_marker not in readme_content:
        print("❌ ERROR: Could not find venues table markers in README", file=sys.stderr)
        all_valid = False
    else:
        start_idx = readme_content.index(start_marker) + len(start_marker)
        end_idx = readme_content.index(end_marker)
        readme_table = readme_content[start_idx:end_idx].strip()

        # Generate the table using gen_venues_table.py
        script_path = Path(__file__).parent / "gen_venues_table.py"
        result = subprocess.run(
            ["python3", str(script_path)], capture_output=True, text=True, check=True
        )
        generated_table = result.stdout.strip()

        if readme_table != generated_table:
            print(
                "❌ ERROR: README 'Venues & referral links' table is out of date", file=sys.stderr
            )
            print("\nRun the following command to update it:", file=sys.stderr)
            print("  python3 scripts/gen_venues_table.py", file=sys.stderr)
            all_valid = False
        else:
            print("✓ README 'Venues & referral links' table is up to date")

    return all_valid


if __name__ == "__main__":
    is_valid = check_readme()
    sys.exit(0 if is_valid else 1)
