"""Tests for centralized duration formatting and safeguard translation."""

import pytest
from support.core.formatting import format_duration, format_timeout, get_clean_safeguard_info


def test_duration_formatting_subsecond():
    """Verify durations under one second format as milliseconds rounded sensibly."""
    assert format_duration(duration_ms=0) == "0 ms"
    assert format_duration(duration_ms=23.5) == "24 ms"
    assert format_duration(duration_ms=23.4) == "23 ms"
    assert format_duration(duration_ms=84.0) == "84 ms"
    assert format_duration(duration_ms=999.0) == "999 ms"
    assert format_duration(duration_seconds=0.054) == "54 ms"


def test_duration_formatting_seconds():
    """Verify durations from 1 second to under 1 minute format as seconds without trailing zeroes."""
    assert format_duration(duration_seconds=1.0) == "1 s"
    assert format_duration(duration_seconds=1.8) == "1.8 s"
    assert format_duration(duration_seconds=10.0) == "10 s"
    assert format_duration(duration_ms=10000.0) == "10 s"
    assert format_duration(duration_seconds=59.4) == "59.4 s"


def test_duration_formatting_minutes():
    """Verify durations from 1 minute to under 1 hour format with minutes and seconds."""
    assert format_duration(duration_seconds=60.0) == "1 min"
    assert format_duration(duration_seconds=75.0) == "1 min 15 s"
    assert format_duration(duration_seconds=130.0) == "2 min 10 s"
    assert format_duration(duration_seconds=3599.0) == "59 min 59 s"


def test_duration_formatting_hours():
    """Verify durations from 1 hour to under 1 day format with hours, minutes, and seconds."""
    assert format_duration(duration_seconds=3600.0) == "1 h"
    assert format_duration(duration_seconds=3660.0) == "1 h 1 min"
    assert format_duration(duration_seconds=3725.0) == "1 h 2 min 5 s"
    assert format_duration(duration_seconds=7200.0) == "2 h"


def test_duration_formatting_days():
    """Verify durations of 1 day or longer format with days and remaining hours."""
    assert format_duration(duration_seconds=86400.0) == "1 d"
    assert format_duration(duration_seconds=90000.0) == "1 d 1 h"
    assert format_duration(duration_seconds=172800.0) == "2 d"


def test_duration_formatting_edge_cases():
    """Verify negative, zero, None, and invalid inputs handle gracefully without crashing."""
    assert format_duration(None) == "-"
    assert format_duration(duration_seconds=-5.0) == "0 ms"
    assert format_duration(duration_ms=-100.0) == "0 ms"
    assert format_duration(float("nan")) == "-"
    assert format_duration(float("inf")) == "-"


def test_format_timeout():
    """Verify format_timeout produces concise human-readable duration strings."""
    assert format_timeout(10.0) == "10 s"
    assert format_timeout(30.0) == "30 s"
    assert format_timeout(0.5) == "500 ms"
    assert format_timeout(None) == "-"


def test_get_clean_safeguard_info_postgres():
    """Verify Postgres safeguard string is translated into user-facing protection and timeout."""
    raw = "PostgreSQL read-only transaction (statement_timeout=10000ms)"
    protection, timeout = get_clean_safeguard_info(raw)
    assert protection == "Read-only transaction"
    assert timeout == "Query timeout: 10 s"


def test_get_clean_safeguard_info_mysql():
    """Verify MySQL safeguard string is translated into user-facing protection and timeout."""
    raw = "MySQL READ ONLY transaction (max_execution_time=15000ms)"
    protection, timeout = get_clean_safeguard_info(raw)
    assert protection == "Read-only transaction"
    assert timeout == "Query timeout: 15 s"


def test_get_clean_safeguard_info_sqlite():
    """Verify SQLite safeguard string is translated into user-facing protection and timeout."""
    raw = "SQLite PRAGMA query_only=ON (busy_timeout=5000ms)"
    protection, timeout = get_clean_safeguard_info(raw)
    assert protection == "PRAGMA query_only"
    assert timeout == "Query timeout: 5 s"


def test_get_clean_safeguard_info_mssql():
    """Verify SQL Server safeguard string is translated into user-facing protection and timeout."""
    raw = "SQL Server READ_ONLY role guard (LOCK_TIMEOUT 10000ms)"
    protection, timeout = get_clean_safeguard_info(raw)
    assert protection == "Read-only transaction"
    assert timeout == "Query timeout: 10 s"
