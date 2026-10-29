"""Logging configuration using Rich handler."""

import logging
from rich.logging import RichHandler


def setup_logging(level: str = "INFO", show_path: bool = False) -> logging.Logger:
    """Configure and return the root logger for the support package."""
    logger = logging.getLogger("support")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Avoid duplicate handlers if re-called
    if not logger.handlers:
        handler = RichHandler(
            rich_tracebacks=True,
            show_time=False,
            show_path=show_path,
            markup=True,
        )
        formatter = logging.Formatter("%(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


logger = logging.getLogger("support")
