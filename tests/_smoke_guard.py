"""Kalshi demo-host guard used only by live tests.

The nightly workflow sets ``KALSHI_API_BASE``. Loading this module imports
``scripts/nightly_smoke.py``. Callers must do that from a live test, not from
``conftest`` at collection time, so the rest of the suite does not depend on
the smoke script.
"""

from __future__ import annotations

import sys
from pathlib import Path


def kalshi_smoke_api_base() -> str | None:
    """Return the demo Trade API root, or None when the variable is unset."""
    scripts = str(Path(__file__).resolve().parents[1] / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from nightly_smoke import kalshi_smoke_api_base as impl

    return impl()
