"""Application ports for database profile persistence."""

from typing import Optional, Protocol
from uuid import UUID

from support.domain.database.profile import DatabaseProfile


class ProfileRepository(Protocol):
    """Abstract repository port for storing and retrieving database profiles."""

    def add(self, profile: DatabaseProfile) -> None:
        """Add a new profile."""
        ...

    def get_by_id(self, profile_id: UUID) -> Optional[DatabaseProfile]:
        """Retrieve a profile by its ID, or None if not found."""
        ...

    def get_by_name(self, name: str) -> Optional[DatabaseProfile]:
        """Retrieve a profile by its name (case-insensitive), or None if not found."""
        ...

    def list_all(self) -> list[DatabaseProfile]:
        """Return all stored profiles."""
        ...

    def update(self, profile: DatabaseProfile) -> None:
        """Update an existing profile (matched by ID)."""
        ...

    def remove(self, profile_id: UUID) -> None:
        """Remove a profile by its ID."""
        ...
