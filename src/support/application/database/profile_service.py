"""ProfileService application service."""

import dataclasses
from typing import Optional
from uuid import UUID

from support.application.database.ports import ProfileRepository
from support.domain.database.errors import (
    DuplicateProfileIdError,
    ProfileAlreadyExistsError,
    ProfileNotFoundError,
    ProfileValidationError,
)
from support.domain.database.profile import DatabaseProfile


class ProfileService:
    """Coordinates database profile operations using a ProfileRepository."""

    def __init__(self, repository: ProfileRepository) -> None:
        self._repository = repository

    def add_profile(self, profile: DatabaseProfile) -> DatabaseProfile:
        """Add a new profile with uniqueness validation.

        Raises:
            ProfileValidationError: If profile is invalid.
            ProfileAlreadyExistsError: If a profile with the same name already exists.
            DuplicateProfileIdError: If a profile with the same ID already exists.
        """
        if not isinstance(profile, DatabaseProfile):
            raise ProfileValidationError(
                f"Expected DatabaseProfile instance, got {type(profile).__name__}."
            )

        if self._repository.get_by_id(profile.id) is not None:
            raise DuplicateProfileIdError(
                f"Database profile with ID '{profile.id}' already exists."
            )

        if self._repository.get_by_name(profile.name) is not None:
            raise ProfileAlreadyExistsError(
                f"Database profile '{profile.name}' already exists (names are case-insensitive)."
            )

        self._repository.add(profile)
        return profile

    def get_profile_by_id(self, profile_id: UUID) -> DatabaseProfile:
        """Retrieve a profile by its unique ID.

        Raises:
            ProfileNotFoundError: If profile does not exist.
        """
        profile = self._repository.get_by_id(profile_id)
        if profile is None:
            raise ProfileNotFoundError(f"Database profile with ID '{profile_id}' not found.")
        return profile

    def get_profile_by_name(self, name: str) -> DatabaseProfile:
        """Retrieve a profile by its name (case-insensitive).

        Raises:
            ProfileValidationError: If name is invalid.
            ProfileNotFoundError: If profile does not exist.
        """
        if not isinstance(name, str) or not name.strip():
            raise ProfileValidationError("Profile name cannot be empty.")

        profile = self._repository.get_by_name(name.strip())
        if profile is None:
            raise ProfileNotFoundError(f"Database profile '{name}' not found.")
        return profile

    def get_profile(self, identifier: str | UUID) -> DatabaseProfile:
        """Retrieve a profile by name or UUID.

        Raises:
            ProfileNotFoundError: If profile does not exist.
        """
        if isinstance(identifier, UUID):
            return self.get_profile_by_id(identifier)

        if not isinstance(identifier, str) or not identifier.strip():
            raise ProfileValidationError("Profile identifier cannot be empty.")

        clean_id = identifier.strip()
        # First attempt case-insensitive lookup by name
        profile = self._repository.get_by_name(clean_id)
        if profile is not None:
            return profile

        # Next attempt lookup by parsed UUID string
        try:
            parsed_uuid = UUID(clean_id)
            profile = self._repository.get_by_id(parsed_uuid)
            if profile is not None:
                return profile
        except (ValueError, AttributeError):
            pass

        raise ProfileNotFoundError(f"Database profile '{identifier}' not found.")

    def list_profiles(self) -> list[DatabaseProfile]:
        """Return all registered profiles."""
        return self._repository.list_all()

    def remove_profile(self, identifier: str | UUID) -> None:
        """Remove a profile by name or UUID.

        Raises:
            ProfileNotFoundError: If profile does not exist.
        """
        profile = self.get_profile(identifier)
        self._repository.remove(profile.id)

    def rename_profile(self, identifier: str | UUID, new_name: str) -> DatabaseProfile:
        """Rename an existing profile while preserving its ID and connection details.

        Raises:
            ProfileNotFoundError: If profile does not exist.
            ProfileValidationError: If new name is invalid.
            ProfileAlreadyExistsError: If new name is already taken.
        """
        profile = self.get_profile(identifier)
        clean_new_name = new_name.strip() if isinstance(new_name, str) else ""
        if not clean_new_name:
            raise ProfileValidationError("New profile name cannot be empty.")

        existing = self._repository.get_by_name(clean_new_name)
        if existing is not None and existing.id != profile.id:
            raise ProfileAlreadyExistsError(
                f"Database profile '{new_name}' already exists (names are case-insensitive)."
            )

        renamed = dataclasses.replace(profile, name=clean_new_name)
        self._repository.update(renamed)
        return renamed
