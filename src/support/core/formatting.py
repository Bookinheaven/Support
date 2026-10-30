"""Centralized human-readable duration, timeout, and safeguard formatting utilities."""

import math
import re
from typing import Optional


def format_duration(
    duration_seconds: Optional[float] = None,
    *,
    duration_ms: Optional[float] = None,
) -> str:
    """Format an elapsed time into standardized, human-readable units.

    Rules:
    - Below 1 second: milliseconds, rounded sensibly (e.g. 23.5 ms -> 24 ms).
    - 1 second to < 1 minute: seconds with at most 1 decimal place without trailing zero (e.g. 1.8 s, 10 s).
    - 1 minute to < 1 hour: minutes and remaining seconds (e.g. 1 min 15 s, 2 min).
    - 1 hour to < 1 day: hours, remaining minutes, and remaining seconds (e.g. 1 h 2 min 5 s).
    - 1 day or longer: days and remaining hours (e.g. 1 d 4 h, 2 d).
    - Handles 0, negative values, missing/None, NaN/Inf gracefully without crashing.
    """
    if duration_seconds is None and duration_ms is None:
        return "-"

    # Normalize to seconds
    if duration_ms is not None:
        try:
            sec = float(duration_ms) / 1000.0
        except (ValueError, TypeError):
            return "-"
    else:
        try:
            sec = float(duration_seconds)  # type: ignore[arg-type]
        except (ValueError, TypeError):
            return "-"

    if math.isnan(sec) or math.isinf(sec):
        return "-"

    if sec <= 0:
        return "0 ms"

    # Below 1 second: show milliseconds rounded to nearest integer
    if sec < 1.0:
        ms = round(sec * 1000)
        if ms >= 1000:
            return "1 s"
        return f"{ms} ms"

    # 1 second to < 60 seconds
    if sec < 60.0:
        val = round(sec, 1)
        if val == int(val):
            return f"{int(val)} s"
        return f"{val:g} s"

    # 1 minute to < 1 hour (60s to 3600s)
    if sec < 3600.0:
        total_sec = round(sec)
        mins = total_sec // 60
        rem_sec = total_sec % 60
        if rem_sec > 0:
            return f"{mins} min {rem_sec} s"
        return f"{mins} min"

    # 1 hour to < 1 day (3600s to 86400s)
    if sec < 86400.0:
        total_sec = round(sec)
        hours = total_sec // 3600
        rem_min = (total_sec % 3600) // 60
        rem_sec = total_sec % 60

        parts = [f"{hours} h"]
        if rem_min > 0:
            parts.append(f"{rem_min} min")
        if rem_sec > 0:
            parts.append(f"{rem_sec} s")
        return " ".join(parts)

    # 1 day or longer
    total_sec = round(sec)
    days = total_sec // 86400
    rem_hours = (total_sec % 86400) // 3600

    parts = [f"{days} d"]
    if rem_hours > 0:
        parts.append(f"{rem_hours} h")
    return " ".join(parts)


def format_timeout(timeout_seconds: Optional[float]) -> str:
    """Format a timeout setting into standardized units (e.g. 10 s)."""
    if timeout_seconds is None:
        return "-"
    return format_duration(timeout_seconds)


def get_clean_safeguard_info(
    safeguard_raw: str,
    timeout_seconds: Optional[float] = None,
) -> tuple[str, str]:
    """Parse technical driver safeguard string into clean user-facing Protection and Timeout descriptions.

    Returns:
        (protection_description, timeout_description)
    """
    if not safeguard_raw:
        tout_str = f"Query timeout: {format_duration(timeout_seconds)}" if timeout_seconds else "Query timeout: Enforced"
        return "Read-only guard", tout_str

    raw_lower = safeguard_raw.lower()

    # Determine protection description
    if "pragma" in raw_lower:
        protection = "PRAGMA query_only"
    else:
        protection = "Read-only transaction"

    # Determine timeout description
    parsed_sec: Optional[float] = None

    # Check for statement_timeout, max_execution_time, lock_timeout, busy_timeout with = or space (e.g. statement_timeout=10000ms or LOCK_TIMEOUT 10000ms)
    ms_match = re.search(
        r"(?:statement_timeout|max_execution_time|lock_timeout|busy_timeout)[=\s]+(\d+)\s*ms",
        raw_lower,
    )
    if ms_match:
        parsed_sec = float(ms_match.group(1)) / 1000.0

    # Check for timeout=10.0s or query_timeout=10s
    if parsed_sec is None:
        sec_match = re.search(r"(?:timeout|query_timeout)[=\s]+(\d+(?:\.\d+)?)\s*s", raw_lower)
        if sec_match:
            parsed_sec = float(sec_match.group(1))

    if parsed_sec is not None:
        timeout_desc = f"Query timeout: {format_duration(parsed_sec)}"
    elif timeout_seconds is not None:
        timeout_desc = f"Query timeout: {format_duration(timeout_seconds)}"
    else:
        timeout_desc = "Query timeout: Enforced"

    return protection, timeout_desc
