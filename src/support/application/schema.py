"""Application workflow for database schema introspection."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from support.application.errors import (
    ObjectNotFoundError,
    ProfileNotFoundError,
)
from support.application.ports import ProfileRepository, SchemaIntrospector
from support.domain.profiles import DatabaseProfile, DatabaseType
from support.domain.schema import (
    DatabaseSchema,
    TableMetadata,
    parse_object_name,
    unquote_identifier,
)

if TYPE_CHECKING:
    from support.application.ports import CredentialStore


class SchemaIntrospectionWorkflow:
    """Application workflow component orchestrating database schema introspection."""

    def __init__(
        self,
        repository: ProfileRepository,
        introspector: SchemaIntrospector,
        credential_store: CredentialStore | None = None,
    ) -> None:
        self._repository = repository
        self._introspector = introspector
        self._credential_store = credential_store

    def get_profile(self, name: str) -> DatabaseProfile:
        """Resolve a profile by name or raise ProfileNotFoundError."""
        profile = self._repository.get_by_name(name)
        if profile is None:
            raise ProfileNotFoundError(f"Profile '{name}' not found.")
        return profile

    def resolve_saved_password(self, profile_id: UUID) -> str | None:
        """Attempt to retrieve saved password for a profile UUID."""
        if self._credential_store is None:
            return None
        return self._credential_store.get_password(profile_id)

    def get_schema(self, profile_name: str) -> DatabaseSchema:
        """Retrieve full database schema metadata for a named profile."""
        profile = self.get_profile(profile_name)
        password = None
        if profile.connection.database_type != DatabaseType.SQLITE:
            try:
                password = self.resolve_saved_password(profile.id)
            except Exception:
                password = None

        return self._introspector.introspect_schema(profile.connection, password=password)

    def describe_object(
        self,
        profile_name: str,
        object_name: str,
        schema: str | None = None,
    ) -> TableMetadata:
        """Retrieve metadata for a specific table, view, or collection."""
        profile = self.get_profile(profile_name)
        clean_object = object_name.strip()
        clean_schema = schema.strip() if schema else None

        if clean_schema is None:
            clean_schema, clean_object = parse_object_name(clean_object)
        else:
            clean_schema = unquote_identifier(clean_schema)
            clean_object = unquote_identifier(clean_object)

        password = None
        if profile.connection.database_type != DatabaseType.SQLITE:
            try:
                password = self.resolve_saved_password(profile.id)
            except Exception:
                password = None

        return self._introspector.introspect_table(
            profile.connection,
            clean_object,
            schema=clean_schema,
            password=password,
        )
