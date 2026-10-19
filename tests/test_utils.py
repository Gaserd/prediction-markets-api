"""Tests for utility functions."""

from datetime import timezone

import pytest

from prediction_markets_api.utils import parse_timestamp


def test_parse_timestamp_seconds():
    """Test parsing timestamp in seconds."""
    # 2021-01-01 00:00:00 UTC
    dt = parse_timestamp(1609459200)
    assert dt.year == 2021
    assert dt.month == 1
    assert dt.day == 1
    assert dt.hour == 0
    assert dt.minute == 0
    assert dt.second == 0
    assert dt.tzinfo == timezone.utc


def test_parse_timestamp_milliseconds():
    """Test parsing timestamp in milliseconds."""
    # 2021-01-01 00:00:00 UTC in milliseconds
    dt = parse_timestamp(1609459200000, is_milliseconds=True)
    assert dt.year == 2021
    assert dt.month == 1
    assert dt.day == 1
    assert dt.hour == 0
    assert dt.minute == 0
    assert dt.second == 0
    assert dt.tzinfo == timezone.utc


def test_parse_timestamp_string():
    """Test parsing timestamp from string."""
    dt = parse_timestamp("1609459200")
    assert dt.year == 2021
    assert dt.tzinfo == timezone.utc


def test_parse_timestamp_string_milliseconds():
    """Test parsing milliseconds timestamp from string."""
    # Polymarket CLOB timestamp format
    dt = parse_timestamp("1791553378012", is_milliseconds=True)
    assert dt.year == 2026
    assert dt.month == 10  # October
    assert dt.tzinfo == timezone.utc


def test_parse_timestamp_zero():
    """Test parsing zero timestamp (epoch)."""
    dt = parse_timestamp(0)
    assert dt.year == 1970
    assert dt.month == 1
    assert dt.day == 1
    assert dt.tzinfo == timezone.utc


def test_parse_timestamp_float():
    """Test parsing float timestamp with fractional seconds."""
    # 2021-01-01 00:00:00.5 UTC
    dt = parse_timestamp(1609459200.5)
    assert dt.year == 2021
    assert dt.microsecond == 500000
    assert dt.tzinfo == timezone.utc


def test_parse_timestamp_far_future():
    """Test parsing far-future timestamp."""
    # Year 2100
    dt = parse_timestamp(4102444800)
    assert dt.year == 2100
    assert dt.tzinfo == timezone.utc


def test_parse_timestamp_recent_future():
    """Test parsing recent future timestamp (like October 2026)."""
    # This is the actual Polymarket fixture timestamp that breaks on Windows
    dt = parse_timestamp("1791553378012", is_milliseconds=True)
    assert dt.year == 2026
    assert dt.month == 10  # October
    assert dt.day == 9
    assert dt.tzinfo == timezone.utc


def test_parse_timestamp_invalid():
    """Test that invalid timestamps raise ValueError."""
    with pytest.raises(ValueError, match="Invalid timestamp"):
        parse_timestamp("invalid")

    with pytest.raises(ValueError, match="Invalid timestamp"):
        parse_timestamp(None)  # type: ignore


def test_parse_timestamp_always_utc():
    """Test that all parsed timestamps are UTC-aware."""
    # Test various timestamps
    timestamps = [0, 1609459200, 1791553378]
    for ts in timestamps:
        dt = parse_timestamp(ts)
        assert dt.tzinfo is not None, f"Timestamp {ts} produced naive datetime"
        assert dt.tzinfo == timezone.utc, f"Timestamp {ts} not UTC"

    # Test milliseconds
    for ts in [0, 1609459200000, 1791553378012]:
        dt = parse_timestamp(ts, is_milliseconds=True)
        assert dt.tzinfo is not None
        assert dt.tzinfo == timezone.utc
