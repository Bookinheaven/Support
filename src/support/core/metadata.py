"""Centralized application metadata for Support."""

import importlib.metadata

APP_NAME = "Support"
APP_DESCRIPTION = "AI-powered multi-database query and exploration environment"
APP_CLI_HELP = "Support: Multi-Database Schema Discovery, LLM Gateway, and Interactive Workspace."

# Read installed version dynamically from package metadata, with safe fallback
try:
    __version__ = importlib.metadata.version("support")
except Exception:
    __version__ = "0.1.0"

APP_VERSION = __version__

__all__ = [
    "APP_NAME",
    "APP_DESCRIPTION",
    "APP_CLI_HELP",
    "APP_VERSION",
    "__version__",
]
