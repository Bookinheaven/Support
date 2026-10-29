"""Application configuration."""

import os
from dataclasses import dataclass, field
from typing import Optional
from support.core.dialect import Dialect


@dataclass
class AppConfig:
    """Core runtime configuration for Support."""

    default_dialect: Dialect = Dialect.UNKNOWN
    max_file_size_bytes: int = 10 * 1024 * 1024  # 10 MB limit for scanned SQL files
    log_level: str = "INFO"
    debug: bool = False
    ignored_directories: list[str] = field(
        default_factory=lambda: [
            ".git",
            ".venv",
            "venv",
            "env",
            "node_modules",
            "dist",
            "build",
            "__pycache__",
            ".idea",
            ".vscode",
            ".mypy_cache",
            ".pytest_cache",
            ".tox",
            ".coverage",
            "htmlcov",
            "target",
        ]
    )

    @classmethod
    def from_env(cls) -> "AppConfig":
        """Load configuration from environment variables."""
        dialect_env = os.getenv("SUPPORT_DEFAULT_DIALECT")
        dialect = Dialect.from_string(dialect_env) if dialect_env else Dialect.UNKNOWN
        log_level = os.getenv("SUPPORT_LOG_LEVEL", "INFO").upper()
        debug = os.getenv("SUPPORT_DEBUG", "false").lower() in ("true", "1", "yes")

        return cls(
            default_dialect=dialect,
            log_level=log_level,
            debug=debug,
        )
