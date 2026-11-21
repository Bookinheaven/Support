"""Persistence infrastructure package."""

from support.infrastructure.persistence.json_profile_repository import (
    CURRENT_STORAGE_VERSION,
    DEFAULT_PROFILES_PATH,
    JsonProfileRepository,
    StorageCorruptedError,
    StorageError,
    StorageVersionError,
)

__all__ = [
    "CURRENT_STORAGE_VERSION",
    "DEFAULT_PROFILES_PATH",
    "JsonProfileRepository",
    "StorageCorruptedError",
    "StorageError",
    "StorageVersionError",
]
