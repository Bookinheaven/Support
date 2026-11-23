"""Application ports / protocols for profile persistence and connectivity."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from support.domain.profiles import ConnectionConfig, DatabaseProfile


class ProfileRepository(Protocol):
    """Protocol for profile persistence operations."""

    def add(self, profile: DatabaseProfile) -> None:
        """Persist a new profile. Raises ProfileAlreadyExistsError or ProfileStorageError."""
        ...

    def get_by_name(self, name: str) -> DatabaseProfile | None:
        """Retrieve a profile by its name, or None if not found."""
        ...

    def list_all(self) -> list[DatabaseProfile]:
        """List all profiles in deterministic name order."""
        ...

    def update(self, profile: DatabaseProfile) -> None:
        """Update an existing profile. Raises ProfileNotFoundError or ProfileAlreadyExistsError."""
        ...

    def delete(self, name: str) -> None:
        """Delete a profile by name. Raises ProfileNotFoundError if not found."""
        ...


class CredentialStore(Protocol):
    """Protocol for secure profile credential storage."""

    def set(self, profile_id: UUID, password: str) -> None:
        """Securely store a password for the given profile ID."""
        ...

    def get(self, profile_id: UUID) -> str | None:
        """Retrieve the password for the given profile ID, or None if absent."""
        ...

    def delete(self, profile_id: UUID) -> None:
        """Delete the password for the given profile ID. Idempotent: absent credentials do not raise."""
        ...

    def contains(self, profile_id: UUID) -> bool:
        """Check whether a password exists for the given profile ID without revealing it."""
        ...

    def store_password(self, profile_id: UUID, password: str) -> None:
        """Alias for set."""
        ...

    def get_password(self, profile_id: UUID) -> str | None:
        """Alias for get."""
        ...

    def delete_password(self, profile_id: UUID) -> None:
        """Alias for delete."""
        ...

    def has_password(self, profile_id: UUID) -> bool:
        """Alias for contains."""
        ...


@dataclass(frozen=True)
class ConnectionTestResult:
    """Outcome of testing a database connection."""

    success: bool
    database_type: str
    database: str
    host: str | None = None
    port: int | None = None
    username: str | None = None
    ssl_mode: str | None = None
    server_version: str | None = None
    message: str = "Connection successful."


class DatabaseConnector(Protocol):
    """Protocol for database connection operations."""

    def test_connection(
        self,
        connection: ConnectionConfig,
        password: str | None = None,
    ) -> ConnectionTestResult:
        """Connect to the database, verify with test query, and return result."""
        ...
