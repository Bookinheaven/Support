"""Application ports / protocols for profile persistence and connectivity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

if TYPE_CHECKING:
    from support.domain.assistant import ChatMessage, ModelInfo, ToolDefinition
    from support.domain.profiles import ConnectionConfig, DatabaseProfile
    from support.domain.schema import DatabaseSchema, TableMetadata


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

    def store_password(self, profile_id: UUID, password: str) -> None:
        """Securely store a password for the given profile ID."""
        ...

    def get_password(self, profile_id: UUID) -> str | None:
        """Retrieve the password for the given profile ID, or None if absent."""
        ...

    def delete_password(self, profile_id: UUID) -> None:
        """Delete the password for the given profile ID. Idempotent: absent credentials do not raise."""
        ...

    def has_password(self, profile_id: UUID) -> bool:
        """Check whether a password exists for the given profile ID without revealing it."""
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


class SchemaIntrospector(Protocol):
    """Protocol for database schema introspection operations."""

    def introspect_schema(
        self,
        connection: ConnectionConfig,
        password: str | None = None,
    ) -> DatabaseSchema:
        """Introspect complete database schema and return normalized metadata."""
        ...

    def introspect_table(
        self,
        connection: ConnectionConfig,
        table_name: str,
        schema: str | None = None,
        password: str | None = None,
    ) -> TableMetadata:
        """Introspect a single table, view, or collection."""
        ...


class LLMProvider(Protocol):
    """Protocol for local language model provider operations."""

    def chat(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition] | None = None,
        model: str | None = None,
    ) -> ChatMessage:
        """Send multi-turn chat messages with optional tool definitions and return model response."""
        ...

    def check_model_status(self, model: str | None = None) -> ModelInfo:
        """Inspect local model availability and tool-calling capability."""
        ...

    def list_models(self) -> list[ModelInfo]:
        """List all installed local models."""
        ...
