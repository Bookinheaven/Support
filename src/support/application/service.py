"""Application service orchestrating database profile workflows."""

from support.application.errors import (
    CredentialDeletionError,
    CredentialStoreUnavailableError,
    ProfileNotFoundError,
    ProfileStorageError,
)
from support.application.ports import CredentialStore, ProfileRepository
from support.domain.profiles import (
    ConnectionConfig,
    DatabaseProfile,
    DatabaseType,
    PostgresConnection,
    ProfileValidationError,
)


class ProfileService:
    """Application workflow service for managing database profiles and credentials."""

    def __init__(
        self,
        repository: ProfileRepository,
        credential_store: CredentialStore | None = None,
    ) -> None:
        self._repository = repository
        self._credential_store = credential_store

    def add_profile(
        self,
        name: str,
        connection: ConnectionConfig | None = None,
        *,
        database: str | None = None,
        username: str | None = None,
        ssl_mode: str | None = None,
        host: str = "localhost",
        port: int = 5432,
    ) -> DatabaseProfile:
        """Create and store a new database profile.

        Accepts either a fully formed ConnectionConfig or PostgreSQL-specific keyword arguments.
        """
        if connection is None:
            if database is None or username is None or ssl_mode is None:
                raise ProfileValidationError(
                    "Either connection or database, username, and ssl_mode must be provided."
                )
            connection = PostgresConnection(
                database=database,
                username=username,
                ssl_mode=ssl_mode,
                host=host,
                port=port,
            )
        profile = DatabaseProfile(name=name, connection=connection)
        self._repository.add(profile)
        return profile

    def get_profile(self, name: str) -> DatabaseProfile:
        """Retrieve a database profile by name."""
        profile = self._repository.get_by_name(name)
        if profile is None:
            raise ProfileNotFoundError(f"Profile '{name}' not found.")
        return profile

    def list_profiles(self) -> list[DatabaseProfile]:
        """Retrieve all database profiles in deterministic order."""
        return self._repository.list_all()

    def rename_profile(self, old_name: str, new_name: str) -> DatabaseProfile:
        """Rename an existing profile, preserving its UUID."""
        if old_name == new_name:
            return self.get_profile(old_name)

        existing = self.get_profile(old_name)
        renamed = existing.rename(new_name)
        self._repository.update(renamed)
        return renamed

    def set_profile_password(self, name: str, password: str) -> None:
        """Securely store a password for the named profile."""
        profile = self.get_profile(name)
        if profile.connection.database_type == DatabaseType.SQLITE:
            raise ProfileValidationError(f"Profile '{name}' uses SQLite, which does not support passwords.")
        if self._credential_store is None:
            raise CredentialStoreUnavailableError("No credential store configured.")
        self._credential_store.set(profile.id, password)

    def get_profile_password(self, name: str) -> str | None:
        """Retrieve the saved password for the named profile."""
        profile = self.get_profile(name)
        if profile.connection.database_type == DatabaseType.SQLITE:
            return None
        if self._credential_store is None:
            raise CredentialStoreUnavailableError("No credential store configured.")
        return self._credential_store.get(profile.id)

    def has_profile_password(self, name: str) -> bool:
        """Check whether a password is saved for the named profile without retrieving it."""
        profile = self.get_profile(name)
        if profile.connection.database_type == DatabaseType.SQLITE:
            return False
        if self._credential_store is None:
            raise CredentialStoreUnavailableError("No credential store configured.")
        return self._credential_store.contains(profile.id)

    def remove_profile_password(self, name: str) -> None:
        """Remove the saved password for the named profile."""
        profile = self.get_profile(name)
        if profile.connection.database_type == DatabaseType.SQLITE:
            return
        if self._credential_store is None:
            raise CredentialStoreUnavailableError("No credential store configured.")
        self._credential_store.delete(profile.id)

    def remove_profile(self, name: str) -> None:
        """Remove a profile and its saved credentials.

        Lifecycle order:
        1. Retrieve and verify the profile exists.
        2. Attempt to delete its saved credential (if applicable).
        3. If credential deletion fails, abort profile deletion and retain the SQLite profile.
        4. Delete the SQLite profile after credential deletion succeeds.
        """
        profile = self.get_profile(name)

        # Only delete credential if database type uses credentials
        if profile.connection.database_type != DatabaseType.SQLITE and self._credential_store is not None:
            self._credential_store.delete(profile.id)

        try:
            self._repository.delete(name)
        except Exception as exc:
            raise ProfileStorageError(
                f"Profile '{name}' password was removed, but removing profile metadata failed: {exc}"
            ) from exc
