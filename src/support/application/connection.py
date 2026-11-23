"""Application workflow for testing database profile connections."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from support.application.errors import (
    AuthenticationFailedError,
    CredentialStoreUnavailableError,
    ProfileNotFoundError,
)
from support.application.ports import ConnectionTestResult, DatabaseConnector, ProfileRepository
from support.domain.profiles import DatabaseProfile

if TYPE_CHECKING:
    from support.application.ports import CredentialStore


class ConnectionTestWorkflow:
    """Application workflow component orchestrating database connection testing."""

    def __init__(
        self,
        repository: ProfileRepository,
        connector: DatabaseConnector,
        credential_store: CredentialStore | None = None,
    ) -> None:
        self._repository = repository
        self._connector = connector
        self._credential_store = credential_store

    def get_profile(self, name: str) -> DatabaseProfile:
        """Resolve a profile by name or raise ProfileNotFoundError."""
        profile = self._repository.get_by_name(name)
        if profile is None:
            raise ProfileNotFoundError(f"Profile '{name}' not found.")
        return profile

    def resolve_saved_password(self, profile_id: UUID) -> str | None:
        """Attempt to retrieve saved password for a profile UUID without failing if store is unavailable."""
        if self._credential_store is None:
            return None
        try:
            return self._credential_store.get(profile_id)
        except CredentialStoreUnavailableError:
            return None

    def test_profile_connection(
        self,
        profile_name: str,
        password: str | None = None,
        used_saved_password: bool = False,
    ) -> ConnectionTestResult:
        """Test database connection for a saved profile."""
        profile = self.get_profile(profile_name)
        try:
            return self._connector.test_connection(profile.connection, password=password)
        except AuthenticationFailedError as exc:
            # Preserve knowledge of whether the rejected credential came from the saved store
            raise AuthenticationFailedError(
                str(exc),
                used_saved_password=used_saved_password,
            ) from exc
