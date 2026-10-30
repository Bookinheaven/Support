"""Core module containing shared configurations, exceptions, dialects, and logging."""

from support.core.config import AppConfig
from support.core.dialect import Dialect
from support.core.exceptions import (
    SupportError,
    InvalidPathError,
    UnreadableFileError,
    SQLParseError,
    UnsupportedDialectError,
    EmptySchemaError,
    DatabaseAccessError,
)
from support.core.formatting import format_duration, format_timeout, get_clean_safeguard_info
from support.core.logging import setup_logging, logger

__all__ = [
    "AppConfig",
    "Dialect",
    "SupportError",
    "InvalidPathError",
    "UnreadableFileError",
    "SQLParseError",
    "UnsupportedDialectError",
    "EmptySchemaError",
    "DatabaseAccessError",
    "setup_logging",
    "logger",
    "format_duration",
    "format_timeout",
    "get_clean_safeguard_info",
]
