"""Utility functions for prediction markets API."""

from datetime import datetime, timedelta, timezone


def parse_timestamp(timestamp: int | float | str, *, is_milliseconds: bool = False) -> datetime:
    """Parse a timestamp into a timezone-aware UTC datetime.

    Args:
        timestamp: Unix timestamp as int, float, or string
        is_milliseconds: If True, timestamp is in milliseconds; if False, in seconds

    Returns:
        Timezone-aware datetime in UTC

    Raises:
        ValueError: If timestamp is invalid or out of range

    Examples:
        >>> from datetime import timezone
        >>> # Seconds
        >>> dt = parse_timestamp(1609459200)  # 2021-01-01 00:00:00 UTC
        >>> dt.tzinfo == timezone.utc
        True
        >>> # Milliseconds
        >>> dt = parse_timestamp(1609459200000, is_milliseconds=True)
        >>> dt.year
        2021
        >>> # String input
        >>> dt = parse_timestamp("1609459200")
        >>> dt.year
        2021
        >>> # Zero
        >>> dt = parse_timestamp(0)
        >>> dt.year
        1970
    """
    try:
        # Convert to float
        if isinstance(timestamp, str):
            ts_float = float(timestamp)
        else:
            ts_float = float(timestamp)

        # Convert milliseconds to seconds
        if is_milliseconds:
            ts_float = ts_float / 1000.0

        # Use timedelta to avoid platform-specific fromtimestamp() issues on Windows
        # Windows has issues with negative timestamps and very large timestamps
        # This approach works consistently across all platforms
        return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=ts_float)
    except (ValueError, TypeError, OSError, OverflowError) as e:
        raise ValueError(f"Invalid timestamp: {timestamp}") from e
